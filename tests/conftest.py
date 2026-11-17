from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from landscape_orchestrator.mock.landscape import MockLandscape, default_landscape_config

AUTH = ("devadm", "mock")


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def stopped_config() -> dict:
    """Sample landscape with all instances stopped: DEV on sapapp1, HANA system HDV on sapdb1."""
    config = default_landscape_config()
    for host in config["hosts"]:
        for instance in host["instances"]:
            instance["running"] = False
    return config


@pytest.fixture
def landscape(clock: FakeClock) -> MockLandscape:
    """Sample landscape with all instances stopped, start 3s, stop 2s."""
    return MockLandscape.from_dict(stopped_config(), clock=clock)


@pytest.fixture
def single_sid_landscape(clock: FakeClock) -> MockLandscape:
    """Variant where the HANA instance belongs to DEV as well.

    sapstartsrv then orders StartSystem by start priority across HANA, ASCS and the
    application server; these tests cover that behaviour.
    """
    config = stopped_config()
    for host in config["hosts"]:
        for instance in host["instances"]:
            instance["sid"] = "DEV"
    return MockLandscape.from_dict(config, clock=clock)


def soap_call(
    client: TestClient,
    namespace: str,
    method: str,
    params_xml: str = "",
    auth: tuple[str, str] | None = None,
):
    """POST a SOAP request; returns (HTTP status, parsed first child of the SOAP body)."""
    envelope = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" '
        f'xmlns:ns="{namespace}"><SOAP-ENV:Body>'
        f"<ns:{method}>{params_xml}</ns:{method}>"
        "</SOAP-ENV:Body></SOAP-ENV:Envelope>"
    )
    response = client.post("/", content=envelope, auth=auth)
    root = ET.fromstring(response.content)
    body = root.find("{http://schemas.xmlsoap.org/soap/envelope/}Body")
    return response.status_code, body[0]
