"""Server-rendered web interface.

Pages are plain HTML without external assets, so the interface also works in networks
without internet access. State is changed only by POST forms.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from ..connections import ClientFactory
from ..crypto import SecretBox
from ..discovery import DiscoveryError, discover_host
from ..models import Activity, Database, Host, Instance, System
from ..operations import (
    ACTIVE_STATUSES,
    OperationConflictError,
    OperationKind,
    OperationRunner,
    TargetNotFoundError,
)
from ..settings import Settings
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
    context = {
        "t": t,
        "language": language,
        "languages": LANGUAGES,
        "path": request.url.path,
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
    return RedirectResponse(f"{path}?{query}" if query else path, status_code=303)


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


# --- overview ---------------------------------------------------------------------------


async def _active_by_system(session: AsyncSession) -> dict[int, Activity]:
    rows = await session.scalars(select(Activity).where(Activity.status.in_(ACTIVE_STATUSES)))
    return {activity.system_id: activity for activity in rows if activity.system_id}


@pages.get("/")
async def overview(request: Request, session: DbSession) -> HTMLResponse:
    systems = list(
        await session.scalars(
            select(System)
            .order_by(System.sid)
            .options(selectinload(System.instances).selectinload(Instance.host))
        )
    )
    databases = list(
        await session.scalars(
            select(Database).order_by(Database.name).options(selectinload(Database.host))
        )
    )
    active = await _active_by_system(session)
    return render(
        request,
        "overview.html",
        systems=systems,
        databases=databases,
        active=active,
        refresh=OVERVIEW_REFRESH_SECONDS if active else None,
    )


# --- hosts ------------------------------------------------------------------------------


@pages.get("/hosts")
async def hosts(request: Request, session: DbSession) -> HTMLResponse:
    rows = list(await session.scalars(select(Host).order_by(Host.name)))
    return render(request, "hosts.html", hosts=rows)


def _optional_int(value: str, minimum: int, maximum: int) -> int | None:
    if not value.strip():
        return None
    number = int(value)
    if not minimum <= number <= maximum:
        raise ValueError(value)
    return number


@pages.post("/hosts")
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
        return redirect("/hosts", error="invalid_input")
    if not name or len(name) > 255:
        return redirect("/hosts", error="invalid_input")
    if bool(username) != bool(password):
        return redirect("/hosts", error="credentials_incomplete")
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
        return redirect("/hosts", error="host_exists")
    return redirect(f"/hosts/{host.id}", notice="host_created")


async def _load_host(session: AsyncSession, host_id: int) -> Host:
    host = await session.scalar(
        select(Host)
        .where(Host.id == host_id)
        .options(
            selectinload(Host.instances).selectinload(Instance.system),
            selectinload(Host.databases),
        )
    )
    if host is None:
        raise HTTPException(404, "Host not found")
    return host


@pages.get("/hosts/{host_id}")
async def host_detail(request: Request, host_id: int, session: DbSession) -> HTMLResponse:
    return render(request, "host.html", host=await _load_host(session, host_id))


@pages.post("/hosts/{host_id}/discover")
async def discover(host_id: int, context: Context, session: DbSession) -> Response:
    host = await _load_host(session, host_id)
    try:
        result = await discover_host(session, host, context.clients)
    except DiscoveryError as exc:
        await session.commit()  # keep the "unreachable" marker on the host
        return redirect(f"/hosts/{host_id}", error="discovery_failed", e_detail=str(exc))
    await session.commit()
    return redirect(
        f"/hosts/{host_id}",
        notice="discovered",
        n_added=len(result.instances_added) + len(result.databases_added),
        n_updated=len(result.instances_updated) + len(result.databases_updated),
        n_missing=len(result.instances_missing) + len(result.databases_missing),
        warning=result.warnings,
    )


@pages.post("/hosts/{host_id}/credentials")
async def set_host_credentials(
    host_id: int,
    context: Context,
    session: DbSession,
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
) -> Response:
    host = await _load_host(session, host_id)
    if not username.strip() or not password:
        return redirect(f"/hosts/{host_id}", error="credentials_incomplete")
    host.username = username.strip()
    host.password_encrypted = context.secret_box.encrypt(password)
    await session.commit()
    return redirect(f"/hosts/{host_id}", notice="credentials_saved")


@pages.post("/hosts/{host_id}/delete")
async def delete_host(host_id: int, session: DbSession) -> Response:
    host = await _load_host(session, host_id)
    await session.delete(host)
    await session.commit()
    return redirect("/hosts", notice="host_deleted")


# --- systems and operations -------------------------------------------------------------


async def _load_system(session: AsyncSession, system_id: int) -> System:
    system = await session.scalar(
        select(System)
        .where(System.id == system_id)
        .options(selectinload(System.instances).selectinload(Instance.host))
    )
    if system is None:
        raise HTTPException(404, "System not found")
    return system


@pages.get("/systems/{system_id}")
async def system_detail(request: Request, system_id: int, session: DbSession) -> HTMLResponse:
    system = await _load_system(session, system_id)
    active = (await _active_by_system(session)).get(system.id)
    refresh = OVERVIEW_REFRESH_SECONDS if active else None
    return render(request, "system.html", system=system, active=active, refresh=refresh)


@pages.post("/systems/{system_id}/credentials")
async def set_system_credentials(
    system_id: int,
    context: Context,
    session: DbSession,
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
) -> Response:
    system = await _load_system(session, system_id)
    if not username.strip() or not password:
        return redirect(f"/systems/{system_id}", error="credentials_incomplete")
    system.username = username.strip()
    system.password_encrypted = context.secret_box.encrypt(password)
    await session.commit()
    return redirect(f"/systems/{system_id}", notice="credentials_saved")


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
    except OperationConflictError:
        return redirect(f"/systems/{instance.system_id}", error="conflict")
    return redirect(f"/activities/{activity.id}")


# --- activities -------------------------------------------------------------------------


@pages.get("/activities")
async def activities(request: Request, session: DbSession) -> HTMLResponse:
    rows = list(await session.scalars(select(Activity).order_by(Activity.id.desc()).limit(100)))
    return render(request, "activities.html", activities=rows)


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


# --- registration -----------------------------------------------------------------------


def register_web(app: FastAPI, context: AppContext) -> None:
    app.state.context = context

    @app.exception_handler(LoginRequiredError)
    async def to_login(_: Request, exc: LoginRequiredError) -> Response:
        return redirect("/login", next=exc.next_path if exc.next_path != "/" else None)

    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    app.include_router(public)
    app.include_router(pages)
