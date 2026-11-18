"""Catalog of the operations the tool offers.

Each operation is described once: which targets it applies to, which role it needs and
whether it may run as a mass operation. Menus, mass operations and the API are built from
this catalog, so operations added later (e.g. scripts run by the host agent through
ExecuteOperation, as LaMa's custom operations) appear everywhere without further changes.
"""

from __future__ import annotations

from dataclasses import dataclass

from .operations import OperationKind, TargetType
from .users import Role, has_role


@dataclass(frozen=True)
class OperationDef:
    key: str
    kind: OperationKind
    targets: frozenset[TargetType]
    role: Role
    mass: bool = True
    # Stops or restarts something; shown in red.
    danger: bool = False

    def label_key(self, target: TargetType) -> str:
        """Translation key, e.g. "op.start_system" for whole systems."""
        return f"op.{self.key}_system" if target == TargetType.SYSTEM else f"op.{self.key}"


_SYSTEM_AND_INSTANCE = frozenset({TargetType.SYSTEM, TargetType.INSTANCE})

CATALOG: tuple[OperationDef, ...] = (
    OperationDef("start", OperationKind.START, _SYSTEM_AND_INSTANCE, Role.OPERATOR),
    OperationDef("stop", OperationKind.STOP, _SYSTEM_AND_INSTANCE, Role.OPERATOR, danger=True),
    OperationDef(
        "restart", OperationKind.RESTART, _SYSTEM_AND_INSTANCE, Role.OPERATOR, danger=True
    ),
    OperationDef(
        "discover", OperationKind.DISCOVER, frozenset({TargetType.HOST}), Role.CONFIGURATOR
    ),
)
_BY_KEY = {operation.key: operation for operation in CATALOG}


def get(key: str) -> OperationDef | None:
    return _BY_KEY.get(key)


def for_target(target: TargetType, user=None, *, mass: bool = False) -> list[OperationDef]:
    """Operations for a target type; with ``user`` only those the user may run."""
    return [
        operation
        for operation in CATALOG
        if target in operation.targets
        and (not mass or operation.mass)
        and (user is None or has_role(user, operation.role))
    ]
