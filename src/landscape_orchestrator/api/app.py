"""REST API of the orchestrator."""

# No "from __future__ import annotations" here: FastAPI resolves the annotations of the
# route functions at runtime, and they refer to names local to create_app.

import asyncio
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Literal

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .. import __version__
from ..connections import ClientFactory
from ..crypto import SecretBox
from ..db import create_engine, create_session_factory, create_tables
from ..dependencies import DependencyError, set_dependencies
from ..discovery import DiscoveryError, discover_host
from ..models import Activity, Host, HostTag, Instance, Pool, Sid, System, SystemTag
from ..operations import (
    DEFAULT_TIMEOUT_SECONDS,
    NotAssignedError,
    OperationConflictError,
    OperationKind,
    OperationRunner,
    TargetNotFoundError,
)
from ..pools import UnknownPoolError, assign_pool
from ..settings import Settings
from ..status import refresh_system
from ..systems import (
    SystemChangeError,
    assign_instances,
    create_system,
    delete_system,
    suggestions,
    unassign_instance,
    update_system,
)
from ..tags import TagError, matches, parse_tags, replace_tags
from ..web import AppContext, register_web
from .schemas import (
    ActivityDetail,
    ActivityOut,
    Credentials,
    DependenciesIn,
    DiscoveryOut,
    HostCreate,
    HostDetail,
    HostOut,
    InstanceIds,
    InstanceListOut,
    InstanceOut,
    PoolAssignment,
    PoolCreate,
    PoolOut,
    RefreshOut,
    SidOut,
    SystemCreate,
    SystemOut,
    SystemUpdate,
    TagsIn,
)

# Status codes of SystemChangeError.code; everything else is 422.
_CHANGE_STATUS = {"unknown_system": 404, "name_taken": 409, "assigned": 409, "busy": 409}


def _systems_query():
    return (
        select(System)
        .order_by(System.name)
        .options(
            selectinload(System.instances).selectinload(Instance.sid),
            selectinload(System.depends_on),
            selectinload(System.required_by),
            selectinload(System.pool),
        )
    )


def create_app(
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    poll_interval: float = 2.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> FastAPI:
    """Build the application.

    ``transport`` replaces the network for connections to managed hosts; it is meant for
    tests and for running against the simulated landscape. ``poll_interval``, ``sleep``
    and ``clock`` control how running operations are tracked.
    """
    secret_box = SecretBox(settings.secret_key)
    clients = ClientFactory(secret_box, transport=transport)
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    runner = OperationRunner(
        session_factory, clients, poll_interval=poll_interval, sleep=sleep, clock=clock
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await create_tables(engine)
        await runner.recover_interrupted()
        yield
        await runner.shutdown()
        await engine.dispose()

    bearer = HTTPBearer(auto_error=False)

    async def require_token(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> None:
        token = credentials.credentials if credentials else ""
        if not secrets.compare_digest(token.encode(), settings.api_token.encode()):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "Missing or invalid API token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    async def get_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    Session = Annotated[AsyncSession, Depends(get_session)]

    app = FastAPI(
        title="Landscape Orchestrator",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    # The interface description under /api/docs is readable without a token; it contains
    # no inventory data. Every call to a route needs the token.
    api = FastAPI(
        title="Landscape Orchestrator API",
        version=__version__,
        dependencies=[Depends(require_token)],
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    async def load_host(session: AsyncSession, host_id: int) -> Host:
        host = await session.scalar(
            select(Host)
            .where(Host.id == host_id)
            .options(
                selectinload(Host.instances).selectinload(Instance.sid),
                selectinload(Host.databases),
            )
        )
        if host is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Host not found")
        return host

    TagFilter = Annotated[
        list[str],
        Query(description='Only entries with one of these tags, e.g. "dev" or "sla=1"'),
    ]

    def parsed_tags(body: TagsIn) -> dict[str, str]:
        try:
            return parse_tags(body.tags)
        except TagError as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.get("/hosts", response_model=list[HostOut])
    async def list_hosts(session: Session, tag: TagFilter = []) -> list[Host]:  # noqa: B006
        hosts = list(await session.scalars(select(Host).order_by(Host.name)))
        return [h for h in hosts if matches(h.tags, tag)] if tag else hosts

    @api.put("/hosts/{host_id}/tags", response_model=HostOut)
    async def set_host_tags(host_id: int, body: TagsIn, session: Session) -> Host:
        host = await load_host(session, host_id)
        replace_tags(host.tags, HostTag, parsed_tags(body))
        await session.commit()
        return host

    @api.post("/hosts", response_model=HostOut, status_code=status.HTTP_201_CREATED)
    async def create_host(body: HostCreate, session: Session) -> Host:
        if (body.username is None) != (body.password is None):
            raise HTTPException(
                422,
                "username and password must be given together",
            )
        host = Host(
            name=body.name,
            address=body.address or body.name,
            hostagent_port=body.hostagent_port,
            https=body.https,
            verify_tls=body.verify_tls,
            sapcontrol_port_offset=body.sapcontrol_port_offset,
            username=body.username,
            password_encrypted=secret_box.encrypt(body.password) if body.password else None,
        )
        host.agent_info = None  # nothing read yet; avoids a lazy load
        host.tags = []
        session.add(host)
        try:
            await session.commit()
        except IntegrityError as exc:
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"Host '{body.name}' already exists"
            ) from exc
        return host

    @api.get("/hosts/{host_id}", response_model=HostDetail)
    async def get_host(host_id: int, session: Session) -> Host:
        return await load_host(session, host_id)

    @api.put("/hosts/{host_id}/credentials", status_code=status.HTTP_204_NO_CONTENT)
    async def set_host_credentials(host_id: int, body: Credentials, session: Session) -> Response:
        host = await load_host(session, host_id)
        host.username = body.username
        host.password_encrypted = secret_box.encrypt(body.password)
        await session.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @api.delete("/hosts/{host_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_host(host_id: int, session: Session) -> Response:
        host = await load_host(session, host_id)
        await session.delete(host)
        await session.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @api.post("/hosts/{host_id}/discover", response_model=DiscoveryOut)
    async def discover(host_id: int, session: Session) -> object:
        host = await load_host(session, host_id)
        try:
            result = await discover_host(session, host, clients)
        except DiscoveryError as exc:
            await session.commit()  # keep the "unreachable" marker on the host
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY, f"Host agent could not be queried: {exc}"
            ) from exc
        await session.commit()
        return result

    async def load_system(session: AsyncSession, system_id: int) -> System:
        session.expire_all()
        system = await session.scalar(_systems_query().where(System.id == system_id))
        if system is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "System not found")
        return system

    async def change(session: AsyncSession, operation: Awaitable[object]) -> None:
        try:
            await operation
        except SystemChangeError as exc:
            await session.rollback()
            raise HTTPException(_CHANGE_STATUS.get(exc.code, 422), str(exc)) from exc
        except UnknownPoolError as exc:
            await session.rollback()
            raise HTTPException(422, str(exc)) from exc
        await session.commit()

    @api.get("/instances", response_model=list[InstanceListOut])
    async def list_instances(
        session: Session,
        unassigned: Annotated[
            bool, Query(description="Only instances that belong to no system")
        ] = False,
    ) -> list[dict[str, object]]:
        query = select(Instance).options(selectinload(Instance.sid)).order_by(Instance.id)
        if unassigned:
            query = query.where(Instance.system_id.is_(None))
        proposed = await suggestions(session)
        result = []
        for instance in await session.scalars(query):
            data = InstanceOut.model_validate(instance).model_dump()
            suggestion = proposed.get(instance.id)
            data["suggested_system_id"] = suggestion.id if suggestion else None
            result.append(data)
        return result

    @api.get("/sids", response_model=list[SidOut])
    async def list_sids(session: Session) -> list[Sid]:
        return list(await session.scalars(select(Sid).order_by(Sid.name)))

    @api.put("/sids/{sid_id}/credentials", status_code=status.HTTP_204_NO_CONTENT)
    async def set_sid_credentials(sid_id: int, body: Credentials, session: Session) -> Response:
        """Credentials of <sid>adm for the sapstartsrv of the instances of this SID."""
        sid = await session.get(Sid, sid_id)
        if sid is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "SID not found")
        sid.username = body.username
        sid.password_encrypted = secret_box.encrypt(body.password)
        await session.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @api.get("/systems", response_model=list[SystemOut])
    async def list_systems(session: Session, tag: TagFilter = []) -> list[System]:  # noqa: B006
        systems = list(await session.scalars(_systems_query()))
        return [s for s in systems if matches(s.tags, tag)] if tag else systems

    @api.put("/systems/{system_id}/tags", response_model=SystemOut)
    async def set_system_tags(system_id: int, body: TagsIn, session: Session) -> System:
        system = await load_system(session, system_id)
        replace_tags(system.tags, SystemTag, parsed_tags(body))
        await session.commit()
        return await load_system(session, system_id)

    @api.post("/systems/{system_id}/refresh", response_model=RefreshOut)
    async def refresh_system_route(system_id: int, session: Session) -> dict[str, object]:
        """Read the status of all instances of the system once. Creates no activity."""
        await load_system(session, system_id)
        warnings = await refresh_system(session, system_id, clients)
        await session.commit()
        return {"system": await load_system(session, system_id), "warnings": warnings}

    @api.post("/systems", response_model=SystemOut, status_code=status.HTTP_201_CREATED)
    async def create_system_route(body: SystemCreate, session: Session) -> System:
        """Create a system from unassigned instances, which may belong to several SIDs."""
        created: list[System] = []

        async def create() -> None:
            created.append(
                await create_system(
                    session,
                    body.name,
                    body.instance_ids,
                    pool_id=body.pool_id,
                    description=body.description,
                )
            )

        await change(session, create())
        return await load_system(session, created[0].id)

    @api.get("/systems/{system_id}", response_model=SystemOut)
    async def get_system(system_id: int, session: Session) -> System:
        return await load_system(session, system_id)

    @api.patch("/systems/{system_id}", response_model=SystemOut)
    async def update_system_route(
        system_id: int, body: SystemUpdate, session: Session
    ) -> System:
        await change(session, update_system(session, system_id, body.name, body.description))
        return await load_system(session, system_id)

    @api.delete("/systems/{system_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_system_route(system_id: int, session: Session) -> Response:
        """Delete a system. Its instances stay in the inventory, unassigned."""
        await change(session, delete_system(session, system_id))
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @api.post("/systems/{system_id}/instances", response_model=SystemOut)
    async def assign_instances_route(
        system_id: int, body: InstanceIds, session: Session
    ) -> System:
        await change(session, assign_instances(session, system_id, body.instance_ids))
        return await load_system(session, system_id)

    @api.delete(
        "/systems/{system_id}/instances/{instance_id}", status_code=status.HTTP_204_NO_CONTENT
    )
    async def unassign_instance_route(
        system_id: int, instance_id: int, session: Session
    ) -> Response:
        await change(session, unassign_instance(session, system_id, instance_id))
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @api.put("/systems/{system_id}/dependencies", response_model=SystemOut)
    async def set_system_dependencies(
        system_id: int, body: DependenciesIn, session: Session
    ) -> System:
        """Replace the systems this system depends on, e.g. its database system."""
        if await session.get(System, system_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "System not found")
        try:
            await set_dependencies(session, system_id, set(body.depends_on))
        except DependencyError as exc:
            raise HTTPException(422, str(exc)) from exc
        await session.commit()
        return await load_system(session, system_id)

    OperationName = Literal["start", "stop", "restart"]

    @api.get("/pools", response_model=list[PoolOut])
    async def list_pools(session: Session) -> list[Pool]:
        query = select(Pool).order_by(Pool.name).options(selectinload(Pool.systems))
        return list(await session.scalars(query))

    @api.post("/pools", response_model=PoolOut, status_code=status.HTTP_201_CREATED)
    async def create_pool(body: PoolCreate, session: Session) -> Pool:
        pool = Pool(name=body.name.strip(), description=body.description, systems=[])
        session.add(pool)
        try:
            await session.commit()
        except IntegrityError as exc:
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"Pool '{body.name}' already exists"
            ) from exc
        return pool

    @api.delete("/pools/{pool_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_pool(pool_id: int, session: Session) -> Response:
        pool = await session.get(Pool, pool_id)
        if pool is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Pool not found")
        await session.delete(pool)
        await session.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @api.put("/systems/{system_id}/pool", response_model=SystemOut)
    async def set_system_pool(system_id: int, body: PoolAssignment, session: Session) -> System:
        """Put a system into a pool; ``null`` takes it out."""
        if await session.get(System, system_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "System not found")
        try:
            await assign_pool(session, system_id, body.pool_id)
        except UnknownPoolError as exc:
            raise HTTPException(422, str(exc)) from exc
        await session.commit()
        return await load_system(session, system_id)

    Timeout = Annotated[
        int,
        Query(ge=10, le=7200, description="Seconds to wait for the target state"),
    ]

    async def submit(submission: Awaitable[Activity]) -> Activity:
        try:
            return await submission
        except TargetNotFoundError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        except (OperationConflictError, NotAssignedError) as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    @api.post(
        "/instances/{instance_id}/{operation}",
        response_model=ActivityOut,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def operate_instance(
        instance_id: int,
        operation: OperationName,
        session: Session,
        timeout: Timeout = DEFAULT_TIMEOUT_SECONDS,
    ) -> Activity:
        """Start, stop or restart one instance through the host agent."""
        kind = OperationKind(operation.upper())
        return await submit(runner.submit_instance(session, instance_id, kind, timeout))

    @api.post(
        "/systems/{system_id}/{operation}",
        response_model=ActivityOut,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def operate_system(
        system_id: int,
        operation: OperationName,
        session: Session,
        timeout: Timeout = DEFAULT_TIMEOUT_SECONDS,
    ) -> Activity:
        """Start, stop or restart a whole system in the order of the start priorities."""
        kind = OperationKind(operation.upper())
        return await submit(runner.submit_system(session, system_id, kind, timeout))

    @api.get("/activities", response_model=list[ActivityOut])
    async def list_activities(
        session: Session, limit: Annotated[int, Query(ge=1, le=500)] = 50
    ) -> list[Activity]:
        query = select(Activity).order_by(Activity.id.desc()).limit(limit)
        return list(await session.scalars(query))

    @api.get("/activities/{activity_id}", response_model=ActivityDetail)
    async def get_activity(activity_id: int, session: Session) -> Activity:
        activity = await session.scalar(
            select(Activity)
            .where(Activity.id == activity_id)
            .options(selectinload(Activity.entries))
        )
        if activity is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Activity not found")
        return activity

    app.state.runner = runner
    app.mount("/api", api)
    register_web(app, AppContext(settings, secret_box, clients, session_factory, runner))
    return app
