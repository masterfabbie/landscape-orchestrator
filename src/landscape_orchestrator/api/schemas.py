"""Request and response models of the REST API. Passwords are never returned."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1)


class HostCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    address: str | None = Field(default=None, min_length=1, max_length=255)
    hostagent_port: int | None = Field(default=None, ge=1, le=65535)
    https: bool = False
    verify_tls: bool = True
    sapcontrol_port_offset: int = Field(default=0, ge=0, le=15000)
    username: str | None = Field(default=None, min_length=1, max_length=255)
    password: str | None = Field(default=None, min_length=1)


class InstanceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    host_id: int
    system_id: int
    number: str
    hostname: str
    sap_version_info: str | None
    features: str | None
    start_priority: str | None
    last_status: str
    status_checked_at: datetime | None


class DatabaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    host_id: int
    name: str
    type: str
    instance_name: str | None
    vendor: str | None
    release: str | None
    last_status: str
    status_checked_at: datetime | None


class HostOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    address: str
    hostagent_port: int | None
    https: bool
    verify_tls: bool
    sapcontrol_port_offset: int
    username: str | None
    has_credentials: bool
    os_name: str | None
    os_version: str | None
    cpu_count: int | None
    memory_kb: int | None
    reachable: bool | None
    last_error: str | None
    last_discovery_at: datetime | None


class HostDetail(HostOut):
    instances: list[InstanceOut]
    databases: list[DatabaseOut]


class SystemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sid: str
    username: str | None
    has_credentials: bool
    instances: list[InstanceOut]


class DiscoveryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    instances_added: list[str]
    instances_updated: list[str]
    instances_missing: list[str]
    databases_added: list[str]
    databases_updated: list[str]
    databases_missing: list[str]
    warnings: list[str]
