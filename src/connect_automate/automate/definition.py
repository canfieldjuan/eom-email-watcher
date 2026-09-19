"""The generic workflow definition model for the Automate host.

This is the host's own closed, strict definition vocabulary. It deliberately mirrors the
discipline of a strict application rule model (every model forbids extras and is
strict, and canonical bytes are produced with sorted keys and fixed separators) rather than
importing one: an application's own rule model tends to be coupled to its own trigger,
identity, and payload concerns, and products ship independently. A workflow here
is provider-agnostic and business-agnostic: it names abstract stages, conditions, and
effect kinds, never a vendor.

Slice 2 supports one trigger source (``operator.decision``) and two effect kinds
(``record.transition`` and ``overlay.set``). The vocabulary is a closed enum so a later
slice adds a member deliberately, and the canonical bytes give a stable identity that
Slice 4's signed pack format signs over.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .actions import ACTION_KINDS, CONNECT_INVOKE
from .store import MAX_ACTIONS_PER_DECISION, MAX_EFFECTS_PER_BATCH, OVERLAY_SET, RECORD_TRANSITION

MAX_DEFINITION_BYTES = 16 * 1024
MAX_CONDITIONS = 8
# The per-definition effect cap is the store's per-batch cap: a definition's effects are
# applied as one batch, so the two must not drift.
MAX_EFFECTS = MAX_EFFECTS_PER_BATCH
# The per-definition cap on emitted actions is the store's per-decision cap, imported so the
# definition model and the store admission bound cannot drift.
MAX_ACTIONS = MAX_ACTIONS_PER_DECISION
MAX_DEFINITIONS = 64
MAX_STAGES = 64
# The maximum length of a name-like identifier (workflow name, definition name, trigger
# decision). Exported so the engine can reject an over-long decision (which can never match
# a trigger) before it is persisted as an unbounded operation name.
MAX_NAME_LENGTH = 80

_Name = Annotated[str, Field(strict=True, min_length=1, max_length=MAX_NAME_LENGTH)]
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


# The connect.invoke request shape a signed definition may declare. The invoker
# (:mod:`connect_automate.automate_connect`) is the exact Connect protocol enforcer at
# dispatch; these bounds fail a malformed pack fast at parse/sign time and keep the canonical
# bytes deterministic. The patterns intentionally mirror the Connect contract (hyphenated
# capability ids and parameter names, uuid4 identities) but are defined locally so this
# definition model stays decoupled from the transport module, which it must not import.
_CAPABILITY_ID_PATTERN = r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$"
_CAPABILITY_VERSION_PATTERN = r"^[0-9]+\.[0-9]+$"
_UUID4_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
MAX_CONNECT_PARAMETERS = 16
# content_base64 is capped well under the 16 KiB canonical-definition bound, which is the
# real ceiling on the whole request; a larger input artifact is a later, streamed concern.
_MAX_CONTENT_BASE64 = 12_000

_CapabilityId = Annotated[str, Field(strict=True, pattern=_CAPABILITY_ID_PATTERN, max_length=100)]
_CapabilityVersion = Annotated[str, Field(strict=True, pattern=_CAPABILITY_VERSION_PATTERN)]
_Uuid4 = Annotated[str, Field(strict=True, pattern=_UUID4_PATTERN)]
_MediaType = Annotated[str, Field(strict=True, min_length=1, max_length=127)]
_Filename = Annotated[str, Field(strict=True, min_length=1, max_length=255)]
_ContentBase64 = Annotated[str, Field(strict=True, max_length=_MAX_CONTENT_BASE64)]
_ParameterKey = Annotated[str, Field(strict=True, pattern=_CAPABILITY_ID_PATTERN, max_length=100)]


# The member of a value binding: ``{"overlay": "<overlay-key>"}`` takes a value from the
# record's overlay projection at admission. A literal parameter value is a bounded primitive
# (ADR-0002) and a literal input content is a base64 string, so an object-valued parameter or
# content field is unambiguously a binding. The key mirrors the overlay-key bound in the
# store's effect model.
OVERLAY_BINDING_KEY = "overlay"


class OverlayBinding(_Strict):
    overlay: Annotated[str, Field(strict=True, min_length=1, max_length=80)]


class ConnectInvokeCapability(_Strict):
    id: _CapabilityId
    version: _CapabilityVersion


class ConnectInvokeProvider(_Strict):
    instance_id: _Uuid4


class ConnectInvokeInput(_Strict):
    artifact_id: _Uuid4
    media_type: _MediaType
    filename: _Filename
    # A literal base64 string, or an OverlayBinding whose (string) overlay value is the raw
    # input content that the host UTF-8- then base64-encodes at admission, so a workflow can
    # assemble a payload in an overlay and send it as the input artifact.
    content_base64: _ContentBase64 | OverlayBinding = ""


class ConnectInvokeRequest(_Strict):
    """The signed connect.invoke request template a definition declares.

    ``provider`` is optional: omit it to let the host match any local provider offering the
    capability, or pin ``instance_id`` to bind one. ``parameters`` and ``confirmed`` carry the
    capability's bounded inputs and its confirmation flag.

    A parameter value, and the input artifact's content, are each either a literal or an
    :class:`OverlayBinding` (``{"overlay": key}``) resolved from the record's overlay projection
    at admission (see :func:`render_connect_invoke_request`), so a capability whose inputs vary
    per record (a specific lead id, a booking key, an assembled payload) is expressible while
    the signed template stays fixed.
    """

    capability: ConnectInvokeCapability
    input: ConnectInvokeInput
    provider: ConnectInvokeProvider | None = None
    parameters: dict[_ParameterKey, str | int | bool | OverlayBinding] = Field(
        default_factory=dict, max_length=MAX_CONNECT_PARAMETERS
    )
    confirmed: bool = False


class RequestBindingError(ValueError):
    """Raised when a connect.invoke request binds record state that is not available.

    This is a runtime resolution failure at admission (an overlay key a parameter binds is
    unset for the record), distinct from :class:`DefinitionError`, which is a parse/validate
    failure of the signed definition itself.
    """


def _resolve_overlay(binding: Mapping[str, object], overlays: Mapping[str, object], what: str):
    key = binding[OVERLAY_BINDING_KEY]
    if key not in overlays:
        raise RequestBindingError(f"connect.invoke {what} binds unset overlay {key!r}")
    return overlays[key]


def _is_binding(value: object) -> bool:
    return isinstance(value, Mapping) and OVERLAY_BINDING_KEY in value


def render_connect_invoke_request(
    request: Mapping[str, object], overlays: Mapping[str, object]
) -> dict[str, object]:
    """Resolve a connect.invoke request's overlay bindings against record state.

    Returns a new request in which each ``{"overlay": key}`` binding is replaced by the
    record's current overlay value for ``key``:

    - a bound *parameter* takes the overlay's scalar value directly;
    - a bound *input content* takes the overlay's string value as the raw input, which the host
      UTF-8- then base64-encodes into ``content_base64`` (so an overlay holds a plain payload,
      not base64).

    A request with no bindings is returned unchanged. Raises :class:`RequestBindingError` when
    a bound key is unset, or when a bound input content resolves to a non-string, so a decision
    that binds missing or unusable state fails cleanly rather than dispatching a request with a
    hole in it. The resolved request is what the outbox freezes, so a retry replays the resolved
    values, never re-resolves against later state (the queue-row-binding discipline).
    """
    result = dict(request)

    parameters = request.get("parameters")
    if isinstance(parameters, Mapping) and parameters:
        rendered: dict[str, object] = {}
        bound = False
        for name, value in parameters.items():
            if _is_binding(value):
                rendered[name] = _resolve_overlay(value, overlays, f"parameter {name!r}")
                bound = True
            else:
                rendered[name] = value
        if bound:
            result["parameters"] = rendered

    input_artifact = request.get("input")
    if isinstance(input_artifact, Mapping):
        content = input_artifact.get("content_base64")
        if _is_binding(content):
            raw = _resolve_overlay(content, overlays, "input content")
            if not isinstance(raw, str):
                raise RequestBindingError(
                    "connect.invoke input content binding must resolve to a string"
                )
            encoded = base64.b64encode(raw.encode("utf-8")).decode("ascii")
            result["input"] = {**input_artifact, "content_base64": encoded}

    return result


class ActionEmit(_Strict):
    """A side-effect action a definition emits when its decision applies.

    ``action`` is an abstract action kind from the shared :data:`ACTION_KINDS` vocabulary
    (``notify.local``, ``mail.send``, ``connect.invoke``, ...), never a vendor; the host
    resolves it to a configured adapter. ``request`` is the action's payload. Actions are
    distinct from record-ledger ``effects``: an effect mutates the record under the ledger
    transaction, while an action is dispatched through the durable outbox (at most once per
    dedupe key).

    A simple kind (``notify.local``) carries a flat scalar request. ``connect.invoke`` carries
    the structured :class:`ConnectInvokeRequest` template, validated here so a malformed pack
    is rejected at parse/sign time rather than at dispatch, and normalized so its canonical
    bytes are deterministic. A connect.invoke parameter value may be an :class:`OverlayBinding`
    resolved from record state when the action is admitted (see
    :func:`render_connect_invoke_request`); every other request is frozen as declared.

    ``request`` is typed loosely (``dict[str, Any]``) because a simple kind's flat scalars and
    connect.invoke's nested, possibly-bound shape do not share one field type; the per-kind
    validation below is the real contract, not the field annotation.
    """

    action: Annotated[str, Field(strict=True, min_length=1, max_length=MAX_NAME_LENGTH)]
    request: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate(self) -> ActionEmit:
        if self.action not in ACTION_KINDS:
            raise ValueError(f"unknown action kind {self.action!r}")
        if self.action == CONNECT_INVOKE:
            try:
                validated = ConnectInvokeRequest.model_validate(self.request)
            except ValidationError as exc:
                raise ValueError(f"invalid connect.invoke request: {exc}") from exc
            # Normalize to the materialized shape (defaults filled, an absent provider dropped
            # rather than left null so the request stays a clean JSON object), so the signed
            # canonical bytes are deterministic and the invoker parses the exact frozen request
            # the pack signed (with any overlay bindings resolved at admission).
            self.request = validated.model_dump(mode="json", exclude_none=True)
        else:
            for value in self.request.values():
                if not isinstance(value, str | int | bool):
                    raise ValueError(
                        f"action kind {self.action!r} requires a flat scalar request"
                    )
        return self


class WorkflowDefinition(_Strict):
    name: _Name
    trigger: Trigger
    conditions: list[Condition] = Field(default_factory=list, max_length=MAX_CONDITIONS)
    effects: list[Effect] = Field(min_length=1, max_length=MAX_EFFECTS)
    actions: list[ActionEmit] = Field(default_factory=list, max_length=MAX_ACTIONS)

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
            # A record.stage condition can only ever be true for a declared stage, so an
            # operand outside the stage set is a typo that makes the definition dead: it
            # would silently never fire rather than being rejected here.
            # record.stage conditions are AND-combined at match time, so a definition's
            # reachable stages are the intersection of its conditions' operand sets. An
            # operand outside the declared stages, or an empty intersection (mutually
            # exclusive conditions such as equals "captured" and equals "reviewing"), makes
            # the definition dead: it can never fire, so reject it here rather than admit it.
            reachable = set(stage_set)
            has_stage_condition = False
            for condition in definition.conditions:
                if condition.field != "record.stage":
                    continue
                has_stage_condition = True
                operands = (
                    {condition.value} if isinstance(condition.value, str) else set(condition.value)
                )
                for operand in operands:
                    if operand not in stage_set:
                        raise ValueError(
                            f"definition {definition.name!r} condition references unknown "
                            f"stage {operand!r}"
                        )
                reachable &= operands
            if has_stage_condition and not reachable:
                raise ValueError(
                    f"definition {definition.name!r} has mutually exclusive record.stage "
                    "conditions that can never match"
                )
        return self


def canonical_workflow(workflow: Workflow) -> bytes:
    """Materialize defaults and encode the contracted canonical JSON bytes.

    Defaults are materialized (not dropped) and keys are sorted with fixed separators so a
    logically identical workflow always yields the same bytes. Slice 4's signed pack format
    signs over exactly these bytes.
    """
    try:
        raw = json.dumps(
            workflow.model_dump(mode="json", exclude_defaults=False),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except ValueError as exc:
        # A value that cannot be serialized or UTF-8 encoded (a lone surrogate such as an
        # overlay value decoded from "\ud800", or an integer past the interpreter digit
        # limit such as 10**5000) is rejected on the same DefinitionError path. This covers
        # a Workflow built directly via model_validate, not only one from parse_workflow.
        # UnicodeEncodeError is a ValueError subclass, so this catch handles both.
        raise DefinitionError(f"workflow contains an unserializable value: {exc}") from exc
    if len(raw) > MAX_DEFINITION_BYTES:
        raise DefinitionError("canonical workflow exceeds 16384 bytes")
    return raw


def _reject_duplicate_members(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """object_pairs_hook that rejects a repeated member at any nesting level.

    The default decoder keeps the last occurrence, so a document with two ``kind`` or
    ``value`` members would pass strict parsing after silently discarding one, potentially
    executing a different effect than it visibly requests.
    """
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON member {key!r}")
        result[key] = value
    return result


def parse_workflow(raw: bytes | str) -> Workflow:
    """Parse and strictly validate a workflow definition from JSON.

    This is the trust boundary for an untrusted definition. Every malformed document has a
    single rejection path (:class:`DefinitionError`): a JSON syntax error, invalid UTF-8
    bytes, an integer past the interpreter digit limit, a duplicate member, or input nested
    too deeply to parse. The canonical-byte size bound is enforced here too, so an oversized
    workflow is rejected before it can reach the engine or the event ledger.
    """
    try:
        data = json.loads(raw, object_pairs_hook=_reject_duplicate_members)
    except (ValueError, RecursionError) as exc:
        raise DefinitionError(f"invalid workflow JSON: {exc}") from exc
    try:
        workflow = Workflow.model_validate(data)
    except ValidationError as exc:
        raise DefinitionError(str(exc)) from exc
    canonical_workflow(workflow)  # raises DefinitionError if it exceeds MAX_DEFINITION_BYTES
    return workflow
