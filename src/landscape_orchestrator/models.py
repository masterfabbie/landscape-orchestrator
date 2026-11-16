"""Inventory: hosts, SAP systems, instances and databases."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

STATUS_UNKNOWN = "UNKNOWN"


class Base(DeclarativeBase):
    pass


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

    @property
    def has_credentials(self) -> bool:
        return bool(self.username and self.password_encrypted)


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
