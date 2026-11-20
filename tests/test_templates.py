"""Operation templates (chains of operations) and schedules."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import test_mass
from conftest import hold_activities, make_user
from landscape_orchestrator.models import Schedule
from landscape_orchestrator.schedules import next_run
from test_custom import LO_TEST
from test_mass import client

pytestmark = pytest.mark.anyio

app, api, transport = test_mass.app, test_mass.api, test_mass.transport

SYSTEM_LO_TEST = LO_TEST | {"key": "lo_test", "target": "SYSTEM", "role": "operator"}


def chain(*steps, **extra) -> dict:
    return {
        "key": "kernel_update",
        "name": "Kernel update",
        "target": "SYSTEM",
        "role": "operator",
        "steps": [s if isinstance(s, dict) else {"operation": s} for s in steps],
    } | extra


async def define(api, body) -> dict:
    response = await api.post("/api/templates", json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def run(app, api, path: str, body: dict | None = None) -> dict:
    response = await api.post(path, json=body or {})
    assert response.status_code == 202, response.text
    activity_id = response.json()["id"]
    await app.state.runner.wait(activity_id)
    return (await api.get(f"/api/activities/{activity_id}")).json()


async def test_define_templates(app, api):
    await api.post("/api/custom-operations", json=SYSTEM_LO_TEST)
    body = chain(
        {"operation": "lo_test", "parameters": {"RC": "0", "SLEEP": "1"}},
        "stop",
        {"operation": "start", "on_error": "continue"},
    )
    template = await define(api, body)
    assert [s["name"] for s in template["steps"]] == ["Test operation", "stop", "start"]
    assert template["valid"] and template["steps"][0]["parameters"] == {"RC": "0", "SLEEP": "1"}

    errors = [
        (chain("stop", key="start"), 422, "template_key"),
        (chain("stop", key="lo_test"), 409, "template_key_taken"),  # a custom operation
        (body, 409, "template_key_taken"),
        (chain("discover", key="x"), 422, "template_step"),  # a host operation
        (
            chain({"operation": "lo_test", "parameters": {"NOPE": "1"}}, key="x"),
            422,
            "template_step",
        ),
        (chain({"operation": "lo_test", "parameters": {"RC": "x"}}, key="x"), 422, "template_step"),
        (chain("stop", key="x", role="viewer"), 422, "template_role"),
        (
            chain({"operation": "discover", "on_error": "stop_and_start"}, key="x", target="HOST"),
            422,
            "invalid_input",
        ),
    ]
    for wrong, code, error in errors:
        response = await api.post("/api/templates", json=wrong)
        assert response.status_code == code, (wrong, response.text)
        assert response.json()["detail"]["code"] == error

    # A custom operation used by a template cannot be deleted.
    custom = (await api.get("/api/custom-operations")).json()[0]
    assert (await api.delete(f"/api/custom-operations/{custom['id']}")).status_code == 409
    # Templates appear in the catalog with the step parameters.
    operations = {op["key"]: op for op in (await api.get("/api/landscape")).json()["operations"]}
    offered = operations["kernel_update"]
    assert (
        offered["template"]
        and offered["custom"]
        and offered["steps"] == ["Test operation", "op.stop_system", "op.start_system"]
    )
    assert [p["name"] for p in offered["parameters"]] == ["1.A", "1.SLEEP", "1.RC"]
    assert offered["parameters"][2]["default"] == "0"

    changed = chain("restart", name="Restart only")
    updated = await api.put(f"/api/templates/{template['id']}", json=changed)
    assert updated.json()["name"] == "Restart only" and len(updated.json()["steps"]) == 1
    async with client(app, await make_user(app, "conf", "configurator")) as configurator:
        assert (await configurator.get("/api/templates")).status_code == 403
    assert (await api.delete(f"/api/templates/{template['id']}")).status_code == 204
    assert app.state.catalog.get("kernel_update") is None


async def test_run_a_chain(app, api, landscape):
    await api.post("/api/custom-operations", json=SYSTEM_LO_TEST)
    await define(
        api,
        chain(
            {"operation": "lo_test", "parameters": {"RC": "0", "SLEEP": "1", "A": "prep"}},
            "stop",
            "start",
        ),
    )
    result = await run(
        app, api, "/api/systems/1/operations/kernel_update", {"parameters": {"1.A": "run"}}
    )
    assert result["status"] == "SUCCEEDED", result
    assert result["kind"] == "TEMPLATE" and len(result["children"]) == 3
    assert [c["kind"] for c in result["children"]] == ["CUSTOM", "STOP", "START"]
    assert all(c["status"] == "SUCCEEDED" for c in result["children"])
    steps = [e["message"] for e in result["entries"] if e["event"] == "step"]
    assert steps == ["Step 1/3: Test operation", "Step 2/3: Stop", "Step 3/3: Start"]
    # The value of the run replaces the template's value.
    run_args = next(iter(landscape.operation_runs.values())).arguments
    assert "run" in run_args and "prep" not in run_args
    assert result["options"]["values"]["1"]["PARAM-A"] == "run"


async def test_error_behavior(app, api):
    await api.post("/api/custom-operations", json=SYSTEM_LO_TEST)
    failing = {"operation": "lo_test", "parameters": {"RC": "3", "SLEEP": "1"}}

    await define(api, chain(failing, "start"))
    stopped = await run(app, api, "/api/systems/1/operations/kernel_update")
    assert stopped["status"] == "FAILED" and len(stopped["children"]) == 1
    assert "Step 1/2: Test operation ended FAILED" in stopped["error"]

    await define(api, chain(failing | {"on_error": "continue"}, "start", key="go_on"))
    partly = await run(app, api, "/api/systems/1/operations/go_on")
    assert partly["status"] == "PARTIAL" and len(partly["children"]) == 2
    assert "1 of 2 steps failed" in partly["error"]

    await define(
        api, chain("stop", failing | {"on_error": "stop_and_start"}, "restart", key="safe")
    )
    safe = await run(app, api, "/api/systems/1/operations/safe")
    assert safe["status"] == "FAILED"
    assert [c["kind"] for c in safe["children"]] == ["STOP", "CUSTOM", "START"]
    assert safe["children"][2]["status"] == "SUCCEEDED"


async def test_template_holds_the_lock_and_can_be_cancelled(app, api):
    await define(api, chain("start", "stop"))  # the systems are stopped: start waits
    gate = hold_activities(app)
    response = await api.post("/api/systems/1/operations/kernel_update", json={})
    parent = response.json()["id"]
    await asyncio.sleep(0.05)
    # Another operation on the system waits for the chain to end.
    assert (await api.post("/api/systems/1/start")).status_code == 409
    cancelled = await api.post(f"/api/activities/{parent}/cancel")
    gate.set()
    assert cancelled.status_code == 200, cancelled.text
    result = cancelled.json()
    assert result["status"] == "CANCELLED"
    assert [c["status"] for c in result["children"]] == ["CANCELLED"]


async def test_mass_run_of_a_template(app, api):
    await define(api, chain("restart"))
    body = {"operation": "kernel_update", "target_type": "SYSTEM", "target_ids": [1, 2]}
    response = await api.post("/api/mass-operations", json=body)
    assert response.status_code == 202, response.text
    await app.state.runner.wait(response.json()["id"])
    parent = (await api.get(f"/api/activities/{response.json()['id']}")).json()
    assert parent["status"] == "SUCCEEDED" and parent["target_label"].startswith("Kernel update")
    assert [c["kind"] for c in parent["children"]] == ["TEMPLATE", "TEMPLATE"]


async def test_role_of_a_template(app, api):
    await define(api, chain("restart", role="configurator"))
    async with client(app, await make_user(app, "op", "operator")) as operator:
        response = await operator.post("/api/systems/1/operations/kernel_update", json={})
        assert response.status_code == 403


# --- schedules ----------------------------------------------------------------------------

BERLIN = ZoneInfo("Europe/Berlin")


def schedule(**fields) -> Schedule:
    base = {"recurrence": "daily", "weekdays": "1234567", "last_run_at": None, "run_at": None}
    return Schedule(**(base | fields))


def test_next_run():
    monday_noon = datetime(2026, 11, 23, 11, 0, tzinfo=UTC)  # 12:00 in Berlin
    daily = schedule(time_of_day="22:00")
    assert next_run(daily, monday_noon, BERLIN) == datetime(2026, 11, 23, 21, 0, tzinfo=UTC)
    weekend = schedule(time_of_day="06:30", weekdays="67")
    assert next_run(weekend, monday_noon, BERLIN) == datetime(2026, 11, 28, 5, 30, tzinfo=UTC)
    summer = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)
    assert next_run(daily, summer, BERLIN) == datetime(2026, 7, 1, 20, 0, tzinfo=UTC)  # CEST

    once = schedule(recurrence="once", run_at=monday_noon + timedelta(hours=3))
    assert next_run(once, monday_noon, BERLIN) == monday_noon + timedelta(hours=3)
    once.last_run_at = monday_noon
    assert next_run(once, monday_noon, BERLIN) is None

    every = schedule(recurrence="interval", interval_minutes=60)
    assert next_run(every, monday_noon, BERLIN) == monday_noon
    every.last_run_at = monday_noon
    assert next_run(every, monday_noon, BERLIN) == monday_noon + timedelta(hours=1)


async def test_schedules_start_operations(app, api):
    scheduler = app.state.scheduler
    now = datetime(2026, 11, 23, 11, 0, tzinfo=UTC)
    scheduler.now = lambda: now
    await define(api, chain("restart"))
    body = {
        "operation": "kernel_update",
        "target_type": "SYSTEM",
        "target_ids": [1],
        "recurrence": "once",
        "run_at": (now + timedelta(minutes=10)).isoformat(),
    }
    created = await api.post("/api/schedules", json=body)
    assert created.status_code == 201, created.text
    planned = created.json()
    assert (
        planned["next_run_at"].startswith("2026-11-23T11:10") and planned["created_by"] == "admin"
    )

    assert await scheduler.tick() == []  # not yet due
    now += timedelta(minutes=11)
    [activity_id] = await scheduler.tick()
    await app.state.runner.wait(activity_id)
    activity = (await api.get(f"/api/activities/{activity_id}")).json()
    assert activity["kind"] == "TEMPLATE" and activity["requested_by"] == "admin (schedule 1)"
    done = (await api.get("/api/schedules")).json()[0]
    assert done["enabled"] is False and done["next_run_at"] is None
    assert done["last_activity_id"] == activity_id
    assert await scheduler.tick() == []  # once only

    # A daily schedule on two systems runs as a mass operation.
    daily = body | {"recurrence": "daily", "time_of_day": "22:00", "target_ids": [1, 2]}
    daily.pop("run_at")
    created = (await api.post("/api/schedules", json=daily)).json()
    assert created["next_run_at"].startswith("2026-11-23T21:00")
    now = datetime(2026, 11, 23, 21, 0, 30, tzinfo=UTC)
    [mass_id] = await scheduler.tick()
    await app.state.runner.wait(mass_id)
    mass = (await api.get(f"/api/activities/{mass_id}")).json()
    assert mass["target_type"] == "MASS" and len(mass["children"]) == 2
    again = next(s for s in (await api.get("/api/schedules")).json() if s["id"] == created["id"])
    assert again["next_run_at"].startswith("2026-11-24T21:00")


async def test_schedule_checks(app, api):
    base = {
        "operation": "start",
        "target_type": "SYSTEM",
        "target_ids": [1],
        "recurrence": "interval",
    }
    cases = [
        (base | {"interval_minutes": 1}, 422, "schedule_time"),
        (base | {"recurrence": "daily", "time_of_day": "25:00"}, 422, "schedule_time"),
        (base | {"recurrence": "once"}, 422, "schedule_time"),
        (base | {"operation": "nothing", "interval_minutes": 60}, 422, "schedule_operation"),
        (base | {"target_type": "HOST", "interval_minutes": 60}, 422, "schedule_operation"),
    ]
    for body, code, error in cases:
        response = await api.post("/api/schedules", json=body)
        assert response.status_code == code, (body, response.text)
        assert response.json()["detail"]["code"] == error
    async with client(app, await make_user(app, "view", "viewer")) as viewer:
        refused = await viewer.post("/api/schedules", json=base | {"interval_minutes": 60})
        assert refused.status_code == 403
    async with client(app, await make_user(app, "op", "operator")) as operator:
        ok = await operator.post("/api/schedules", json=base | {"interval_minutes": 60})
        assert ok.status_code == 201
    # The user lost the role: the run is refused and recorded.
    users = (await api.get("/api/users")).json()
    op_id = next(u["id"] for u in users if u["username"] == "op")
    await api.patch(f"/api/users/{op_id}", json={"role": "viewer"})
    assert await app.state.scheduler.tick() == []
    record = (await api.get("/api/schedules")).json()[0]
    assert "no longer has the role operator" in record["last_error"]
    assert (await api.delete(f"/api/schedules/{record['id']}")).status_code == 204
