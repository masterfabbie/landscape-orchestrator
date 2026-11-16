from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import AUTH, soap_call
from landscape_orchestrator.mock.apps import create_hostagent_app

NS = "urn:SAPHostControl"
INSTANCE_00 = "<aInstance><mSid>DEV</mSid><mSystemNumber>00</mSystemNumber></aInstance>"


@pytest.fixture
def client(landscape) -> TestClient:
    return TestClient(create_hostagent_app(landscape, "sapapp1"))


def listed(client: TestClient, selector: str | None = None) -> list[str]:
    params = (
        f"<aSelector><aInstanceStatus>{selector}</aInstanceStatus></aSelector>" if selector else ""
    )
    status, result = soap_call(client, NS, "ListInstances", params)
    assert status == 200
    return sorted(item.findtext("mSystemNumber") for item in result.find("result"))


def test_list_instances(client):
    status, result = soap_call(client, NS, "ListInstances")
    assert status == 200
    first = result.find("result")[0]
    assert first.findtext("mSid") == "DEV"
    assert first.findtext("mHostname") == "sapapp1"
    assert listed(client) == ["00", "01"]


def test_list_instances_filters_by_status(client, clock):
    assert listed(client, "S-INSTANCE-RUNNING") == []
    assert listed(client, "S-INSTANCE-STOPPED") == ["00", "01"]

    status, _ = soap_call(client, NS, "StartInstance", INSTANCE_00, auth=AUTH)
    assert status == 200
    clock.advance(3)
    assert listed(client, "S-INSTANCE-RUNNING") == ["00"]
    assert listed(client, "S-INSTANCE-STOPPED") == ["01"]

    soap_call(client, NS, "StopInstance", INSTANCE_00, auth=AUTH)
    clock.advance(2)
    assert listed(client, "S-INSTANCE-RUNNING") == []


def test_start_instance_requires_credentials(client):
    status, _ = soap_call(client, NS, "StartInstance", INSTANCE_00)
    assert status == 401
    assert listed(client, "S-INSTANCE-RUNNING") == []


def test_start_unknown_instance_returns_fault(client):
    params = "<aInstance><mSid>DEV</mSid><mSystemNumber>77</mSystemNumber></aInstance>"
    status, fault = soap_call(client, NS, "StartInstance", params, auth=AUTH)
    assert status == 500
    assert "not found" in fault.findtext("faultstring")


def test_get_computer_system(client):
    _, result = soap_call(client, NS, "GetComputerSystem")
    item = result.find("result")[0]
    assert item.findtext("mClassname") == "SAP_ITSAMComputerSystem"
    properties = {p.findtext("mName"): p.findtext("mValue") for p in item.find("mProperties")}
    assert properties["Name"] == "sapapp1"


def test_databases_on_database_host(landscape, clock):
    client = TestClient(create_hostagent_app(landscape, "sapdb1"))

    _, result = soap_call(client, NS, "ListDatabases")
    database = result.find("result")[0]
    properties = {p.findtext("mKey"): p.findtext("mValue") for p in database.find("mDatabase")}
    assert properties == {
        "Database/Name": "DEV",
        "Database/Type": "hdb",
        "Database/Host": "sapdb1",
    }
    assert database.findtext("mStatus") == "SAPHostControl-DB-STOPPED"

    hdb = "<aInstance><mSid>DEV</mSid><mSystemNumber>02</mSystemNumber></aInstance>"
    soap_call(client, NS, "StartInstance", hdb, auth=AUTH)
    arguments = (
        "<aArguments><item><mKey>Database/Name</mKey><mValue>DEV</mValue></item></aArguments>"
    )
    _, result = soap_call(client, NS, "GetDatabaseStatus", arguments)
    assert result.findtext("status") == "SAPHostControl-DB-WARNING"
    clock.advance(3)
    _, result = soap_call(client, NS, "GetDatabaseStatus", arguments)
    assert result.findtext("status") == "SAPHostControl-DB-RUNNING"


def test_host_without_databases_returns_empty_list(client):
    status, result = soap_call(client, NS, "ListDatabases")
    assert status == 200
    assert len(result.find("result")) == 0


def test_unknown_database_returns_fault(client):
    arguments = (
        "<aArguments><item><mKey>Database/Name</mKey><mValue>XXX</mValue></item></aArguments>"
    )
    status, fault = soap_call(client, NS, "GetDatabaseStatus", arguments)
    assert status == 500
    assert "not found" in fault.findtext("faultstring")
