"""Custom operations (operations.d of the host agent) and cancelling activities."""

from __future__ import annotations

import asyncio

import pytest

import test_mass
from conftest import hold_activities, make_user
from landscape_orchestrator.customops import ParameterError, parameter_values
from landscape_orchestrator.customspec import CustomSpec, ParameterSpec, service_subtype
from landscape_orchestrator.models import Instance, System
from test_mass import client, sign_in

pytestmark = pytest.mark.anyio

# Same fixtures as the mass operations: systems A (DEV/01), B (DEV/00), C (HDV/02).
app, api, transport = test_mass.app, test_mass.api, test_mass.transport

LO_TEST = {
    "key": "lo_test",
    "name": "Test operation",
    "agent_operation": "LO_TEST",
    "target": "INSTANCE",
    "role": "operator",
    "timeout_seconds": 60,
    "parameters": [
        {"name": "A", "label": "Text"},
        {"name": "SLEEP", "label": "Duration", "type": "INTEGER", "default": "2"},
        {"name": "RC", "label": "Exit code", "type": "INTEGER", "required": True},
    ],
}


def spec(*parameters: ParameterSpec) -> CustomSpec:
    return CustomSpec(1, "T", None, "LO_TEST", "HOST", (), "parallel", 60, parameters)


def log(activity: dict) -> list[str]:
    return [entry["message"] for entry in activity["entries"]]


async def finished(app, api, activity_id: int) -> dict:
    await app.state.runner.wait(activity_id)
    return (await api.get(f"/api/activities/{activity_id}")).json()


# --- values ---------------------------------------------------------------------------------


def test_parameter_values():
    text = ParameterSpec("A", "A", "STRING", False, None, None, None)
    number = ParameterSpec("N", "N", "INTEGER", True, None, None, None)
    flag = ParameterSpec("F", "F", "BOOLEAN", False, None, None, None)
    own = ParameterSpec("P", "P", "STRING", False, None, r"\d{3}", None)
    values = parameter_values(spec(text, number, flag, own), {"N": 5, "F": "true", "P": "793"})
    # The empty optional text is not passed at all.
    assert values == {"PARAM-N": "5", "PARAM-F": "true", "PARAM-P": "793"}
    assert parameter_values(spec(flag), {})["PARAM-F"] == "false"
    with pytest.raises(ParameterError) as problems:
        parameter_values(spec(text, number, own), {"A": "a;rm -rf /", "N": "x", "P": "79"})
    assert set(problems.value.problems) == {"A", "N", "P"}
    with pytest.raises(ParameterError, match="N: required"):
        parameter_values(spec(number), {"N": " "})


def test_service_subtype():
    def Inst(features):  # noqa: N802
        return Instance(features=features)

    def Sys(*instances):  # noqa: N802
        return System(instances=list(instances))

    abap, java = Inst("ABAP|GATEWAY|ICMAN|IGS"), Inst("J2EE|IGS")
    scs, hana, agent = Inst("MESSAGESERVER|ENQUE"), Inst("HDB|HDB_WORKER"), Inst("SMDAGENT")
    assert service_subtype(abap, Sys(abap, scs)) == "Instance_Application_Server/ABAP"
    assert service_subtype(scs, Sys(abap, scs)) == "Instance_SCS/ABAP"
    assert service_subtype(scs, Sys(java, scs)) == "Instance_SCS/J2EE"
    assert service_subtype(hana, Sys(abap, hana)) == "Instance_System_HDB/ABAP"
    assert service_subtype(agent, None) == "Instance_Diag_Agnt/SolMan"


# --- API ------------------------------------------------------------------------------------


async def create(api, body=LO_TEST) -> dict:
    response = await api.post("/api/custom-operations", json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def test_define_custom_operations(app, api):
    operation = await create(api)
    assert [p["name"] for p in operation["parameters"]] == ["A", "SLEEP", "RC"]
    assert (await api.post("/api/custom-operations", json=LO_TEST)).status_code == 409
    builtin = LO_TEST | {"key": "start"}
    assert (await api.post("/api/custom-operations", json=builtin)).status_code == 422
    bad_default = LO_TEST | {
        "key": "x",
        "parameters": [{"name": "N", "label": "N", "type": "INTEGER", "default": "a"}],
    }
    assert (await api.post("/api/custom-operations", json=bad_default)).status_code == 422

    listed = {op["key"]: op for op in (await api.get("/api/operations")).json()}
    assert listed["lo_test"]["custom"] and listed["lo_test"]["targets"] == ["INSTANCE"]
    assert listed["lo_test"]["parameters"][2]["required"]

    changed = LO_TEST | {"name": "Renamed", "parameters": [{"name": "RC", "label": "RC"}]}
    response = await api.put(f"/api/custom-operations/{operation['id']}", json=changed)
    assert response.json()["name"] == "Renamed" and len(response.json()["parameters"]) == 1
    assert app.state.catalog.get("lo_test").custom.name == "Renamed"

    async with client(app, await make_user(app, "conf", "configurator")) as configurator:
        assert (await configurator.get("/api/custom-operations")).status_code == 403
        assert (await configurator.post("/api/custom-operations", json=LO_TEST)).status_code == 403

    assert (await api.delete(f"/api/custom-operations/{operation['id']}")).status_code == 204
    assert app.state.catalog.get("lo_test") is None


async def test_run_on_instance(app, api, landscape):
    await create(api)
    async with client(app, await make_user(app, "op", "operator")) as operator:
        body = {"parameters": {"A": "hello", "RC": 0}}
        response = await operator.post("/api/instances/2/operations/lo_test", json=body)
        assert response.status_code == 202, response.text
        result = await finished(app, api, response.json()["id"])
    assert result["status"] == "SUCCEEDED" and result["kind"] == "CUSTOM"
    messages = log(result)
    assert any(m.startswith("Started on sapapp1, operation ") for m in messages)
    assert any("hook successfully executed" in m for m in messages)
    assert not any("Executing" in m for m in messages)
    run = next(iter(landscape.operation_runs.values()))
    # In the order of Command: SAPSYSTEMNAME SAPSYSTEM SERVICE_SUBTYPE PARAM-A -SLEEP -RC
    assert run.arguments == ["DEV", "00", "Instance_Application_Server/ABAP", "hello", "2", "0"]

    missing = await api.post("/api/instances/2/operations/lo_test", json={"parameters": {}})
    assert missing.status_code == 422 and "RC: required" in missing.text
    assert (await api.post("/api/systems/1/operations/lo_test", json={})).status_code == 404
    assert (await api.post("/api/instances/2/operations/nothing", json={})).status_code == 404


async def test_failed_script(app, api):
    await create(api)
    response = await api.post("/api/instances/2/operations/lo_test", json={"parameters": {"RC": 3}})
    result = await finished(app, api, response.json()["id"])
    assert result["status"] == "FAILED"
    assert "exit code = 3" in result["error"]


async def test_timeout_cancels_on_the_host(app, api, landscape):
    await create(api)
    body = {"parameters": {"RC": 0, "SLEEP": 500}}
    response = await api.post("/api/instances/2/operations/lo_test?timeout=10", json=body)
    result = await finished(app, api, response.json()["id"])
    assert result["status"] == "FAILED"
    assert not landscape.operation_runs  # cancelled and forgotten by the host agent


async def test_role_of_the_operation(app, api):
    await create(api, LO_TEST | {"role": "admin"})
    async with client(app, await make_user(app, "op", "operator")) as operator:
        body = {"parameters": {"RC": 0}}
        response = await operator.post("/api/instances/2/operations/lo_test", json=body)
        assert response.status_code == 403
        assert all(op["key"] != "lo_test" for op in (await operator.get("/api/operations")).json())
    async with client(app, await make_user(app, "view", "viewer")) as viewer:
        response = await viewer.post("/api/instances/2/operations/lo_test", json=body)
        assert response.status_code == 403


async def test_run_on_system_by_roles(app, api, landscape):
    """System A (DEV/01) and B (DEV/00) in one system with C: roles order the instances."""
    ids = {i["label"]: i["id"] for i in (await api.get("/api/instances")).json()}
    for system in (1, 2):
        await api.delete(f"/api/systems/{system}")
    await api.post(
        "/api/systems/3/instances", json={"instance_ids": [ids["DEV/00"], ids["DEV/01"]]}
    )
    await create(api, LO_TEST | {"target": "SYSTEM", "ordering": "roles"})
    body = {"parameters": {"RC": 0, "SLEEP": 1}}
    response = await api.post("/api/systems/3/operations/lo_test", json=body)
    assert response.status_code == 202, response.text
    result = await finished(app, api, response.json()["id"])
    assert result["status"] == "SUCCEEDED"
    started = [m for m in log(result) if "Started on" in m]
    assert len(started) == 3
    # The application server comes after the database and the central services.
    assert started[-1].startswith("DEV/00: Started on sapapp1")


async def test_run_on_host(app, api, landscape):
    await create(api, LO_TEST | {"target": "HOST", "parameters": []})
    response = await api.post("/api/hosts/2/operations/lo_test", json={})
    result = await finished(app, api, response.json()["id"])
    assert result["status"] == "SUCCEEDED", log(result)
    run = next(iter(landscape.operation_runs.values()))
    assert run.host == "sapdb1" and run.arguments == []


async def test_cancel_custom_operation(app, api, landscape):
    await create(api)
    gate = hold_activities(app)
    body = {"parameters": {"RC": 0, "SLEEP": 30}}
    response = await api.post("/api/instances/2/operations/lo_test", json=body)
    activity_id = response.json()["id"]
    await asyncio.sleep(0.05)
    async with client(app, await make_user(app, "op", "operator")) as operator:
        cancelled = await operator.post(f"/api/activities/{activity_id}/cancel")
    gate.set()
    assert cancelled.status_code == 200, cancelled.text
    result = cancelled.json()
    assert result["status"] == "CANCELLED"
    assert any("Cancelled by op" in m for m in log(result))
    assert not landscape.operation_runs
    again = await api.post(f"/api/activities/{activity_id}/cancel")
    assert again.status_code == 409
    assert (await api.post("/api/activities/999/cancel")).status_code == 404


async def test_cancel_start_only_stops_waiting(app, api):
    gate = hold_activities(app)
    activity_id = (await api.post("/api/systems/1/start")).json()["id"]
    await asyncio.sleep(0.05)
    result = (await api.post(f"/api/activities/{activity_id}/cancel")).json()
    gate.set()
    assert result["status"] == "CANCELLED"
    assert any("keep running" in m or "already requested" in m for m in log(result)), log(result)
    async with client(app, await make_user(app, "view", "viewer")) as viewer:
        assert (await viewer.post(f"/api/activities/{activity_id}/cancel")).status_code == 403


async def test_cancel_mass_operation(app, api):
    await create(api)
    gate = hold_activities(app)
    body = {
        "operation": "lo_test",
        "target_type": "INSTANCE",
        "target_ids": [1, 2, 3],
        "parallel": 1,
        "parameters": {"RC": 0, "SLEEP": 30},
    }
    response = await api.post("/api/mass-operations", json=body)
    assert response.status_code == 202, response.text
    parent_id = response.json()["id"]
    await asyncio.sleep(0.05)
    result = (await api.post(f"/api/activities/{parent_id}/cancel")).json()
    gate.set()
    assert result["status"] == "CANCELLED"
    statuses = {child["status"] for child in result["children"]}
    assert "CANCELLED" in statuses and "SUCCEEDED" not in statuses

    wrong = body | {"parameters": {}}
    assert (await api.post("/api/mass-operations", json=wrong)).status_code == 422


# --- web ------------------------------------------------------------------------------------


async def test_custom_operations_in_the_browser(app, api, landscape):
    async with sign_in(app) as browser:
        tab = (await browser.get("/operations/custom")).text
        assert "No custom operations yet." in tab
        form = {
            "key": "lo_test",
            "name": "Test operation",
            "agent_operation": "LO_TEST",
            "target": "INSTANCE",
            "instance_type": ["abap"],
            "ordering": "parallel",
            "role": "operator",
            "mass": "true",
            "timeout_seconds": "60",
        }
        created = await browser.post("/operations/custom", data=form)
        page_url = created.headers["location"].split("?")[0]
        assert page_url == "/operations/custom/1"
        taken = await browser.post("/operations/custom", data=form)
        assert taken.headers["location"].endswith("error=operation_key_taken")

        parameter = {"name": "rc", "label": "Exit code", "type": "INTEGER", "default": "0"}
        await browser.post(f"{page_url}/parameters", data=parameter)
        await browser.post(f"{page_url}/parameters", data={"name": "A", "label": "Text"})
        bad = await browser.post(f"{page_url}/parameters", data={"name": "A", "label": "x"})
        assert bad.headers["location"].endswith("error=parameter_taken")
        detail = (await browser.get(page_url)).text
        assert "PARAM-RC" in detail and "SERVICE_SUBTYPE" in detail

        # Only the ABAP application server gets the operation in its menu.
        system = (await browser.get("/systems/2")).text
        assert 'href="/run/instances/2/lo_test"' in system
        assert 'href="/run/instances/1/lo_test"' not in (await browser.get("/systems/1")).text
        assert (await browser.get("/run/instances/1/lo_test")).status_code == 404

        run_form = (await browser.get("/run/instances/2/lo_test")).text
        assert 'name="param.RC" value="0"' in run_form and 'name="param.A" value=""' in run_form
        invalid = await browser.post("/run/instances/2/lo_test", data={"param.RC": "x"})
        assert "error=parameter_values" in invalid.headers["location"]
        started = await browser.post("/run/instances/2/lo_test", data={"param.RC": "0"})
        activity_id = int(started.headers["location"].rsplit("/", 1)[1])
        await app.state.runner.wait(activity_id)
        activity = (await browser.get(f"/api/activities/{activity_id}")).json()
        assert activity["options"]["custom"]["name"] == "Test operation"
        # The empty optional parameter A is not passed.
        assert activity["options"]["parameters"] == {"PARAM-RC": "0"}
        started_line = next(e for e in activity["entries"] if e["event"] == "op_started")
        assert started_line["subject"] == "DEV/00"

        data = (await browser.get("/api/landscape")).json()
        offered = {op["key"]: op for op in data["operations"]}
        assert offered["lo_test"]["instance_types"] == ["abap"]
        assert [p["name"] for p in offered["lo_test"]["parameters"]] == ["RC", "A"]

        await browser.post(f"{page_url}/parameters/1/delete")
        assert "PARAM-RC" not in (await browser.get(page_url)).text
        await browser.post(f"{page_url}/delete")
        assert app.state.catalog.get("lo_test") is None

    await make_user(app, "conf", "configurator")
    async with sign_in(app, "conf") as configurator:
        assert (await configurator.get("/operations/custom")).status_code == 403
        assert (await configurator.get("/operations/templates")).status_code == 403
        page = (await configurator.get("/configuration/hosts")).text
        assert 'href="/operations"' not in page


async def test_cancel_in_the_browser(app, api):
    gate = hold_activities(app)
    activity_id = (await api.post("/api/systems/1/start")).json()["id"]
    await asyncio.sleep(0.05)
    headers = {"X-LO-Request": "1"}
    async with sign_in(app) as browser:
        # The activity page cancels through the API with the session cookie.
        response = await browser.post(f"/api/activities/{activity_id}/cancel", headers=headers)
        gate.set()
        assert response.status_code == 200 and response.json()["status"] == "CANCELLED"
        again = await browser.post(f"/api/activities/{activity_id}/cancel", headers=headers)
        assert again.status_code == 409
