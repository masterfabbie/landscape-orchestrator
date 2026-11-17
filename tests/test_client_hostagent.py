from __future__ import annotations

import httpx
import pytest

from conftest import AUTH
from landscape_orchestrator.clients import (
    AuthenticationRequiredError,
    HostAgentClient,
    SoapFaultError,
)
from landscape_orchestrator.clients.hostagent import DatabaseStatus, InstanceStatus
from landscape_orchestrator.mock.apps import create_hostagent_app

pytestmark = pytest.mark.anyio


def make_client(landscape, host: str = "sapapp1", auth=AUTH) -> HostAgentClient:
    transport = httpx.ASGITransport(app=create_hostagent_app(landscape, host))
    return HostAgentClient(host, auth=auth, transport=transport)


def test_base_url():
    assert HostAgentClient("host1").base_url == "http://host1:1128"
    assert HostAgentClient("host1", https=True).base_url == "https://host1:1129"
    assert HostAgentClient("host1", port=11128).base_url == "http://host1:11128"


async def test_list_instances(landscape):
    async with make_client(landscape) as client:
        instances = await client.list_instances()
    assert sorted(i.system_number for i in instances) == ["00", "01"]
    first = instances[0]
    assert (first.sid, first.hostname) == ("DEV", "sapapp1")
    assert first.sap_version_info.startswith("753")


async def test_start_and_stop_instance(landscape, clock):
    async with make_client(landscape) as client:
        assert await client.list_instances(InstanceStatus.RUNNING) == []

        result = await client.start_instance("DEV", 0)
        assert "Start of instance DEV/00" in result.messages["description"]
        clock.advance(3)
        running = await client.list_instances(InstanceStatus.RUNNING)
        assert [i.system_number for i in running] == ["00"]

        await client.stop_instance("DEV", "00", soft_timeout=30)
        clock.advance(2)
        stopped = await client.list_instances(InstanceStatus.STOPPED)
        assert sorted(i.system_number for i in stopped) == ["00", "01"]


async def test_list_instances_by_hostname(landscape):
    async with make_client(landscape) as client:
        assert len(await client.list_instances(hostname="sapapp1")) == 2
        assert await client.list_instances(hostname="elsewhere") == []


async def test_start_unknown_instance_raises_fault(landscape):
    async with make_client(landscape) as client:
        with pytest.raises(SoapFaultError, match="not found"):
            await client.start_instance("DEV", 77)


async def test_start_instance_without_credentials(landscape):
    async with make_client(landscape, auth=None) as client:
        with pytest.raises(AuthenticationRequiredError):
            await client.start_instance("DEV", 0)


async def test_databases(landscape, clock):
    async with make_client(landscape, host="sapdb1") as client:
        system_db, database = await client.list_databases()
        assert system_db.name == "SYSTEMDB@HDV"
        assert (database.name, database.type) == ("HDV@HDV", "hdb")
        assert database.status is DatabaseStatus.STOPPED
        assert database.properties["Database/InstanceName"] == "HDB02"
        assert [c.name for c in database.components] == [
            "hdbdaemon",
            "hdbnameserver",
            "hdbindexserver",
        ]

        await client.start_instance("HDV", 2)
        state = await client.get_database_status("HDV@HDV", "hdb")
        assert state.status is DatabaseStatus.STARTING

        clock.advance(3)
        state = await client.get_database_status("HDV@HDV", "hdb", host="sapdb1")
        assert state.status is DatabaseStatus.RUNNING
        component = state.components[0]
        assert component.description == "HDB Daemon"
        assert component.status is DatabaseStatus.RUNNING
        assert component.status_description == "Running"


async def test_host_without_databases(landscape):
    async with make_client(landscape) as client:
        assert await client.list_databases() == []


async def test_unknown_database_raises_fault(landscape):
    async with make_client(landscape) as client:
        with pytest.raises(SoapFaultError, match="not found"):
            await client.get_database_status("XXX", "hdb")


async def test_computer_system(landscape):
    async with make_client(landscape) as client:
        system = await client.get_computer_system()
    assert system.class_name == "ITSAMComputerSystem"
    assert system.value("Name") == "sapapp1"
    assert system.properties["Hostnames"].type == "String[] sep=;"
    assert system.value("Missing", "fallback") == "fallback"

    (operating_system,) = system.find("ITSAMOperatingSystem")
    assert operating_system.value("Name") == "Linux"
    assert operating_system.properties["CPU Count"].type == "uint16"
    assert len(system.find("ITSAMFileSystem")) == 1
    assert system.find("ITSAMComputerSystem") == [system]
