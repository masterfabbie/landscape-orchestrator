"""Which role a route needs. Shared by the REST API and the web interface.

Roles build on each other (viewer < operator < configurator < admin):

- viewer: read everything, refresh the status of a system, own account and preferences
- operator: also start, stop and restart instances and systems
- configurator: also change the configuration (hosts, discovery, systems, pools, tags,
  credentials)
- admin: also users and platform settings
"""

from __future__ import annotations

from .users import Role

OPERATION_ROUTES = frozenset(
    {"/instances/{instance_id}/{operation}", "/systems/{system_id}/{operation}"}
)


def required_role(method: str, path: str) -> Role:
    """``path`` is the route template, e.g. "/systems/{system_id}/{operation}", without the
    "/api" prefix for API routes."""
    if path.startswith(("/administration", "/users")):
        return Role.ADMIN
    if path.startswith(("/account", "/me")):
        return Role.VIEWER
    if path.startswith("/configuration"):
        return Role.CONFIGURATOR
    if method in ("GET", "HEAD"):
        return Role.VIEWER
    if path.endswith("/refresh") or path == "/landscape/filters":
        return Role.VIEWER
    if path in OPERATION_ROUTES:
        return Role.OPERATOR
    return Role.CONFIGURATOR
