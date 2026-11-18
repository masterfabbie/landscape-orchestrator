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
from ..filters import FIELDS as FILTER_FIELDS
from ..filters import PARAMS as FILTER_PARAMS
from ..filters import (
    REACH_VALUES,
    STATUS_VALUES,
    TYPE_VALUES,
    filter_hosts,
    filter_systems,
    read_values,
    tag_options,
    visible_fields,
)
from ..hosts import Connection, HostChangeError, update_connection
from ..models import (
    STATUS_UNKNOWN,
    Activity,
    ActivityLogEntry,
    ApiToken,
    Host,
    HostTag,
    Instance,
    Pool,
    Sid,
    System,
    SystemTag,
    User,
)
from ..operations import (
    ACTIVE_STATUSES,
    NotAssignedError,
    OperationConflictError,
    OperationKind,
    OperationRunner,
    TargetNotFoundError,
)
from ..permissions import required_role
from ..pools import UnknownPoolError, assign_pool
from ..settings import Settings
from ..status import refresh_system
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
from ..tags import TagError, parse_tags, replace_tags
from ..users import (
    ROLE_ORDER,
    SESSION_LIFETIME_SECONDS,
    Role,
    UserError,
    authenticate,
    create_session,
    create_token,
    create_user,
    get_setting,
    has_role,
    session_user,
    set_password,
    set_setting,
    update_user,
    user_count,
    verify_password,
)
from .i18n import LANGUAGES, instance_type_key, pick_language, translate

SESSION_COOKIE = "lo_session"

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


class SetupRequiredError(Exception):
    """No user exists yet; the first admin has to be created."""


class PasswordChangeRequiredError(Exception):
    """The user signed in with a start password and must choose a new one."""


# --- dependencies -----------------------------------------------------------------------


def get_context(request: Request) -> AppContext:
    return request.app.state.context


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with get_context(request).session_factory() as session:
        yield session


DbSessionDep = Annotated[AsyncSession, Depends(get_session)]


async def require_login(request: Request, session: DbSessionDep) -> User:
    """The signed-in user, if the page allows the user's role (see permissions.py)."""
    secret_key = get_context(request).settings.secret_key
    user = await session_user(session, secret_key, request.cookies.get(SESSION_COOKIE))
    if user is None:
        if await user_count(session) == 0:
            raise SetupRequiredError()
        path = request.url.path if request.method == "GET" else "/"
        raise LoginRequiredError(path)
    route = request.scope.get("route")
    template = getattr(route, "path", request.url.path)
    if user.must_change_password and not template.startswith("/account/password"):
        raise PasswordChangeRequiredError()
    needed = required_role(request.method, template)
    if not has_role(user, needed):
        raise HTTPException(403, f"Requires the role {needed}")
    request.state.user = user
    return user


def check_origin(request: Request) -> None:
    """Reject form posts from other sites, in addition to the SameSite cookie."""
    origin = request.headers.get("origin")
    if request.method == "POST" and origin and origin != "null":
        if urlsplit(origin).netloc != request.headers.get("host"):
            raise HTTPException(403, "Cross-site request rejected")


Context = Annotated[AppContext, Depends(get_context)]
CurrentUser = Annotated[User, Depends(require_login)]
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
    kept = [(k, v) for k, v in query.multi_items() if k == "tab" or k in FILTER_PARAMS]
    user = getattr(request.state, "user", None)
    context = {
        "t": t,
        "user": user,
        "can": lambda role: user is not None and has_role(user, Role(role)),
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


def _set_session_cookie(request: Request, response: Response, secret_key: str, user: User) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        create_session(secret_key, user),
        max_age=SESSION_LIFETIME_SECONDS,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
    )


@public.get("/login")
async def login_form(request: Request, session: DbSessionDep, next: str = "/") -> Response:
    if await user_count(session) == 0:
        return RedirectResponse("/setup", status_code=303)
    return render(request, "login.html", next=_safe_next(next), invalid=False)


@public.post("/login")
async def login(
    request: Request,
    context: Context,
    session: DbSessionDep,
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "/",
) -> Response:
    user = await authenticate(session, username, password)
    await session.commit()  # keep the counter of failed attempts
    if user is None:
        return render(
            request,
            "login.html",
            status_code=401,
            next=_safe_next(next),
            invalid=True,
            username=username,
        )
    target = "/account/password" if user.must_change_password else _safe_next(next)
    response = RedirectResponse(target, status_code=303)
    _set_session_cookie(request, response, context.settings.secret_key, user)
    return response


@public.post("/logout")
async def logout() -> Response:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@public.get("/setup")
async def setup_form(request: Request, session: DbSessionDep) -> Response:
    if await user_count(session) > 0:
        return RedirectResponse("/login", status_code=303)
    return render(request, "setup.html")


@public.post("/setup")
async def setup(
    request: Request,
    context: Context,
    session: DbSessionDep,
    code: Annotated[str, Form()] = "",
    username: Annotated[str, Form()] = "",
    display_name: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    password_repeat: Annotated[str, Form()] = "",
) -> Response:
    """Create the first admin with the one-time setup code printed at server start."""
    if await user_count(session) > 0:
        return RedirectResponse("/login", status_code=303)
    expected = request.app.state.setup_code or ""
    if not expected or not secrets.compare_digest(code.strip().encode(), expected.encode()):
        return redirect("/setup", error="setup_code")
    if password != password_repeat:
        return redirect("/setup", error="password_mismatch")
    try:
        user = await create_user(
            session, username, password, Role.ADMIN, display_name=display_name
        )
    except UserError as exc:
        await session.rollback()
        return redirect("/setup", error=exc.code)
    await session.commit()
    request.app.state.setup_code = None
    response = redirect("/", notice="setup_done")
    _set_session_cookie(request, response, context.settings.secret_key, user)
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
    """Overall status of a system from the stored status of its instances.

    Diagnostics agents are left out, as in system operations.
    """
    instances = [i for i in system.instances if not i.is_agent] or system.instances
    statuses = {instance.last_status for instance in instances}
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


def _instance_type(instance: Instance) -> str | None:
    key = instance_type_key(instance.features)
    return key.removeprefix("itype.") if key else None


@dataclass
class TreeView:
    """What the landscape tree shows of one system."""

    instances: list[Instance]
    agents: list[Instance]
    # Instances left out because they do not match the instance type filter.
    hidden: int
    # Open from the start; set while the instance type filter is active.
    expanded: bool


def _tree_view(system: System, types: list[str] | None) -> TreeView:
    ordered = sorted(system.instances, key=lambda i: (i.priority, i.label))
    if types:
        shown = [i for i in ordered if _instance_type(i) in types]
    else:
        shown = ordered
    return TreeView(
        instances=[i for i in shown if not i.is_agent],
        agents=sorted((i for i in shown if i.is_agent), key=lambda i: i.label),
        hidden=len(ordered) - len(shown),
        expanded=bool(types),
    )


def _filter_setting(tab: str) -> str:
    return f"filters.{tab}"


@pages.get("/landscape")
async def landscape(
    request: Request, session: DbSession, user: CurrentUser, tab: str | None = None
) -> HTMLResponse:
    tab = _tab(tab, ("systems", "hosts"))
    query = {key: request.query_params.getlist(key) for key in request.query_params}
    values = read_values(tab, query)
    active = await _active_by_system(session)
    context: dict[str, Any] = {
        "tab": tab,
        "active": active,
        "filter_values": values,
        "filter_fields": [f.name for f in FILTER_FIELDS[tab]],
        "visible_filters": visible_fields(
            tab, await get_setting(session, user, _filter_setting(tab)), values
        ),
    }
    if tab == "systems":
        all_systems = await _all_systems(session)
        systems = filter_systems(all_systems, values, system_status, _instance_type)
        hosts = await _all_hosts(session)
        options = {
            "status": [(v, f"status.{v}", True) for v in STATUS_VALUES],
            "pool": [(str(p.id), p.name, False) for p in await _all_pools(session)]
            + [("none", "pool.none", True)],
            "type": [(v, f"itype.{v}", True) for v in TYPE_VALUES],
            "tag": [(v, v, False) for v in tag_options(all_systems)],
            "hosttag": [(v, v, False) for v in tag_options(hosts)],
        }
        context |= {
            "systems": systems,
            "filter_options": options,
            "instance_count": sum(len(s.instances) for s in systems),
            "tree": {s.id: _tree_view(s, values.get("type")) for s in systems},
            "depends": {s.id: depends_on_sids(s) for s in systems},
        }
    else:
        all_hosts = await _all_hosts(session)
        versions = sorted({h.hostagent_version for h in all_hosts if h.hostagent_version})
        context |= {
            "hosts": filter_hosts(all_hosts, values),
            "filter_options": {
                "reach": [(v, f"reach.{v}", True) for v in REACH_VALUES],
                "agent": [(v, v, False) for v in versions],
                "tag": [(v, v, False) for v in tag_options(all_hosts)],
            },
        }
    refresh = OVERVIEW_REFRESH_SECONDS if active else None
    return render(request, "landscape.html", refresh=refresh, **context)


@pages.post("/landscape/filters")
async def choose_filters(
    session: DbSession,
    user: CurrentUser,
    tab: Annotated[str, Form()] = "systems",
    field: Annotated[list[str], Form()] = [],  # noqa: B006 - FastAPI copies it
) -> Response:
    """Remember which filters this user sees."""
    tab = _tab(tab, ("systems", "hosts"))
    known = [f.name for f in FILTER_FIELDS[tab]]
    chosen = ".".join(name for name in known if name in field)
    await set_setting(session, user, _filter_setting(tab), chosen)
    await session.commit()
    return redirect("/landscape", tab=tab)


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


@pages.post("/systems/{system_id}/refresh")
async def refresh_system_status(
    system_id: int, context: Context, session: DbSession, back: Annotated[str, Form()] = ""
) -> Response:
    """Read the status of all instances of the system once; no activity is created."""
    await _load_system(session, system_id)
    warnings = await refresh_system(session, system_id, context.clients)
    await session.commit()
    target = "/landscape" if back == "landscape" else f"/systems/{system_id}"
    return redirect(target, notice="status_refreshed", warning=warnings)


@pages.post("/systems/{system_id}/{operation}")
async def operate_system(
    system_id: int,
    operation: OperationName,
    context: Context,
    session: DbSession,
    user: CurrentUser,
) -> Response:
    try:
        activity = await context.runner.submit_system(
            session, system_id, OperationKind(operation.upper()), requested_by=user.username
        )
    except TargetNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except OperationConflictError:
        return redirect(f"/systems/{system_id}", error="conflict")
    return redirect(f"/activities/{activity.id}")


@pages.post("/instances/{instance_id}/{operation}")
async def operate_instance(
    instance_id: int,
    operation: OperationName,
    context: Context,
    session: DbSession,
    user: CurrentUser,
) -> Response:
    instance = await session.get(Instance, instance_id)
    if instance is None:
        raise HTTPException(404, "Instance not found")
    try:
        activity = await context.runner.submit_instance(
            session, instance_id, OperationKind(operation.upper()), requested_by=user.username
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
    host.agent_info = None  # nothing read yet; avoids a lazy load
    host.tags = []
    session.add(host)
    try:
        await session.commit()
    except IntegrityError:
        return redirect(HOSTS_TAB, error="host_exists")
    return redirect(f"/configuration/hosts/{host.id}", notice="host_created")


@pages.get("/configuration/hosts/{host_id}")
async def configure_host(request: Request, host_id: int, session: DbSession) -> HTMLResponse:
    return render(request, "config_host.html", host=await _load_host(session, host_id))


@pages.post("/configuration/hosts/{host_id}/connection")
async def set_host_connection(
    host_id: int,
    session: DbSession,
    name: Annotated[str, Form()] = "",
    address: Annotated[str, Form()] = "",
    hostagent_port: Annotated[str, Form()] = "",
    sapcontrol_port_offset: Annotated[str, Form()] = "",
    https: Annotated[bool, Form()] = False,
    verify_tls: Annotated[bool, Form()] = False,
) -> Response:
    host = await _load_host(session, host_id)
    target = f"/configuration/hosts/{host_id}"
    try:
        change = Connection(
            name=name,
            address=address,
            hostagent_port=_optional_int(hostagent_port, 1, 65535) or 0,
            https=https,
            verify_tls=verify_tls,
            sapcontrol_port_offset=_optional_int(sapcontrol_port_offset, 0, 15000) or 0,
        )
    except ValueError:
        return redirect(target, error="invalid_input")
    try:
        await update_connection(session, host, change)
    except HostChangeError as exc:
        await session.rollback()
        return redirect(target, error="host_busy" if exc.code == "busy" else exc.code)
    await session.commit()
    return redirect(target, notice="connection_saved")


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


async def _save_tags(
    session: AsyncSession, owner: Host | System, tag_class: type, text: str, target: str
) -> Response:
    try:
        tags = parse_tags([text])
    except TagError as exc:
        return redirect(target, error=exc.code, e_tag=exc.tag)
    replace_tags(owner.tags, tag_class, tags)
    await session.commit()
    return redirect(target, notice="tags_saved")


@pages.post("/configuration/hosts/{host_id}/tags")
async def set_host_tags(
    host_id: int, session: DbSession, tags: Annotated[str, Form()] = ""
) -> Response:
    host = await _load_host(session, host_id)
    return await _save_tags(session, host, HostTag, tags, f"/configuration/hosts/{host_id}")


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


@pages.post("/configuration/systems/{system_id}/tags")
async def set_system_tags(
    system_id: int, session: DbSession, tags: Annotated[str, Form()] = ""
) -> Response:
    system = await _load_system(session, system_id)
    target = f"/configuration/systems/{system_id}"
    return await _save_tags(session, system, SystemTag, tags, target)


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
async def administration(
    request: Request, context: Context, session: DbSession, tab: str | None = None
) -> HTMLResponse:
    tab = _tab(tab, ("general", "users"))
    database = context.settings.database_url.split(":", 1)[0]
    users = list(await session.scalars(select(User).order_by(User.username)))
    return render(
        request,
        "administration.html",
        tab=tab,
        version=__version__,
        database=database,
        session_hours=SESSION_LIFETIME_SECONDS // 3600,
        users=users,
        roles=[role.value for role in ROLE_ORDER],
    )


USERS_TAB = "/administration?tab=users"


@pages.post("/administration/users")
async def add_user(
    session: DbSession,
    username: Annotated[str, Form()] = "",
    display_name: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "viewer",
    password: Annotated[str, Form()] = "",
) -> Response:
    """Create a user with a start password that must be changed at the first sign-in."""
    if role not in {r.value for r in ROLE_ORDER}:
        return redirect(USERS_TAB, error="invalid_input")
    try:
        user = await create_user(
            session,
            username,
            password,
            role,
            display_name=display_name,
            must_change_password=True,
        )
    except UserError as exc:
        await session.rollback()
        return redirect(USERS_TAB, error=exc.code)
    await session.commit()
    return redirect(USERS_TAB, notice="user_created", n_name=user.username)


async def _load_user(session: AsyncSession, user_id: int) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    return user


@pages.get("/administration/users/{user_id}")
async def edit_user(request: Request, user_id: int, session: DbSession) -> HTMLResponse:
    return render(
        request,
        "user.html",
        edited=await _load_user(session, user_id),
        roles=[role.value for role in ROLE_ORDER],
    )


@pages.post("/administration/users/{user_id}")
async def save_user(
    user_id: int,
    session: DbSession,
    display_name: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "",
    active: Annotated[bool, Form()] = False,
) -> Response:
    edited = await _load_user(session, user_id)
    target = f"/administration/users/{user_id}"
    if role not in {r.value for r in ROLE_ORDER}:
        return redirect(target, error="invalid_input")
    try:
        await update_user(session, edited, role=role, active=active, display_name=display_name)
    except UserError as exc:
        await session.rollback()
        return redirect(target, error=exc.code)
    await session.commit()
    return redirect(target, notice="user_saved")


@pages.post("/administration/users/{user_id}/password")
async def reset_user_password(
    user_id: int, session: DbSession, password: Annotated[str, Form()] = ""
) -> Response:
    """Set a start password; the user must change it at the next sign-in."""
    edited = await _load_user(session, user_id)
    target = f"/administration/users/{user_id}"
    try:
        set_password(edited, password, must_change=True)
    except UserError as exc:
        return redirect(target, error=exc.code)
    await session.commit()
    return redirect(target, notice="password_reset")


# --- own account ------------------------------------------------------------------------


async def _render_account(
    request: Request, session: AsyncSession, user: User, new_token: str | None = None
) -> HTMLResponse:
    tokens = list(
        await session.scalars(
            select(ApiToken).where(ApiToken.user_id == user.id).order_by(ApiToken.id)
        )
    )
    return render(request, "account.html", tokens=tokens, new_token=new_token)


@pages.get("/account")
async def account(request: Request, session: DbSession, user: CurrentUser) -> HTMLResponse:
    return await _render_account(request, session, user)


@pages.post("/account/tokens")
async def add_token(
    request: Request,
    session: DbSession,
    user: CurrentUser,
    name: Annotated[str, Form()] = "",
    expires_days: Annotated[str, Form()] = "",
) -> Response:
    """Create a personal API token. Its value is shown once, on this response only."""
    expires_at = None
    if expires_days.strip():
        if not expires_days.strip().isdigit() or not 1 <= int(expires_days) <= 3650:
            return redirect("/account", error="invalid_input")
        expires_at = datetime.now(UTC) + timedelta(days=int(expires_days))
    try:
        _, plain = await create_token(session, user, name, expires_at)
    except UserError as exc:
        return redirect("/account", error=exc.code)
    await session.commit()
    return await _render_account(request, session, user, new_token=plain)


@pages.post("/account/tokens/{token_id}/delete")
async def delete_token(token_id: int, session: DbSession, user: CurrentUser) -> Response:
    token = await session.get(ApiToken, token_id)
    if token is None or token.user_id != user.id:
        raise HTTPException(404, "Token not found")
    await session.delete(token)
    await session.commit()
    return redirect("/account", notice="token_deleted")


@pages.get("/account/password")
async def password_form(request: Request, user: CurrentUser) -> HTMLResponse:
    return render(request, "password.html")


@pages.post("/account/password")
async def change_password(
    request: Request,
    context: Context,
    session: DbSession,
    user: CurrentUser,
    current: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    password_repeat: Annotated[str, Form()] = "",
) -> Response:
    if not verify_password(current, user.password_hash):
        return redirect("/account/password", error="password_wrong")
    if password != password_repeat:
        return redirect("/account/password", error="password_mismatch")
    if password == current:
        return redirect("/account/password", error="password_same")
    try:
        set_password(user, password)
    except UserError as exc:
        return redirect("/account/password", error=exc.code)
    await session.commit()
    # The change ended all sessions; this browser gets a new one.
    response = redirect("/account", notice="password_changed")
    _set_session_cookie(request, response, context.settings.secret_key, user)
    return response


# --- registration -----------------------------------------------------------------------


def register_web(app: FastAPI, context: AppContext) -> None:
    app.state.context = context

    @app.exception_handler(LoginRequiredError)
    async def to_login(_: Request, exc: LoginRequiredError) -> Response:
        return redirect("/login", next=exc.next_path if exc.next_path != "/" else None)

    @app.exception_handler(SetupRequiredError)
    async def to_setup(_: Request, __: SetupRequiredError) -> Response:
        return RedirectResponse("/setup", status_code=303)

    @app.exception_handler(PasswordChangeRequiredError)
    async def to_password(_: Request, __: PasswordChangeRequiredError) -> Response:
        return redirect("/account/password", notice="password_change_required")

    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    app.include_router(public)
    app.include_router(pages)
