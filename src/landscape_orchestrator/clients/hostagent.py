"""Client for the SAPHostControl web service of the SAP Host Agent (one host)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import httpx

from ..soapxml import SAPHOSTCONTROL_NS, items
from .base import SoapClient, SoapProtocolError, as_dict

HOSTAGENT_HTTP_PORT = 1128
HOSTAGENT_HTTPS_PORT = 1129


class InstanceStatus(Enum):
    INSTALLED = "S-INSTALLED"
    RUNNING = "S-RUNNING"
    STOPPED = "S-STOPPED"


class DatabaseStatus(Enum):
    UNKNOWN = "SAPHostControl-DB-UNKNOWN"
    STOPPED = "SAPHostControl-DB-STOPPED"
    STOPPING = "SAPHostControl-DB-STOPPING"
    RUNNING = "SAPHostControl-DB-RUNNING"
    STARTING = "SAPHostControl-DB-STARTING"
    WARNING = "SAPHostControl-DB-WARNING"
    INSERVICE = "SAPHostControl-DB-INSERVICE"
    ERROR = "SAPHostControl-DB-ERROR"


class InstanceOption(Enum):
    INSTANCE = "O-INSTANCE"
    SERVICE = "O-SERVICE"
    PREHOOK = "O-PREHOOK"
    POSTHOOK = "O-POSTHOOK"
    CLEANUP = "O-CLEANUP"
    FORCE = "O-FORCE"
    ERRORHOOK = "O-ERRORHOOK"
    SERVICEONLY = "O-SERVICEONLY"
    NOSYSTEMD = "O-NOSYSTEMD"


@dataclass(frozen=True)
class SapInstance:
    sid: str
    hostname: str
    system_number: str
    sap_version_info: str


@dataclass(frozen=True)
class DatabaseComponent:
    name: str
    description: str
    status: DatabaseStatus
    status_description: str
    properties: dict[str, str]


@dataclass(frozen=True)
class Database:
    status: DatabaseStatus
    properties: dict[str, str]
    components: list[DatabaseComponent]

    @property
    def name(self) -> str:
        return self.properties.get("Database/Name", "")

    @property
    def type(self) -> str:
        return self.properties.get("Database/Type", "")


@dataclass(frozen=True)
class DatabaseState:
    status: DatabaseStatus
    components: list[DatabaseComponent]


@dataclass(frozen=True)
class CimAttribute:
    type: str
    value: str


@dataclass(frozen=True)
class CimObject:
    properties: dict[str, CimAttribute]
    members: list[CimObject] = field(default_factory=list)

    @property
    def class_name(self) -> str:
        return self.value("CreationClassName")

    def value(self, name: str, default: str = "") -> str:
        attribute = self.properties.get(name)
        return attribute.value if attribute else default

    def find(self, class_name: str) -> list[CimObject]:
        """This object and all nested members of the given class."""
        found = [self] if self.class_name == class_name else []
        for member in self.members:
            found += member.find(class_name)
        return found


@dataclass(frozen=True)
class OperationResult:
    operation_id: str
    messages: dict[str, str]


def _property_dict(value: Any) -> dict[str, str]:
    return {
        item.get("mKey", ""): item.get("mValue", "") for item in map(as_dict, items(value))
    }


def _property_list(values: dict[str, str | None]) -> list[dict[str, str]]:
    return [{"mKey": key, "mValue": value} for key, value in values.items() if value is not None]


def _db_status(value: Any) -> DatabaseStatus:
    try:
        return DatabaseStatus(value)
    except ValueError as exc:
        raise SoapProtocolError(f"Unknown database status: {value!r}") from exc


def _components(value: Any) -> list[DatabaseComponent]:
    components = []
    for item in map(as_dict, items(value)):
        properties = _property_dict(item.get("mProperties"))
        components.append(
            DatabaseComponent(
                name=properties.get("Database/ComponentName", ""),
                description=properties.get("Database/ComponentDescription", ""),
                status=_db_status(item.get("mStatus")),
                status_description=properties.get("Database/ComponentStatusDescription", ""),
                properties=properties,
            )
        )
    return components


def _cim_object(value: Any) -> CimObject:
    data = as_dict(value)
    return CimObject(
        properties={
            item.get("mName", ""): CimAttribute(item.get("mType", ""), item.get("mValue", ""))
            for item in map(as_dict, items(data.get("mProperties")))
        },
        members=[_cim_object(member) for member in items(data.get("mMembers"))],
    )


def _operation_result(value: Any) -> OperationResult:
    data = as_dict(value)
    return OperationResult(
        operation_id=data.get("mOperationID", "") or "",
        messages={
            item.get("mMessageKey", ""): item.get("mMessageValue", "")
            for item in map(as_dict, items(data.get("mOperationResults")))
        },
    )


class HostAgentClient(SoapClient):
    namespace = SAPHOSTCONTROL_NS
    path = "/SAPHostControl.cgi"

    def __init__(
        self,
        host: str,
        *,
        port: int | None = None,
        https: bool = False,
        auth: tuple[str, str] | None = None,
        verify: bool | str = True,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if port is None:
            port = HOSTAGENT_HTTPS_PORT if https else HOSTAGENT_HTTP_PORT
        scheme = "https" if https else "http"
        super().__init__(
            f"{scheme}://{host}:{port}",
            auth=auth,
            verify=verify,
            timeout=timeout,
            transport=transport,
        )
        self.host = host

    async def list_instances(
        self,
        status: InstanceStatus = InstanceStatus.INSTALLED,
        hostname: str | None = None,
    ) -> list[SapInstance]:
        """SAP instances on the host. ``hostname`` limits the list to one SAPLOCALHOST."""
        result = await self.call(
            "ListInstances",
            {"aSelector": {"aInstanceStatus": status.value, "aHostname": hostname}},
        )
        return [
            SapInstance(
                sid=item.get("mSid", ""),
                hostname=item.get("mHostname", ""),
                system_number=item.get("mSystemNumber", ""),
                sap_version_info=item.get("mSapVersionInfo", ""),
            )
            for item in map(as_dict, items(result.get("aInstances")))
        ]

    async def list_databases(self) -> list[Database]:
        result = await self.call("ListDatabases", {"aArguments": []})
        return [
            Database(
                status=_db_status(item.get("mStatus")),
                properties=_property_dict(item.get("mDatabase")),
                components=_components(item.get("mComponents")),
            )
            for item in map(as_dict, items(result.get("result")))
        ]

    async def get_database_status(
        self,
        name: str,
        type: str,
        *,
        instance_name: str | None = None,
        host: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ) -> DatabaseState:
        """Overall status and components of one database.

        Database user and password are required for the types ``ada``, ``sap`` and ``db6``.
        """
        arguments = _property_list(
            {
                "Database/Name": name,
                "Database/Type": type,
                "Database/InstanceName": instance_name,
                "Database/Host": host,
                "Database/Username": username,
                "Database/Password": password,
            }
        )
        result = await self.call("GetDatabaseStatus", {"aArguments": arguments})
        return DatabaseState(
            status=_db_status(result.get("status")),
            components=_components(result.get("result")),
        )

    async def get_computer_system(self) -> CimObject:
        """Host information: computer system, operating system, file systems, processes."""
        result = await self.call("GetComputerSystem", {"aArguments": []})
        return _cim_object(result.get("result"))

    async def start_instance(
        self,
        sid: str,
        system_number: str | int,
        *,
        timeout: int = 0,
        options: Sequence[InstanceOption] = (InstanceOption.INSTANCE,),
    ) -> OperationResult:
        """Start an instance. ``timeout`` 0 is asynchronous, -1 waits until it is done."""
        return await self._instance_operation("StartInstance", sid, system_number, timeout, options)

    async def stop_instance(
        self,
        sid: str,
        system_number: str | int,
        *,
        timeout: int = 0,
        soft_timeout: int = 0,
        options: Sequence[InstanceOption] = (InstanceOption.INSTANCE,),
    ) -> OperationResult:
        """Stop an instance. ``timeout`` 0 is asynchronous, -1 waits until it is done."""
        return await self._instance_operation(
            "StopInstance", sid, system_number, timeout, options, soft_timeout
        )

    async def _instance_operation(
        self,
        method: str,
        sid: str,
        system_number: str | int,
        timeout: int,
        options: Sequence[InstanceOption],
        soft_timeout: int = 0,
    ) -> OperationResult:
        number = f"{int(system_number):02d}"
        result = await self.call(
            method,
            {
                "aInstance": {"mSid": sid, "mSystemNumber": number},
                "aOptions": {
                    "mTimeout": timeout,
                    "mOptions": [option.value for option in options],
                    "mSoftTimeout": soft_timeout,
                },
            },
        )
        return _operation_result(result.get("response"))
