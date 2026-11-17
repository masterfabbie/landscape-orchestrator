"""Server-rendered web interface.

Pages are plain HTML without external assets, so the interface also works in networks
without internet access. State is changed only by POST forms.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from .. import __version__
from ..connections import ClientFactory
from ..crypto import SecretBox
from ..dependencies import DependencyError, load_graph, set_dependencies, would_create_cycle
from ..discovery import DiscoveryError, discover_host
from ..models import (
    STATUS_UNKNOWN,
    Activity,
    ActivityLogEntry,
    Host,
    Instance,
    Pool,
    Sid,
    System,
)
from ..operations import (
    ACTIVE_STATUSES,
    NotAssignedError,
    OperationConflictError,
    OperationKind,
    OperationRunner,
    TargetNotFoundError,
)
from ..pools import UnknownPoolError, assign_pool
from ..settings import Settings
from ..systems import (
    SystemChangeError,
    assign_instances,
    create_system,
    delete_system,
    depends_on_sids,
    sids_of_system,
    suggest_name,
    suggestions,
    unassign_instance,
    unassigned_instances,
    update_system,
)
from .i18n import LANGUAGES, instance_type_key, pick_language, translate
from .sessions import SESSION_COOKIE, SESSION_LIFETIME_SECONDS, create_session, is_valid_session

WEB_DIR = Path(__file__).parent
LANGUAGE_COOKIE = "lo_lang"
ACTIVITY_REFRESH_SECONDS = 2
OVERVIEW_REFRESH_SECONDS = 5

templates = Jinja2Templates(directory=WEB_DIR / "templates")


@dataclass
class AppContext:
    """What the request handlers need from the application."""

    settings: Settings
    secret_box: SecretBox
    clients: ClientFactory
    session_factory: async_sessionmaker[AsyncSession]
    runner: OperationRunner


class LoginRequiredError(Exception):
    def __init__(self, next_path: str) -> None:
        self.next_path = next_path


# --- dependencies -----------------------------------------------------------------------


def get_context(request: Request) -> AppContext:
    return request.app.state.context


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with get_context(request).session_factory() as session:
        yield session


def require_login(request: Request) -> None:
    token = get_context(request).settings.api_token
    if not is_valid_session(token, request.cookies.get(SESSION_COOKIE)):
        path = request.url.path if request.method == "GET" else "/"
        raise LoginRequiredError(path)


def check_origin(request: Request) -> None:
    """Reject form posts from other sites, in addition to the SameSite cookie."""
    origin = request.headers.get("origin")
    if request.method == "POST" and origin and origin != "null":
        if urlsplit(origin).netloc != request.headers.get("host"):
            raise HTTPException(403, "Cross-site request rejected")


Context = Annotated[AppContext, Depends(get_context)]
DbSession = Annotated[AsyncSession, Depends(get_session)]
OperationName = Literal["start", "stop", "restart"]

public = APIRouter(dependencies=[Depends(check_origin)], include_in_schema=False)
pages = APIRouter(
    dependencies=[Depends(require_login), Depends(check_origin)], include_in_schema=False
)


# --- rendering --------------------------------------------------------------------------


def _language(request: Request) -> str:
    return pick_language(
        request.cookies.get(LANGUAGE_COOKIE), request.headers.get("accept-language")
    )


def _format_time(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S") + " UTC" if value else ""


def _format_memory(kilobytes: int | None) -> str:
    return f"{kilobytes / 1024 / 1024:.1f} GB" if kilobytes else ""


templates.env.filters["time"] = _format_time
templates.env.filters["memory"] = _format_memory


def render(request: Request, name: str, status_code: int = 200, **context: Any) -> HTMLResponse:
    language = _language(request)

    def t(key: str, **values: object) -> str:
        return translate(language, key, **values)

    query = request.query_params
    notice = error = None
    if query.get("notice"):
        values = {k[2:]: v for k, v in query.items() if k.startswith("n_")}
        notice = t(f"notice.{query['notice']}", **values)
    if query.get("error"):
        values = {k[2:]: v for k, v in query.items() if k.startswith("e_")}
        error = t(f"error.{query['error']}", **values)
    kept = [(k, v) for k, v in query.multi_items() if k == "tab" or k == "pool"]
    context = {
        "t": t,
        "language": language,
        "languages": LANGUAGES,
        "path": request.url.path,
        # Current page for the language switch, without one-off notices.
        "here": request.url.path + (f"?{urlencode(kept)}" if kept else ""),
        "notice": notice,
        "error": error,
        "warnings": query.getlist("warning"),
        "instance_type": lambda features: (
            t(key) if (key := instance_type_key(features)) else (features or "")
        ),
    } | context
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def redirect(path: str, **params: Any) -> RedirectResponse:
    """Redirect after a POST; ``params`` carry the notice or error to show."""
    query = urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)
    separator = "&" if "?" in path else "?"
    return RedirectResponse(f"{path}{separator}{query}" if query else path, status_code=303)


def _safe_next(target: str | None) -> str:
    """Only allow redirects to paths of this application."""
    if target and target.startswith("/") and not target.startswith(("//", "/\\")):
        return target
    return "/"


# --- sign in, language ------------------------------------------------------------------


@public.get("/login")
async def login_form(request: Request, next: str = "/") -> HTMLResponse:
    return render(request, "login.html", next=_safe_next(next), invalid=False)


@public.post("/login")
async def login(
    request: Request,
    context: Context,
    token: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/",
) -> Response:
    expected = context.settings.api_token
    if not secrets.compare_digest(token.strip().encode(), expected.encode()):
        return render(request, "login.html", status_code=401, next=_safe_next(next), invalid=True)
    response = RedirectResponse(_safe_next(next), status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        create_session(expected),
        max_age=SESSION_LIFETIME_SECONDS,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
    )
    return response


@public.post("/logout")
async def logout() -> Response:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@public.get("/language/{code}")
async def set_language(code: str, next: str = "/") -> Response:
    response = RedirectResponse(_safe_next(next), status_code=303)
    if code in LANGUAGES:
        response.set_cookie(LANGUAGE_COOKIE, code, max_age=365 * 24 * 3600, samesite="lax")
    return response


# --- shared helpers ---------------------------------------------------------------------


async def _active_by_system(session: AsyncSession) -> dict[int, Activity]:
    """Running activity per system, including systems touched through dependencies."""
    rows = await session.scalars(
        select(Activity)
        .where(Activity.status.in_(ACTIVE_STATUSES))
        .options(selectinload(Activity.systems))
    )
    active: dict[int, Activity] = {}
    for activity in rows:
        for system_id in {activity.system_id, *(s.id for s in activity.systems)}:
            if system_id:
                active[system_id] = activity
    return active


def system_status(system: System) -> str:
    """Overall status of a system from the stored status of its instances."""
    statuses = {instance.last_status for instance in system.instances}
    if not statuses:
        return STATUS_UNKNOWN
    if statuses == {"GREEN"}:
        return "GREEN"
    if statuses == {"GRAY"}:
        return "GRAY"
    if "RED" in statuses:
        return "RED"
    if statuses == {STATUS_UNKNOWN}:
        return STATUS_UNKNOWN
    return "YELLOW"


templates.env.globals["system_status"] = system_status


def _tab(value: str | None, allowed: tuple[str, ...]) -> str:
    return value if value in allowed else allowed[0]


async def _all_systems(session: AsyncSession) -> list[System]:
    return list(
        await session.scalars(
            select(System)
            .order_by(System.name)
            .options(
                selectinload(System.instances).selectinload(Instance.host),
                selectinload(System.instances).selectinload(Instance.sid),
                selectinload(System.depends_on),
                selectinload(System.pool),
            )
        )
    )


async def _all_pools(session: AsyncSession) -> list[Pool]:
    query = select(Pool).order_by(Pool.name).options(selectinload(Pool.systems))
    return list(await session.scalars(query))


async def _all_hosts(session: AsyncSession) -> list[Host]:
    query = select(Host).order_by(Host.name).options(selectinload(Host.instances))
    return list(await session.scalars(query))


def _count(values: list[str], keys: tuple[str, ...]) -> list[tuple[str, int]]:
    """Counts per key, in the given order; keys that do not occur are left out."""
    return [(key, values.count(key)) for key in keys if values.count(key)]


STATUS_ORDER = ("GREEN", "YELLOW", "RED", "GRAY", STATUS_UNKNOWN)


# --- overview ---------------------------------------------------------------------------


@pages.get("/")
async def overview(request: Request, session: DbSession) -> HTMLResponse:
    systems = await _all_systems(session)
    hosts = await _all_hosts(session)
    # Only instances of systems; unassigned instances are not part of the landscape yet.
    instances = [instance for system in systems for instance in system.instances]
    host_states = [
        "UNKNOWN" if host.reachable is None else ("GREEN" if host.reachable else "RED")
        for host in hosts
    ]
    since = datetime.now(UTC) - timedelta(hours=24)
    recent = list(
        await session.scalars(select(Activity).where(Activity.requested_at >= since))
    )
    active = await _active_by_system(session)
    return render(
        request,
        "overview.html",
        tiles=[
            ("overview.systems", "/landscape", len(systems),
             _count([system_status(s) for s in systems], STATUS_ORDER), "status"),
            ("overview.instances", "/landscape", len(instances),
             _count([i.last_status for i in instances], STATUS_ORDER), "status"),
            ("overview.hosts", "/landscape?tab=hosts", len(hosts),
             _count(host_states, ("GREEN", "RED", "UNKNOWN")), "reach"),
            ("overview.activities_24h", "/activities", len(recent),
             _count([a.status for a in recent], ("RUNNING", "PENDING", "SUCCEEDED", "FAILED")),
             "activity"),
        ],
        refresh=OVERVIEW_REFRESH_SECONDS if active else None,
    )


# --- landscape --------------------------------------------------------------------------


@pages.get("/landscape")
async def landscape(
    request: Request, session: DbSession, tab: str | None = None, pool: str | None = None
) -> HTMLResponse:
    tab = _tab(tab, ("systems", "hosts"))
    active = await _active_by_system(session)
    context: dict[str, Any] = {"tab": tab, "active": active}
    if tab == "systems":
        systems = await _all_systems(session)
        pools = await _all_pools(session)
        if pool == "none":
            systems = [s for s in systems if s.pool is None]
        elif pool and pool.isdigit():
            systems = [s for s in systems if s.pool_id == int(pool)]
        context |= {"systems": systems, "pools": pools, "pool_filter": pool or ""}
    else:
        context["hosts"] = await _all_hosts(session)
    refresh = OVERVIEW_REFRESH_SECONDS if active else None
    return render(request, "landscape.html", refresh=refresh, **context)


@pages.get("/hosts")
async def hosts_redirect() -> Response:
    return RedirectResponse("/landscape?tab=hosts", status_code=303)


async def _load_host(session: AsyncSession, host_id: int) -> Host:
    host = await session.scalar(
        select(Host)
        .where(Host.id == host_id)
        .options(
            selectinload(Host.instances).selectinload(Instance.system),
            selectinload(Host.instances).selectinload(Instance.sid),
            selectinload(Host.databases),
        )
    )
    if host is None:
        raise HTTPException(404, "Host not found")
    return host


@pages.get("/hosts/{host_id}")
async def host_detail(request: Request, host_id: int, session: DbSession) -> HTMLResponse:
    host = await _load_host(session, host_id)
    active = await _active_by_system(session)
    return render(request, "host.html", host=host, active=active)


async def _load_system(session: AsyncSession, system_id: int) -> System:
    system = await session.scalar(
        select(System)
        .where(System.id == system_id)
        .options(
            selectinload(System.instances).selectinload(Instance.host),
            selectinload(System.instances).selectinload(Instance.sid),
            selectinload(System.depends_on),
            selectinload(System.required_by),
            selectinload(System.pool),
        )
        .execution_options(populate_existing=True)
    )
    if system is None:
        raise HTTPException(404, "System not found")
    return system


@pages.get("/systems/{system_id}")
async def system_detail(request: Request, system_id: int, session: DbSession) -> HTMLResponse:
    system = await _load_system(session, system_id)
    active = (await _active_by_system(session)).get(system.id)
    refresh = OVERVIEW_REFRESH_SECONDS if active else None
    return render(
        request,
        "system.html",
        system=system,
        active=active,
        refresh=refresh,
        depends=depends_on_sids(system),
    )


@pages.post("/systems/{system_id}/{operation}")
async def operate_system(
    system_id: int, operation: OperationName, context: Context, session: DbSession
) -> Response:
    try:
        activity = await context.runner.submit_system(
            session, system_id, OperationKind(operation.upper())
        )
    except TargetNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except OperationConflictError:
        return redirect(f"/systems/{system_id}", error="conflict")
    return redirect(f"/activities/{activity.id}")


@pages.post("/instances/{instance_id}/{operation}")
async def operate_instance(
    instance_id: int, operation: OperationName, context: Context, session: DbSession
) -> Response:
    instance = await session.get(Instance, instance_id)
    if instance is None:
        raise HTTPException(404, "Instance not found")
    try:
        activity = await context.runner.submit_instance(
            session, instance_id, OperationKind(operation.upper())
        )
    except NotAssignedError:
        return redirect(f"/hosts/{instance.host_id}", error="not_assigned")
    except OperationConflictError:
        return redirect(f"/systems/{instance.system_id}", error="conflict")
    return redirect(f"/activities/{activity.id}")


# --- activities -------------------------------------------------------------------------

LOG_ENTRIES_SHOWN = 200


@pages.get("/activities")
async def activities(request: Request, session: DbSession, tab: str | None = None) -> HTMLResponse:
    tab = _tab(tab, ("activities", "logs"))
    if tab == "activities":
        rows = list(
            await session.scalars(select(Activity).order_by(Activity.id.desc()).limit(100))
        )
        return render(request, "activities.html", tab=tab, activities=rows)
    entries = list(
        await session.scalars(
            select(ActivityLogEntry)
            .order_by(ActivityLogEntry.id.desc())
            .limit(LOG_ENTRIES_SHOWN)
            .options(selectinload(ActivityLogEntry.activity))
        )
    )
    return render(request, "activities.html", tab=tab, entries=entries, limit=LOG_ENTRIES_SHOWN)


@pages.get("/activities/{activity_id}")
async def activity_detail(request: Request, activity_id: int, session: DbSession) -> HTMLResponse:
    activity = await session.scalar(
        select(Activity).where(Activity.id == activity_id).options(selectinload(Activity.entries))
    )
    if activity is None:
        raise HTTPException(404, "Activity not found")
    running = activity.status in ACTIVE_STATUSES
    return render(
        request,
        "activity.html",
        activity=activity,
        refresh=ACTIVITY_REFRESH_SECONDS if running else None,
    )


# --- configuration ----------------------------------------------------------------------


@pages.get("/configuration")
async def configuration(
    request: Request, session: DbSession, tab: str | None = None
) -> HTMLResponse:
    tab = _tab(tab, ("hosts", "systems", "pools"))
    context: dict[str, Any] = {"tab": tab}
    if tab == "hosts":
        context["hosts"] = await _all_hosts(session)
    elif tab == "systems":
        systems = await _all_systems(session)
        context |= {
            "systems": systems,
            "unassigned": await unassigned_instances(session),
            "suggested": await suggestions(session),
            "depends": {s.id: depends_on_sids(s) for s in systems},
        }
    else:
        context["pools"] = await _all_pools(session)
    return render(request, "configuration.html", **context)


def _optional_int(value: str, minimum: int, maximum: int) -> int | None:
    if not value.strip():
        return None
    number = int(value)
    if not minimum <= number <= maximum:
        raise ValueError(value)
    return number


HOSTS_TAB = "/configuration?tab=hosts"


@pages.post("/configuration/hosts")
async def create_host(
    context: Context,
    session: DbSession,
    name: Annotated[str, Form()],
    address: Annotated[str, Form()] = "",
    hostagent_port: Annotated[str, Form()] = "",
    sapcontrol_port_offset: Annotated[str, Form()] = "",
    https: Annotated[bool, Form()] = False,
    verify_tls: Annotated[bool, Form()] = False,
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
) -> Response:
    name, address, username = name.strip(), address.strip(), username.strip()
    try:
        port = _optional_int(hostagent_port, 1, 65535)
        offset = _optional_int(sapcontrol_port_offset, 0, 15000) or 0
    except ValueError:
        return redirect(HOSTS_TAB, error="invalid_input")
    if not name or len(name) > 255:
        return redirect(HOSTS_TAB, error="invalid_input")
    if bool(username) != bool(password):
        return redirect(HOSTS_TAB, error="credentials_incomplete")
    host = Host(
        name=name,
        address=address or name,
        hostagent_port=port,
        https=https,
        verify_tls=verify_tls,
        sapcontrol_port_offset=offset,
        username=username or None,
        password_encrypted=context.secret_box.encrypt(password) if password else None,
    )
    session.add(host)
    try:
        await session.commit()
    except IntegrityError:
        return redirect(HOSTS_TAB, error="host_exists")
    return redirect(f"/configuration/hosts/{host.id}", notice="host_created")


@pages.get("/configuration/hosts/{host_id}")
async def configure_host(request: Request, host_id: int, session: DbSession) -> HTMLResponse:
    return render(request, "config_host.html", host=await _load_host(session, host_id))


@pages.post("/configuration/hosts/{host_id}/discover")
async def discover(host_id: int, context: Context, session: DbSession) -> Response:
    host = await _load_host(session, host_id)
    target = f"/configuration/hosts/{host_id}"
    try:
        result = await discover_host(session, host, context.clients)
    except DiscoveryError as exc:
        await session.commit()  # keep the "unreachable" marker on the host
        return redirect(target, error="discovery_failed", e_detail=str(exc))
    await session.commit()
    return redirect(
        target,
        notice="discovered",
        n_added=len(result.instances_added) + len(result.databases_added),
        n_updated=len(result.instances_updated) + len(result.databases_updated),
        n_missing=len(result.instances_missing) + len(result.databases_missing),
        warning=result.warnings,
    )


@pages.post("/configuration/hosts/{host_id}/credentials")
async def set_host_credentials(
    host_id: int,
    context: Context,
    session: DbSession,
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
) -> Response:
    host = await _load_host(session, host_id)
    target = f"/configuration/hosts/{host_id}"
    if not username.strip() or not password:
        return redirect(target, error="credentials_incomplete")
    host.username = username.strip()
    host.password_encrypted = context.secret_box.encrypt(password)
    await session.commit()
    return redirect(target, notice="credentials_saved")


@pages.post("/configuration/hosts/{host_id}/delete")
async def delete_host(host_id: int, session: DbSession) -> Response:
    host = await _load_host(session, host_id)
    await session.delete(host)
    await session.commit()
    return redirect("/configuration", tab="hosts", notice="host_deleted")


SYSTEMS_TAB = "/configuration?tab=systems"


def _change_error(target: str, exc: SystemChangeError | UnknownPoolError) -> Response:
    code = exc.code if isinstance(exc, SystemChangeError) else "unknown_pool"
    return redirect(target, error=f"system_{code}" if code != "unknown_pool" else "invalid_input")


@pages.get("/configuration/systems/new")
async def new_system(
    request: Request,
    session: DbSession,
    instance_id: Annotated[list[int], Query()] = [],  # noqa: B006 - FastAPI copies it
) -> Response:
    """Confirmation page for a new system from the selected unassigned instances."""
    selected = [i for i in await unassigned_instances(session) if i.id in set(instance_id)]
    if not selected:
        return redirect(SYSTEMS_TAB, error="system_no_instances")
    pools = list(await session.scalars(select(Pool).order_by(Pool.name)))
    return render(
        request,
        "config_system_new.html",
        instances=selected,
        suggested_name=suggest_name(selected),
        pools=pools,
    )


@pages.post("/configuration/systems")
async def create_system_form(
    session: DbSession,
    name: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    pool_id: Annotated[str, Form()] = "",
    instance_id: Annotated[list[int], Form()] = [],  # noqa: B006 - FastAPI copies it
) -> Response:
    try:
        system = await create_system(
            session,
            name,
            instance_id,
            pool_id=int(pool_id) if pool_id.isdigit() else None,
            description=description,
        )
    except (SystemChangeError, UnknownPoolError) as exc:
        await session.rollback()
        return _change_error(SYSTEMS_TAB, exc)
    await session.commit()
    return redirect(SYSTEMS_TAB, notice="system_created", n_name=system.name)


@pages.post("/configuration/systems/assign")
async def assign_selected(
    session: DbSession,
    system_id: Annotated[str, Form()] = "",
    instance_id: Annotated[list[int], Form()] = [],  # noqa: B006 - FastAPI copies it
) -> Response:
    """Assign the selected unassigned instances to an existing system."""
    if not system_id.isdigit():
        return redirect(SYSTEMS_TAB, error="system_unknown_system")
    try:
        await assign_instances(session, int(system_id), instance_id)
    except SystemChangeError as exc:
        await session.rollback()
        return _change_error(SYSTEMS_TAB, exc)
    await session.commit()
    return redirect(SYSTEMS_TAB, notice="instances_assigned")


@pages.post("/configuration/systems/{system_id}/assign/{instance_id}")
async def assign_suggested(system_id: int, instance_id: int, session: DbSession) -> Response:
    """Accept the suggestion for one unassigned instance."""
    try:
        await assign_instances(session, system_id, [instance_id])
    except SystemChangeError as exc:
        await session.rollback()
        return _change_error(SYSTEMS_TAB, exc)
    await session.commit()
    return redirect(SYSTEMS_TAB, notice="instances_assigned")


@pages.get("/configuration/systems/{system_id}")
async def configure_system(request: Request, system_id: int, session: DbSession) -> HTMLResponse:
    system = await _load_system(session, system_id)
    graph = await load_graph(session)
    linked = {s.id for s in system.depends_on}
    candidates = [
        other
        for other in await session.scalars(select(System).order_by(System.name))
        if other.id != system.id
        and other.id not in linked
        and not would_create_cycle(system.id, other.id, graph)
    ]
    pools = list(await session.scalars(select(Pool).order_by(Pool.name)))
    return render(
        request,
        "config_system.html",
        system=system,
        candidates=candidates,
        pools=pools,
        sids=await sids_of_system(session, system.id),
        depends=depends_on_sids(system),
    )


@pages.post("/configuration/systems/{system_id}")
async def save_system(
    system_id: int,
    session: DbSession,
    name: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
) -> Response:
    target = f"/configuration/systems/{system_id}"
    try:
        await update_system(session, system_id, name, description)
    except SystemChangeError as exc:
        await session.rollback()
        if exc.code == "unknown_system":
            raise HTTPException(404, "System not found") from exc
        return _change_error(target, exc)
    await session.commit()
    return redirect(target, notice="system_saved")


@pages.post("/configuration/systems/{system_id}/delete")
async def delete_system_form(system_id: int, session: DbSession) -> Response:
    try:
        await delete_system(session, system_id)
    except SystemChangeError as exc:
        await session.rollback()
        if exc.code == "unknown_system":
            raise HTTPException(404, "System not found") from exc
        return _change_error(f"/configuration/systems/{system_id}", exc)
    await session.commit()
    return redirect(SYSTEMS_TAB, notice="system_deleted")


@pages.post("/configuration/systems/{system_id}/instances/{instance_id}/remove")
async def remove_instance(
    system_id: int, instance_id: int, session: DbSession, back: Annotated[str, Form()] = ""
) -> Response:
    target = f"/configuration/systems/{system_id}" if back == "system" else SYSTEMS_TAB
    try:
        await unassign_instance(session, system_id, instance_id)
    except SystemChangeError as exc:
        await session.rollback()
        return _change_error(target, exc)
    await session.commit()
    return redirect(target, notice="instance_removed")


async def _change_dependencies(
    session: AsyncSession, system_id: int, change: Callable[[set[int]], set[int]]
) -> Response:
    system = await _load_system(session, system_id)
    target = f"/configuration/systems/{system_id}"
    current = {s.id for s in system.depends_on}
    try:
        await set_dependencies(session, system_id, change(current))
    except DependencyError as exc:
        return redirect(target, error=f"dependency_{exc.code}")
    await session.commit()
    return redirect(target, notice="dependencies_saved")


@pages.post("/configuration/systems/{system_id}/dependencies")
async def add_dependency(
    system_id: int, session: DbSession, depends_on: Annotated[int, Form()]
) -> Response:
    return await _change_dependencies(session, system_id, lambda current: current | {depends_on})


@pages.post("/configuration/systems/{system_id}/dependencies/{depends_on}/delete")
async def remove_dependency(system_id: int, depends_on: int, session: DbSession) -> Response:
    return await _change_dependencies(session, system_id, lambda current: current - {depends_on})


@pages.post("/configuration/systems/{system_id}/sids/{sid_id}/credentials")
async def set_sid_credentials(
    system_id: int,
    sid_id: int,
    context: Context,
    session: DbSession,
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
) -> Response:
    """Credentials for the sapstartsrv of one SID, maintained from a system using it."""
    await _load_system(session, system_id)
    target = f"/configuration/systems/{system_id}"
    sid = await session.get(Sid, sid_id)
    if sid is None or sid.id not in {s.id for s in await sids_of_system(session, system_id)}:
        raise HTTPException(404, "SID not found in this system")
    if not username.strip() or not password:
        return redirect(target, error="credentials_incomplete")
    sid.username = username.strip()
    sid.password_encrypted = context.secret_box.encrypt(password)
    await session.commit()
    return redirect(target, notice="credentials_saved")


@pages.post("/configuration/systems/{system_id}/pool")
async def set_system_pool(
    system_id: int, session: DbSession, pool_id: Annotated[str, Form()] = ""
) -> Response:
    await _load_system(session, system_id)
    target = f"/configuration/systems/{system_id}"
    try:
        await assign_pool(session, system_id, int(pool_id) if pool_id.isdigit() else None)
    except UnknownPoolError:
        return redirect(target, error="invalid_input")
    await session.commit()
    return redirect(target, notice="pool_saved")


@pages.post("/configuration/pools")
async def create_pool(
    session: DbSession,
    name: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
) -> Response:
    name = name.strip()
    if not name or len(name) > 64:
        return redirect("/configuration", tab="pools", error="invalid_input")
    session.add(Pool(name=name, description=description.strip() or None))
    try:
        await session.commit()
    except IntegrityError:
        return redirect("/configuration", tab="pools", error="pool_exists")
    return redirect("/configuration", tab="pools", notice="pool_created")


@pages.post("/configuration/pools/{pool_id}/delete")
async def delete_pool(pool_id: int, session: DbSession) -> Response:
    pool = await session.get(Pool, pool_id)
    if pool is None:
        raise HTTPException(404, "Pool not found")
    await session.delete(pool)
    await session.commit()
    return redirect("/configuration", tab="pools", notice="pool_deleted")


# --- administration ---------------------------------------------------------------------


@pages.get("/administration")
async def administration(request: Request, context: Context) -> HTMLResponse:
    database = context.settings.database_url.split(":", 1)[0]
    return render(
        request,
        "administration.html",
        version=__version__,
        database=database,
        session_hours=SESSION_LIFETIME_SECONDS // 3600,
    )


# --- registration -----------------------------------------------------------------------


def register_web(app: FastAPI, context: AppContext) -> None:
    app.state.context = context

    @app.exception_handler(LoginRequiredError)
    async def to_login(_: Request, exc: LoginRequiredError) -> Response:
        return redirect("/login", next=exc.next_path if exc.next_path != "/" else None)

    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    app.include_router(public)
    app.include_router(pages)
