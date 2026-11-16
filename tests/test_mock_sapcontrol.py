from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import AUTH, soap_call
from landscape_orchestrator.mock.apps import create_sapcontrol_app

NS = "urn:SAPControl"


@pytest.fixture
def client(landscape) -> TestClient:
    return TestClient(create_sapcontrol_app(landscape, "sapapp1", 0))


def process_states(client: TestClient) -> list[tuple[str, str, str]]:
    status, result = soap_call(client, NS, "GetProcessList")
    assert status == 200
    assert result.tag == f"{{{NS}}}GetProcessListResponse"
    return [
        (item.findtext("name"), item.findtext("dispstatus"), item.findtext("textstatus"))
        for item in result.find("process")
    ]


def test_get_process_list_reflects_start(client, clock):
    assert process_states(client)[0] == ("disp+work", "SAPControl-GRAY", "Stopped")

    status, _ = soap_call(client, NS, "Start", auth=AUTH)
    assert status == 200
    assert {state[1] for state in process_states(client)} == {"SAPControl-YELLOW"}

    clock.advance(3)
    assert process_states(client)[0] == ("disp+work", "SAPControl-GREEN", "Running")
    assert len(process_states(client)) == 4


def test_running_process_has_pid_and_elapsed_time(client, clock):
    soap_call(client, NS, "Start", auth=AUTH)
    clock.advance(3 + 3725)
    _, result = soap_call(client, NS, "GetProcessList")
    first = result.find("process")[0]
    assert int(first.findtext("pid")) > 0
    assert first.findtext("elapsedtime") == "1:02:08"


def test_protected_method_requires_credentials(client):
    status, fault = soap_call(client, NS, "Start")
    assert status == 401
    assert fault.findtext("faultstring") == "HTTP Error: 'Unauthorized'"

    status, fault = soap_call(client, NS, "Start", auth=("devadm", "wrong"))
    assert status == 500
    assert fault.findtext("faultstring") == "Invalid Credentials"

    assert process_states(client)[0][1] == "SAPControl-GRAY"


def test_unknown_method_returns_fault(client):
    status, fault = soap_call(client, NS, "DoesNotExist")
    assert status == 500
    assert "not implemented" in fault.findtext("faultstring")


def test_malformed_request_returns_fault(client):
    response = client.post("/", content=b"this is not xml")
    assert response.status_code == 500
    assert b"Invalid SOAP request" in response.content


def test_get_system_instance_list_covers_all_hosts(client):
    _, result = soap_call(client, NS, "GetSystemInstanceList")
    instances = {
        item.findtext("hostname"): (
            item.findtext("instanceNr"),
            item.findtext("startPriority"),
            item.findtext("features"),
            item.findtext("httpPort"),
        )
        for item in result.find("instance")
    }
    assert instances["sapdb1"] == ("2", "0.3", "HDB|HDB_WORKER", "60213")
    assert len(result.find("instance")) == 3


def test_start_system_and_stop_system(client, clock):
    params = "<options>SAPControl-ALL-INSTANCES</options><waittimeout>300</waittimeout>"
    status, _ = soap_call(client, NS, "StartSystem", params, auth=AUTH)
    assert status == 200

    def colors() -> list[str]:
        _, result = soap_call(client, NS, "GetSystemInstanceList")
        return sorted(item.findtext("dispstatus") for item in result.find("instance"))

    assert colors() == ["SAPControl-GRAY", "SAPControl-GRAY", "SAPControl-YELLOW"]
    clock.advance(9)
    assert colors() == ["SAPControl-GREEN"] * 3

    soap_call(client, NS, "StopSystem", params, auth=AUTH)
    clock.advance(6)
    assert colors() == ["SAPControl-GRAY"] * 3


def test_unsupported_start_option_returns_fault(client):
    params = "<options>SAPControl-PRIORITY-LEVEL</options>"
    status, fault = soap_call(client, NS, "StartSystem", params, auth=AUTH)
    assert status == 500
    assert "not supported" in fault.findtext("faultstring")
