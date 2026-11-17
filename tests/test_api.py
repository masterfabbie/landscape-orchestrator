from __future__ import annotations

import sqlite3

import httpx
import pytest

from landscape_orchestrator.api import create_app
from landscape_orchestrator.crypto import generate_api_token, generate_secret_key
from landscape_orchestrator.mock.transport import MockLandscapeTransport
from landscape_orchestrator.settings import Settings

pytestmark = pytest.mark.anyio

APP_HOST = {
    "name": "sapapp1",
    "address": "127.0.0.1",
    "username": "devadm",
    "password": "mock",
}
DB_HOST = APP_HOST | {"name": "sapdb1", "hostagent_port": 11128, "sapcontrol_port_offset": 10000}


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        secret_key=generate_secret_key(),
        api_token=generate_api_token(),
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'api.db'}",
    )


@pytest.fixture
async def api(settings, landscape):
    app = create_app(settings, transport=MockLandscapeTransport(landscape))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {settings.api_token}"},
        ) as client:
            yield client


async def create_host(api: httpx.AsyncClient, body: dict) -> dict:
    response = await api.post("/api/hosts", json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def test_health_needs_no_token(api):
    response = await api.get("/health", headers={"Authorization": ""})
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.parametrize("header", ["", "Bearer wrong-token", "Basic abc"])
async def test_api_requires_token(api, header):
    response = await api.get("/api/hosts", headers={"Authorization": header})
    assert response.status_code == 401


async def test_create_and_list_hosts(api):
    host = await create_host(api, APP_HOST)
    assert host["name"] == "sapapp1"
    assert host["has_credentials"] is True
    assert host["reachable"] is None
    assert "password" not in host and "password_encrypted" not in host

    listed = (await api.get("/api/hosts")).json()
    assert [h["name"] for h in listed] == ["sapapp1"]


async def test_address_defaults_to_name(api):
    host = await create_host(api, {"name": "somehost"})
    assert host["address"] == "somehost"
    assert host["has_credentials"] is False


async def test_duplicate_host_is_rejected(api):
    await create_host(api, APP_HOST)
    response = await api.post("/api/hosts", json=APP_HOST)
    assert response.status_code == 409


async def test_username_without_password_is_rejected(api):
    response = await api.post("/api/hosts", json={"name": "h", "username": "u"})
    assert response.status_code == 422


async def test_password_is_stored_encrypted(api, settings):
    await create_host(api, APP_HOST | {"password": "very-secret-value"})
    database_file = settings.database_url.removeprefix("sqlite+aiosqlite:///")
    with sqlite3.connect(database_file) as connection:
        (stored,) = connection.execute("SELECT password_encrypted FROM hosts").fetchone()
    assert stored and "very-secret-value" not in stored


async def test_discover_fills_inventory(api):
    host = await create_host(api, APP_HOST)

    response = await api.post(f"/api/hosts/{host['id']}/discover")
    assert response.status_code == 200
    assert response.json()["instances_added"] == ["DEV/01", "DEV/00"]

    detail = (await api.get(f"/api/hosts/{host['id']}")).json()
    assert detail["reachable"] is True
    assert detail["os_name"] == "Linux"
    assert [i["number"] for i in detail["instances"]] == ["00", "01"]
    assert detail["instances"][0]["features"] == "ABAP|GATEWAY|ICMAN|IGS"
    assert detail["databases"] == []


async def test_systems_from_two_hosts(api):
    for body in (APP_HOST, DB_HOST):
        host = await create_host(api, body)
        assert (await api.post(f"/api/hosts/{host['id']}/discover")).status_code == 200

    dev, hdv = (await api.get("/api/systems")).json()
    assert (dev["sid"], hdv["sid"]) == ("DEV", "HDV")
    assert dev["has_credentials"] is False
    assert [i["number"] for i in dev["instances"]] == ["00", "01"]
    assert [i["number"] for i in hdv["instances"]] == ["02"]
    assert dev["depends_on_ids"] == [] and hdv["required_by_ids"] == []

    db_detail = (await api.get("/api/hosts/2")).json()
    assert [d["name"] for d in db_detail["databases"]] == ["HDV@HDV", "SYSTEMDB@HDV"]


async def test_discover_unreachable_host_returns_502(api):
    host = await create_host(api, APP_HOST | {"hostagent_port": 4711})

    response = await api.post(f"/api/hosts/{host['id']}/discover")
    assert response.status_code == 502
    assert "Connection refused" in response.json()["detail"]

    detail = (await api.get(f"/api/hosts/{host['id']}")).json()
    assert detail["reachable"] is False
    assert "Connection refused" in detail["last_error"]


async def test_set_credentials(api):
    host = await create_host(api, {"name": "sapapp1", "address": "127.0.0.1"})
    credentials = {"username": "devadm", "password": "mock"}

    response = await api.put(f"/api/hosts/{host['id']}/credentials", json=credentials)
    assert response.status_code == 204
    assert (await api.get(f"/api/hosts/{host['id']}")).json()["username"] == "devadm"

    await api.post(f"/api/hosts/{host['id']}/discover")
    (system,) = (await api.get("/api/systems")).json()
    response = await api.put(f"/api/systems/{system['id']}/credentials", json=credentials)
    assert response.status_code == 204
    assert (await api.get("/api/systems")).json()[0]["has_credentials"] is True


async def test_delete_host_removes_its_instances(api):
    host = await create_host(api, APP_HOST)
    await api.post(f"/api/hosts/{host['id']}/discover")

    assert (await api.delete(f"/api/hosts/{host['id']}")).status_code == 204
    assert (await api.get("/api/hosts")).json() == []
    assert (await api.get("/api/systems")).json()[0]["instances"] == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/hosts/99"),
        ("DELETE", "/api/hosts/99"),
        ("POST", "/api/hosts/99/discover"),
    ],
)
async def test_unknown_host_returns_404(api, method, path):
    assert (await api.request(method, path)).status_code == 404


async def test_unknown_system_returns_404(api):
    body = {"username": "u", "password": "p"}
    assert (await api.put("/api/systems/99/credentials", json=body)).status_code == 404


async def discover_both(api) -> tuple[int, int]:
    for body in (APP_HOST, DB_HOST):
        host = await create_host(api, body)
        await api.post(f"/api/hosts/{host['id']}/discover")
    systems = {s["sid"]: s["id"] for s in (await api.get("/api/systems")).json()}
    return systems["DEV"], systems["HDV"]


async def test_set_dependencies(api):
    dev, hdv = await discover_both(api)

    response = await api.put(f"/api/systems/{dev}/dependencies", json={"depends_on": [hdv]})
    assert response.status_code == 200
    assert response.json()["depends_on_ids"] == [hdv]

    systems = {s["sid"]: s for s in (await api.get("/api/systems")).json()}
    assert systems["HDV"]["required_by_ids"] == [dev]

    cleared = await api.put(f"/api/systems/{dev}/dependencies", json={"depends_on": []})
    assert cleared.json()["depends_on_ids"] == []


@pytest.mark.parametrize(
    ("depends_on", "message"),
    [("self", "itself"), ("unknown", "Unknown systems"), ("cycle", "cycle")],
)
async def test_invalid_dependencies(api, depends_on, message):
    dev, hdv = await discover_both(api)
    if depends_on == "cycle":
        await api.put(f"/api/systems/{hdv}/dependencies", json={"depends_on": [dev]})
    target = {"self": [dev], "unknown": [999], "cycle": [hdv]}[depends_on]

    response = await api.put(f"/api/systems/{dev}/dependencies", json={"depends_on": target})

    assert response.status_code == 422
    assert message in response.json()["detail"]


async def test_dependencies_of_unknown_system(api):
    response = await api.put("/api/systems/99/dependencies", json={"depends_on": []})
    assert response.status_code == 404
