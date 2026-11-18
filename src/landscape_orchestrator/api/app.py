"""REST API of the orchestrator."""

# No "from __future__ import annotations" here: FastAPI resolves the annotations of the
# route functions at runtime, and they refer to names local to create_app.

import asyncio
import secrets
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Literal

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .. import __version__
from ..catalog import Catalog
from ..connections import ClientFactory
from ..crypto import SecretBox
from ..customops import (
    CustomOperationError,
    ParameterError,
    parameter_values,
    save_operation,
    save_parameter,
)
from ..db import create_engine, create_session_factory, upgrade_database
from ..dependencies import DependencyError, set_dependencies
from ..discovery import DiscoveryError, discover_host
from ..hosts import Connection, HostChangeError, update_connection
from ..models import (
    Activity,
    ApiToken,
    CustomOperation,
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
    DEFAULT_TIMEOUT_SECONDS,
    NotActiveError,
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
    suggestions,
    unassign_instance,
    update_system,
)
from ..tags import TagError, matches, parse_tags, replace_tags
from ..users import (
    UserError,
    create_token,
    create_user,
    has_role,
    set_password,
    token_user,
    update_user,
    user_count,
)
from ..web import AppContext, register_web
from .schemas import (
    ActivityDetail,
    ActivityOut,
    Credentials,
    CustomOperationIn,
    CustomOperationOut,
    DependenciesIn,
    DiscoveryOut,
    HostCreate,
    HostDetail,
    HostOut,
    HostUpdate,
    InstanceIds,
    InstanceListOut,
    InstanceOut,
    MassOperationIn,
    NewTokenOut,
    OperationOut,
    PasswordReset,
    PoolAssignment,
    PoolCreate,
    PoolOut,
    RefreshOut,
    RunIn,
    SidOut,
    SystemCreate,
    SystemOut,
    SystemUpdate,
    TagsIn,
    TokenCreate,
    TokenOut,
    UserCreate,
    UserOut,
    UserUpdate,
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
    catalog = Catalog()
    runner = OperationRunner(
        session_factory, clients, poll_interval=poll_interval, sleep=sleep, clock=clock
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await upgrade_database(engine, settings.database_url)
        await runner.recover_interrupted()
        async with session_factory() as session:
            await catalog.reload(session)
            if await user_count(session) == 0:
                # One-time code for creating the first admin on the page /setup.
                code = "-".join(secrets.token_hex(3).upper() for _ in range(3))
                app.state.setup_code = code
                print(
                    f"No user exists yet. Open /setup and create the first admin with the "
                    f"setup code {code}",
                    file=sys.stderr,
                    flush=True,
                )
        yield
        await runner.shutdown()
        await engine.dispose()

    bearer = HTTPBearer(auto_error=False)

    async def get_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    Session = Annotated[AsyncSession, Depends(get_session)]

    async def require_user(
        request: Request,
        session: Session,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> User:
        """The user of the personal API token, if the route allows the user's role."""
        user = await token_user(session, credentials.credentials if credentials else "")
        if user is None:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "Missing, invalid or expired API token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        await session.commit()  # records when the token was last used
        route = request.scope.get("route")
        needed = required_role(request.method, getattr(route, "path", request.url.path))
        if not has_role(user, needed):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Requires the role {needed}")
        request.state.user = user
        return user

    CurrentUser = Annotated[User, Depends(require_user)]

    app = FastAPI(
        title="Landscape Orchestrator",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    # The interface description under /api/docs is readable without a token; it contains
    # no inventory data. Every call to a route needs a personal API token, and the role of
    # its user must allow the route (see permissions.py).
    api = FastAPI(
        title="Landscape Orchestrator API",
        version=__version__,
        dependencies=[Depends(require_user)],
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

    @api.patch("/hosts/{host_id}", response_model=HostOut)
    async def update_host(host_id: int, body: HostUpdate, session: Session) -> Host:
        """Change connection data. Not checked against the host; run a discovery afterwards."""
        host = await load_host(session, host_id)
        try:
            await update_connection(session, host, Connection(**body.model_dump()))
        except HostChangeError as exc:
            await session.rollback()
            code = {"busy": 409, "host_exists": 409}.get(exc.code, 422)
            raise HTTPException(code, str(exc)) from exc
        await session.commit()
        return host

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
    async def update_system_route(system_id: int, body: SystemUpdate, session: Session) -> System:
        await change(session, update_system(session, system_id, body.name, body.description))
        return await load_system(session, system_id)

    @api.delete("/systems/{system_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_system_route(system_id: int, session: Session) -> Response:
        """Delete a system. Its instances stay in the inventory, unassigned."""
        await change(session, delete_system(session, system_id))
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @api.post("/systems/{system_id}/instances", response_model=SystemOut)
    async def assign_instances_route(system_id: int, body: InstanceIds, session: Session) -> System:
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
        user: CurrentUser,
        timeout: Timeout = DEFAULT_TIMEOUT_SECONDS,
    ) -> Activity:
        """Start, stop or restart one instance through the host agent."""
        kind = OperationKind(operation.upper())
        return await submit(
            runner.submit_instance(session, instance_id, kind, timeout, user.username)
        )

    @api.post(
        "/systems/{system_id}/{operation}",
        response_model=ActivityOut,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def operate_system(
        system_id: int,
        operation: OperationName,
        session: Session,
        user: CurrentUser,
        timeout: Timeout = DEFAULT_TIMEOUT_SECONDS,
    ) -> Activity:
        """Start, stop or restart a whole system in the order of the start priorities."""
        kind = OperationKind(operation.upper())
        return await submit(runner.submit_system(session, system_id, kind, timeout, user.username))

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
            .options(selectinload(Activity.entries), selectinload(Activity.children))
        )
        if activity is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Activity not found")
        return activity

    @api.get("/operations", response_model=list[OperationOut])
    async def list_operations(user: CurrentUser) -> list[dict[str, object]]:
        """The operations of the catalog this user may run, custom ones with parameters."""
        return [
            {
                "key": operation.key,
                "name": operation.custom.name if operation.custom else None,
                "targets": sorted(operation.targets),
                "role": operation.role.value,
                "mass": operation.mass,
                "custom": operation.custom is not None,
                "parameters": [p.__dict__ for p in operation.custom.parameters]
                if operation.custom
                else [],
            }
            for operation in catalog.all()
            if has_role(user, operation.role)
        ]

    # --- custom operations ----------------------------------------------------------------

    def custom_out(operation: CustomOperation) -> dict[str, object]:
        return {
            "id": operation.id,
            "key": operation.key,
            "name": operation.name,
            "description": operation.description,
            "agent_operation": operation.agent_operation,
            "target": operation.target,
            "instance_types": [t for t in operation.instance_types.split(",") if t],
            "ordering": operation.ordering,
            "role": operation.role,
            "mass": operation.mass,
            "timeout_seconds": operation.timeout_seconds,
            "parameters": [
                {
                    "name": p.name,
                    "label": p.label,
                    "type": p.type,
                    "required": p.required,
                    "default": p.default,
                    "pattern": p.pattern,
                    "help": p.help,
                }
                for p in operation.parameters
            ],
        }

    async def save_custom(
        session: AsyncSession, operation: CustomOperation | None, body: CustomOperationIn
    ) -> CustomOperation:
        try:
            operation = await save_operation(
                session,
                operation,
                key=body.key,
                name=body.name,
                description=body.description,
                agent_operation=body.agent_operation,
                target=body.target,
                instance_types=body.instance_types,
                ordering=body.ordering,
                role=body.role,
                mass=body.mass,
                timeout_seconds=body.timeout_seconds,
            )
            # The parameters are replaced as a whole, in the given order.
            operation.parameters.clear()
            await session.flush()
            for parameter in body.parameters:
                await save_parameter(session, operation, None, **parameter.model_dump())
        except CustomOperationError as exc:
            await session.rollback()
            code = 409 if exc.code == "operation_key_taken" else 422
            raise HTTPException(code, str(exc)) from exc
        await session.commit()
        await catalog.reload(session)
        return operation

    async def load_custom(session: AsyncSession, operation_id: int) -> CustomOperation:
        operation = await session.get(CustomOperation, operation_id)
        if operation is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Custom operation not found")
        return operation

    @api.get("/custom-operations", response_model=list[CustomOperationOut])
    async def list_custom_operations(session: Session) -> list[dict[str, object]]:
        rows = await session.scalars(select(CustomOperation).order_by(CustomOperation.key))
        return [custom_out(op) for op in rows]

    @api.post(
        "/custom-operations",
        response_model=CustomOperationOut,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_custom_operation(
        body: CustomOperationIn, session: Session
    ) -> dict[str, object]:
        """Define a custom operation (admin). It runs the script configured under
        ``agent_operation`` in operations.d of the host agent."""
        return custom_out(await save_custom(session, None, body))

    @api.get("/custom-operations/{operation_id}", response_model=CustomOperationOut)
    async def get_custom_operation(operation_id: int, session: Session) -> dict[str, object]:
        return custom_out(await load_custom(session, operation_id))

    @api.put("/custom-operations/{operation_id}", response_model=CustomOperationOut)
    async def update_custom_operation(
        operation_id: int, body: CustomOperationIn, session: Session
    ) -> dict[str, object]:
        operation = await load_custom(session, operation_id)
        return custom_out(await save_custom(session, operation, body))

    @api.delete("/custom-operations/{operation_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_custom_operation(operation_id: int, session: Session) -> Response:
        await session.delete(await load_custom(session, operation_id))
        await session.commit()
        await catalog.reload(session)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    async def run_custom(
        session: AsyncSession,
        user: User,
        target_type: TargetType,
        target_id: int,
        key: str,
        body: RunIn,
        timeout: int | None,
    ) -> Activity:
        definition = catalog.get(key)
        if definition is None or definition.custom is None or target_type not in definition.targets:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No custom operation {key} here")
        if not has_role(user, definition.role):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Requires the role {definition.role}")
        if target_type == TargetType.INSTANCE:
            instance = await session.scalar(select(Instance).where(Instance.id == target_id))
            if instance is not None and not definition.applies_to(instance):
                raise HTTPException(
                    422, f"{definition.custom.name} does not apply to this instance"
                )
        try:
            values = parameter_values(definition.custom, body.parameters)
        except ParameterError as exc:
            raise HTTPException(422, str(exc)) from exc
        return await submit(
            runner.submit_custom(
                session,
                definition.custom,
                target_type,
                target_id,
                values,
                timeout,
                user.username,
            )
        )

    CustomTimeout = Annotated[int | None, Query(ge=10, le=86400)]

    @api.post(
        "/instances/{instance_id}/operations/{key}",
        response_model=ActivityOut,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def run_on_instance(
        instance_id: int,
        key: str,
        session: Session,
        user: CurrentUser,
        body: RunIn = RunIn(),  # noqa: B008
        timeout: CustomTimeout = None,
    ) -> Activity:
        """Run a custom operation on an instance."""
        return await run_custom(session, user, TargetType.INSTANCE, instance_id, key, body, timeout)

    @api.post(
        "/systems/{system_id}/operations/{key}",
        response_model=ActivityOut,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def run_on_system(
        system_id: int,
        key: str,
        session: Session,
        user: CurrentUser,
        body: RunIn = RunIn(),  # noqa: B008
        timeout: CustomTimeout = None,
    ) -> Activity:
        """Run a custom operation on the matching instances of a system."""
        return await run_custom(session, user, TargetType.SYSTEM, system_id, key, body, timeout)

    @api.post(
        "/hosts/{host_id}/operations/{key}",
        response_model=ActivityOut,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def run_on_host(
        host_id: int,
        key: str,
        session: Session,
        user: CurrentUser,
        body: RunIn = RunIn(),  # noqa: B008
        timeout: CustomTimeout = None,
    ) -> Activity:
        """Run a custom operation on a host."""
        return await run_custom(session, user, TargetType.HOST, host_id, key, body, timeout)

    @api.post("/activities/{activity_id}/cancel", response_model=ActivityDetail)
    async def cancel_activity(activity_id: int, session: Session, user: CurrentUser) -> Activity:
        """Cancel a running activity. Start and stop only stop waiting; custom operations are
        cancelled on the host agent; a mass operation cancels its running children."""
        try:
            await runner.cancel(session, activity_id, user.username)
            return await get_activity(activity_id, session)
        except TargetNotFoundError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        except NotActiveError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    @api.post("/mass-operations", response_model=ActivityOut, status_code=status.HTTP_202_ACCEPTED)
    async def create_mass_operation(
        body: MassOperationIn,
        session: Session,
        user: CurrentUser,
        timeout: CustomTimeout = None,
    ) -> Activity:
        """Run an operation on many targets; returns the parent activity with the children."""
        definition = catalog.get(body.operation)
        target_type = TargetType(body.target_type)
        if definition is None or target_type not in definition.targets or not definition.mass:
            raise HTTPException(422, f"No mass operation {body.operation} on {target_type}")
        if not has_role(user, definition.role):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Requires the role {definition.role}")
        values = None
        if definition.custom is not None:
            try:
                values = parameter_values(definition.custom, body.parameters)
            except ParameterError as exc:
                raise HTTPException(422, str(exc)) from exc
        default_timeout = (
            definition.custom.timeout_seconds if definition.custom else DEFAULT_TIMEOUT_SECONDS
        )
        try:
            return await runner.submit_mass(
                session,
                definition.kind,
                target_type,
                body.target_ids,
                parallel=body.parallel,
                stop_on_error=body.stop_on_error,
                timeout_seconds=timeout or default_timeout,
                requested_by=user.username,
                custom=definition.custom,
                parameters=values,
            )
        except TargetNotFoundError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    # --- own account and users ------------------------------------------------------------

    def user_error(exc: UserError) -> HTTPException:
        code = {"username_taken": 409, "last_admin": 409}.get(exc.code, 422)
        return HTTPException(code, str(exc))

    async def load_user(session: AsyncSession, user_id: int) -> User:
        user = await session.get(User, user_id)
        if user is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
        return user

    @api.get("/me", response_model=UserOut)
    async def me(user: CurrentUser) -> User:
        return user

    @api.get("/me/tokens", response_model=list[TokenOut])
    async def my_tokens(user: CurrentUser, session: Session) -> list[ApiToken]:
        query = select(ApiToken).where(ApiToken.user_id == user.id).order_by(ApiToken.id)
        return list(await session.scalars(query))

    @api.post("/me/tokens", response_model=NewTokenOut, status_code=status.HTTP_201_CREATED)
    async def create_my_token(
        body: TokenCreate, user: CurrentUser, session: Session
    ) -> dict[str, object]:
        """Create a token; its value is returned only in this response."""
        try:
            token, plain = await create_token(session, user, body.name, body.expires_at)
        except UserError as exc:
            raise user_error(exc) from exc
        await session.commit()
        return {"token": plain, "info": token}

    @api.delete("/me/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_my_token(token_id: int, user: CurrentUser, session: Session) -> Response:
        token = await session.get(ApiToken, token_id)
        if token is None or token.user_id != user.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Token not found")
        await session.delete(token)
        await session.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @api.get("/users", response_model=list[UserOut])
    async def list_users(session: Session) -> list[User]:
        return list(await session.scalars(select(User).order_by(User.username)))

    @api.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
    async def create_user_route(body: UserCreate, session: Session) -> User:
        """Create a user with a start password that must be changed at the first sign-in."""
        try:
            user = await create_user(
                session,
                body.username,
                body.password,
                body.role,
                display_name=body.display_name,
                must_change_password=True,
            )
        except UserError as exc:
            raise user_error(exc) from exc
        await session.commit()
        return user

    @api.patch("/users/{user_id}", response_model=UserOut)
    async def update_user_route(user_id: int, body: UserUpdate, session: Session) -> User:
        user = await load_user(session, user_id)
        try:
            await update_user(
                session,
                user,
                role=body.role,
                active=body.active,
                display_name=body.display_name,
            )
        except UserError as exc:
            await session.rollback()
            raise user_error(exc) from exc
        await session.commit()
        return user

    @api.put("/users/{user_id}/password", status_code=status.HTTP_204_NO_CONTENT)
    async def reset_password(user_id: int, body: PasswordReset, session: Session) -> Response:
        """Set a start password; the user must change it at the next sign-in."""
        user = await load_user(session, user_id)
        try:
            set_password(user, body.password, must_change=True)
        except UserError as exc:
            raise user_error(exc) from exc
        await session.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    app.state.runner = runner
    app.state.catalog = catalog
    app.state.setup_code = None
    app.state.session_factory = session_factory
    app.mount("/api", api)
    register_web(app, AppContext(settings, secret_box, clients, session_factory, runner, catalog))
    return app
