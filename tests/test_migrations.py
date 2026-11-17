from __future__ import annotations

import sqlite3
import sys

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine as create_sync_engine

from landscape_orchestrator import db
from landscape_orchestrator.db import (
    SchemaError,
    backup_sqlite,
    create_engine,
    current_revision,
    head_revision,
    upgrade_database,
)
from landscape_orchestrator.models import Base

pytestmark = pytest.mark.anyio


def url_of(path) -> str:
    return f"sqlite+aiosqlite:///{path}"


async def upgrade(path):
    engine = create_engine(url_of(path))
    try:
        backup = await upgrade_database(engine, url_of(path))
        return backup, await current_revision(engine)
    finally:
        await engine.dispose()


def tables(path) -> set[str]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        return {name for (name,) in rows}


async def test_new_database_is_created_by_migrations(tmp_path):
    path = tmp_path / "new.db"
    backup, revision = await upgrade(path)
    assert backup is None and revision == head_revision()
    assert {"hosts", "systems", "host_tags", "alembic_version"} <= tables(path)
    assert "schema_info" not in tables(path)

    # Nothing to do the second time, and no backup.
    assert await upgrade(path) == (None, head_revision())
    assert list(tmp_path.glob("new.db.bak-*")) == []


def test_models_match_migrations(tmp_path):
    """Fails when a model changes without a migration."""
    import asyncio

    path = tmp_path / "compare.db"
    asyncio.run(upgrade(path))
    engine = create_sync_engine(f"sqlite:///{path}")
    with engine.connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    engine.dispose()
    assert diff == []


def legacy_database(path) -> None:
    """A database as the last version before migrations left it, without host_tags."""
    engine = create_sync_engine(f"sqlite:///{path}")
    tables_without_tags = [t for t in Base.metadata.sorted_tables if t.name != "host_tags"]
    Base.metadata.create_all(engine, tables=tables_without_tags)
    engine.dispose()
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE schema_info (version INTEGER NOT NULL)")
        connection.execute("INSERT INTO schema_info VALUES (2)")
        connection.execute(
            "INSERT INTO hosts (name, address, https, verify_tls, sapcontrol_port_offset) "
            "VALUES ('sapapp1', '127.0.0.1', 0, 1, 0)"
        )


async def test_legacy_database_is_taken_over_with_backup(tmp_path):
    path = tmp_path / "landscape.db"
    legacy_database(path)

    backup, revision = await upgrade(path)

    assert revision == head_revision()
    assert backup is not None and backup.exists()
    assert backup.name.startswith("landscape.db.bak-")
    assert "schema_info" in tables(backup)  # the copy is the state before
    assert "schema_info" not in tables(path)
    assert "host_tags" in tables(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT name FROM hosts").fetchall() == [("sapapp1",)]


async def test_older_revision_is_migrated_with_backup(tmp_path):
    path = tmp_path / "landscape.db"
    engine = create_engine(url_of(path))
    async with engine.begin() as connection:
        await connection.run_sync(db._run, "upgrade", "0001")
    await engine.dispose()

    backup, revision = await upgrade(path)
    assert revision == head_revision() and backup is not None


async def test_unusable_databases_are_refused(tmp_path):
    first = tmp_path / "schema1.db"
    with sqlite3.connect(first) as connection:
        connection.execute("CREATE TABLE systems (id INTEGER PRIMARY KEY, sid VARCHAR(8))")
    with pytest.raises(SchemaError, match="incompatible version"):
        await upgrade(first)

    newer = tmp_path / "newer.db"
    await upgrade(newer)
    with sqlite3.connect(newer) as connection:
        connection.execute("UPDATE alembic_version SET version_num = '9999'")
    with pytest.raises(SchemaError, match="9999"):
        await upgrade(newer)


def test_only_the_newest_backups_are_kept(tmp_path):
    database = tmp_path / "landscape.db"
    database.write_text("data")
    for _ in range(7):
        backup_sqlite(database)
    assert len(list(tmp_path.glob("landscape.db.bak-*"))) == 5
    assert database.read_text() == "data"


def test_lo_db_command(tmp_path, monkeypatch, capsys):
    path = tmp_path / "cli.db"
    monkeypatch.setenv("LO_DATABASE_URL", url_of(path))

    def run(action: str) -> str:
        monkeypatch.setattr(sys, "argv", ["lo-db", action])
        db.main()
        return capsys.readouterr().out

    assert "current: none" in run("current")
    assert f"Schema is at revision {head_revision()}" in run("upgrade")
    assert f"current: {head_revision()}" in run("current")
    history = run("history")
    assert "0001" in history and "0002" in history
