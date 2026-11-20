"""Description of an operation template: a chain of steps run one after another.

Kept free of the catalog and the runner, which both use it. A snapshot of the template
(``TemplateSpec.to_dict``) is stored with each activity that runs it, so later changes do
not affect running chains.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .customspec import CustomSpec, ParameterSpec

ON_ERROR = ("stop", "continue", "stop_and_start")
TEMPLATE_TARGETS = ("SYSTEM", "HOST")
BUILT_IN_NAMES = {"START": "Start", "STOP": "Stop", "RESTART": "Restart", "DISCOVER": "Discovery"}
# Parameters of a step are passed in a run as "<position>.<NAME>", e.g. "3.KRNLREL".
SEPARATOR = "."


@dataclass(frozen=True)
class StepSpec:
    position: int
    operation: str  # key of a built-in or custom operation
    kind: str  # START, STOP, RESTART, DISCOVER or CUSTOM
    on_error: str = "stop"
    # Default values of the custom operation's parameters (name -> value).
    parameters: dict[str, str] = field(default_factory=dict)
    custom: CustomSpec | None = None

    @property
    def name(self) -> str:
        """For the (English) log: the custom operation's name or e.g. "Stop"."""
        return self.custom.name if self.custom else BUILT_IN_NAMES.get(self.kind, self.operation)

    def label_key(self, target: str) -> str:
        """For the interface: a translation key like "op.stop_system", or the custom
        operation's name, which the translation returns unchanged."""
        if self.custom:
            return self.custom.name
        return f"op.{self.operation}_system" if target == "SYSTEM" else f"op.{self.operation}"

    def to_dict(self) -> dict:
        return {
            "position": self.position,
            "operation": self.operation,
            "kind": self.kind,
            "on_error": self.on_error,
            "parameters": dict(self.parameters),
            "custom": self.custom.to_dict() if self.custom else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> StepSpec:
        custom = data.get("custom")
        return cls(
            position=data["position"],
            operation=data["operation"],
            kind=data["kind"],
            on_error=data.get("on_error", "stop"),
            parameters=dict(data.get("parameters") or {}),
            custom=CustomSpec.from_dict(custom) if custom else None,
        )

    def run_parameters(self) -> list[ParameterSpec]:
        """The parameters a run asks for, named "<position>.<NAME>", with the template's
        values as defaults."""
        if self.custom is None:
            return []
        return [
            ParameterSpec(
                name=f"{self.position}{SEPARATOR}{p.name}",
                label=f"{self.position}. {self.custom.name}: {p.label}",
                type=p.type,
                required=p.required,
                default=self.parameters.get(p.name, p.default),
                pattern=p.pattern,
                help=p.help,
            )
            for p in self.custom.parameters
        ]


@dataclass(frozen=True)
class TemplateSpec:
    id: int
    key: str
    name: str
    description: str | None
    target: str  # SYSTEM or HOST
    steps: tuple[StepSpec, ...]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "target": self.target,
            "steps": [step.to_dict() for step in self.steps],
        }

    @classmethod
    def from_dict(cls, data: dict) -> TemplateSpec:
        return cls(
            id=data["id"],
            key=data["key"],
            name=data["name"],
            description=data.get("description"),
            target=data["target"],
            steps=tuple(StepSpec.from_dict(s) for s in data["steps"]),
        )

    def run_parameters(self) -> list[ParameterSpec]:
        return [p for step in self.steps for p in step.run_parameters()]
