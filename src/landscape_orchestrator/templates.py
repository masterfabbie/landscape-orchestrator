"""Operation templates: define and check them, and the parameter values of a run.

A template is a chain of operations of the catalog (built-in or custom) run one after
another on one system or host, like an operation template in LaMa. Each step has its own
parameter values (defaults that can be changed when the template runs) and its own
behavior on error. Only admins define templates (like custom operations); running one needs
its role, which is at least the highest role of its steps.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .catalog import BUILT_IN_KEYS, Catalog
from .customops import KEY_PATTERN, ParameterError, parameter_values
from .models import CustomOperation, OperationTemplate, TemplateStep
from .templatespec import ON_ERROR, SEPARATOR, TEMPLATE_TARGETS, TemplateSpec
from .users import ROLE_ORDER, Role

MAX_STEPS = 30


class TemplateError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


async def key_taken(session: AsyncSession, key: str, *, template_id: int | None = None) -> bool:
    """Templates and custom operations share one key space (both are catalog operations)."""
    if await session.scalar(select(CustomOperation.id).where(CustomOperation.key == key)):
        return True
    taken = await session.scalar(select(OperationTemplate.id).where(OperationTemplate.key == key))
    return taken is not None and taken != template_id


async def save_template(
    session: AsyncSession,
    catalog: Catalog,
    template: OperationTemplate | None,
    *,
    key: str,
    name: str,
    description: str | None,
    target: str,
    role: str,
    mass: bool,
    steps: Sequence[Mapping],
) -> OperationTemplate:
    """Create (``template`` None) or replace a template with its steps. The caller commits.

    ``steps``: dicts with ``operation`` (catalog key), ``parameters`` (name -> value) and
    ``on_error``.
    """
    key, name = key.strip(), name.strip()
    if not KEY_PATTERN.fullmatch(key) or key in BUILT_IN_KEYS:
        raise TemplateError(
            "template_key", "Key: lowercase letters, digits and _, not a built-in operation"
        )
    if await key_taken(session, key, template_id=template.id if template else None):
        raise TemplateError("template_key_taken", f"An operation {key} already exists")
    if not name or len(name) > 128:
        raise TemplateError("template_name", "The template needs a name")
    if target not in TEMPLATE_TARGETS:
        raise TemplateError("invalid_input", "A template runs on a system or a host")
    if role not in {r.value for r in ROLE_ORDER}:
        raise TemplateError("invalid_input", "Unknown role")
    if not steps:
        raise TemplateError("template_no_steps", "A template needs at least one step")
    if len(steps) > MAX_STEPS:
        raise TemplateError("invalid_input", f"At most {MAX_STEPS} steps")

    known = {op.key: op for op in catalog.steps()}
    needed = Role.VIEWER
    checked = []
    for position, step in enumerate(steps, start=1):
        operation = known.get(str(step.get("operation", "")))
        if operation is None or target not in operation.targets:
            raise TemplateError(
                "template_step", f"Step {position}: no operation {step.get('operation')} here"
            )
        on_error = str(step.get("on_error") or "stop")
        if on_error not in ON_ERROR or (on_error == "stop_and_start" and target != "SYSTEM"):
            raise TemplateError("invalid_input", f"Step {position}: unknown behavior on error")
        values = {str(k): str(v) for k, v in dict(step.get("parameters") or {}).items()}
        if operation.custom is not None:
            names = {p.name for p in operation.custom.parameters}
            if unknown := set(values) - names:
                raise TemplateError(
                    "template_step", f"Step {position}: unknown parameters {sorted(unknown)}"
                )
            # Only the format of the given values; a required one may be left for the run.
            loose = replace(
                operation.custom,
                parameters=tuple(replace(p, required=False) for p in operation.custom.parameters),
            )
            try:
                parameter_values(loose, {k: v for k, v in values.items() if v != ""})
            except ParameterError as exc:
                raise TemplateError("template_step", f"Step {position}: {exc}") from exc
        elif values:
            raise TemplateError("template_step", f"Step {position}: takes no parameters")
        if ROLE_ORDER.index(operation.role) > ROLE_ORDER.index(needed):
            needed = operation.role
        checked.append((position, operation.key, values, on_error))
    if ROLE_ORDER.index(Role(role)) < ROLE_ORDER.index(needed):
        raise TemplateError("template_role", f"The steps need at least the role {needed.value}")

    if template is None:
        template = OperationTemplate(created_at=datetime.now(UTC), steps=[])
        session.add(template)
    template.key = key
    template.name = name
    template.description = (description or "").strip() or None
    template.target = target
    template.role = role
    template.mass = mass
    template.steps.clear()
    await session.flush()
    for position, operation, values, on_error in checked:
        template.steps.append(
            TemplateStep(
                position=position,
                operation=operation,
                parameters=json.dumps(values),
                on_error=on_error,
            )
        )
    await session.flush()
    return template


def template_values(
    spec: TemplateSpec, given: Mapping[str, str | int | bool]
) -> dict[str, dict[str, str]]:
    """Values of a run per step (position -> PARAM-<name> -> value), checked like those of
    custom operations. ``given`` uses "<position>.<NAME>"; a parameter not given keeps the
    template's value, then the operation's default."""
    result: dict[str, dict[str, str]] = {}
    problems: dict[str, str] = {}
    for step in spec.steps:
        if step.custom is None:
            continue
        prefix = f"{step.position}{SEPARATOR}"
        values: dict[str, str | int | bool] = {k: v for k, v in step.parameters.items() if v != ""}
        values |= {k[len(prefix) :]: v for k, v in given.items() if k.startswith(prefix)}
        try:
            result[str(step.position)] = parameter_values(step.custom, values)
        except ParameterError as exc:
            problems |= {f"{prefix}{name}": text for name, text in exc.problems.items()}
    if problems:
        raise ParameterError(problems)
    return result


async def steps_using(session: AsyncSession, operation: str) -> list[str]:
    """Names of the templates that use an operation, e.g. before deleting it."""
    rows = await session.scalars(
        select(OperationTemplate.name)
        .join(TemplateStep, TemplateStep.template_id == OperationTemplate.id)
        .where(TemplateStep.operation == operation)
        .distinct()
    )
    return sorted(rows)
