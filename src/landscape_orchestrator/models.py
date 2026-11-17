"""Inventory: hosts, SAP systems, instances and databases."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

STATUS_UNKNOWN = "UNKNOWN"


class Base(DeclarativeBase):
    pass


# Added after the first release. Only new tables are used for new features, so existing
# databases get them from create_all at startup without a migration.

system_dependencies = Table(
    "system_dependencies",
    Base.metadata,
    Column("system_id", ForeignKey("systems.id", ondelete="CASCADE"), primary_key=True),
    Column("depends_on_id", ForeignKey("systems.id", ondelete="CASCADE"), primary_key=True),
)

activity_systems = Table(
    "activity_systems",
    Base.metadata,
    Column("activity_id", ForeignKey("activities.id", ondelete="CASCADE"), primary_key=True),
    Column("system_id", ForeignKey("systems.id", ondelete="CASCADE"), primary_key=True),
)


class Host(Base):
    __tablename__ = "hosts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    # Hostname or IP used to connect; ``name`` is what the landscape calls the host.
    address: Mapped[str] = mapped_column(String(255))
    hostagent_port: Mapped[int | None] = mapped_column(Integer)
    https: Mapped[bool] = mapped_column(Boolean, default=False)
    verify_tls: Mapped[bool] = mapped_column(Boolean, default=True)
    # Added to the default sapstartsrv ports (5NN13). Only needed for test setups where
    # several simulated hosts share one machine.
    sapcontrol_port_offset: Mapped[int] = mapped_column(Integer, default=0)

    username: Mapped[str | None] = mapped_column(String(255))
    password_encrypted: Mapped[str | None] = mapped_column(Text)

    os_name: Mapped[str | None] = mapped_column(String(255))
    os_version: Mapped[str | None] = mapped_column(String(255))
    cpu_count: Mapped[int | None] = mapped_column(Integer)
    memory_kb: Mapped[int | None] = mapped_column(Integer)

    reachable: Mapped[bool | None] = mapped_column(Boolean)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_discovery_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    instances: Mapped[list[Instance]] = relationship(
        back_populates="host",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Instance.number",
    )
    databases: Mapped[list[Database]] = relationship(
        back_populates="host",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Database.name",
    )

    @property
    def has_credentials(self) -> bool:
        return bool(self.username and self.password_encrypted)


class System(Base):
    """An SAP system, identified by its SID."""

    __tablename__ = "systems"

    id: Mapped[int] = mapped_column(primary_key=True)
    sid: Mapped[str] = mapped_column(String(8), unique=True)
    # Optional credentials of <sid>adm for the sapstartsrv of this system's instances.
    # Without them the credentials of the host are used.
    username: Mapped[str | None] = mapped_column(String(255))
    password_encrypted: Mapped[str | None] = mapped_column(Text)

    instances: Mapped[list[Instance]] = relationship(
        back_populates="system", passive_deletes=True, order_by="Instance.number"
    )
    # Systems that must run for this one, e.g. the database system of an ABAP system.
    depends_on: Mapped[list[System]] = relationship(
        secondary=system_dependencies,
        primaryjoin=lambda: System.id == system_dependencies.c.system_id,
        secondaryjoin=lambda: System.id == system_dependencies.c.depends_on_id,
        back_populates="required_by",
        order_by="System.sid",
    )
    required_by: Mapped[list[System]] = relationship(
        secondary=system_dependencies,
        primaryjoin=lambda: System.id == system_dependencies.c.depends_on_id,
        secondaryjoin=lambda: System.id == system_dependencies.c.system_id,
        back_populates="depends_on",
        order_by="System.sid",
    )

    @property
    def has_credentials(self) -> bool:
        return bool(self.username and self.password_encrypted)

    @property
    def depends_on_ids(self) -> list[int]:
        return [system.id for system in self.depends_on]

    @property
    def required_by_ids(self) -> list[int]:
        return [system.id for system in self.required_by]


class Instance(Base):
    __tablename__ = "instances"
    __table_args__ = (UniqueConstraint("host_id", "system_id", "number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    host_id: Mapped[int] = mapped_column(ForeignKey("hosts.id", ondelete="CASCADE"))
    system_id: Mapped[int] = mapped_column(ForeignKey("systems.id", ondelete="CASCADE"))
    number: Mapped[str] = mapped_column(String(2))
    # SAPLOCALHOST of the instance; may be a virtual hostname.
    hostname: Mapped[str] = mapped_column(String(255))
    sap_version_info: Mapped[str | None] = mapped_column(String(255))
    features: Mapped[str | None] = mapped_column(String(255))
    start_priority: Mapped[str | None] = mapped_column(String(16))
    last_status: Mapped[str] = mapped_column(String(16), default=STATUS_UNKNOWN)
    status_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    host: Mapped[Host] = relationship(back_populates="instances")
    system: Mapped[System] = relationship(back_populates="instances")


class Database(Base):
    __tablename__ = "databases"
    __table_args__ = (UniqueConstraint("host_id", "name", "type"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    host_id: Mapped[int] = mapped_column(ForeignKey("hosts.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255))
    type: Mapped[str] = mapped_column(String(32))
    instance_name: Mapped[str | None] = mapped_column(String(255))
    vendor: Mapped[str | None] = mapped_column(String(64))
    release: Mapped[str | None] = mapped_column(String(64))
    last_status: Mapped[str] = mapped_column(String(16), default=STATUS_UNKNOWN)
    status_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    host: Mapped[Host] = relationship(back_populates="databases")


class Activity(Base):
    """One requested operation and its outcome. Kept as history when the target is deleted."""

    __tablename__ = "activities"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    target_type: Mapped[str] = mapped_column(String(16))
    target_label: Mapped[str] = mapped_column(String(255))
    system_id: Mapped[int | None] = mapped_column(
        ForeignKey("systems.id", ondelete="SET NULL"), index=True
    )
    instance_id: Mapped[int | None] = mapped_column(
        ForeignKey("instances.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(16), index=True)
    timeout_seconds: Mapped[int] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # All systems the activity may touch; used to keep activities from overlapping.
    systems: Mapped[list[System]] = relationship(secondary=activity_systems)
    entries: Mapped[list[ActivityLogEntry]] = relationship(
        back_populates="activity",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ActivityLogEntry.id",
    )


class ActivityLogEntry(Base):
    __tablename__ = "activity_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    activity_id: Mapped[int] = mapped_column(
        ForeignKey("activities.id", ondelete="CASCADE"), index=True
    )
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    level: Mapped[str] = mapped_column(String(8))
    message: Mapped[str] = mapped_column(Text)

    activity: Mapped[Activity] = relationship(back_populates="entries")
