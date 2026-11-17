"""Role of an instance within a system, derived from the features sapstartsrv reports.

The role decides how system operations treat an instance:

- database and central services start first and in parallel,
- application servers start once both run, and stop first,
- diagnostics agents are not part of system operations at all.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    DATABASE = "database"
    CENTRAL = "central"
    APPLICATION = "application"
    AGENT = "agent"


def instance_role(features: str | None) -> Role:
    """Role for a feature list such as "MESSAGESERVER|ENQUE".

    Instances with unknown or missing features count as application servers, so they are
    started last and stopped first.
    """
    names = set((features or "").split("|"))
    if "SMDAGENT" in names:
        return Role.AGENT
    if "HDB" in names:
        return Role.DATABASE
    if names & {"MESSAGESERVER", "ENQUE", "ENQREP"}:
        return Role.CENTRAL
    return Role.APPLICATION
