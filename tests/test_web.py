from __future__ import annotations

import asyncio
import re

import httpx
import pytest

from landscape_orchestrator.api import create_app
from landscape_orchestrator.crypto import generate_api_token, generate_secret_key
from landscape_orchestrator.mock.transport import MockLandscapeTransport
from landscape_orchestrator.settings import Settings
from landscape_orchestrator.web.i18n import TEXTS, instance_type_key, pick_language, translate
from landscape_orchestrator.web.sessions import (
    SESSION_COOKIE,
    SESSION_LIFETIME_SECONDS,
    create_session,
    is_valid_session,
)

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
        api_token=generate_api_token(),
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
        yield client


@pytest.fixture
async def browser(anonymous, settings):
    """A signed-in browser session."""
    response = await anonymous.post("/login", data={"token": settings.api_token})
    assert response.status_code == 303
    return anonymous


async def add_hosts(browser: httpx.AsyncClient) -> None:
    for number, body in enumerate((APP_HOST, DB_HOST), start=1):
        assert (await browser.post("/hosts", data=body)).status_code == 303
        assert (await browser.post(f"/hosts/{number}/discover")).status_code == 303


# --- sessions and translations -----------------------------------------------------------


def test_session_cookie_validation():
    value = create_session("token-a", now=1000)
    assert is_valid_session("token-a", value, now=1000)
    assert is_valid_session("token-a", value, now=1000 + SESSION_LIFETIME_SECONDS - 1)
    assert not is_valid_session("token-a", value, now=1000 + SESSION_LIFETIME_SECONDS + 1)
    assert not is_valid_session("token-b", value, now=1000)
    assert not is_valid_session("token-a", None)
    assert not is_valid_session("token-a", "garbage")
    expires, signature = value.split(".")
    assert not is_valid_session("token-a", f"{int(expires) + 1000}.{signature}", now=1000)


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


@pytest.mark.parametrize("path", ["/", "/hosts", "/hosts/1", "/systems/1", "/activities"])
async def test_pages_require_login(anonymous, path):
    response = await anonymous.get(path)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


async def test_posts_require_login(anonymous):
    response = await anonymous.post("/hosts", data=APP_HOST)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    response = await anonymous.post("/instances/1/stop")
    assert response.headers["location"] == "/login"


async def test_login_with_wrong_token(anonymous):
    response = await anonymous.post("/login", data={"token": "wrong"})
    assert response.status_code == 401
    assert "The token is not valid." in response.text
    assert SESSION_COOKIE not in response.cookies


async def test_login_sets_protected_cookie_and_returns_to_page(anonymous, settings):
    redirect = await anonymous.get("/activities")
    assert redirect.headers["location"] == "/login?next=%2Factivities"

    response = await anonymous.post(
        "/login", data={"token": settings.api_token, "next": "/activities"}
    )
    assert response.headers["location"] == "/activities"
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert (await anonymous.get("/activities")).status_code == 200


@pytest.mark.parametrize("target", ["https://evil.example/", "//evil.example", "/\\evil"])
async def test_login_does_not_redirect_to_other_sites(anonymous, settings, target):
    response = await anonymous.post("/login", data={"token": settings.api_token, "next": target})
    assert response.headers["location"] == "/"


async def test_logout(browser):
    assert (await browser.post("/logout")).headers["location"] == "/login"
    assert (await browser.get("/")).status_code == 303


async def test_cross_site_post_is_rejected(browser):
    response = await browser.post(
        "/hosts", data=APP_HOST, headers={"Origin": "https://evil.example"}
    )
    assert response.status_code == 403
    same_site = await browser.post("/hosts", data=APP_HOST, headers={"Origin": "http://test"})
    assert same_site.status_code == 303


async def test_api_token_is_not_accepted_as_session_on_pages(anonymous, settings):
    response = await anonymous.get("/", headers={"Authorization": f"Bearer {settings.api_token}"})
    assert response.status_code == 303


# --- pages -------------------------------------------------------------------------------


async def test_empty_overview(browser):
    response = await browser.get("/")
    assert response.status_code == 200
    assert "No systems yet." in response.text


async def test_language_switch(browser):
    response = await browser.get("/language/de?next=/hosts")
    assert response.headers["location"] == "/hosts"
    page = await browser.get("/hosts")
    assert "Host anlegen" in page.text and '<html lang="de">' in page.text

    await browser.get("/language/en")
    assert "Add host" in (await browser.get("/hosts")).text

    unknown = await browser.get("/language/xx")
    assert unknown.status_code == 303
    assert "Add host" in (await browser.get("/hosts")).text


async def test_accept_language_header_selects_german(anonymous):
    page = await anonymous.get("/login", headers={"Accept-Language": "de-DE,de;q=0.9"})
    assert "Anmelden" in page.text


async def test_add_host_and_discover(browser):
    created = await browser.post("/hosts", data=APP_HOST)
    assert created.headers["location"] == "/hosts/1?notice=host_created"
    assert "Host created." in (await browser.get(created.headers["location"])).text

    discovered = await browser.post("/hosts/1/discover")
    location = discovered.headers["location"]
    assert "notice=discovered" in location and "n_added=2" in location
    page = await browser.get(location)
    assert "Discovery finished: 2 new, 0 updated, 0 missing." in page.text
    assert "ABAP application server" in page.text
    assert "Central services" in page.text
    assert "16.0 GB" in page.text

    hosts = await browser.get("/hosts")
    assert "sapapp1" in hosts.text and "stored" in hosts.text


async def test_overview_shows_systems_and_databases(browser):
    await add_hosts(browser)
    page = (await browser.get("/")).text
    assert ">DEV</a>" in page
    assert "SAP HANA" in page
    assert "SYSTEMDB@DEV" in page and "DEV@DEV" in page
    assert page.count("badge-gray") == 3
    assert 'action="/systems/1/start"' in page
    assert 'action="/instances/1/stop"' in page


async def test_invalid_host_input(browser):
    for body, error in [
        (APP_HOST | {"hostagent_port": "99999"}, "invalid_input"),
        (APP_HOST | {"password": ""}, "credentials_incomplete"),
        ({"name": "  "}, "invalid_input"),
    ]:
        response = await browser.post("/hosts", data=body)
        assert response.headers["location"] == f"/hosts?error={error}"
    await browser.post("/hosts", data=APP_HOST)
    duplicate = await browser.post("/hosts", data=APP_HOST)
    assert duplicate.headers["location"] == "/hosts?error=host_exists"
    assert "already exists" in (await browser.get(duplicate.headers["location"])).text


async def test_discovery_failure_is_shown(browser):
    await browser.post("/hosts", data=APP_HOST | {"hostagent_port": "4711"})
    response = await browser.post("/hosts/1/discover")
    page = await browser.get(response.headers["location"])
    assert "The host agent could not be queried" in page.text
    assert "Connection refused" in page.text
    assert "badge-red" in page.text


async def test_discovery_warning_is_shown(browser, app):
    await browser.post("/hosts", data=APP_HOST)
    app.state.context.clients._transport.remove_port(50013)
    response = await browser.post("/hosts/1/discover")
    page = await browser.get(response.headers["location"])
    assert "Warnings" in page.text
    assert "DEV/00: sapstartsrv could not be queried" in page.text


async def test_host_values_are_escaped(browser):
    await browser.post("/hosts", data={"name": "<script>alert(1)</script>"})
    page = (await browser.get("/hosts")).text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


async def test_credentials_forms(browser):
    await browser.post("/hosts", data={"name": "sapapp1", "address": "127.0.0.1"})
    assert "not set" in (await browser.get("/hosts/1")).text

    saved = await browser.post("/hosts/1/credentials", data=CREDENTIALS)
    assert saved.headers["location"] == "/hosts/1?notice=credentials_saved"
    page = (await browser.get("/hosts/1")).text
    assert "stored" in page and "devadm" in page and "mock" not in page

    incomplete = await browser.post("/hosts/1/credentials", data={"username": "devadm"})
    assert incomplete.headers["location"] == "/hosts/1?error=credentials_incomplete"

    await browser.post("/hosts/1/discover")
    saved = await browser.post("/systems/1/credentials", data=CREDENTIALS)
    assert saved.headers["location"] == "/systems/1?notice=credentials_saved"
    assert "stored" in (await browser.get("/systems/1")).text


async def test_delete_host(browser):
    await add_hosts(browser)
    response = await browser.post("/hosts/1/delete")
    assert response.headers["location"] == "/hosts?notice=host_deleted"
    page = (await browser.get("/hosts")).text
    assert "sapapp1" not in page and "sapdb1" in page


# --- operations --------------------------------------------------------------------------


async def test_start_instance_from_the_browser(browser, app):
    await add_hosts(browser)
    response = await browser.post("/instances/1/start")
    assert response.headers["location"] == "/activities/1"

    running = await browser.get("/activities/1")
    assert '<meta http-equiv="refresh" content="2">' in running.text

    await app.state.runner.wait(1)
    finished = (await browser.get("/activities/1")).text
    assert "http-equiv" not in finished
    assert "succeeded" in finished
    assert "Instance status: GREEN" in finished
    assert "Finished successfully" in finished

    assert "badge-green" in (await browser.get("/")).text
    listing = (await browser.get("/activities")).text
    assert 'href="/activities/1"' in listing and "DEV/01" in listing


async def test_system_operation_and_conflict(browser, app):
    await add_hosts(browser)
    started = await browser.post("/systems/1/start")
    assert started.headers["location"] == "/activities/1"

    overview = (await browser.get("/")).text
    assert "Activity running" in overview
    assert "disabled" in overview
    assert '<meta http-equiv="refresh" content="5">' in overview

    conflict = await browser.post("/instances/1/stop")
    assert conflict.headers["location"] == "/systems/1?error=conflict"
    page = await browser.get(conflict.headers["location"])
    assert "Another activity is still running" in page.text

    await app.state.runner.wait(1)
    overview = (await browser.get("/")).text
    assert "Activity running" not in overview
    assert "http-equiv" not in overview
    # Three instances and the two HANA databases, whose status follows the operation.
    assert overview.count("badge-green") == 3
    assert overview.count("badge-running") == 2


async def test_failed_activity_shows_error_in_german(browser, app):
    await add_hosts(browser)
    await browser.post("/hosts/1/credentials", data={"username": "devadm", "password": "wrong"})
    await browser.get("/language/de")
    await browser.post("/instances/1/start")
    await app.state.runner.wait(1)
    page = (await browser.get("/activities/1")).text
    assert "fehlgeschlagen" in page
    assert "Invalid Credentials" in page
    assert "Protokollmeldungen werden auf Englisch aufgezeichnet." in page


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("GET", "/hosts/99", 404),
        ("GET", "/systems/99", 404),
        ("GET", "/activities/99", 404),
        ("POST", "/instances/99/start", 404),
        ("POST", "/systems/99/stop", 404),
        ("POST", "/instances/1/explode", 422),
    ],
)
async def test_unknown_targets(browser, method, path, expected):
    assert (await browser.request(method, path)).status_code == expected


async def test_static_stylesheet_is_served(anonymous):
    response = await anonymous.get("/static/style.css")
    assert response.status_code == 200
    assert "text/css" in response.headers["content-type"]
