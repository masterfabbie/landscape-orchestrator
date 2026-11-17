"""Database engine and sessions."""

from __future__ import annotations

from typing import Any

from sqlalchemy import event, insert, inspect, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .models import SCHEMA_VERSION, Base, schema_info


def create_engine(database_url: str) -> AsyncEngine:
    engine = create_async_engine(database_url)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine.sync_engine, "connect")
        def enable_foreign_keys(dbapi_connection: Any, _: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


class SchemaError(Exception):
    """The database was created by an incompatible version."""


def _check_schema(connection: Any) -> None:
    tables = set(inspect(connection).get_table_names())
    if not tables:
        return
    version = None
    if "schema_info" in tables:
        version = connection.execute(select(schema_info.c.version)).scalar()
    if version != SCHEMA_VERSION:
        raise SchemaError(
            f"The database was created by an incompatible version (schema "
            f"{version or 1}, expected {SCHEMA_VERSION}). Delete it or point LO_DATABASE_URL "
            "to a new database; the inventory has to be set up again."
        )


async def create_tables(engine: AsyncEngine) -> None:
    """Create the schema, or check that an existing database has the current one."""
    async with engine.begin() as connection:
        await connection.run_sync(_check_schema)
        await connection.run_sync(Base.metadata.create_all)
        if (await connection.execute(select(schema_info.c.version))).first() is None:
            await connection.execute(insert(schema_info).values(version=SCHEMA_VERSION))
