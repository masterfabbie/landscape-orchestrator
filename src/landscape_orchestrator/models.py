"""Inventory: hosts, SIDs, instances, systems, pools and databases.

A *system* is a group of instances put together by the user, e.g. an ABAP system with its
HANA database. Its instances may belong to different SIDs. Discovery finds instances and
SIDs; it never creates systems.
"""

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
    func,
    select,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    column_property,
    mapped_column,
    relationship,
)

from .roles import Role, instance_role

STATUS_UNKNOWN = "UNKNOWN"

# Increase when the schema changes incompatibly; the server refuses older databases.
SCHEMA_VERSION = 2


class Base(DeclarativeBase):
    pass


schema_info = Table(
    "schema_info",
    Base.metadata,
    Column("version", Integer, nullable=False),
)

system_dependencies = Table(
    "system_dependencies",
    Base.metadata,
    Column("system_id", ForeignKey("systems.id", ondelete="CASCADE"), primary_key=True),
    Column("depends_on_id", ForeignKey("systems.id", ondelete="CASCADE"), primary_key=True),
)

# A system belongs to at most one pool, hence system_id alone is the key.
pool_systems = Table(
    "pool_systems",
    Base.metadata,
    Column("system_id", ForeignKey("systems.id", ondelete="CASCADE"), primary_key=True),
    Column("pool_id", ForeignKey("pools.id", ondelete="CASCADE"), nullable=False, index=True),
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
    agent_info: Mapped[HostAgentInfo | None] = relationship(
        back_populates="host", cascade="all, delete-orphan", passive_deletes=True, lazy="selectin"
    )

    @property
    def has_credentials(self) -> bool:
        return bool(self.username and self.password_encrypted)

    @property
    def hostagent_version(self) -> str | None:
        return self.agent_info.display if self.agent_info else None


class HostAgentInfo(Base):
    """Version of the host agent, read at each discovery.

    A separate table so that existing databases get it at startup without a migration.
    """

    __tablename__ = "host_agent_info"

    host_id: Mapped[int] = mapped_column(
        ForeignKey("hosts.id", ondelete="CASCADE"), primary_key=True
    )
    release: Mapped[str] = mapped_column(String(16))
    patch: Mapped[str | None] = mapped_column(String(16))
    details: Mapped[str | None] = mapped_column(String(255))
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    host: Mapped[Host] = relationship(back_populates="agent_info")

    @property
    def display(self) -> str:
        """E.g. "7.22 PL72"."""
        release = self.release
        if len(release) == 3 and release.isdigit():
            release = f"{release[0]}.{release[1:]}"
        return f"{release} PL{self.patch}" if self.patch else release


class Sid(Base):
    """An SAP system ID as reported by the host agent."""

    __tablename__ = "sids"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(8), unique=True)
    # Optional credentials of <sid>adm for the sapstartsrv of the instances of this SID.
    # Without them the credentials of the host are used.
    username: Mapped[str | None] = mapped_column(String(255))
    password_encrypted: Mapped[str | None] = mapped_column(Text)

    instances: Mapped[list[Instance]] = relationship(
        back_populates="sid", passive_deletes=True, order_by="Instance.number"
    )

    @property
    def has_credentials(self) -> bool:
        return bool(self.username and self.password_encrypted)


class System(Base):
    """A group of instances managed together, possibly of several SIDs."""

    __tablename__ = "systems"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    description: Mapped[str | None] = mapped_column(Text)

    instances: Mapped[list[Instance]] = relationship(
        back_populates="system", order_by="Instance.number"
    )
    # Other systems that must run for this one, e.g. a shared database system.
    depends_on: Mapped[list[System]] = relationship(
        secondary=system_dependencies,
        primaryjoin=lambda: System.id == system_dependencies.c.system_id,
        secondaryjoin=lambda: System.id == system_dependencies.c.depends_on_id,
        back_populates="required_by",
        order_by="System.name",
    )
    pool: Mapped[Pool | None] = relationship(
        secondary=pool_systems, back_populates="systems", uselist=False
    )
    required_by: Mapped[list[System]] = relationship(
        secondary=system_dependencies,
        primaryjoin=lambda: System.id == system_dependencies.c.depends_on_id,
        secondaryjoin=lambda: System.id == system_dependencies.c.system_id,
        back_populates="depends_on",
        order_by="System.name",
    )

    @property
    def pool_id(self) -> int | None:
        return self.pool.id if self.pool else None

    @property
    def depends_on_ids(self) -> list[int]:
        return [system.id for system in self.depends_on]

    @property
    def required_by_ids(self) -> list[int]:
        return [system.id for system in self.required_by]


class Pool(Base):
    """Systems that belong together, e.g. development, test and production of one product."""

    __tablename__ = "pools"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    description: Mapped[str | None] = mapped_column(Text)

    systems: Mapped[list[System]] = relationship(
        secondary=pool_systems, back_populates="pool", order_by="System.name"
    )

    @property
    def system_ids(self) -> list[int]:
        return [system.id for system in self.systems]


class Instance(Base):
    __tablename__ = "instances"
    __table_args__ = (UniqueConstraint("host_id", "sid_id", "number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    host_id: Mapped[int] = mapped_column(ForeignKey("hosts.id", ondelete="CASCADE"))
    sid_id: Mapped[int] = mapped_column(ForeignKey("sids.id", ondelete="CASCADE"))
    # None while the instance is not assigned to a system.
    system_id: Mapped[int | None] = mapped_column(
        ForeignKey("systems.id", ondelete="SET NULL"), index=True
    )
    number: Mapped[str] = mapped_column(String(2))
    # SAPLOCALHOST of the instance; may be a virtual hostname.
    hostname: Mapped[str] = mapped_column(String(255))
    sap_version_info: Mapped[str | None] = mapped_column(String(255))
    features: Mapped[str | None] = mapped_column(String(255))
    start_priority: Mapped[str | None] = mapped_column(String(16))
    last_status: Mapped[str] = mapped_column(String(16), default=STATUS_UNKNOWN)
    status_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    host: Mapped[Host] = relationship(back_populates="instances")
    sid: Mapped[Sid] = relationship(back_populates="instances")
    system: Mapped[System | None] = relationship(back_populates="instances")

    @property
    def sid_name(self) -> str:
        return self.sid.name

    @property
    def label(self) -> str:
        """SID and instance number, e.g. DEV/00.

        If the same SID and number exist on several hosts, the instance hostname is added,
        e.g. DEV/00 (sapapp2).
        """
        label = f"{self.sid.name}/{self.number}"
        if (self.same_number_count or 0) > 1:
            label += f" ({self.hostname})"
        return label

    @property
    def role(self) -> Role:
        return instance_role(self.features)

    @property
    def is_agent(self) -> bool:
        return self.role is Role.AGENT

    @property
    def priority(self) -> float:
        """Start priority as a number; unknown priorities start last."""
        try:
            return float(self.start_priority or "")
        except ValueError:
            return float("inf")


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


# Number of instances with the same SID and instance number, on any host. A table alias is
# used because aliasing the mapped class here would configure the mappers too early.
_other_instance = Instance.__table__.alias("other_instance")
Instance.same_number_count = column_property(
    select(func.count(_other_instance.c.id))
    .where(
        _other_instance.c.sid_id == Instance.sid_id,
        _other_instance.c.number == Instance.number,
    )
    .correlate_except(_other_instance)
    .scalar_subquery(),
    # Keep the loaded value when the instance is updated; reloading it would need IO at
    # attribute access, which async sessions do not allow.
    expire_on_flush=False,
)
