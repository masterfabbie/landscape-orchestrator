"""Server-rendered web interface.

Pages are plain HTML without external assets, so the interface also works in networks
without internet access. State is changed only by POST forms.
"""

from __future__ import annotations

import hashlib
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
from ..catalog import Catalog, OperationDef
from ..connections import ClientFactory
from ..crypto import SecretBox
from ..customops import (
    ORDERINGS,
    PARAMETER_TYPES,
    CustomOperationError,
    ParameterError,
    parameter_values,
    save_operation,
    save_parameter,
)
from ..dependencies import DependencyError, load_graph, set_dependencies, would_create_cycle
from ..discovery import DiscoveryError, discover_host
from ..filters import PARAMS as FILTER_PARAMS
from ..filters import (
    TYPE_VALUES,
)
from ..hosts import Connection, HostChangeError, update_connection
from ..landscape import active_by_host as _active_by_host
from ..landscape import active_by_system as _active_by_system
from ..landscape import all_hosts as _all_hosts
from ..landscape import all_systems as _all_systems
from ..landscape import system_status
from ..models import (
    STATUS_UNKNOWN,
    Activity,
    ActivityLogEntry,
    ApiToken,
    CustomOperation,
    CustomOperationParameter,
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
    NotAssignedError,
    OperationConflictError,
    OperationKind,
    OperationRunner,
    TargetNotFoundError,
    TargetType,
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
    SESSION_COOKIE,
    SESSION_LIFETIME_SECONDS,
    Role,
    UserError,
    authenticate,
    create_session,
    create_token,
    create_user,
    has_role,
    session_user,
    set_password,
    update_user,
    user_count,
    verify_password,
)
from .i18n import LANGUAGE_COOKIE, LANGUAGES, all_texts, instance_type_key, pick_language, translate

WEB_DIR = Path(__file__).parent
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
    catalog: Catalog


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


# Areas with a navigation on the left: path prefix -> (title, [(href, text key)]).
SECTIONS: dict[str, dict[str, Any]] = {
    "/configuration": {
        "title": "nav.configuration",
        "items": [
            ("/configuration/hosts", "tab.hosts"),
            ("/configuration/systems", "tab.systems"),
            ("/configuration/pools", "tab.pools"),
        ],
    },
    "/operations": {
        "title": "nav.operations",
        "items": [
            ("/operations/custom", "tab.custom_operations"),
            ("/operations/templates", "tpl.title"),
        ],
    },
}


def _section(path: str) -> dict[str, Any] | None:
    for prefix, section in SECTIONS.items():
        if path == prefix or path.startswith(prefix + "/"):
            return section
    return None


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
        # Operations of the catalog this user may run on a target type, e.g. "SYSTEM".
        "ops": lambda target, instance=None: (
            # Templates are run from the landscape (React), not from these menus.
            [
                op
                for op in get_context(request).catalog.for_target(
                    TargetType(target), user, instance=instance
                )
                if op.template is None
            ]
            if user
            else []
        ),
        "language": language,
        "languages": LANGUAGES,
        "path": request.url.path,
        "section": _section(request.url.path),
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
        user = await create_user(session, username, password, Role.ADMIN, display_name=display_name)
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






templates.env.globals["system_status"] = system_status


def _tab(value: str | None, allowed: tuple[str, ...]) -> str:
    return value if value in allowed else allowed[0]




async def _all_pools(session: AsyncSession) -> list[Pool]:
    query = select(Pool).order_by(Pool.name).options(selectinload(Pool.systems))
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
    recent = list(await session.scalars(select(Activity).where(Activity.requested_at >= since)))
    active = await _active_by_system(session)
    return render(
        request,
        "overview.html",
        tiles=[
            (
                "overview.systems",
                "/landscape",
                len(systems),
                _count([system_status(s) for s in systems], STATUS_ORDER),
                "status",
            ),
            (
                "overview.instances",
                "/landscape",
                len(instances),
                _count([i.last_status for i in instances], STATUS_ORDER),
                "status",
            ),
            (
                "overview.hosts",
                "/landscape?tab=hosts",
                len(hosts),
                _count(host_states, ("GREEN", "RED", "UNKNOWN")),
                "reach",
            ),
            (
                "overview.activities_24h",
                "/activities",
                len(recent),
                _count([a.status for a in recent], ("RUNNING", "PENDING", "SUCCEEDED", "FAILED")),
                "activity",
            ),
        ],
        refresh=OVERVIEW_REFRESH_SECONDS if active else None,
    )


# --- landscape --------------------------------------------------------------------------

APP_DIR = WEB_DIR / "static" / "app"
_asset_versions: dict[str, tuple[float, str]] = {}


def asset_url(name: str) -> str:
    """URL of a built file of the browser interface with a content hash, so a new build is
    loaded at once even though the names stay the same."""
    path = APP_DIR / name
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return f"/static/app/{name}"
    cached = _asset_versions.get(name)
    if cached is None or cached[0] != mtime:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        cached = _asset_versions[name] = (mtime, digest)
    return f"/static/app/{name}?v={cached[1]}"


templates.env.globals["asset_url"] = asset_url


@pages.get("/landscape")
async def landscape(request: Request, user: CurrentUser) -> HTMLResponse:
    """The landscape is a React page (frontend/src/landscape); it loads its data from
    /api/landscape. Texts and the user come with the page."""
    return render(
        request,
        "app_page.html",
        title=translate(_language(request), "nav.landscape"),
        app="landscape",
        boot=_boot(request, user),
        wide=True,
    )


def _boot(request: Request, user: User, **extra: Any) -> dict[str, Any]:
    """Start data of a React page: language, all texts, the user and the time zone of
    schedules."""
    language = _language(request)
    return {
        "language": language,
        "texts": all_texts(language),
        "user": {"name": user.username, "role": user.role},
        "timezone": get_context(request).settings.timezone,
    } | extra


@pages.get("/operations/templates")
async def templates_page(request: Request, user: CurrentUser) -> HTMLResponse:
    """Operation templates (admin): list and composer, a React page (frontend/src/templates)."""
    return render(
        request,
        "app_page.html",
        title=translate(_language(request), "tpl.title"),
        app="templates",
        boot=_boot(request, user),
        wide=True,
    )


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
    host_activity = (await _active_by_host(session)).get(host.id)
    return render(request, "host.html", host=host, active=active, host_activity=host_activity)


@pages.post("/hosts/{host_id}/{operation}")
async def operate_host(
    host_id: int, operation: str, context: Context, session: DbSession, user: CurrentUser
) -> Response:
    """Run a host operation of the catalog, e.g. a discovery, as an activity."""
    definition = context.catalog.get(operation)
    if definition is None or definition.custom or TargetType.HOST not in definition.targets:
        raise HTTPException(404, "Unknown operation")
    if not has_role(user, definition.role):
        raise HTTPException(403, f"Requires the role {definition.role}")
    try:
        activity = await context.runner.submit_host(
            session, host_id, definition.kind, requested_by=user.username
        )
    except TargetNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except OperationConflictError:
        return redirect(f"/hosts/{host_id}", error="host_busy")
    return redirect(f"/activities/{activity.id}")


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


# --- custom operations: run ------------------------------------------------------------

RUN_KINDS = {
    "instances": TargetType.INSTANCE,
    "systems": TargetType.SYSTEM,
    "hosts": TargetType.HOST,
}
PARAMETER_PREFIX = "param."


async def _form_parameters(request: Request) -> dict[str, str]:
    """Parameter values from fields named ``param.<NAME>``; unchecked boxes are missing."""
    form = await request.form()
    return {
        str(key)[len(PARAMETER_PREFIX) :]: str(value)
        for key, value in form.multi_items()
        if str(key).startswith(PARAMETER_PREFIX)
    }


async def _run_target(
    request: Request, session: AsyncSession, user: User, kind: str, target_id: int, key: str
) -> tuple[OperationDef, TargetType, str, str]:
    """The custom operation, the target type, the target's label and its page."""
    target_type = RUN_KINDS.get(kind)
    definition = get_context(request).catalog.get(key)
    if (
        target_type is None
        or definition is None
        or definition.custom is None
        or target_type not in definition.targets
    ):
        raise HTTPException(404, "Unknown operation")
    if not has_role(user, definition.role):
        raise HTTPException(403, f"Requires the role {definition.role}")
    if target_type == TargetType.INSTANCE:
        instance = await session.scalar(
            select(Instance)
            .where(Instance.id == target_id)
            .options(selectinload(Instance.host), selectinload(Instance.sid))
        )
        if instance is None:
            raise HTTPException(404, "Instance not found")
        if not definition.applies_to(instance):
            raise HTTPException(404, "The operation does not apply to this instance")
        page = (
            f"/systems/{instance.system_id}" if instance.system_id else f"/hosts/{instance.host_id}"
        )
        return definition, target_type, instance.label, page
    if target_type == TargetType.SYSTEM:
        system = await session.get(System, target_id)
        if system is None:
            raise HTTPException(404, "System not found")
        return definition, target_type, system.name, f"/systems/{target_id}"
    host = await session.get(Host, target_id)
    if host is None:
        raise HTTPException(404, "Host not found")
    return definition, target_type, host.name, f"/hosts/{target_id}"


@pages.get("/run/{kind}/{target_id}/{key}")
async def run_form(
    request: Request, kind: str, target_id: int, key: str, session: DbSession, user: CurrentUser
) -> HTMLResponse:
    """Parameters of a custom operation before it runs; prefilled with the defaults."""
    definition, target_type, label, page = await _run_target(
        request, session, user, kind, target_id, key
    )
    return render(
        request,
        "run.html",
        operation=definition.custom,
        target_type=target_type.value,
        target_label=label,
        back=page,
        action=f"/run/{kind}/{target_id}/{key}",
    )


@pages.post("/run/{kind}/{target_id}/{key}")
async def run_submit(
    request: Request,
    kind: str,
    target_id: int,
    key: str,
    context: Context,
    session: DbSession,
    user: CurrentUser,
) -> Response:
    definition, target_type, _, page = await _run_target(
        request, session, user, kind, target_id, key
    )
    assert definition.custom is not None
    try:
        values = parameter_values(definition.custom, await _form_parameters(request))
    except ParameterError as exc:
        return redirect(
            f"/run/{kind}/{target_id}/{key}", error="parameter_values", e_detail=str(exc)
        )
    try:
        activity = await context.runner.submit_custom(
            session,
            definition.custom,
            target_type,
            target_id,
            values,
            requested_by=user.username,
        )
    except NotAssignedError:
        return redirect(page, error="not_assigned")
    except TargetNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except OperationConflictError:
        return redirect(page, error="conflict")
    return redirect(f"/activities/{activity.id}")


# --- activities -------------------------------------------------------------------------

LOG_ENTRIES_SHOWN = 200


@pages.get("/activities")
async def activities(request: Request, session: DbSession, tab: str | None = None) -> HTMLResponse:
    tab = _tab(tab, ("activities", "logs"))
    if tab == "activities":
        rows = list(await session.scalars(select(Activity).order_by(Activity.id.desc()).limit(100)))
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
async def activity_detail(
    request: Request, activity_id: int, session: DbSession, user: CurrentUser
) -> HTMLResponse:
    """A React page (frontend/src/activity) that follows the activity live; its data comes
    from /api/activities/{id}."""
    if await session.get(Activity, activity_id) is None:
        raise HTTPException(404, "Activity not found")
    return render(
        request,
        "app_page.html",
        title=f"{translate(_language(request), 'activity.title')} {activity_id}",
        app="activity",
        boot=_boot(request, user, activity_id=activity_id),
        wide=True,
    )


ACTIVITY_STATUS_ORDER = (
    "RUNNING",
    "PENDING",
    "SUCCEEDED",
    "PARTIAL",
    "FAILED",
    "CANCELLED",
)


# --- configuration ----------------------------------------------------------------------


@pages.get("/configuration")
async def configuration(tab: str | None = None) -> Response:
    """The configuration starts with the hosts; ``tab`` keeps old links working."""
    if tab == "operations":
        return RedirectResponse("/operations/custom", status_code=303)
    tab = _tab(tab, ("hosts", "systems", "pools"))
    return RedirectResponse(f"/configuration/{tab}", status_code=303)


@pages.get("/configuration/hosts")
async def configuration_hosts(request: Request, session: DbSession) -> HTMLResponse:
    return render(request, "config_hosts.html", hosts=await _all_hosts(session))


@pages.get("/configuration/hosts/new")
async def new_host(request: Request) -> HTMLResponse:
    return render(request, "config_host_new.html")


@pages.get("/configuration/systems")
async def configuration_systems(request: Request, session: DbSession) -> HTMLResponse:
    systems = await _all_systems(session)
    return render(
        request,
        "config_systems.html",
        tab="overview",
        systems=systems,
        unassigned_count=len(await unassigned_instances(session)),
        depends={s.id: depends_on_sids(s) for s in systems},
    )


@pages.get("/configuration/systems/unassigned")
async def configuration_unassigned(request: Request, session: DbSession) -> HTMLResponse:
    unassigned = await unassigned_instances(session)
    return render(
        request,
        "config_systems.html",
        tab="unassigned",
        systems=await _all_systems(session),
        unassigned=unassigned,
        unassigned_count=len(unassigned),
        suggested=await suggestions(session),
    )


@pages.get("/configuration/pools")
async def configuration_pools(request: Request, session: DbSession) -> HTMLResponse:
    return render(request, "config_pools.html", pools=await _all_pools(session))


@pages.get("/configuration/pools/new")
async def new_pool(request: Request) -> HTMLResponse:
    return render(request, "config_pool_new.html")


@pages.get("/operations")
async def operations_area() -> Response:
    return RedirectResponse("/operations/custom", status_code=303)


# --- configuration: custom operations ---------------------------------------------------

OPERATIONS_PAGE = "/operations/custom"
# Context parameters the tool passes along with the own ones (see customspec.py).
INSTANCE_CONTEXT = (
    "SAPSYSTEMNAME",
    "SAPSYSTEM",
    "SAPLOCALHOST",
    "PHYSICAL_HOSTNAME",
    "SERVICE_CLASS",
    "SERVICE_SUBTYPE",
)
HOST_CONTEXT = ("PHYSICAL_HOSTNAME", "RealHostName", "OS_TYPE", "OS_VERSION", "CPUS", "MEMAV")


def _operation_choices() -> dict[str, Any]:
    return {
        "custom_targets": ("INSTANCE", "SYSTEM", "HOST"),
        "orderings": ORDERINGS,
        "parameter_types": PARAMETER_TYPES,
        "instance_types": TYPE_VALUES,
        "roles": [role.value for role in ROLE_ORDER],
    }


async def _load_custom(session: AsyncSession, operation_id: int) -> CustomOperation:
    operation = await session.get(CustomOperation, operation_id)
    if operation is None:
        raise HTTPException(404, "Custom operation not found")
    return operation


class OperationForm:
    """Fields of the form for a custom operation."""

    def __init__(
        self,
        key: Annotated[str, Form()] = "",
        name: Annotated[str, Form()] = "",
        description: Annotated[str, Form()] = "",
        agent_operation: Annotated[str, Form()] = "",
        target: Annotated[str, Form()] = "INSTANCE",
        instance_type: Annotated[list[str], Form()] = [],  # noqa: B006 - FastAPI copies it
        ordering: Annotated[str, Form()] = "parallel",
        role: Annotated[str, Form()] = "configurator",
        mass: Annotated[bool, Form()] = False,
        timeout_seconds: Annotated[int, Form()] = 600,
    ) -> None:
        self.values = {
            "key": key,
            "name": name,
            "description": description,
            "agent_operation": agent_operation,
            "target": target,
            "instance_types": instance_type,
            "ordering": ordering,
            "role": role,
            "mass": mass,
            "timeout_seconds": timeout_seconds,
        }


class ParameterForm:
    def __init__(
        self,
        name: Annotated[str, Form()] = "",
        label: Annotated[str, Form()] = "",
        type: Annotated[str, Form()] = "STRING",
        required: Annotated[bool, Form()] = False,
        default: Annotated[str, Form()] = "",
        pattern: Annotated[str, Form()] = "",
        help: Annotated[str, Form()] = "",
    ) -> None:
        self.values = {
            "name": name,
            "label": label,
            "type": type,
            "required": required,
            "default": default,
            "pattern": pattern,
            "help": help,
        }


@pages.post("/operations/custom")
async def create_custom_operation(
    context: Context, session: DbSession, form: Annotated[OperationForm, Depends()]
) -> Response:
    try:
        operation = await save_operation(session, None, **form.values)
    except CustomOperationError as exc:
        await session.rollback()
        return redirect(OPERATIONS_PAGE, error=exc.code)
    await session.commit()
    await context.catalog.reload(session)
    return redirect(f"/operations/custom/{operation.id}", notice="operation_saved")


@pages.get("/operations/custom")
async def custom_operations(request: Request, session: DbSession) -> HTMLResponse:
    operations = await session.scalars(select(CustomOperation).order_by(CustomOperation.key))
    return render(request, "operations_custom.html", operations=list(operations))


@pages.get("/operations/custom/new")
async def new_custom_operation(request: Request) -> HTMLResponse:
    return render(request, "operations_custom_new.html", **_operation_choices())


@pages.get("/operations/custom/{operation_id}")
async def custom_operation_detail(
    request: Request, operation_id: int, session: DbSession
) -> HTMLResponse:
    operation = await _load_custom(session, operation_id)
    context = INSTANCE_CONTEXT if operation.target != "HOST" else HOST_CONTEXT
    return render(
        request,
        "operations_custom_detail.html",
        operation=operation,
        context_parameters=context,
        **_operation_choices(),
    )


@pages.post("/operations/custom/{operation_id}")
async def update_custom_operation(
    operation_id: int,
    context: Context,
    session: DbSession,
    form: Annotated[OperationForm, Depends()],
) -> Response:
    page = f"/operations/custom/{operation_id}"
    operation = await _load_custom(session, operation_id)
    try:
        await save_operation(session, operation, **form.values)
    except CustomOperationError as exc:
        await session.rollback()
        return redirect(page, error=exc.code)
    await session.commit()
    await context.catalog.reload(session)
    return redirect(page, notice="operation_saved")


@pages.post("/operations/custom/{operation_id}/delete")
async def delete_custom_operation(
    operation_id: int, context: Context, session: DbSession
) -> Response:
    await session.delete(await _load_custom(session, operation_id))
    await session.commit()
    await context.catalog.reload(session)
    return redirect(OPERATIONS_PAGE, notice="operation_deleted")


async def _save_parameter(
    context: AppContext,
    session: AsyncSession,
    operation_id: int,
    parameter_id: int | None,
    form: ParameterForm,
) -> Response:
    page = f"/operations/custom/{operation_id}"
    operation = await _load_custom(session, operation_id)
    parameter = None
    if parameter_id is not None:
        parameter = await session.get(CustomOperationParameter, parameter_id)
        if parameter is None or parameter.operation_id != operation_id:
            raise HTTPException(404, "Parameter not found")
    try:
        await save_parameter(session, operation, parameter, **form.values)
    except CustomOperationError as exc:
        await session.rollback()
        return redirect(page, error=exc.code)
    await session.commit()
    await context.catalog.reload(session)
    return redirect(page, notice="parameter_saved")


@pages.post("/operations/custom/{operation_id}/parameters")
async def add_custom_parameter(
    operation_id: int,
    context: Context,
    session: DbSession,
    form: Annotated[ParameterForm, Depends()],
) -> Response:
    return await _save_parameter(context, session, operation_id, None, form)


@pages.post("/operations/custom/{operation_id}/parameters/{parameter_id}")
async def update_custom_parameter(
    operation_id: int,
    parameter_id: int,
    context: Context,
    session: DbSession,
    form: Annotated[ParameterForm, Depends()],
) -> Response:
    return await _save_parameter(context, session, operation_id, parameter_id, form)


@pages.post("/operations/custom/{operation_id}/parameters/{parameter_id}/delete")
async def delete_custom_parameter(
    operation_id: int, parameter_id: int, context: Context, session: DbSession
) -> Response:
    parameter = await session.get(CustomOperationParameter, parameter_id)
    if parameter is None or parameter.operation_id != operation_id:
        raise HTTPException(404, "Parameter not found")
    await session.delete(parameter)
    await session.commit()
    session.expire_all()
    await context.catalog.reload(session)
    return redirect(f"/operations/custom/{operation_id}", notice="parameter_deleted")


def _optional_int(value: str, minimum: int, maximum: int) -> int | None:
    if not value.strip():
        return None
    number = int(value)
    if not minimum <= number <= maximum:
        raise ValueError(value)
    return number


NEW_HOST = "/configuration/hosts/new"


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
        return redirect(NEW_HOST, error="invalid_input")
    if not name or len(name) > 255:
        return redirect(NEW_HOST, error="invalid_input")
    if bool(username) != bool(password):
        return redirect(NEW_HOST, error="credentials_incomplete")
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
        return redirect(NEW_HOST, error="host_exists")
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
    return redirect("/configuration/hosts", notice="host_deleted")


SYSTEMS_PAGE = "/configuration/systems"
UNASSIGNED_PAGE = "/configuration/systems/unassigned"
NEW_SYSTEM = "/configuration/systems/new"


def _change_error(target: str, exc: SystemChangeError | UnknownPoolError) -> Response:
    code = exc.code if isinstance(exc, SystemChangeError) else "unknown_pool"
    return redirect(target, error=f"system_{code}" if code != "unknown_pool" else "invalid_input")


@pages.get("/configuration/systems/new")
async def new_system(
    request: Request,
    session: DbSession,
    instance_id: Annotated[list[int], Query()] = [],  # noqa: B006 - FastAPI copies it
) -> Response:
    """A new system from unassigned instances; those selected on the list come preselected."""
    unassigned = await unassigned_instances(session)
    selected = [i for i in unassigned if i.id in set(instance_id)]
    pools = list(await session.scalars(select(Pool).order_by(Pool.name)))
    return render(
        request,
        "config_system_new.html",
        instances=unassigned,
        selected={i.id for i in selected},
        suggested_name=suggest_name(selected) if selected else "",
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
        return _change_error(NEW_SYSTEM, exc)
    await session.commit()
    return redirect(f"{SYSTEMS_PAGE}/{system.id}", notice="system_created", n_name=system.name)


@pages.post("/configuration/systems/assign")
async def assign_selected(
    session: DbSession,
    system_id: Annotated[str, Form()] = "",
    instance_id: Annotated[list[int], Form()] = [],  # noqa: B006 - FastAPI copies it
) -> Response:
    """Assign the selected unassigned instances to an existing system."""
    if not system_id.isdigit():
        return redirect(UNASSIGNED_PAGE, error="system_unknown_system")
    try:
        await assign_instances(session, int(system_id), instance_id)
    except SystemChangeError as exc:
        await session.rollback()
        return _change_error(UNASSIGNED_PAGE, exc)
    await session.commit()
    return redirect(UNASSIGNED_PAGE, notice="instances_assigned")


@pages.post("/configuration/systems/{system_id}/assign/{instance_id}")
async def assign_suggested(system_id: int, instance_id: int, session: DbSession) -> Response:
    """Accept the suggestion for one unassigned instance."""
    try:
        await assign_instances(session, system_id, [instance_id])
    except SystemChangeError as exc:
        await session.rollback()
        return _change_error(UNASSIGNED_PAGE, exc)
    await session.commit()
    return redirect(UNASSIGNED_PAGE, notice="instances_assigned")


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
    return redirect(SYSTEMS_PAGE, notice="system_deleted")


@pages.post("/configuration/systems/{system_id}/instances/{instance_id}/remove")
async def remove_instance(
    system_id: int, instance_id: int, session: DbSession, back: Annotated[str, Form()] = ""
) -> Response:
    target = f"/configuration/systems/{system_id}" if back == "system" else SYSTEMS_PAGE
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
        return redirect("/configuration/pools/new", error="invalid_input")
    session.add(Pool(name=name, description=description.strip() or None))
    try:
        await session.commit()
    except IntegrityError:
        return redirect("/configuration/pools/new", error="pool_exists")
    return redirect("/configuration/pools", notice="pool_created")


@pages.post("/configuration/pools/{pool_id}/delete")
async def delete_pool(pool_id: int, session: DbSession) -> Response:
    pool = await session.get(Pool, pool_id)
    if pool is None:
        raise HTTPException(404, "Pool not found")
    await session.delete(pool)
    await session.commit()
    return redirect("/configuration/pools", notice="pool_deleted")


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
