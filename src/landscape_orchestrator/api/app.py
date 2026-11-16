"""REST API of the orchestrator."""

# No "from __future__ import annotations" here: FastAPI resolves the annotations of the
# route functions at runtime, and they refer to names local to create_app.

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, HTTPException, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .. import __version__
from ..connections import ClientFactory
from ..crypto import SecretBox
from ..db import create_engine, create_session_factory, create_tables
from ..discovery import DiscoveryError, discover_host
from ..models import Host, System
from ..settings import Settings
from .schemas import Credentials, DiscoveryOut, HostCreate, HostDetail, HostOut, SystemOut


def create_app(
    settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    """Build the application.

    ``transport`` replaces the network for connections to managed hosts; it is meant for
    tests and for running against the simulated landscape.
    """
    secret_box = SecretBox(settings.secret_key)
    clients = ClientFactory(secret_box, transport=transport)
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await create_tables(engine)
        yield
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
            .options(selectinload(Host.instances), selectinload(Host.databases))
        )
        if host is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Host not found")
        return host

    @api.get("/hosts", response_model=list[HostOut])
    async def list_hosts(session: Session) -> list[Host]:
        return list(await session.scalars(select(Host).order_by(Host.name)))

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

    @api.get("/systems", response_model=list[SystemOut])
    async def list_systems(session: Session) -> list[System]:
        query = select(System).order_by(System.sid).options(selectinload(System.instances))
        return list(await session.scalars(query))

    @api.put("/systems/{system_id}/credentials", status_code=status.HTTP_204_NO_CONTENT)
    async def set_system_credentials(
        system_id: int, body: Credentials, session: Session
    ) -> Response:
        system = await session.get(System, system_id)
        if system is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "System not found")
        system.username = body.username
        system.password_encrypted = secret_box.encrypt(body.password)
        await session.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    app.mount("/api", api)
    return app
