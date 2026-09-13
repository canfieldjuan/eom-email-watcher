"""Strict Connect-only rule definitions and pure metadata matching."""

from __future__ import annotations

import fnmatch
import json
import re
import uuid
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..config import normalize_validated_address
from ..mime import AttachmentDescriptor

MAX_RULE_DEFINITION_BYTES = 16 * 1024
MAX_AUTOMATION_RULES = 100
MAX_AUTOMATION_ATTACHMENTS = 1_000
MAX_AUTOMATION_FIRES_PER_MESSAGE = 1_000
MAX_CONDITIONS = 8
MAX_PARAMETERS = 16
MAX_REASON_CHARS = 128

IDENTIFIER_PATTERN = r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$"
CAPABILITY_VERSION_PATTERN = r"^[0-9]+\.[0-9]+$"
APP_VERSION_PATTERN = r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$"
MEDIA_TYPE_PATTERN = r"^[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+$"
DOMAIN_PATTERN = (
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)

CATEGORIES = (
    "invoice",
    "scheduling",
    "customer_request",
    "automated_notice",
    "informational",
    "other",
)
PRIORITIES = ("urgent", "high", "normal", "low")

ConditionField = Literal[
    "sender",
    "sender_name",
    "subject",
    "category",
    "priority",
    "action_required",
    "attachment.media_type",
    "attachment.filename",
    "attachment.byte_size",
    "attachment.count",
]
ConditionOp = Literal[
    "equals",
    "domain_equals",
    "contains",
    "starts_with",
    "in",
    "glob",
    "lte",
    "gte",
]
ALLOWED_OPS: dict[str, frozenset[str]] = {
    "sender": frozenset({"equals", "domain_equals"}),
    "sender_name": frozenset({"equals", "contains"}),
    "subject": frozenset({"contains", "starts_with"}),
    "category": frozenset({"equals", "in"}),
    "priority": frozenset({"equals", "in"}),
    "action_required": frozenset({"equals"}),
    "attachment.media_type": frozenset({"equals"}),
    "attachment.filename": frozenset({"glob"}),
    "attachment.byte_size": frozenset({"lte"}),
    "attachment.count": frozenset({"gte", "lte"}),
}
ATTACHMENT_FIELDS = frozenset(
    {"attachment.media_type", "attachment.filename", "attachment.byte_size"}
)

Identifier = Annotated[str, Field(strict=True, pattern=IDENTIFIER_PATTERN, max_length=100)]
CapabilityVersion = Annotated[
    str, Field(strict=True, pattern=CAPABILITY_VERSION_PATTERN, max_length=16)
]
AppVersion = Annotated[str, Field(strict=True, pattern=APP_VERSION_PATTERN, max_length=128)]
ParameterValue = (
    Annotated[str, Field(strict=True, max_length=1_000)]
    | Annotated[int, Field(strict=True, ge=-9_007_199_254_740_991, le=9_007_199_254_740_991)]
    | Annotated[bool, Field(strict=True)]
)


class RuleValidationError(ValueError):
    """A submitted or stored rule definition is invalid."""

    def __init__(self, reason: str):
        self.reason = reason.encode("utf-8", errors="replace")[:MAX_REASON_CHARS].decode(
            "utf-8", errors="ignore"
        )
        super().__init__(self.reason)


class AutomationFanoutLimit(RuntimeError):
    """Matching would retain more than the contracted all-or-zero fire bound."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Scope(_Strict):
    provider: Identifier | None = None
    account_id: Annotated[str, Field(strict=True, min_length=1, max_length=128)] | None = None

    @model_validator(mode="after")
    def _validate_scope(self) -> Scope:
        if self.account_id is not None:
            if self.provider is None:
                raise ValueError("account_id requires provider")
            if self.account_id != self.account_id.strip():
                raise ValueError("account_id must be trimmed")
        return self


class Trigger(_Strict):
    source_kind: Literal["mail.message"]


class CapabilityRef(_Strict):
    id: Identifier
    version: CapabilityVersion


class ProviderRef(_Strict):
    app_id: Identifier
    version: AppVersion
    instance_id: Annotated[str, Field(strict=True, min_length=36, max_length=36)]

    @model_validator(mode="after")
    def _validate_instance(self) -> ProviderRef:
        try:
            parsed = uuid.UUID(self.instance_id)
        except ValueError as exc:
            raise ValueError("provider.instance_id must be a lower-case UUIDv4") from exc
        if parsed.version != 4 or str(parsed) != self.instance_id:
            raise ValueError("provider.instance_id must be a lower-case UUIDv4")
        return self


class ConnectInvokeAction(_Strict):
    kind: Literal["connect.invoke"]
    capability: CapabilityRef
    provider: ProviderRef
    parameters: dict[Identifier, ParameterValue] = Field(
        default_factory=dict, max_length=MAX_PARAMETERS
    )


class Condition(_Strict):
    field: ConditionField
    op: ConditionOp
    value: str | int | bool | list[str]

    @model_validator(mode="after")
    def _validate_operand(self) -> Condition:
        if self.op not in ALLOWED_OPS[self.field]:
            raise ValueError(f"op {self.op} is not permitted for {self.field}")
        value = self.value
        if self.field == "sender":
            if not isinstance(value, str) or not value:
                raise ValueError("sender value must be text")
            if self.op == "equals":
                if len(value) > 320 or normalize_validated_address(value) != value:
                    raise ValueError("sender must be a normalized address")
            elif len(value) > 253 or re.fullmatch(DOMAIN_PATTERN, value) is None:
                raise ValueError("sender domain must be a lower-case DNS domain")
        elif self.field in {"sender_name", "subject"}:
            limit = 320 if self.field == "sender_name" else 4_096
            if not isinstance(value, str) or not 1 <= len(value) <= limit:
                raise ValueError(f"{self.field} value must be 1..{limit} characters")
            if value != value.casefold():
                raise ValueError(f"{self.field} value must be case-folded")
        elif self.field in {"category", "priority"}:
            allowed = CATEGORIES if self.field == "category" else PRIORITIES
            maximum = len(allowed)
            if self.op == "in":
                if not isinstance(value, list):
                    raise ValueError("in takes a list")
                values = value
                if not 1 <= len(values) <= maximum or len(set(values)) != len(values):
                    raise ValueError(f"{self.field} list must contain distinct closed values")
            else:
                if isinstance(value, list):
                    raise ValueError("equals takes one value")
                values = [value]
            if any(not isinstance(item, str) or item not in allowed for item in values):
                raise ValueError(f"{self.field} value is outside the closed set")
        elif self.field == "action_required":
            if not isinstance(value, bool):
                raise ValueError("action_required value must be a boolean")
        elif self.field == "attachment.media_type":
            if (
                not isinstance(value, str)
                or len(value) > 127
                or re.fullmatch(MEDIA_TYPE_PATTERN, value) is None
            ):
                raise ValueError("attachment.media_type must be a lower-case media type")
        elif self.field == "attachment.filename":
            if not isinstance(value, str) or not 1 <= len(value) <= 512:
                raise ValueError("attachment.filename glob must be 1..512 characters")
            if "/" in value or "\\" in value or value != value.casefold():
                raise ValueError("attachment.filename glob must be case-folded with no separators")
        elif self.field == "attachment.byte_size":
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= 104_857_600
            ):
                raise ValueError("attachment.byte_size must be 0..104857600")
        elif self.field == "attachment.count" and (
            isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 64
        ):
            raise ValueError("attachment.count must be 0..64")
        return self


class RuleDefinition(_Strict):
    name: Annotated[str, Field(strict=True, min_length=1, max_length=80)]
    scope: Scope = Field(default_factory=Scope)
    trigger: Trigger
    conditions: list[Condition] = Field(min_length=1, max_length=MAX_CONDITIONS)
    action: ConnectInvokeAction
    confirm_each: Annotated[bool, Field(strict=True)] = False

    @model_validator(mode="after")
    def _validate_definition(self) -> RuleDefinition:
        if any(not character.isprintable() for character in self.name):
            raise ValueError("name must contain only printable characters")
        if not any(condition.field == "attachment.media_type" for condition in self.conditions):
            raise ValueError("connect.invoke needs an attachment.media_type condition")
        return self


def _bounded_reason(exc: ValidationError) -> str:
    error = exc.errors(include_url=False, include_context=False)[0]
    location = ".".join(str(item) for item in error.get("loc", ())) or "$"
    kind = str(error.get("type", "invalid"))
    message = str(error.get("msg", ""))
    if kind == "extra_forbidden":
        reason = f"unknown member at {location}"
    elif kind == "missing":
        reason = f"missing member at {location}"
    elif kind == "value_error":
        reason = message.removeprefix("Value error, ")
    else:
        reason = f"invalid value at {location}"
    return reason[:MAX_REASON_CHARS]


def parse_rule_definition(value: object) -> RuleDefinition:
    """Validate one decoded definition through the canonical strict model."""
    if not isinstance(value, dict):
        raise RuleValidationError("definition must be a JSON object")
    try:
        return RuleDefinition.model_validate(value)
    except ValidationError as exc:
        raise RuleValidationError(_bounded_reason(exc)) from None


def canonical_rule_definition(definition: RuleDefinition) -> bytes:
    """Materialize defaults and encode the contracted canonical JSON bytes."""
    raw = json.dumps(
        definition.model_dump(mode="json", exclude_defaults=False),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    if len(raw) > MAX_RULE_DEFINITION_BYTES:
        raise RuleValidationError("canonical definition exceeds 16384 bytes")
    return raw


@dataclass(frozen=True)
class MatchRule:
    rule_id: str
    version: int
    scope_mailbox_identity_key: str | None
    definition: RuleDefinition


@dataclass(frozen=True)
class MatchedFire:
    rule_id: str
    rule_version: int
    part_id: str
    action: ConnectInvokeAction
    confirm_each: bool


def _message_condition(
    condition: Condition,
    *,
    sender: str,
    sender_name: str | None,
    subject: str,
    result: dict[str, object],
    attachment_count: int,
) -> bool:
    value = condition.value
    if condition.field == "sender":
        if condition.op == "equals":
            return sender == value
        return sender.rsplit("@", 1)[-1].casefold() == value
    if condition.field == "sender_name":
        candidate = (sender_name or "").casefold()
        return candidate == value if condition.op == "equals" else value in candidate
    if condition.field == "subject":
        candidate = subject.casefold()
        return candidate.startswith(value) if condition.op == "starts_with" else value in candidate
    if condition.field in {"category", "priority", "action_required"}:
        candidate = result[condition.field]
        return candidate in value if condition.op == "in" else candidate == value
    if condition.field == "attachment.count":
        return attachment_count >= value if condition.op == "gte" else attachment_count <= value
    raise AssertionError(f"not a message condition: {condition.field}")


def _attachment_condition(condition: Condition, attachment: AttachmentDescriptor) -> bool:
    if condition.field == "attachment.media_type":
        return attachment.media_type == condition.value
    if condition.field == "attachment.filename":
        final_name = attachment.filename.replace("\\", "/").rsplit("/", 1)[-1].casefold()
        return fnmatch.fnmatchcase(final_name, condition.value)
    if condition.field == "attachment.byte_size":
        return attachment.byte_size_known and attachment.byte_size <= condition.value
    raise AssertionError(f"not an attachment condition: {condition.field}")


def match_rules(
    rules: list[MatchRule],
    *,
    provider: str,
    account_id: str,
    mailbox_identity_key: str,
    sender: str,
    sender_name: str | None,
    subject: str,
    result: dict[str, object],
    attachments: list[AttachmentDescriptor],
) -> list[MatchedFire]:
    """Return deterministic matches without I/O or mutation."""
    if len(attachments) > MAX_AUTOMATION_ATTACHMENTS:
        raise AutomationFanoutLimit("automation attachment limit exceeded")
    ordered_attachments = sorted(attachments, key=lambda item: (item.position, item.part_id))
    matched: list[MatchedFire] = []
    for rule in rules:
        scope = rule.definition.scope
        if scope.provider is not None and scope.provider != provider:
            continue
        if scope.account_id is not None:
            if scope.account_id != account_id:
                continue
            if rule.scope_mailbox_identity_key != mailbox_identity_key:
                continue
        message_conditions = [
            condition
            for condition in rule.definition.conditions
            if condition.field not in ATTACHMENT_FIELDS
        ]
        if not all(
            _message_condition(
                condition,
                sender=sender,
                sender_name=sender_name,
                subject=subject,
                result=result,
                attachment_count=len(ordered_attachments),
            )
            for condition in message_conditions
        ):
            continue
        attachment_conditions = [
            condition
            for condition in rule.definition.conditions
            if condition.field in ATTACHMENT_FIELDS
        ]
        for attachment in ordered_attachments:
            if not all(
                _attachment_condition(condition, attachment) for condition in attachment_conditions
            ):
                continue
            if len(matched) >= MAX_AUTOMATION_FIRES_PER_MESSAGE:
                raise AutomationFanoutLimit("automation fire limit exceeded")
            matched.append(
                MatchedFire(
                    rule_id=rule.rule_id,
                    rule_version=rule.version,
                    part_id=attachment.part_id,
                    action=rule.definition.action,
                    confirm_each=rule.definition.confirm_each,
                )
            )
    return matched
