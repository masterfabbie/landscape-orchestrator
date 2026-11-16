from __future__ import annotations

import asyncio

import httpx
import pytest

from landscape_orchestrator.api import create_app
from landscape_orchestrator.crypto import generate_api_token, generate_secret_key
from landscape_orchestrator.mock.landscape import Status
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
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'ops.db'}",
    )


@pytest.fixture
def transport(landscape) -> MockLandscapeTransport:
    return MockLandscapeTransport(landscape)


@pytest.fixture
def make_app(settings, transport, clock):
    """App whose operation tracking runs on the fake clock: each poll advances it by 1 s."""

    async def fake_sleep(seconds: float) -> None:
        clock.advance(seconds)
        await asyncio.sleep(0)

    def factory():
        return create_app(
            settings, transport=transport, poll_interval=1.0, sleep=fake_sleep, clock=clock
        )

    return factory


@pytest.fixture
async def app(make_app):
    application = make_app()
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def api(app, settings):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {settings.api_token}"},
    ) as client:
        for body in (APP_HOST, DB_HOST):
            host = (await client.post("/api/hosts", json=body)).json()
            assert (await client.post(f"/api/hosts/{host['id']}/discover")).status_code == 200
        yield client


async def instance_id(api: httpx.AsyncClient, number: str) -> int:
    (system,) = (await api.get("/api/systems")).json()
    return next(i["id"] for i in system["instances"] if i["number"] == number)


async def statuses(api: httpx.AsyncClient) -> dict[str, str]:
    (system,) = (await api.get("/api/systems")).json()
    return {i["number"]: i["last_status"] for i in system["instances"]}


async def run(app, api: httpx.AsyncClient, path: str) -> dict:
    """Submit an operation, wait for it and return the finished activity with its log."""
    response = await api.post(path)
    assert response.status_code == 202, response.text
    activity = response.json()
    assert activity["status"] == "PENDING"
    await app.state.runner.wait(activity["id"])
    return (await api.get(f"/api/activities/{activity['id']}")).json()


def messages(activity: dict) -> list[str]:
    return [entry["message"] for entry in activity["entries"]]


async def test_start_instance(app, api, landscape, clock):
    dialog = await instance_id(api, "00")

    activity = await run(app, api, f"/api/instances/{dialog}/start")

    assert activity["status"] == "SUCCEEDED"
    assert (activity["kind"], activity["target_type"]) == ("START", "INSTANCE")
    assert activity["target_label"] == "DEV/00"
    assert activity["error"] is None
    assert activity["started_at"] and activity["finished_at"]
    assert messages(activity) == [
        "Start of DEV/00 requested via host agent",
        "Instance status: YELLOW",
        "Instance status: GREEN",
        "Finished successfully",
    ]
    assert landscape.host("sapapp1").instance(0).status(clock()) is Status.RUNNING
    assert await statuses(api) == {"00": "GREEN", "01": "GRAY", "02": "GRAY"}


async def test_stop_and_restart_instance(app, api, landscape, clock):
    dialog = await instance_id(api, "00")
    await run(app, api, f"/api/instances/{dialog}/start")

    restart = await run(app, api, f"/api/instances/{dialog}/restart")
    assert restart["status"] == "SUCCEEDED"
    assert messages(restart) == [
        "Stop of DEV/00 requested via host agent",
        "Instance status: YELLOW",
        "Instance status: GRAY",
        "Start of DEV/00 requested via host agent",
        "Instance status: YELLOW",
        "Instance status: GREEN",
        "Finished successfully",
    ]

    stop = await run(app, api, f"/api/instances/{dialog}/stop")
    assert stop["status"] == "SUCCEEDED"
    assert landscape.host("sapapp1").instance(0).status(clock()) is Status.STOPPED
    assert (await statuses(api))["00"] == "GRAY"


async def test_start_and_stop_system(app, api):
    (system,) = (await api.get("/api/systems")).json()

    start = await run(app, api, f"/api/systems/{system['id']}/start")
    assert start["status"] == "SUCCEEDED"
    assert start["target_label"] == "DEV"
    log = messages(start)
    assert log[0] == (
        "Start of system DEV requested via sapstartsrv of instance 02 on sapdb1"
    )
    # HANA first, then central services, then the dialog instance.
    assert "System status: 00:GRAY 01:GRAY 02:YELLOW" in log
    assert "System status: 00:GRAY 01:YELLOW 02:GREEN" in log
    assert log[-2:] == ["System status: 00:GREEN 01:GREEN 02:GREEN", "Finished successfully"]
    assert set((await statuses(api)).values()) == {"GREEN"}

    stop = await run(app, api, f"/api/systems/{system['id']}/stop")
    assert stop["status"] == "SUCCEEDED"
    assert set((await statuses(api)).values()) == {"GRAY"}


async def test_restart_system(app, api):
    (system,) = (await api.get("/api/systems")).json()
    await run(app, api, f"/api/systems/{system['id']}/start")

    restart = await run(app, api, f"/api/systems/{system['id']}/restart")

    assert restart["status"] == "SUCCEEDED"
    log = messages(restart)
    assert "System status: 00:GRAY 01:GRAY 02:GRAY" in log
    assert log[-2] == "System status: 00:GREEN 01:GREEN 02:GREEN"


async def test_timeout_fails_the_activity(app, api, landscape):
    landscape.start_duration = 10_000
    dialog = await instance_id(api, "00")

    activity = await run(app, api, f"/api/instances/{dialog}/start?timeout=10")

    assert activity["status"] == "FAILED"
    assert activity["error"] == "Timeout after 10 s: instance is YELLOW, expected GREEN"
    assert messages(activity)[-1] == activity["error"]
    assert (await statuses(api))["00"] == "YELLOW"


async def test_wrong_credentials_fail_the_activity(app, api):
    await api.put("/api/hosts/1/credentials", json={"username": "devadm", "password": "wrong"})
    dialog = await instance_id(api, "00")

    activity = await run(app, api, f"/api/instances/{dialog}/start")

    assert activity["status"] == "FAILED"
    assert activity["error"] == "Invalid Credentials"
    assert (await statuses(api))["00"] == "GRAY"


async def test_unreachable_sapstartsrv_during_instance_start(app, api, transport):
    transport.remove_port(50013)
    dialog = await instance_id(api, "00")

    activity = await run(app, api, f"/api/instances/{dialog}/start?timeout=10")

    assert activity["status"] == "FAILED"
    assert "instance is UNKNOWN" in activity["error"]


async def test_system_operation_uses_another_sapstartsrv_if_one_is_down(app, api, transport):
    transport.remove_port(60213)  # sapstartsrv of the HANA instance
    (system,) = (await api.get("/api/systems")).json()

    activity = await run(app, api, f"/api/systems/{system['id']}/start")

    assert activity["status"] == "SUCCEEDED"
    assert "via sapstartsrv of instance 01 on sapapp1" in messages(activity)[0]


async def test_system_without_reachable_sapstartsrv_fails(app, api, transport):
    for port in (50013, 50113, 60213):
        transport.remove_port(port)
    (system,) = (await api.get("/api/systems")).json()

    activity = await run(app, api, f"/api/systems/{system['id']}/start")

    assert activity["status"] == "FAILED"
    assert activity["error"] == "No sapstartsrv of the system could be reached"


async def test_only_one_activity_per_system(app, api):
    dialog = await instance_id(api, "00")
    (system,) = (await api.get("/api/systems")).json()

    first = await api.post(f"/api/instances/{dialog}/start")
    assert first.status_code == 202
    second = await api.post(f"/api/systems/{system['id']}/stop")
    assert second.status_code == 409
    assert "START DEV/00" in second.json()["detail"]

    await app.state.runner.wait(first.json()["id"])
    third = await api.post(f"/api/systems/{system['id']}/stop")
    assert third.status_code == 202
    await app.state.runner.wait(third.json()["id"])


async def test_activity_list_is_newest_first(app, api):
    dialog = await instance_id(api, "00")
    await run(app, api, f"/api/instances/{dialog}/start")
    await run(app, api, f"/api/instances/{dialog}/stop")

    listed = (await api.get("/api/activities")).json()
    assert [(a["kind"], a["status"]) for a in listed] == [
        ("STOP", "SUCCEEDED"),
        ("START", "SUCCEEDED"),
    ]
    assert "entries" not in listed[0]
    assert len((await api.get("/api/activities?limit=1")).json()) == 1


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/api/instances/999/start", 404),
        ("/api/systems/999/stop", 404),
        ("/api/instances/1/explode", 422),
        ("/api/instances/1/start?timeout=1", 422),
        ("/api/activities/999", 404),
    ],
)
async def test_invalid_requests(api, path, expected):
    method = "GET" if "activities" in path else "POST"
    assert (await api.request(method, path)).status_code == expected


async def test_activities_interrupted_by_restart_are_marked_failed(
    make_app, settings, landscape
):
    landscape.start_duration = 10_000
    headers = {"Authorization": f"Bearer {settings.api_token}"}

    first = make_app()
    async with first.router.lifespan_context(first):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=first), base_url="http://test", headers=headers
        ) as client:
            host = (await client.post("/api/hosts", json=APP_HOST)).json()
            await client.post(f"/api/hosts/{host['id']}/discover")
            activity = (await client.post("/api/instances/1/start")).json()
            await asyncio.sleep(0)
    # Leaving the lifespan shuts the server down while the activity is still running.

    second = make_app()
    async with second.router.lifespan_context(second):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=second), base_url="http://test", headers=headers
        ) as client:
            stored = (await client.get(f"/api/activities/{activity['id']}")).json()
            assert stored["status"] == "FAILED"
            assert "Interrupted" in stored["error"]
            # The system is free for new operations again.
            assert (await client.post("/api/instances/1/stop")).status_code == 202
