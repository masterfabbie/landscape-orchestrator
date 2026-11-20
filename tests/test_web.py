from __future__ import annotations

import asyncio
import re

import httpx
import pytest

from conftest import PASSWORD, make_user
from landscape_orchestrator.api import create_app
from landscape_orchestrator.crypto import generate_secret_key
from landscape_orchestrator.mock.transport import MockLandscapeTransport
from landscape_orchestrator.settings import Settings
from landscape_orchestrator.web.i18n import TEXTS, instance_type_key, pick_language, translate
from landscape_orchestrator.web.routes import SESSION_COOKIE

pytestmark = pytest.mark.anyio

CREDENTIALS = {"username": "devadm", "password": "mock"}
APP_HOST = {"name": "sapapp1", "address": "127.0.0.1"} | CREDENTIALS
DB_HOST = APP_HOST | {
    "name": "sapdb1",
    "hostagent_port": "11128",
    "sapcontrol_port_offset": "10000",
}


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        secret_key=generate_secret_key(),
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'web.db'}",
    )


@pytest.fixture
async def app(settings, landscape, clock):
    async def fake_sleep(seconds: float) -> None:
        clock.advance(seconds)
        await asyncio.sleep(0)

    application = create_app(
        settings,
        transport=MockLandscapeTransport(landscape),
        poll_interval=1.0,
        sleep=fake_sleep,
        clock=clock,
    )
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def anonymous(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await make_user(app)  # the admin "admin"; without any user, pages lead to /setup
        yield client


async def sign_in(client: httpx.AsyncClient, username: str = "admin") -> httpx.Response:
    response = await client.post("/login", data={"username": username, "password": PASSWORD})
    assert response.status_code == 303, response.text
    return response


@pytest.fixture
async def browser(anonymous):
    """A browser signed in as admin."""
    await sign_in(anonymous)
    return anonymous


async def add_hosts(browser: httpx.AsyncClient, systems: bool = True) -> None:
    """Hosts 1 (sapapp1) and 2 (sapdb1) with instances 1 (DEV/01), 2 (DEV/00), 3 (HDV/02).

    With ``systems``, also systems 1 (DEV: instances 1, 2) and 2 (HDV: instance 3).
    """
    for number, body in enumerate((APP_HOST, DB_HOST), start=1):
        assert (await browser.post("/configuration/hosts", data=body)).status_code == 303
        response = await browser.post(f"/configuration/hosts/{number}/discover")
        assert response.status_code == 303
    if systems:
        for name, ids in (("DEV", ["1", "2"]), ("HDV", ["3"])):
            data = {"name": name, "instance_id": ids}
            response = await browser.post("/configuration/systems", data=data)
            assert "notice=system_created" in response.headers["location"]


# --- sessions and translations -----------------------------------------------------------


def test_every_text_has_both_languages():
    for key, (english, german) in TEXTS.items():
        assert english and german, key
        assert re.findall(r"{(\w+)}", english) == re.findall(r"{(\w+)}", german), key


def test_translate_and_language_choice():
    assert translate("de", "nav.overview") == "Übersicht"
    assert translate("en", "nav.overview") == "Overview"
    assert translate("de", "no.such.key") == "no.such.key"
    assert translate("en", "error.discovery_failed", detail="x").endswith(": x")
    assert pick_language("de", "en-US") == "de"
    assert pick_language(None, "de-DE,de;q=0.9,en;q=0.8") == "de"
    assert pick_language(None, "fr-FR") == "en"
    assert pick_language("xx", None) == "en"


def test_instance_type_from_features():
    assert instance_type_key("HDB|HDB_WORKER") == "itype.hana"
    assert instance_type_key("MESSAGESERVER|ENQUE") == "itype.scs"
    assert instance_type_key("ABAP|GATEWAY|ICMAN|IGS") == "itype.abap"
    assert instance_type_key("ABAP|J2EE|GATEWAY") == "itype.dual"
    assert instance_type_key(None) is None


# --- sign in -----------------------------------------------------------------------------

PAGES = [
    "/",
    "/landscape",
    "/landscape?tab=hosts",
    "/activities",
    "/activities?tab=logs",
    "/configuration/hosts",
    "/configuration/hosts/new",
    "/configuration/systems",
    "/configuration/systems/unassigned",
    "/configuration/systems/new",
    "/configuration/pools",
    "/configuration/pools/new",
    "/operations/custom",
    "/operations/custom/new",
    "/operations/templates",
    "/administration",
    "/hosts/1",
    "/systems/1",
    "/configuration/hosts/1",
    "/configuration/systems/1",
]


@pytest.mark.parametrize("path", PAGES)
async def test_pages_require_login(anonymous, path):
    response = await anonymous.get(path)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


async def test_posts_require_login(anonymous):
    response = await anonymous.post("/configuration/hosts", data=APP_HOST)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    response = await anonymous.post("/instances/1/stop")
    assert response.headers["location"] == "/login"


async def test_login_with_wrong_password(anonymous):
    response = await anonymous.post("/login", data={"username": "admin", "password": "wrong"})
    assert response.status_code == 401
    assert "Sign-in failed." in response.text
    assert SESSION_COOKIE not in response.cookies
    unknown = await anonymous.post("/login", data={"username": "nobody", "password": PASSWORD})
    assert unknown.status_code == 401


async def test_login_sets_protected_cookie_and_returns_to_page(anonymous):
    redirect = await anonymous.get("/activities")
    assert redirect.headers["location"] == "/login?next=%2Factivities"

    response = await anonymous.post(
        "/login", data={"username": "admin", "password": PASSWORD, "next": "/activities"}
    )
    assert response.headers["location"] == "/activities"
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert (await anonymous.get("/activities")).status_code == 200


@pytest.mark.parametrize("target", ["https://evil.example/", "//evil.example", "/\\evil"])
async def test_login_does_not_redirect_to_other_sites(anonymous, target):
    data = {"username": "admin", "password": PASSWORD, "next": target}
    response = await anonymous.post("/login", data=data)
    assert response.headers["location"] == "/"


async def test_logout(browser):
    assert (await browser.post("/logout")).headers["location"] == "/login"
    assert (await browser.get("/")).status_code == 303


async def test_cross_site_post_is_rejected(browser):
    response = await browser.post(
        "/configuration/hosts", data=APP_HOST, headers={"Origin": "https://evil.example"}
    )
    assert response.status_code == 403
    same_site = await browser.post(
        "/configuration/hosts", data=APP_HOST, headers={"Origin": "http://test"}
    )
    assert same_site.status_code == 303


async def test_api_token_is_not_accepted_as_session_on_pages(anonymous, app):
    token = await make_user(app, "script", "viewer")
    response = await anonymous.get("/", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 303


# --- structure and language --------------------------------------------------------------


@pytest.mark.parametrize("path", PAGES[:9])
async def test_main_pages_render_with_navigation(browser, path):
    await add_hosts(browser)
    page = (await browser.get(path)).text
    for item in ("Overview", "Landscape", "Activities", "Configuration", "Administration"):
        assert f">{item}</a>" in page


async def test_detail_pages_render(browser):
    await add_hosts(browser)
    for path in PAGES[9:]:
        assert (await browser.get(path)).status_code == 200, path


async def test_navigation_names_in_german(browser):
    await browser.get("/language/de")
    page = (await browser.get("/")).text
    for item in ("Übersicht", "Landschaft", "Aktivitäten", "Konfiguration", "Operationen"):
        assert f">{item}</a>" in page
    assert ">Administration</a>" in page
    await browser.get("/language/en")
    page = (await browser.get("/")).text
    for item in ("Overview", "Landscape", "Activities", "Configuration", "Operations"):
        assert f">{item}</a>" in page


async def test_language_switch_keeps_the_tab(browser):
    page = (await browser.get("/activities?tab=logs&notice=pool_created")).text
    assert 'href="/language/de?next=/activities%3Ftab%3Dlogs"' in page
    response = await browser.get("/language/de?next=/activities?tab=logs")
    assert response.headers["location"] == "/activities?tab=logs"
    assert "Pool anlegen" in (await browser.get("/configuration/pools")).text

    await browser.get("/language/en")
    assert "Create pool" in (await browser.get("/configuration/pools")).text
    unknown = await browser.get("/language/xx")
    assert unknown.status_code == 303
    assert "Create pool" in (await browser.get("/configuration/pools")).text


async def test_areas_have_a_navigation_on_the_left(browser):
    hosts = (await browser.get("/configuration/hosts")).text
    assert 'class="section-nav"' in hosts
    assert '<a href="/configuration/hosts" aria-current="page">Hosts</a>' in hosts
    assert '<a href="/configuration/systems" >Systems</a>' in hosts
    assert 'class="button-primary" href="/configuration/hosts/new">Create host</a>' in hosts
    assert 'aria-current="page">Overview</a>' in hosts
    # Detail pages keep the sub-area marked.
    new = (await browser.get("/configuration/hosts/new")).text
    assert '<a href="/configuration/hosts" aria-current="page">Hosts</a>' in new
    operations = (await browser.get("/operations/custom")).text
    assert '<a href="/operations/custom" aria-current="page">Custom operations</a>' in operations
    assert '<a href="/operations/templates" >Operation templates</a>' in operations
    assert 'href="/operations/custom/new">Create operation</a>' in operations
    templates = (await browser.get("/operations/templates")).text
    assert '<a href="/operations/templates" aria-current="page">' in templates
    # Pages outside the areas have no such navigation.
    assert "section-nav" not in (await browser.get("/activities")).text


async def test_old_configuration_links_redirect(browser):
    for old, new in (
        ("/configuration", "/configuration/hosts"),
        ("/configuration?tab=systems", "/configuration/systems"),
        ("/configuration?tab=pools", "/configuration/pools"),
        ("/configuration?tab=operations", "/operations/custom"),
        ("/operations", "/operations/custom"),
    ):
        assert (await browser.get(old)).headers["location"] == new


async def test_accept_language_header_selects_german(anonymous):
    page = await anonymous.get("/login", headers={"Accept-Language": "de-DE,de;q=0.9"})
    assert "Anmelden" in page.text


async def test_old_hosts_page_redirects_to_landscape(browser):
    response = await browser.get("/hosts")
    assert response.headers["location"] == "/landscape?tab=hosts"


async def landscape_data(browser) -> dict:
    """What the landscape page shows; it loads this from the API with the session cookie."""
    response = await browser.get("/api/landscape")
    assert response.status_code == 200
    return response.json()


async def activity_data(browser, activity_id: int) -> dict:
    """What the activity page shows; it loads this from the API."""
    response = await browser.get(f"/api/activities/{activity_id}")
    assert response.status_code == 200
    return response.json()


def system_names(data: dict) -> list[str]:
    return [system["name"] for system in data["systems"]]


async def test_landscape_page_loads_the_interface(browser):
    page = await browser.get("/landscape?tab=hosts")
    assert page.status_code == 200
    assert '<div id="lo-app">' in page.text
    assert 'src="/static/app/landscape.js?v=' in page.text
    assert 'href="/static/app/app.css?v=' in page.text
    # Start data for the app: texts and user, as JSON that is read, not run.
    assert '<script type="application/json" id="lo-boot">' in page.text
    assert '"nav.landscape": "Landscape"' in page.text and '"role": "admin"' in page.text
    script = await browser.get("/static/app/landscape.js")
    assert script.status_code == 200 and "lo-app" in script.text


# --- overview ----------------------------------------------------------------------------


async def test_empty_overview(browser):
    page = (await browser.get("/")).text
    assert page.count('class="tile-total">0<') == 4
    assert "Nothing yet." in page


async def test_overview_counts(browser, app):
    await add_hosts(browser)
    page = (await browser.get("/")).text
    assert '<p class="tile-total">2</p>' in page  # systems and hosts
    assert '<p class="tile-total">3</p>' in page  # instances
    assert "stopped" in page and "reachable" in page

    await browser.post("/systems/1/start")
    await app.state.runner.wait(1)
    page = (await browser.get("/")).text
    assert "running" in page and "succeeded" in page
    assert '<p class="tile-total">1</p>' in page  # activities in the last 24 hours


# --- configuration: hosts ----------------------------------------------------------------


async def test_add_host_and_discover(browser):
    created = await browser.post("/configuration/hosts", data=APP_HOST)
    assert created.headers["location"] == "/configuration/hosts/1?notice=host_created"
    assert "Host created." in (await browser.get(created.headers["location"])).text

    discovered = await browser.post("/configuration/hosts/1/discover")
    location = discovered.headers["location"]
    assert location.startswith("/configuration/hosts/1?notice=discovered&n_added=2")
    page = await browser.get(location)
    assert "Discovery finished: 2 new, 0 updated, 0 missing." in page.text
    assert "ABAP application server" in page.text and "Central services" in page.text

    listing = (await browser.get("/configuration/hosts")).text
    assert "sapapp1" in listing and "devadm (stored)" in listing
    assert [h["name"] for h in (await landscape_data(browser))["hosts"]] == ["sapapp1"]


async def test_invalid_host_input(browser):
    for body, error in [
        (APP_HOST | {"hostagent_port": "99999"}, "invalid_input"),
        (APP_HOST | {"password": ""}, "credentials_incomplete"),
        ({"name": "  "}, "invalid_input"),
    ]:
        response = await browser.post("/configuration/hosts", data=body)
        assert response.headers["location"] == f"/configuration/hosts/new?error={error}"
    await browser.post("/configuration/hosts", data=APP_HOST)
    duplicate = await browser.post("/configuration/hosts", data=APP_HOST)
    assert duplicate.headers["location"] == "/configuration/hosts/new?error=host_exists"
    assert "already exists" in (await browser.get(duplicate.headers["location"])).text


async def test_discovery_failure_is_shown(browser):
    await browser.post("/configuration/hosts", data=APP_HOST | {"hostagent_port": "4711"})
    response = await browser.post("/configuration/hosts/1/discover")
    page = await browser.get(response.headers["location"])
    assert "The host agent could not be queried" in page.text
    assert "Connection refused" in page.text
    assert "badge-red" in page.text


async def test_discovery_warning_is_shown(browser, app):
    await browser.post("/configuration/hosts", data=APP_HOST)
    app.state.context.clients._transport.remove_port(50013)
    response = await browser.post("/configuration/hosts/1/discover")
    page = await browser.get(response.headers["location"])
    assert "Warnings" in page.text
    assert "DEV/00: sapstartsrv could not be queried" in page.text


async def test_host_values_are_escaped(browser):
    await browser.post("/configuration/hosts", data={"name": "<script>alert(1)</script>"})
    page = (await browser.get("/configuration/hosts")).text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


async def test_credentials_forms(browser):
    await browser.post("/configuration/hosts", data={"name": "sapapp1", "address": "127.0.0.1"})
    assert "not set" in (await browser.get("/configuration/hosts/1")).text

    saved = await browser.post("/configuration/hosts/1/credentials", data=CREDENTIALS)
    assert saved.headers["location"] == "/configuration/hosts/1?notice=credentials_saved"
    page = (await browser.get("/configuration/hosts/1")).text
    assert "stored" in page and "devadm" in page and "mock" not in page

    incomplete = await browser.post("/configuration/hosts/1/credentials", data={"username": "x"})
    assert incomplete.headers["location"] == "/configuration/hosts/1?error=credentials_incomplete"

    await browser.post("/configuration/hosts/1/discover")
    await browser.post("/configuration/systems", data={"name": "DEV", "instance_id": ["1"]})
    page = (await browser.get("/configuration/systems/1")).text
    assert "Credentials per SID" in page
    assert 'action="/configuration/systems/1/sids/1/credentials"' in page
    saved = await browser.post("/configuration/systems/1/sids/1/credentials", data=CREDENTIALS)
    assert saved.headers["location"] == "/configuration/systems/1?notice=credentials_saved"
    assert "stored" in (await browser.get("/configuration/systems/1")).text
    other = await browser.post("/configuration/systems/1/sids/99/credentials", data=CREDENTIALS)
    assert other.status_code == 404


# --- systems as groups of instances --------------------------------------------------------


async def test_discovered_instances_are_unassigned(browser):
    await add_hosts(browser, systems=False)
    page = (await browser.get("/configuration/systems/unassigned")).text
    assert "Unassigned instances (3)" in page
    for number in ("1", "2", "3"):
        assert f'name="instance_id" value="{number}"' in page
    assert 'formaction="/configuration/systems/new"' in page
    assert "Assign to existing" not in page  # there is no system yet
    assert "No systems yet." in (await browser.get("/configuration/systems")).text
    # Unassigned instances are not part of the landscape.
    assert (await landscape_data(browser))["systems"] == []
    host = (await browser.get("/hosts/1")).text
    assert "not assigned" in host and 'action="/instances/1/start"' not in host


async def test_create_system_suggests_a_name(browser):
    await add_hosts(browser, systems=False)
    page = await browser.get("/configuration/systems/new?instance_id=3&instance_id=2")
    assert page.status_code == 200
    # The SID of the application instance, not that of the HANA database.
    assert 'name="name" required maxlength="64" value="DEV"' in page.text
    # All unassigned instances can be chosen; the selected ones are preselected.
    assert 'name="instance_id" value="1" aria-label="DEV/01" >' in page.text
    assert 'name="instance_id" value="2" aria-label="DEV/00" checked>' in page.text
    assert 'name="instance_id" value="3" aria-label="HDV/02" checked>' in page.text

    hana_only = (await browser.get("/configuration/systems/new?instance_id=3")).text
    assert 'value="HDV"' in hana_only

    nothing = (await browser.get("/configuration/systems/new")).text
    assert 'name="name" required maxlength="64" value=""' in nothing
    assert 'value="1" aria-label="DEV/01" >' in nothing


async def test_create_system_with_pool_and_errors(browser):
    await add_hosts(browser, systems=False)
    await browser.post("/configuration/pools", data={"name": "xPool"})
    created = await browser.post(
        "/configuration/systems",
        data={"name": " XDV ", "instance_id": ["2", "3"], "pool_id": "1", "description": "d"},
    )
    assert created.headers["location"] == (
        "/configuration/systems/1?notice=system_created&n_name=XDV"
    )
    assert "System XDV created." in (await browser.get(created.headers["location"])).text
    page = (await browser.get("/configuration/systems")).text
    assert "<details" in page and "xPool" in page
    # Derived dependency: the DEV instance starts after the HANA instance of HDV.
    assert re.search(r"DEV/00</td>.*?<td class=\"mono\">HDV</td>", page, re.S)

    def location(response: httpx.Response) -> str:
        return response.headers["location"]

    taken = await browser.post("/configuration/systems", data={"name": "xdv", "instance_id": "1"})
    assert location(taken) == "/configuration/systems/new?error=system_name_taken"
    assigned = await browser.post("/configuration/systems", data={"name": "X", "instance_id": "2"})
    assert location(assigned) == "/configuration/systems/new?error=system_assigned"
    empty = await browser.post("/configuration/systems", data={"name": "X"})
    assert location(empty) == "/configuration/systems/new?error=system_no_instances"
    nameless = await browser.post("/configuration/systems", data={"name": " ", "instance_id": "1"})
    assert location(nameless) == "/configuration/systems/new?error=system_name_missing"


UNASSIGNED = "/configuration/systems/unassigned"


async def test_suggestion_and_assign_to_existing(browser):
    await add_hosts(browser, systems=False)
    await browser.post("/configuration/systems", data={"name": "DEV", "instance_id": "2"})
    page = (await browser.get("/configuration/systems/unassigned")).text
    # DEV/01 has the SID of system DEV: suggested, but not assigned yet.
    assert 'formaction="/configuration/systems/1/assign/1"' in page
    assert "Assign to DEV" in page
    assert "/configuration/systems/1/assign/3" not in page  # HDV has no system yet

    accepted = await browser.post("/configuration/systems/1/assign/1")
    assert accepted.headers["location"] == f"{UNASSIGNED}?notice=instances_assigned"
    again = await browser.post("/configuration/systems/1/assign/1")
    assert again.headers["location"] == f"{UNASSIGNED}?error=system_assigned"

    assigned = await browser.post(
        "/configuration/systems/assign", data={"system_id": "1", "instance_id": ["3"]}
    )
    assert assigned.headers["location"] == f"{UNASSIGNED}?notice=instances_assigned"
    page = (await browser.get("/configuration/systems/unassigned")).text
    assert "All known instances belong to a system." in page
    missing = await browser.post("/configuration/systems/assign", data={"instance_id": ["3"]})
    assert missing.headers["location"] == f"{UNASSIGNED}?error=system_unknown_system"


async def test_rename_remove_instance_and_delete_system(browser):
    await add_hosts(browser)
    renamed = await browser.post("/configuration/systems/1", data={"name": "Development"})
    assert renamed.headers["location"] == "/configuration/systems/1?notice=system_saved"
    assert "Development" in system_names(await landscape_data(browser))
    taken = await browser.post("/configuration/systems/1", data={"name": "HDV"})
    assert taken.headers["location"] == "/configuration/systems/1?error=system_name_taken"

    removed = await browser.post(
        "/configuration/systems/1/instances/2/remove", data={"back": "system"}
    )
    assert removed.headers["location"] == "/configuration/systems/1?notice=instance_removed"
    page = (await browser.get("/configuration/systems/unassigned")).text
    assert 'name="instance_id" value="2"' in page

    deleted = await browser.post("/configuration/systems/1/delete")
    assert deleted.headers["location"] == "/configuration/systems?notice=system_deleted"
    page = (await browser.get("/configuration/systems/unassigned")).text
    assert 'name="instance_id" value="1"' in page
    assert (await browser.get("/systems/1")).status_code == 404


async def test_unassigned_instance_cannot_be_started(browser):
    await add_hosts(browser)
    await browser.post("/configuration/systems/1/instances/2/remove")
    response = await browser.post("/instances/2/start")
    assert response.headers["location"] == "/hosts/1?error=not_assigned"


async def test_delete_host(browser):
    await add_hosts(browser)
    response = await browser.post("/configuration/hosts/1/delete")
    assert response.headers["location"] == "/configuration/hosts?notice=host_deleted"
    page = (await browser.get("/configuration/hosts")).text
    assert "sapapp1" not in page and "sapdb1" in page


# --- configuration: pools and dependencies ---------------------------------------------


async def test_pools(browser):
    await add_hosts(browser)
    assert "No pools yet." in (await browser.get("/configuration/pools")).text

    created = await browser.post(
        "/configuration/pools", data={"name": "xDV", "description": "Development line"}
    )
    assert created.headers["location"] == "/configuration/pools?notice=pool_created"
    duplicate = await browser.post("/configuration/pools", data={"name": "xDV"})
    assert duplicate.headers["location"] == "/configuration/pools/new?error=pool_exists"
    assert (await browser.post("/configuration/pools", data={"name": " "})).headers[
        "location"
    ] == "/configuration/pools/new?error=invalid_input"

    saved = await browser.post("/configuration/systems/1/pool", data={"pool_id": "1"})
    assert saved.headers["location"] == "/configuration/systems/1?notice=pool_saved"
    pools = (await browser.get("/configuration/pools")).text
    assert "Development line" in pools and 'href="/configuration/systems/1">DEV</a>' in pools

    data = await landscape_data(browser)
    assert data["pools"] == [{"id": 1, "name": "xDV"}]
    pools = {s["name"]: s["pool"] for s in data["systems"]}
    assert pools == {"DEV": {"id": 1, "name": "xDV"}, "HDV": None}
    assert "xDV" in (await browser.get("/systems/1")).text

    cleared = await browser.post("/configuration/systems/1/pool", data={"pool_id": ""})
    assert cleared.headers["location"] == "/configuration/systems/1?notice=pool_saved"
    unknown = await browser.post("/configuration/systems/1/pool", data={"pool_id": "99"})
    assert unknown.headers["location"] == "/configuration/systems/1?error=invalid_input"

    await browser.post("/configuration/systems/1/pool", data={"pool_id": "1"})
    deleted = await browser.post("/configuration/pools/1/delete")
    assert deleted.headers["location"] == "/configuration/pools?notice=pool_deleted"
    assert "No pool" in (await browser.get("/systems/1")).text


async def test_manage_dependencies(browser):
    await add_hosts(browser)
    page = (await browser.get("/configuration/systems/1")).text
    assert "Dependencies" in page and '<option value="2">HDV</option>' in page

    added = await browser.post("/configuration/systems/1/dependencies", data={"depends_on": "2"})
    assert added.headers["location"] == "/configuration/systems/1?notice=dependencies_saved"
    page = (await browser.get("/configuration/systems/1")).text
    assert 'action="/configuration/systems/1/dependencies/2/delete"' in page
    assert "No other system can be added." in page
    data = await landscape_data(browser)
    assert {s["name"]: s["depends_on"] for s in data["systems"]} == {"DEV": ["HDV"], "HDV": []}

    hana = (await browser.get("/configuration/systems/2")).text
    assert 'href="/configuration/systems/1">DEV</a>' in hana
    assert "Stopping this system does not stop the systems that need it." in hana
    assert '<option value="1">' not in hana  # DEV would close a cycle

    cycle = await browser.post("/configuration/systems/2/dependencies", data={"depends_on": "1"})
    assert cycle.headers["location"] == "/configuration/systems/2?error=dependency_cycle"
    assert "would create a cycle" in (await browser.get(cycle.headers["location"])).text
    itself = await browser.post("/configuration/systems/1/dependencies", data={"depends_on": "1"})
    assert itself.headers["location"] == "/configuration/systems/1?error=dependency_self"

    removed = await browser.post("/configuration/systems/1/dependencies/2/delete")
    assert removed.headers["location"] == "/configuration/systems/1?notice=dependencies_saved"


async def test_configuration_texts_in_german(browser):
    await add_hosts(browser)
    await browser.get("/language/de")
    page = (await browser.get("/configuration/systems/1")).text
    assert "Abhängigkeiten" in page and "Abhängigkeit hinzufügen" in page and "Kein Pool" in page


# --- landscape and operations ----------------------------------------------------------


async def test_landscape_lists_systems_with_status(browser):
    await add_hosts(browser)
    data = await landscape_data(browser)
    assert {s["name"]: s["status"] for s in data["systems"]} == {"DEV": "GRAY", "HDV": "GRAY"}
    assert "start" in {op["key"] for op in data["operations"]}
    assert [h["name"] for h in data["hosts"]] == ["sapapp1", "sapdb1"]


async def test_detail_pages_link_to_configuration(browser):
    await add_hosts(browser)
    host = (await browser.get("/hosts/2")).text
    assert 'href="/configuration/hosts/2"' in host
    assert "SYSTEMDB@HDV" in host and "HDV@HDV" in host
    system = (await browser.get("/systems/1")).text
    assert 'href="/configuration/systems/1"' in system
    assert 'action="/instances/1/stop"' in system


async def test_start_instance_from_the_browser(browser, app):
    await add_hosts(browser)
    response = await browser.post("/instances/1/start")
    assert response.headers["location"] == "/activities/1"

    page = (await browser.get("/activities/1")).text
    # The page follows the activity itself (React, /api/activities/1).
    assert 'src="/static/app/activity.js?v=' in page and '"activity_id": 1' in page

    await app.state.runner.wait(1)
    finished = await activity_data(browser, 1)
    assert finished["status"] == "SUCCEEDED"
    messages = [e["message"] for e in finished["entries"]]
    assert "Instance status: GREEN" in messages and "Finished successfully" in messages
    status = next(e for e in finished["entries"] if e["message"] == "Instance status: GREEN")
    assert status["event"] == "status" and status["data"] == {"DEV/01": "GREEN"}

    listing = (await browser.get("/activities")).text
    assert 'href="/activities/1"' in listing and "DEV/01" in listing
    logs = (await browser.get("/activities?tab=logs")).text
    assert "Instance status: GREEN" in logs and 'href="/activities/1"' in logs


def hold_activities(app) -> asyncio.Event:
    """Keep running activities at their first status poll until the event is set."""
    runner = app.state.runner
    gate = asyncio.Event()
    original = runner._sleep

    async def gated(seconds: float) -> None:
        await gate.wait()
        await original(seconds)

    runner._sleep = gated
    return gate


async def test_system_operation_with_dependency_and_conflict(browser, app):
    await add_hosts(browser)
    # System 1 is DEV, system 2 the HANA system HDV; instance 3 is the HANA instance.
    await browser.post("/configuration/systems/1/dependencies", data={"depends_on": "2"})
    gate = hold_activities(app)

    started = await browser.post("/systems/1/start")
    assert started.headers["location"] == "/activities/1"

    running = {s["name"]: s["activity"] for s in (await landscape_data(browser))["systems"]}
    # DEV and, through the dependency, HDV
    assert running["DEV"]["id"] == 1 and running["HDV"]["id"] == 1

    conflict = await browser.post("/instances/3/stop")
    assert conflict.headers["location"] == "/systems/2?error=conflict"
    page = await browser.get(conflict.headers["location"])
    assert "Another activity is still running" in page.text

    gate.set()
    await app.state.runner.wait(1)
    data = await landscape_data(browser)
    assert [s["activity"] for s in data["systems"]] == [None, None]
    assert [s["status"] for s in data["systems"]] == ["GREEN", "GREEN"]
    assert "badge-running" in (await browser.get("/hosts/2")).text  # databases follow
    messages = [e["message"] for e in (await activity_data(browser, 1))["entries"]]
    assert "Start order including dependencies: HDV, DEV" in messages


async def test_failed_activity_shows_error_in_german(browser, app):
    await add_hosts(browser)
    await browser.post(
        "/configuration/hosts/1/credentials", data={"username": "devadm", "password": "wrong"}
    )
    await browser.get("/language/de")
    await browser.post("/instances/1/start")
    await app.state.runner.wait(1)
    page = (await browser.get("/activities/1")).text
    # German texts for the page come with it; the error itself stays English.
    assert '"activity.FAILED": "fehlgeschlagen"' in page
    assert "Protokollmeldungen werden auf Englisch aufgezeichnet." in page
    activity = await activity_data(browser, 1)
    assert activity["status"] == "FAILED" and "Invalid Credentials" in activity["error"]


async def test_administration_shows_settings(browser):
    page = (await browser.get("/administration")).text
    assert "0.1.0" in page and "sqlite+aiosqlite" in page and "8 hours" in page


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("GET", "/hosts/99", 404),
        ("GET", "/systems/99", 404),
        ("GET", "/activities/99", 404),
        ("GET", "/configuration/hosts/99", 404),
        ("GET", "/configuration/systems/99", 404),
        ("POST", "/instances/99/start", 404),
        ("POST", "/systems/99/stop", 404),
        ("POST", "/configuration/pools/99/delete", 404),
        ("POST", "/instances/1/explode", 422),
    ],
)
async def test_unknown_targets(browser, method, path, expected):
    assert (await browser.request(method, path)).status_code == expected


async def test_static_stylesheet_is_served(anonymous):
    response = await anonymous.get("/static/style.css")
    assert response.status_code == 200
    assert "text/css" in response.headers["content-type"]


async def test_change_host_connection(browser):
    await browser.post("/configuration/hosts", data=APP_HOST | {"hostagent_port": "4711"})
    page = (await browser.get("/configuration/hosts/1")).text
    assert 'action="/configuration/hosts/1/connection"' in page and 'value="4711"' in page

    data = {"name": "app1", "address": "127.0.0.1", "hostagent_port": "", "verify_tls": "on"}
    saved = await browser.post("/configuration/hosts/1/connection", data=data)
    assert saved.headers["location"] == "/configuration/hosts/1?notice=connection_saved"
    page = (await browser.get(saved.headers["location"])).text
    assert "Connection saved. Run a discovery to check it." in page
    assert "Host app1" in page
    discovered = await browser.post("/configuration/hosts/1/discover")
    assert "notice=discovered" in discovered.headers["location"]

    await browser.post("/configuration/hosts", data={"name": "other"})
    taken = await browser.post("/configuration/hosts/1/connection", data={"name": "other"})
    assert taken.headers["location"] == "/configuration/hosts/1?error=host_exists"
    bad = await browser.post(
        "/configuration/hosts/1/connection", data={"name": "app1", "hostagent_port": "99999"}
    )
    assert bad.headers["location"] == "/configuration/hosts/1?error=invalid_input"



# --- diagnostics agents, duplicate instance numbers, host agent version -----------------

APP2_HOST = APP_HOST | {
    "name": "sapapp2",
    "hostagent_port": "6128",
    "sapcontrol_port_offset": "5000",
}


@pytest.fixture
async def extended_browser(settings, extended_landscape, clock):
    """Signed in, hosts sapapp1, sapdb1, sapapp2 discovered, one system DEV with all six
    instances (including the diagnostics agent DAA/98 and DEV/00 on two hosts)."""

    async def fake_sleep(seconds: float) -> None:
        clock.advance(seconds)
        await asyncio.sleep(0)

    application = create_app(
        settings,
        transport=MockLandscapeTransport(extended_landscape),
        poll_interval=1.0,
        sleep=fake_sleep,
        clock=clock,
    )
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://test"
        ) as client:
            await make_user(application)
            await sign_in(client)
            for number, body in enumerate((APP_HOST, DB_HOST, APP2_HOST), start=1):
                await client.post("/configuration/hosts", data=body)
                await client.post(f"/configuration/hosts/{number}/discover")
            data = {"name": "DEV", "instance_id": [str(i) for i in range(1, 6)]}
            response = await client.post("/configuration/systems", data=data)
            assert "system_created" in response.headers["location"]
            yield client


async def test_diagnostics_agents_are_grouped(extended_browser):
    page = (await extended_browser.get("/configuration/systems")).text
    assert 'class="agent-group"' in page and "Diagnostics agents (1)" in page
    assert "Diagnostics agent</td>" in page
    # Central services need nothing, application servers need database and central services.
    assert re.search(r"DEV/01</td>.*?<td class=\"mono\"></td>", page, re.S)
    assert re.search(r"DEV/00 \(sapapp1\)</td>.*?<td class=\"mono\">HDV</td>", page, re.S)

    system = (await extended_browser.get("/systems/1")).text
    assert "Diagnostics agents (1)" in system
    assert 'action="/instances/3/start"' in system  # the agent keeps its own operations
    data = await landscape_data(extended_browser)
    instances = {i["label"]: i for s in data["systems"] for i in s["instances"]}
    assert instances["DAA/98"]["agent"] and instances["DAA/98"]["type"] == "agent"
    assert {"DEV/00 (sapapp1)", "DEV/00 (sapapp2)"} <= set(instances)
    # Agents come last in the start order of the landscape.
    assert data["systems"][0]["instances"][-1]["label"] == "DAA/98"


async def test_host_agent_version_is_shown(extended_browser):
    hosts = (await landscape_data(extended_browser))["hosts"]
    assert [h["hostagent_version"] for h in hosts] == ["7.22 PL72"] * 3
    assert "7.22 PL72" in (await extended_browser.get("/hosts/1")).text
    assert "7.22 PL72" in (await extended_browser.get("/configuration/hosts/1")).text


# --- filters, tags, status refresh -----------------------------------------------------


async def test_tags_forms(browser):
    await add_hosts(browser)
    saved = await browser.post("/configuration/systems/1/tags", data={"tags": "dev, SLA=1"})
    assert saved.headers["location"] == "/configuration/systems/1?notice=tags_saved"
    page = (await browser.get("/configuration/systems/1")).text
    assert 'value="dev, sla=1"' in page
    assert '<span class="tag">sla=1</span>' in (await browser.get("/systems/1")).text

    invalid = await browser.post("/configuration/systems/1/tags", data={"tags": "bad tag"})
    assert invalid.headers["location"] == "/configuration/systems/1?error=tag_invalid&e_tag=bad+tag"
    assert "Invalid tag: bad tag" in (await browser.get(invalid.headers["location"])).text
    twice = await browser.post("/configuration/hosts/1/tags", data={"tags": "a=1, a=2"})
    assert twice.headers["location"].endswith("error=tag_duplicate&e_tag=a")

    host = await browser.post("/configuration/hosts/2/tags", data={"tags": "db"})
    assert host.headers["location"] == "/configuration/hosts/2?notice=tags_saved"
    tags = {h["name"]: h["tags"] for h in (await landscape_data(browser))["hosts"]}
    assert tags["sapdb1"] == ["db"]


async def test_refresh_button(browser, landscape):
    await add_hosts(browser)
    assert 'action="/systems/1/refresh"' in (await browser.get("/systems/1")).text
    landscape.start_instance(landscape.host("sapapp1").instance(0))

    response = await browser.post("/systems/1/refresh")
    assert response.headers["location"] == "/systems/1?notice=status_refreshed"
    page = (await browser.get(response.headers["location"])).text
    assert "Status refreshed." in page and "in transition" in page

    back = await browser.post("/systems/1/refresh", data={"back": "landscape"})
    assert back.headers["location"] == "/landscape?notice=status_refreshed"
    assert (await browser.post("/systems/99/refresh")).status_code == 404



async def test_ui_script_is_served(anonymous):
    response = await anonymous.get("/static/ui.js")
    assert response.status_code == 200 and "data-confirm" in response.text
