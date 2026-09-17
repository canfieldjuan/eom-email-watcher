"""The generic workflow definition model for the Automate host.

This is the host's own closed, strict definition vocabulary. It deliberately mirrors the
discipline of :mod:`eom_email_watcher.automation.rules` (every model forbids extras and is
strict, and canonical bytes are produced with sorted keys and fixed separators) rather than
importing it: the mail-watcher rule model is coupled to ``mail.message`` triggers, mailbox
identity, and attachment fan-out, and the two products ship independently. A workflow here
is provider-agnostic and business-agnostic: it names abstract stages, conditions, and
effect kinds, never a vendor.

Slice 2 supports one trigger source (``operator.decision``) and two effect kinds
(``record.transition`` and ``overlay.set``). The vocabulary is a closed enum so a later
slice adds a member deliberately, and the canonical bytes give a stable identity that
Slice 4's signed pack format signs over.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .store import OVERLAY_SET, RECORD_TRANSITION

MAX_DEFINITION_BYTES = 16 * 1024
MAX_CONDITIONS = 8
MAX_EFFECTS = 8
MAX_DEFINITIONS = 64
MAX_STAGES = 64

_Name = Annotated[str, Field(strict=True, min_length=1, max_length=80)]
_Stage = Annotated[str, Field(strict=True, min_length=1, max_length=80)]
_OverlayKey = Annotated[str, Field(strict=True, min_length=1, max_length=80)]

# Closed condition vocabulary. Slice 2 gates a decision on the record's current stage; a
# later slice adds fields deliberately, each with its own allowed operators.
ConditionField = Literal["record.stage"]
ConditionOp = Literal["equals", "in"]
ALLOWED_OPS: dict[str, frozenset[str]] = {
    "record.stage": frozenset({"equals", "in"}),
}


class DefinitionError(ValueError):
    """Raised when a workflow definition fails to parse or validate."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Condition(_Strict):
    field: ConditionField
    op: ConditionOp
    value: str | list[str]

    @model_validator(mode="after")
    def _validate(self) -> Condition:
        if self.op not in ALLOWED_OPS[self.field]:
            raise ValueError(f"op {self.op} is not permitted for {self.field}")
        if self.op == "equals":
            if not isinstance(self.value, str):
                raise ValueError("equals requires a string value")
        else:  # "in"
            if not isinstance(self.value, list) or not self.value:
                raise ValueError("in requires a non-empty list of values")
            if len(self.value) != len(set(self.value)):
                raise ValueError("in requires distinct values")
        return self


class Trigger(_Strict):
    source_kind: Literal["operator.decision"]
    decision: _Name


class RecordTransitionEffect(_Strict):
    kind: Literal["record.transition"]
    to_stage: _Stage


class OverlaySetEffect(_Strict):
    kind: Literal["overlay.set"]
    key: _OverlayKey
    value: str | int | bool


Effect = Annotated[
    RecordTransitionEffect | OverlaySetEffect,
    Field(discriminator="kind"),
]


class WorkflowDefinition(_Strict):
    name: _Name
    trigger: Trigger
    conditions: list[Condition] = Field(default_factory=list, max_length=MAX_CONDITIONS)
    effects: list[Effect] = Field(min_length=1, max_length=MAX_EFFECTS)

    @model_validator(mode="after")
    def _validate(self) -> WorkflowDefinition:
        transitions = [effect for effect in self.effects if effect.kind == RECORD_TRANSITION]
        if len(transitions) > 1:
            raise ValueError("a definition may carry at most one record.transition effect")
        overlay_keys = [effect.key for effect in self.effects if effect.kind == OVERLAY_SET]
        if len(overlay_keys) != len(set(overlay_keys)):
            raise ValueError("overlay.set keys must be distinct within a definition")
        return self


class Workflow(_Strict):
    name: _Name
    stages: list[_Stage] = Field(min_length=1, max_length=MAX_STAGES)
    initial_stage: _Stage
    definitions: list[WorkflowDefinition] = Field(min_length=1, max_length=MAX_DEFINITIONS)

    @model_validator(mode="after")
    def _validate(self) -> Workflow:
        if len(self.stages) != len(set(self.stages)):
            raise ValueError("stages must be distinct")
        stage_set = set(self.stages)
        if self.initial_stage not in stage_set:
            raise ValueError("initial_stage must be one of stages")
        names = [definition.name for definition in self.definitions]
        if len(names) != len(set(names)):
            raise ValueError("definition names must be distinct")
        for definition in self.definitions:
            for effect in definition.effects:
                if effect.kind == RECORD_TRANSITION and effect.to_stage not in stage_set:
                    raise ValueError(
                        f"definition {definition.name!r} transitions to unknown stage "
                        f"{effect.to_stage!r}"
                    )
        return self


def canonical_workflow(workflow: Workflow) -> bytes:
    """Materialize defaults and encode the contracted canonical JSON bytes.

    Defaults are materialized (not dropped) and keys are sorted with fixed separators so a
    logically identical workflow always yields the same bytes. Slice 4's signed pack format
    signs over exactly these bytes.
    """
    raw = json.dumps(
        workflow.model_dump(mode="json", exclude_defaults=False),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    if len(raw) > MAX_DEFINITION_BYTES:
        raise DefinitionError("canonical workflow exceeds 16384 bytes")
    return raw


def parse_workflow(raw: bytes | str) -> Workflow:
    """Parse and strictly validate a workflow definition from JSON."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DefinitionError(f"invalid workflow JSON: {exc}") from exc
    try:
        return Workflow.model_validate(data)
    except ValidationError as exc:
        raise DefinitionError(str(exc)) from exc
