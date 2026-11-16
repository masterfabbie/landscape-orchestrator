"""In-process access to the simulated landscape, without opening network ports."""

from __future__ import annotations

import httpx

from .apps import create_hostagent_app, create_sapcontrol_app
from .landscape import MockLandscape


class MockLandscapeTransport(httpx.AsyncBaseTransport):
    """Routes requests by port to the mock services, as the mock server would.

    The host part of the URL is ignored. Requests to a port without a service fail with
    a connection error, like a port nobody listens on.
    """

    def __init__(self, landscape: MockLandscape) -> None:
        self._transports: dict[int, httpx.ASGITransport] = {}
        for host in landscape.hosts:
            agent = create_hostagent_app(landscape, host.name)
            self._transports[host.hostagent_port] = httpx.ASGITransport(app=agent)
            for instance in host.instances:
                app = create_sapcontrol_app(landscape, host.name, instance.nr)
                port = host.sapcontrol_port(instance.nr)
                self._transports[port] = httpx.ASGITransport(app=app)

    def remove_port(self, port: int) -> None:
        """Simulate a service that is down."""
        del self._transports[port]

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        transport = self._transports.get(request.url.port or 80)
        if transport is None:
            raise httpx.ConnectError(f"Connection refused: port {request.url.port}")
        return await transport.handle_async_request(request)
