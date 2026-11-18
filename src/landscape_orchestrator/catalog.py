"""Catalog of the operations the tool offers.

Each operation is described once: which targets it applies to, which role it needs and
whether it may run as a mass operation. Menus, mass operations and the API are built from
this catalog. Besides the built-in operations it holds the custom operations defined by an
admin (see customops.py); they run scripts configured in the host agent's operations.d,
like LaMa's custom operations, and appear everywhere without further changes.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .customspec import CustomSpec, ParameterSpec, spec_applies
from .models import CustomOperation, Instance
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
    custom: CustomSpec | None = None

    def label_key(self, target: str) -> str:
        """Translation key, e.g. "op.start_system"; for custom operations their name, which
        the translation returns unchanged."""
        if self.custom is not None:
            return self.custom.name
        return f"op.{self.key}_system" if target == TargetType.SYSTEM else f"op.{self.key}"

    def applies_to(self, instance: Instance | None) -> bool:
        """Whether an instance has one of the instance types of a custom operation."""
        if self.custom is None or instance is None:
            return True
        return spec_applies(self.custom, instance)


_SYSTEM_AND_INSTANCE = frozenset({TargetType.SYSTEM, TargetType.INSTANCE})

BUILT_IN: tuple[OperationDef, ...] = (
    OperationDef("start", OperationKind.START, _SYSTEM_AND_INSTANCE, Role.OPERATOR),
    OperationDef("stop", OperationKind.STOP, _SYSTEM_AND_INSTANCE, Role.OPERATOR, danger=True),
    OperationDef(
        "restart", OperationKind.RESTART, _SYSTEM_AND_INSTANCE, Role.OPERATOR, danger=True
    ),
    OperationDef(
        "discover", OperationKind.DISCOVER, frozenset({TargetType.HOST}), Role.CONFIGURATOR
    ),
)
BUILT_IN_KEYS = frozenset(op.key for op in BUILT_IN)
# Kept for code that only needs the built-in operations.
CATALOG = BUILT_IN


def spec_of(operation: CustomOperation) -> CustomSpec:
    return CustomSpec(
        id=operation.id,
        name=operation.name,
        description=operation.description,
        agent_operation=operation.agent_operation,
        target=operation.target,
        instance_types=tuple(t for t in operation.instance_types.split(",") if t),
        ordering=operation.ordering,
        timeout_seconds=operation.timeout_seconds,
        parameters=tuple(
            ParameterSpec(p.name, p.label, p.type, p.required, p.default, p.pattern, p.help)
            for p in operation.parameters
        ),
    )


def definition_of(operation: CustomOperation) -> OperationDef:
    return OperationDef(
        key=operation.key,
        kind=OperationKind.CUSTOM,
        targets=frozenset({TargetType(operation.target)}),
        role=Role(operation.role),
        mass=operation.mass,
        custom=spec_of(operation),
    )


class Catalog:
    """The built-in operations plus the custom operations of one application.

    The custom operations are read from the database at startup and after each change.
    """

    def __init__(self) -> None:
        self._custom: tuple[OperationDef, ...] = ()

    async def reload(self, session: AsyncSession) -> None:
        rows = await session.scalars(select(CustomOperation).order_by(CustomOperation.name))
        self._custom = tuple(definition_of(op) for op in rows)

    def all(self) -> tuple[OperationDef, ...]:
        return BUILT_IN + self._custom

    def get(self, key: str) -> OperationDef | None:
        return next((op for op in self.all() if op.key == key), None)

    def for_target(
        self, target: TargetType | str, user=None, *, mass: bool = False, instance=None
    ) -> list[OperationDef]:
        """Operations for a target type; with ``user`` only those the user may run, with
        ``instance`` only those that apply to its instance type."""
        return [
            op
            for op in self.all()
            if target in op.targets
            and (not mass or op.mass)
            and (user is None or has_role(user, op.role))
            and op.applies_to(instance)
        ]


_BUILT_IN_CATALOG = Catalog()


def get(key: str) -> OperationDef | None:
    """A built-in operation."""
    return _BUILT_IN_CATALOG.get(key)


def for_target(target: TargetType | str, user=None, *, mass: bool = False) -> list[OperationDef]:
    """Built-in operations for a target type."""
    return _BUILT_IN_CATALOG.for_target(target, user, mass=mass)
