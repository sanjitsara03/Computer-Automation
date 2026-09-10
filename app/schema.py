from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _must_compile(pattern: str) -> str:
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"invalid regex {pattern!r}: {exc}") from exc
    return pattern


#target descriptor

class PrimaryTarget(Strict):
    role: str
    name: str


class FallbackTarget(Strict):
    css: str
    kind: Literal["test_id", "attribute", "generated", "positional"]


class LastTarget(Strict):
    point: tuple[int, int]
    viewport: tuple[int, int]
    anchor_text: str


class TargetDescriptor(Strict):
    primary: PrimaryTarget | None = None
    primary_null_reason: str | None = None
    fallback: FallbackTarget | None = None
    last: LastTarget | None = None

    @model_validator(mode="after")
    def _tiers(self) -> "TargetDescriptor":
        if (self.primary is None) == (self.primary_null_reason is None):
            raise ValueError("exactly one of primary / primary_null_reason must be set")
        if self.primary is None and self.fallback is None and self.last is None:
            raise ValueError("descriptor has no usable tier")
        return self


#step values


class LiteralValue(Strict):
    literal: str


class ParamRef(Strict):
    param: str


class SecretRef(Strict):
    secret: str


Value = Union[LiteralValue, ParamRef, SecretRef]


#checkpoints


class RoleVisible(Strict):
    kind: Literal["role_visible"]
    role: str
    name: str


class TextMatches(Strict):
    kind: Literal["text_matches"]
    pattern: str

    _compiles = field_validator("pattern")(_must_compile)


class UrlMatches(Strict):
    kind: Literal["url_matches"]
    pattern: str

    _compiles = field_validator("pattern")(_must_compile)


class ValueMatches(Strict):
    """Acted-on field holds the entered value."""

    kind: Literal["value_matches"]


Checkpoint = Annotated[
    Union[RoleVisible, TextMatches, UrlMatches, ValueMatches],
    Field(discriminator="kind"),
]


class Parse(Strict):
    pattern: str

    _compiles = field_validator("pattern")(_must_compile)


#steps

_STEP_SHAPE: dict[str, dict[str, bool]] = {
    "navigate": {"path": True, "target": False, "value": False, "output": False},
    "type": {"path": False, "target": True, "value": True, "output": False},
    "select": {"path": False, "target": True, "value": True, "output": False},
    "click": {"path": False, "target": True, "value": False, "output": False},
    "extract": {"path": False, "target": True, "value": False, "output": True},
}


class Step(Strict):
    id: str
    action: Literal["navigate", "type", "select", "click", "extract"]
    path: str | None = None
    target: TargetDescriptor | None = None
    value: Value | None = None
    output: str | None = None
    parse: Parse | None = None
    checkpoint: Checkpoint
    timeout_ms: int = 10000
    risk: Literal["safe", "irreversible", "destructive"] = "safe"

    @model_validator(mode="after")
    def _shape(self) -> "Step":
        for field, required in _STEP_SHAPE[self.action].items():
            if (getattr(self, field) is not None) != required:
                state = "required" if required else "not allowed"
                raise ValueError(f"step {self.id}: '{field}' is {state} for {self.action}")
        if (self.parse is not None) != (self.action == "extract"):
            raise ValueError(f"step {self.id}: 'parse' belongs to extract steps only")
        return self


#capability contract


class ParamSpec(Strict):
    type: Literal["str", "enum"] = "str"
    enum: list[str] | None = None
    secret: bool = False

    @model_validator(mode="after")
    def _enum_shape(self) -> "ParamSpec":
        if (self.enum is not None) != (self.type == "enum"):
            raise ValueError("'enum' values are required iff type is 'enum'")
        return self


class OutputSpec(Strict):
    type: Literal["str"] = "str"
    pattern: str | None = None

    _compiles = field_validator("pattern")(lambda p: p if p is None else _must_compile(p))


class Surface(Strict):
    kind: Literal["web"]
    entry_path: str


class Artifact(Strict):
    schema_version: Literal[1]
    name: str
    version: int
    goal: str
    surface: Surface
    params: dict[str, ParamSpec]
    outputs: dict[str, OutputSpec]
    steps: list[Step]

    @model_validator(mode="after")
    def _contract(self) -> "Artifact":
        ids = [s.id for s in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("step ids must be unique")

        declared_plain = {k for k, p in self.params.items() if not p.secret}
        declared_secret = {k for k, p in self.params.items() if p.secret}
        used_plain = {s.value.param for s in self.steps if isinstance(s.value, ParamRef)}
        used_secret = {s.value.secret for s in self.steps if isinstance(s.value, SecretRef)}

        if used_plain - declared_plain:
            raise ValueError(f"undeclared (or secret-mismatched) params: {used_plain - declared_plain}")
        if used_secret - declared_secret:
            raise ValueError(f"undeclared (or secret-mismatched) secrets: {used_secret - declared_secret}")
        unused = (declared_plain - used_plain) | (declared_secret - used_secret)
        if unused:
            raise ValueError(f"declared params never used by any step: {unused}")

        extracted = {s.output for s in self.steps if s.action == "extract"}
        if extracted != set(self.outputs):
            raise ValueError(
                f"outputs/extract mismatch: declared {set(self.outputs)}, extracted {extracted}"
            )
        return self


def load_artifact(path: str | Path) -> Artifact:
    return Artifact.model_validate_json(Path(path).read_text())
