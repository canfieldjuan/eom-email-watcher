from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from . import entitlement
from .connect_windows import (
    local_app_data_root,
    read_bounded_regular_file,
    validate_private_directory,
)

PROTOCOL_VERSION = 1
GENERIC_PROTOCOL_VERSION = 2
CAPABILITY_ID = "document.summarize"
CAPABILITY_VERSION = "1.0"
INPUT_MEDIA_TYPE = "application/pdf"
OUTPUT_MEDIA_TYPE = "application/vnd.local-connect.document-summary+json"
SOURCE_APP_ID = "email-watcher"
MAX_INPUT_BYTES = 100 * 1024 * 1024
MAX_REGISTRATION_BYTES = 16 * 1024
MAX_WINDOWS_REGISTRATION_ENTRIES = 256
MAX_MANIFEST_BYTES = 64 * 1024
MAX_STATUS_BYTES = 2 * 1024 * 1024 + 64 * 1024
MAX_GENERIC_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_GENERIC_STATUS_BYTES = 24 * 1024 * 1024
DEFAULT_JOB_TIMEOUT_SECONDS = 30 * 60

UUID_V4_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
IDENTIFIER_PATTERN = r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$"
MEDIA_TYPE_PATTERN = r"^[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+$"
VERSION_PATTERN = r"^[0-9]+\.[0-9]+$"
APP_VERSION_PATTERN = r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$"
TOKEN_PATTERN = r"^[A-Za-z0-9_-]{43,128}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
ERROR_CODE_PATTERN = r"^[A-Z0-9_]+$"

UuidV4 = Annotated[StrictStr, Field(pattern=UUID_V4_PATTERN)]
Identifier = Annotated[StrictStr, Field(pattern=IDENTIFIER_PATTERN, max_length=100)]
MediaType = Annotated[StrictStr, Field(pattern=MEDIA_TYPE_PATTERN, max_length=127)]
CapabilityVersion = Annotated[StrictStr, Field(pattern=VERSION_PATTERN)]
Sha256 = Annotated[StrictStr, Field(pattern=SHA256_PATTERN)]
ParameterString = Annotated[StrictStr, Field(max_length=1000)]
ParameterInteger = Annotated[
    StrictInt,
    Field(ge=-9_007_199_254_740_991, le=9_007_199_254_740_991),
]
ParameterValue = ParameterString | ParameterInteger | StrictBool


class ConnectError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _validate_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp must use ISO 8601") from exc
    if "T" not in value or parsed.tzinfo is None:
        raise ValueError("timestamp must include a time and UTC offset")
    return value


class _TransportRegistration(_WireModel):
    kind: Literal["http-loopback-v1"]
    base_url: Annotated[StrictStr, Field(max_length=100)]


class _AuthRegistration(_WireModel):
    scheme: Literal["bearer"]
    token: Annotated[StrictStr, Field(pattern=TOKEN_PATTERN)]


class _RuntimeRegistration(_WireModel):
    protocol_version: Literal[1]
    instance_id: UuidV4
    app_id: Identifier
    pid: Annotated[StrictInt, Field(ge=1)]
    started_at: StrictStr
    transport: _TransportRegistration
    auth: _AuthRegistration

    _timestamp = field_validator("started_at")(_validate_timestamp)


class _TransportRegistrationV2(_WireModel):
    kind: Literal["http-loopback-v2"]
    base_url: Annotated[StrictStr, Field(max_length=100)]


class _RuntimeRegistrationV2(_WireModel):
    protocol_version: Literal[2]
    instance_id: UuidV4
    app_id: Identifier
    pid: Annotated[StrictInt, Field(ge=1)]
    started_at: StrictStr
    transport: _TransportRegistrationV2
    auth: _AuthRegistration

    _timestamp = field_validator("started_at")(_validate_timestamp)


class _AppDescription(_WireModel):
    id: Identifier
    name: Annotated[StrictStr, Field(min_length=1, max_length=100)]
    version: Annotated[StrictStr, Field(pattern=APP_VERSION_PATTERN)]


class _AcceptedMedia(_WireModel):
    media_type: MediaType
    max_bytes: Annotated[StrictInt, Field(ge=1, le=1024 * 1024 * 1024)]


class _CapabilityDeclaration(_WireModel):
    id: Identifier
    version: CapabilityVersion
    accepts: Annotated[list[_AcceptedMedia], Field(min_length=1, max_length=16)]
    produces: Annotated[list[MediaType], Field(min_length=1, max_length=16)]


class _AppManifest(_WireModel):
    protocol_version: Literal[1]
    instance_id: UuidV4
    app: _AppDescription
    capabilities: Annotated[list[_CapabilityDeclaration], Field(min_length=1, max_length=64)]


class _ActionDescription(_WireModel):
    label: Annotated[StrictStr, Field(min_length=1, max_length=80)]
    description: Annotated[StrictStr, Field(min_length=1, max_length=500)]


class _ParameterDeclaration(_WireModel):
    name: Identifier
    value_type: Literal["string", "integer", "boolean"]
    required: StrictBool
    label: Annotated[StrictStr, Field(min_length=1, max_length=80)]
    description: Annotated[StrictStr, Field(min_length=1, max_length=500)]


class _CapabilityEffects(_WireModel):
    external: StrictBool
    confirmation_required: StrictBool


class _CapabilityDeclarationV2(_WireModel):
    id: Identifier
    version: CapabilityVersion
    action: _ActionDescription
    accepts: Annotated[list[_AcceptedMedia], Field(min_length=1, max_length=16)]
    produces: Annotated[list[MediaType], Field(min_length=1, max_length=16)]
    parameters: Annotated[list[_ParameterDeclaration], Field(max_length=16)]
    effects: _CapabilityEffects

    @model_validator(mode="after")
    def validate_unique_members(self) -> _CapabilityDeclarationV2:
        accepted_media_types = [accepted.media_type for accepted in self.accepts]
        if len(accepted_media_types) != len(set(accepted_media_types)):
            raise ValueError("accepted media types must be unique")
        parameter_names = [parameter.name for parameter in self.parameters]
        if len(parameter_names) != len(set(parameter_names)):
            raise ValueError("capability parameter names must be unique")
        return self


class _AppManifestV2(_WireModel):
    protocol_version: Literal[2]
    instance_id: UuidV4
    app: _AppDescription
    capabilities: Annotated[list[_CapabilityDeclarationV2], Field(min_length=1, max_length=64)]

    @model_validator(mode="after")
    def validate_unique_capabilities(self) -> _AppManifestV2:
        references = [(capability.id, capability.version) for capability in self.capabilities]
        if len(references) != len(set(references)):
            raise ValueError("capability identifiers and versions must be unique")
        return self


class _CapabilityRef(_WireModel):
    id: Identifier
    version: CapabilityVersion


class _ProviderRef(_WireModel):
    app_id: Identifier
    instance_id: UuidV4


class _ArtifactProvenance(_WireModel):
    artifact_id: UuidV4
    media_type: MediaType
    byte_size: Annotated[StrictInt, Field(ge=0, le=MAX_INPUT_BYTES)]
    sha256: Sha256


class _Warning(_WireModel):
    code: Annotated[StrictStr, Field(pattern=ERROR_CODE_PATTERN, max_length=100)]
    message: Annotated[StrictStr, Field(min_length=1, max_length=1000)]


class _SummaryContent(_WireModel):
    summary_version: CapabilityVersion
    text: Annotated[StrictStr, Field(min_length=1, max_length=1024 * 1024)]
    warnings: Annotated[list[_Warning], Field(max_length=256)]
    input_artifact: _ArtifactProvenance


class _SummaryOutput(_WireModel):
    artifact_id: UuidV4
    media_type: Literal[OUTPUT_MEDIA_TYPE]
    byte_size: Annotated[StrictInt, Field(ge=1, le=2 * 1024 * 1024)]
    sha256: Sha256
    content: _SummaryContent


class _JobResult(_WireModel):
    outputs: Annotated[list[_SummaryOutput], Field(min_length=1, max_length=8)]


class _JobError(_WireModel):
    code: Annotated[StrictStr, Field(pattern=ERROR_CODE_PATTERN, max_length=100)]
    message: Annotated[StrictStr, Field(min_length=1, max_length=1000)]
    retryable: StrictBool


class _JobStatus(_WireModel):
    protocol_version: Literal[1]
    job_id: UuidV4
    capability: _CapabilityRef
    provider: _ProviderRef
    status: Literal["accepted", "processing", "completed", "failed"]
    created_at: StrictStr
    updated_at: StrictStr
    input_artifacts: Annotated[list[_ArtifactProvenance], Field(min_length=1, max_length=8)]
    result: _JobResult | None = None
    error: _JobError | None = None

    _created_timestamp = field_validator("created_at")(_validate_timestamp)
    _updated_timestamp = field_validator("updated_at")(_validate_timestamp)

    @model_validator(mode="after")
    def validate_terminal_shape(self) -> _JobStatus:
        if self.status == "completed" and (self.result is None or self.error is not None):
            raise ValueError("completed jobs require only a result")
        if self.status == "failed" and (self.error is None or self.result is not None):
            raise ValueError("failed jobs require only an error")
        if self.status in {"accepted", "processing"} and (
            self.result is not None or self.error is not None
        ):
            raise ValueError("active jobs cannot contain terminal data")
        return self


class _ErrorEnvelope(_WireModel):
    protocol_version: Literal[1]
    error: _JobError


class _InputArtifactV2(_WireModel):
    artifact_id: UuidV4
    media_type: MediaType
    byte_size: Annotated[StrictInt, Field(ge=0, le=1024 * 1024 * 1024)]
    sha256: Sha256
    display_name: Annotated[StrictStr, Field(min_length=1, max_length=255)]
    source_app_id: Identifier


class _JobRequestV2(_WireModel):
    protocol_version: Literal[2]
    job_id: UuidV4
    capability: _CapabilityRef
    inputs: Annotated[list[_InputArtifactV2], Field(min_length=1, max_length=1)]
    parameters: Annotated[dict[Identifier, ParameterValue], Field(max_length=16)]


class _ArtifactProvenanceV2(_WireModel):
    artifact_id: UuidV4
    media_type: MediaType
    byte_size: Annotated[StrictInt, Field(ge=0, le=1024 * 1024 * 1024)]
    sha256: Sha256


class _GenericOutputV2(_WireModel):
    artifact_id: UuidV4
    media_type: MediaType
    display_name: Annotated[StrictStr, Field(min_length=1, max_length=255)]
    byte_size: Annotated[StrictInt, Field(ge=0, le=MAX_GENERIC_OUTPUT_BYTES)]
    sha256: Sha256
    payload_base64: Annotated[StrictStr, Field(max_length=2_796_204)]


class _JobResultV2(_WireModel):
    outputs: Annotated[list[_GenericOutputV2], Field(min_length=1, max_length=8)]


class _JobStatusV2(_WireModel):
    protocol_version: Literal[2]
    job_id: UuidV4
    capability: _CapabilityRef
    provider: _ProviderRef
    status: Literal["accepted", "processing", "completed", "failed"]
    created_at: StrictStr
    updated_at: StrictStr
    input_artifacts: Annotated[
        list[_ArtifactProvenanceV2], Field(min_length=1, max_length=1)
    ]
    result: _JobResultV2 | None = None
    error: _JobError | None = None

    _created_timestamp = field_validator("created_at")(_validate_timestamp)
    _updated_timestamp = field_validator("updated_at")(_validate_timestamp)

    @model_validator(mode="after")
    def validate_terminal_shape(self) -> _JobStatusV2:
        if self.status == "completed" and (self.result is None or self.error is not None):
            raise ValueError("completed jobs require only a result")
        if self.status == "failed" and (self.error is None or self.result is not None):
            raise ValueError("failed jobs require only an error")
        if self.status in {"accepted", "processing"} and (
            self.result is not None or self.error is not None
        ):
            raise ValueError("active jobs cannot contain terminal data")
        if self.result is not None:
            artifact_ids = [output.artifact_id for output in self.result.outputs]
            if len(artifact_ids) != len(set(artifact_ids)):
                raise ValueError("output artifact identifiers must be unique")
        return self


class _ErrorEnvelopeV2(_WireModel):
    protocol_version: Literal[2]
    error: _JobError


@dataclass(frozen=True)
class ProviderCapability:
    base_url: str
    token: str
    app_id: str
    instance_id: str
    max_input_bytes: int


@dataclass(frozen=True)
class CapabilityDiscovery:
    provider: ProviderCapability | None
    diagnostic_code: str | None = None

    def public_result(self) -> dict[str, object]:
        if self.provider is None:
            return {
                "items": [],
                "diagnostic": (
                    {"code": self.diagnostic_code}
                    if self.diagnostic_code == "ambiguous_provider"
                    else None
                ),
            }
        return {
            "items": [
                {
                    "id": CAPABILITY_ID,
                    "version": CAPABILITY_VERSION,
                    "accepts": [INPUT_MEDIA_TYPE],
                    "max_input_bytes": self.provider.max_input_bytes,
                }
            ],
            "diagnostic": None,
        }


@dataclass(frozen=True)
class AcceptedArtifactType:
    media_type: str
    max_bytes: int

    def public_dict(self) -> dict[str, object]:
        return {"media_type": self.media_type, "max_bytes": self.max_bytes}


@dataclass(frozen=True)
class CapabilityParameter:
    name: str
    value_type: str
    required: bool
    label: str
    description: str

    def public_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "value_type": self.value_type,
            "required": self.required,
            "label": self.label,
            "description": self.description,
        }


@dataclass(frozen=True)
class DiscoveredCapability:
    protocol_version: int
    base_url: str
    token: str
    app_id: str
    app_name: str
    app_version: str
    instance_id: str
    capability_id: str
    capability_version: str
    action_label: str
    action_description: str
    accepts: tuple[AcceptedArtifactType, ...]
    produces: tuple[str, ...]
    parameters: tuple[CapabilityParameter, ...]
    external_effects: bool
    confirmation_required: bool

    def accepts_artifact(self, media_type: str, byte_size: int) -> bool:
        if byte_size < 0 or byte_size > MAX_INPUT_BYTES:
            return False
        normalized_media_type = media_type.casefold()
        return any(
            accepted.media_type == normalized_media_type and byte_size <= accepted.max_bytes
            for accepted in self.accepts
        )

    def public_dict(self) -> dict[str, object]:
        return {
            "protocol_version": self.protocol_version,
            "provider": {
                "app_id": self.app_id,
                "name": self.app_name,
                "version": self.app_version,
                "instance_id": self.instance_id,
                "available": True,
            },
            "capability": {
                "id": self.capability_id,
                "version": self.capability_version,
                "action": {
                    "label": self.action_label,
                    "description": self.action_description,
                },
                "accepts": [accepted.public_dict() for accepted in self.accepts],
                "produces": list(self.produces),
                "parameters": [parameter.public_dict() for parameter in self.parameters],
                "effects": {
                    "external": self.external_effects,
                    "confirmation_required": self.confirmation_required,
                },
            },
        }


@dataclass(frozen=True)
class CapabilityCatalog:
    items: tuple[DiscoveredCapability, ...]
    diagnostic_code: str | None = None

    def compatible(self, media_type: str, byte_size: int) -> tuple[DiscoveredCapability, ...]:
        return tuple(
            capability
            for capability in self.items
            if capability.accepts_artifact(media_type, byte_size)
        )

    def public_result(self) -> dict[str, object]:
        return {
            "items": [capability.public_dict() for capability in self.items],
            "diagnostic": (
                {"code": self.diagnostic_code} if self.diagnostic_code is not None else None
            ),
        }


@dataclass(frozen=True)
class ArtifactIdentity:
    artifact_id: str
    media_type: str
    byte_size: int
    sha256: str

    def public_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "media_type": self.media_type,
            "byte_size": self.byte_size,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class PreparedCapabilityJob:
    job_id: str
    provider_app_id: str
    provider_app_version: str
    provider_instance_id: str
    capability_id: str
    capability_version: str
    artifact: ArtifactIdentity
    display_name: str
    parameters: tuple[tuple[str, str | int | bool], ...]
    request_json: bytes = field(repr=False)


@dataclass(frozen=True)
class CapabilityOutput:
    artifact_id: str
    media_type: str
    display_name: str
    byte_size: int
    sha256: str
    payload: bytes = field(repr=False)

    def store_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "media_type": self.media_type,
            "display_name": self.display_name,
            "byte_size": self.byte_size,
            "sha256": self.sha256,
            "payload_base64": base64.b64encode(self.payload).decode("ascii"),
        }


@dataclass(frozen=True)
class CapabilityResult:
    outputs: tuple[CapabilityOutput, ...]

    def store_dict(self) -> dict[str, object]:
        return {"outputs": [output.store_dict() for output in self.outputs]}


@dataclass(frozen=True)
class CapabilityJobUpdate:
    job_id: str
    status: str
    provider_app_id: str
    provider_instance_id: str
    result: CapabilityResult | None
    error: ConnectError | None


@dataclass(frozen=True)
class PreparedSummaryJob:
    job_id: str
    artifact: ArtifactIdentity
    display_name: str
    request: dict[str, object]


@dataclass(frozen=True)
class SummaryResult:
    artifact_id: str
    media_type: str
    byte_size: int
    sha256: str
    summary_version: str
    text: str
    warnings: tuple[dict[str, str], ...]

    def store_dict(self) -> dict[str, object]:
        return {
            "output": {
                "artifact_id": self.artifact_id,
                "media_type": self.media_type,
                "byte_size": self.byte_size,
                "sha256": self.sha256,
                "summary_version": self.summary_version,
                "text": self.text,
                "warnings": [dict(warning) for warning in self.warnings],
            }
        }


def decode_document_summary_output(
    output: CapabilityOutput,
    expected_input: ArtifactIdentity,
) -> SummaryResult:
    if (
        output.media_type != OUTPUT_MEDIA_TYPE
        or len(output.payload) != output.byte_size
        or hashlib.sha256(output.payload).hexdigest() != output.sha256
    ):
        raise ConnectError(
            "OUTPUT_CONTENT_INVALID",
            "The document summary output failed integrity validation.",
        )
    try:
        content = _SummaryContent.model_validate_json(output.payload)
    except ValueError as exc:
        raise ConnectError(
            "OUTPUT_CONTENT_INVALID",
            "The document summary output is invalid.",
        ) from exc
    if content.input_artifact.model_dump() != expected_input.public_dict():
        raise ConnectError(
            "OUTPUT_PROVENANCE_MISMATCH",
            "The document summary output does not match its input artifact.",
        )
    return SummaryResult(
        artifact_id=output.artifact_id,
        media_type=output.media_type,
        byte_size=output.byte_size,
        sha256=output.sha256,
        summary_version=content.summary_version,
        text=content.text,
        warnings=tuple(warning.model_dump() for warning in content.warnings),
    )


@dataclass(frozen=True)
class JobUpdate:
    job_id: str
    status: str
    provider_app_id: str
    provider_instance_id: str
    result: SummaryResult | None
    error: ConnectError | None


def _secure_directory(path: Path, *, windows_root: Path | None = None) -> bool:
    if os.name == "nt":
        try:
            root = windows_root or local_app_data_root()
            validate_private_directory(path, root=root)
            return True
        except OSError:
            return False
    try:
        info = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(info.st_mode)
        and not path.is_symlink()
        and info.st_uid == os.getuid()
        and info.st_mode & 0o077 == 0
    )


def _read_private_json(
    path: Path,
    limit: int,
    *,
    windows_root: Path | None = None,
) -> object | None:
    if os.name == "nt":
        try:
            return json.loads(
                read_bounded_regular_file(
                    path,
                    limit,
                    private_root=windows_root,
                )
            )
        except (OSError, ValueError, TypeError):
            return None
    try:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or path.is_symlink()
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077 != 0
            or not 0 < info.st_size <= limit
        ):
            return None
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid():
                return None
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                raw = stream.read(limit + 1)
        finally:
            os.close(descriptor)
        if len(raw) > limit:
            return None
        return json.loads(raw)
    except (OSError, ValueError, TypeError):
        return None


def _providers_directory(
    runtime_dir: Path | None,
    protocol_version: int,
) -> tuple[Path, Path] | None:
    if runtime_dir is not None:
        root = Path(runtime_dir)
        return root, root / f"local-connect/v{protocol_version}/providers"
    if os.name == "nt":
        try:
            root = local_app_data_root()
        except OSError:
            return None
        return root, root / f"LocalConnect/runtime/v{protocol_version}/providers"
    value = os.environ.get("XDG_RUNTIME_DIR")
    if not value:
        return None
    root = Path(value)
    return root, root / f"local-connect/v{protocol_version}/providers"


def _registration_candidates(providers_dir: Path) -> tuple[Path, ...] | None:
    if os.name != "nt":
        try:
            return tuple(sorted(providers_dir.iterdir(), key=lambda item: item.name))
        except OSError:
            return None
    candidates: list[Path] = []
    entry_count = 0
    try:
        with os.scandir(providers_dir) as entries:
            for entry in entries:
                entry_count += 1
                if entry_count > MAX_WINDOWS_REGISTRATION_ENTRIES:
                    return None
                if not entry.name.lower().endswith(".json"):
                    continue
                candidates.append(Path(entry.path))
    except OSError:
        return None
    return tuple(sorted(candidates, key=lambda item: item.name))


def _read_registration(
    path: Path,
    *,
    windows_root: Path | None = None,
) -> _RuntimeRegistration | None:
    value = _read_private_json(
        path,
        MAX_REGISTRATION_BYTES,
        windows_root=windows_root,
    )
    if value is None:
        return None
    try:
        return _RuntimeRegistration.model_validate(value)
    except (ValueError, TypeError):
        return None


def _read_registration_v2(
    path: Path,
    *,
    windows_root: Path | None = None,
) -> _RuntimeRegistrationV2 | None:
    value = _read_private_json(
        path,
        MAX_REGISTRATION_BYTES,
        windows_root=windows_root,
    )
    if value is None:
        return None
    try:
        return _RuntimeRegistrationV2.model_validate(value)
    except (ValueError, TypeError):
        return None


def _validated_base_url(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or port is None
        or not 1 <= port <= 65_535
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return None
    host = "[::1]" if parsed.hostname == "::1" else "127.0.0.1"
    return f"http://{host}:{port}/"


def _response_json(response: httpx.Response, limit: int) -> object:
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
            if declared_length < 0:
                raise ConnectError("RESPONSE_INVALID", "Connect response was invalid.")
            if declared_length > limit:
                raise ConnectError("RESPONSE_TOO_LARGE", "Connect response exceeded its limit.")
        except ValueError as exc:
            raise ConnectError("RESPONSE_INVALID", "Connect response was invalid.") from exc
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise ConnectError("RESPONSE_INVALID", "Connect response was not JSON.")

    content = bytearray()
    for chunk in response.iter_bytes():
        if len(content) + len(chunk) > limit:
            raise ConnectError("RESPONSE_TOO_LARGE", "Connect response exceeded its limit.")
        content.extend(chunk)
    try:
        return json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise ConnectError("RESPONSE_INVALID", "Connect response was invalid JSON.") from exc


def _http_error(response: httpx.Response) -> ConnectError:
    try:
        value = _response_json(response, 64 * 1024)
        envelope = _ErrorEnvelope.model_validate(value)
    except (ConnectError, ValueError, TypeError):
        return ConnectError(
            "PROVIDER_REQUEST_FAILED",
            "The local capability provider rejected the request.",
            retryable=response.status_code >= 500,
        )
    return ConnectError(
        envelope.error.code,
        envelope.error.message,
        retryable=envelope.error.retryable,
    )


def _http_error_v2(response: httpx.Response) -> ConnectError:
    try:
        value = _response_json(response, 64 * 1024)
        envelope = _ErrorEnvelopeV2.model_validate(value)
    except (ConnectError, ValueError, TypeError):
        return ConnectError(
            "PROVIDER_REQUEST_FAILED",
            "The local capability provider rejected the request.",
            retryable=response.status_code >= 500,
        )
    return ConnectError(
        envelope.error.code,
        envelope.error.message,
        retryable=envelope.error.retryable,
    )


def _client() -> httpx.Client:
    return httpx.Client(
        follow_redirects=False,
        timeout=httpx.Timeout(connect=1.0, read=10.0, write=60.0, pool=1.0),
        trust_env=False,
    )


def require_connect_entitlement() -> None:
    if entitlement.connect_entitlement_decision().is_active:
        return
    raise ConnectError(
        "CONNECT_ENTITLEMENT_REQUIRED",
        "An active Connect entitlement is required.",
    )


def discover_summary_capability(
    runtime_dir: Path | None = None,
    *,
    client: httpx.Client | None = None,
    provider_instance_id: str | None = None,
) -> CapabilityDiscovery:
    if not entitlement.connect_entitlement_decision().is_active:
        return CapabilityDiscovery(None, "connect_entitlement_required")
    locations = _providers_directory(runtime_dir, PROTOCOL_VERSION)
    if locations is None:
        return CapabilityDiscovery(None, "connect_unavailable")
    root_value, providers_dir = locations
    if not root_value.is_absolute() or not _secure_directory(
        root_value,
        windows_root=root_value,
    ):
        return CapabilityDiscovery(None, "connect_unavailable")
    if not _secure_directory(providers_dir, windows_root=root_value):
        return CapabilityDiscovery(None, "provider_unavailable")

    owned_client = client is None
    active_client = client or _client()
    providers: dict[str, ProviderCapability] = {}
    try:
        registrations = _registration_candidates(providers_dir)
        if registrations is None:
            return CapabilityDiscovery(None, "provider_unavailable")
        for path in registrations:
            registration = _read_registration(path, windows_root=root_value)
            if registration is None:
                continue
            base_url = _validated_base_url(registration.transport.base_url)
            if base_url is None:
                continue
            try:
                with active_client.stream(
                    "GET",
                    f"{base_url}v1/manifest",
                    headers={
                        "Accept": "application/json",
                        "Authorization": f"Bearer {registration.auth.token}",
                    },
                ) as response:
                    if response.status_code != 200:
                        continue
                    manifest = _AppManifest.model_validate(
                        _response_json(response, MAX_MANIFEST_BYTES)
                    )
            except (httpx.HTTPError, ConnectError, ValueError, TypeError):
                continue
            if (
                manifest.instance_id != registration.instance_id
                or manifest.app.id != registration.app_id
            ):
                continue
            match = next(
                (
                    accepted
                    for capability in manifest.capabilities
                    if capability.id == CAPABILITY_ID
                    and capability.version == CAPABILITY_VERSION
                    and OUTPUT_MEDIA_TYPE in capability.produces
                    for accepted in capability.accepts
                    if accepted.media_type == INPUT_MEDIA_TYPE
                ),
                None,
            )
            if match is None:
                continue
            providers[manifest.instance_id] = ProviderCapability(
                base_url=base_url,
                token=registration.auth.token,
                app_id=manifest.app.id,
                instance_id=manifest.instance_id,
                max_input_bytes=min(match.max_bytes, MAX_INPUT_BYTES),
            )
    finally:
        if owned_client:
            active_client.close()

    if provider_instance_id is not None:
        provider = providers.get(provider_instance_id)
        return CapabilityDiscovery(
            provider,
            None if provider is not None else "provider_unavailable",
        )
    if len(providers) > 1:
        return CapabilityDiscovery(None, "ambiguous_provider")
    if not providers:
        return CapabilityDiscovery(None, "provider_unavailable")
    return CapabilityDiscovery(next(iter(providers.values())))


def _generic_capabilities(
    registration: _RuntimeRegistrationV2,
    manifest: _AppManifestV2,
    base_url: str,
) -> tuple[DiscoveredCapability, ...]:
    capabilities = [
        DiscoveredCapability(
            protocol_version=GENERIC_PROTOCOL_VERSION,
            base_url=base_url,
            token=registration.auth.token,
            app_id=manifest.app.id,
            app_name=manifest.app.name,
            app_version=manifest.app.version,
            instance_id=manifest.instance_id,
            capability_id=capability.id,
            capability_version=capability.version,
            action_label=capability.action.label,
            action_description=capability.action.description,
            accepts=tuple(
                AcceptedArtifactType(
                    accepted.media_type,
                    min(accepted.max_bytes, MAX_INPUT_BYTES),
                )
                for accepted in capability.accepts
            ),
            produces=tuple(capability.produces),
            parameters=tuple(
                CapabilityParameter(
                    name=parameter.name,
                    value_type=parameter.value_type,
                    required=parameter.required,
                    label=parameter.label,
                    description=parameter.description,
                )
                for parameter in capability.parameters
            ),
            external_effects=capability.effects.external,
            confirmation_required=capability.effects.confirmation_required,
        )
        for capability in manifest.capabilities
    ]
    return tuple(
        sorted(
            capabilities,
            key=lambda capability: (
                capability.capability_id,
                capability.capability_version,
            ),
        )
    )


def _discover_capabilities(
    runtime_dir: Path | None = None,
    *,
    client: httpx.Client | None = None,
    provider_instance_id: str | None = None,
    require_entitlement: bool = True,
) -> CapabilityCatalog:
    if require_entitlement and not entitlement.connect_entitlement_decision().is_active:
        return CapabilityCatalog((), "connect_entitlement_required")
    locations = _providers_directory(runtime_dir, GENERIC_PROTOCOL_VERSION)
    if locations is None:
        return CapabilityCatalog((), "connect_unavailable")
    root_value, providers_dir = locations
    if not root_value.is_absolute() or not _secure_directory(
        root_value,
        windows_root=root_value,
    ):
        return CapabilityCatalog((), "connect_unavailable")
    if not _secure_directory(providers_dir, windows_root=root_value):
        return CapabilityCatalog((), "provider_unavailable")

    owned_client = client is None
    active_client = client or _client()
    providers: dict[str, tuple[DiscoveredCapability, ...]] = {}
    conflicting_instances: set[str] = set()
    try:
        registrations = _registration_candidates(providers_dir)
        if registrations is None:
            return CapabilityCatalog((), "provider_unavailable")
        for path in registrations:
            registration = _read_registration_v2(path, windows_root=root_value)
            if registration is None or registration.instance_id in conflicting_instances:
                continue
            if (
                provider_instance_id is not None
                and registration.instance_id != provider_instance_id
            ):
                continue
            base_url = _validated_base_url(registration.transport.base_url)
            if base_url is None:
                continue
            try:
                with active_client.stream(
                    "GET",
                    f"{base_url}v2/manifest",
                    headers={
                        "Accept": "application/json",
                        "Authorization": f"Bearer {registration.auth.token}",
                    },
                ) as response:
                    if response.status_code != 200:
                        continue
                    manifest = _AppManifestV2.model_validate(
                        _response_json(response, MAX_MANIFEST_BYTES)
                    )
            except (httpx.HTTPError, ConnectError, ValueError, TypeError):
                continue
            if (
                manifest.instance_id != registration.instance_id
                or manifest.app.id != registration.app_id
            ):
                continue
            capabilities = _generic_capabilities(registration, manifest, base_url)
            existing = providers.get(manifest.instance_id)
            if existing is not None and existing != capabilities:
                providers.pop(manifest.instance_id)
                conflicting_instances.add(manifest.instance_id)
                continue
            providers[manifest.instance_id] = capabilities
    finally:
        if owned_client:
            active_client.close()

    items = tuple(
        sorted(
            (
                capability
                for provider_capabilities in providers.values()
                for capability in provider_capabilities
            ),
            key=lambda capability: (
                capability.action_label.casefold(),
                capability.capability_id,
                capability.capability_version,
                capability.app_name.casefold(),
                capability.app_id,
                capability.instance_id,
            ),
        )
    )
    if not items:
        return CapabilityCatalog((), "provider_unavailable")
    return CapabilityCatalog(items)


def discover_capabilities(
    runtime_dir: Path | None = None,
    *,
    client: httpx.Client | None = None,
    provider_instance_id: str | None = None,
) -> CapabilityCatalog:
    return _discover_capabilities(
        runtime_dir,
        client=client,
        provider_instance_id=provider_instance_id,
        require_entitlement=True,
    )


def discover_capabilities_for_reconciliation(
    runtime_dir: Path | None = None,
    *,
    client: httpx.Client | None = None,
    provider_instance_id: str,
) -> CapabilityCatalog:
    return _discover_capabilities(
        runtime_dir,
        client=client,
        provider_instance_id=provider_instance_id,
        require_entitlement=False,
    )


def _safe_display_name(filename: str) -> str:
    basename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(character for character in basename if character.isprintable())
    cleaned = cleaned.strip()
    if not cleaned:
        cleaned = "attachment.pdf"
    encoded = cleaned.encode("utf-8")
    if len(encoded) <= 255:
        return cleaned
    stem = cleaned.removesuffix(".pdf")
    while len(f"{stem}.pdf".encode()) > 255:
        stem = stem[:-1]
    return f"{stem}.pdf"


def _safe_artifact_display_name(filename: str) -> str:
    basename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(character for character in basename if character.isprintable()).strip()
    if not cleaned:
        cleaned = "attachment"
    while len(cleaned.encode("utf-8")) > 255:
        cleaned = cleaned[:-1]
    return cleaned


def validate_job_id(value: object) -> str:
    if not isinstance(value, str):
        raise ConnectError(
            "JOB_REQUEST_INVALID",
            "The capability request identity must be a UUIDv4 string.",
        )
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ConnectError(
            "JOB_REQUEST_INVALID",
            "The capability request identity must be a UUIDv4 string.",
        ) from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ConnectError(
            "JOB_REQUEST_INVALID",
            "The capability request identity must be a UUIDv4 string.",
        )
    return value


def _validated_parameters(
    capability: DiscoveredCapability,
    values: dict[str, object] | None,
) -> dict[str, str | int | bool]:
    supplied = values or {}
    declarations = {parameter.name: parameter for parameter in capability.parameters}
    missing = sorted(
        name
        for name, parameter in declarations.items()
        if parameter.required and name not in supplied
    )
    if missing:
        raise ConnectError(
            "PARAMETERS_INVALID",
            f"Required capability parameter is missing: {missing[0]}",
        )
    undeclared = sorted(set(supplied) - set(declarations))
    if undeclared:
        raise ConnectError(
            "PARAMETERS_INVALID",
            f"Capability parameter is not declared: {undeclared[0]}",
        )

    validated: dict[str, str | int | bool] = {}
    for name in sorted(supplied):
        value = supplied[name]
        value_type = declarations[name].value_type
        integral_number = (
            type(value) is int
            and -9_007_199_254_740_991 <= value <= 9_007_199_254_740_991
        ) or (
            type(value) is float
            and math.isfinite(value)
            and value.is_integer()
            and -9_007_199_254_740_991 <= value <= 9_007_199_254_740_991
        )
        valid = (
            value_type == "string"
            and isinstance(value, str)
            and len(value) <= 1000
        ) or (value_type == "integer" and integral_number) or (
            value_type == "boolean" and type(value) is bool
        )
        if not valid:
            raise ConnectError(
                "PARAMETERS_INVALID",
                f"Capability parameter has the wrong type or size: {name}",
            )
        validated[name] = int(value) if value_type == "integer" else value  # type: ignore[arg-type, assignment]
    return validated


def validate_capability_parameters(
    capability: DiscoveredCapability,
    values: dict[str, object] | None,
) -> dict[str, str | int | bool]:
    return _validated_parameters(capability, values)


def _build_capability_job(
    capability: DiscoveredCapability,
    *,
    job_id: str,
    artifact_id: str,
    media_type: str,
    byte_size: int,
    sha256: str,
    filename: str,
    parameters: dict[str, object] | None,
) -> PreparedCapabilityJob:
    if capability.protocol_version != GENERIC_PROTOCOL_VERSION:
        raise ConnectError(
            "PROTOCOL_VERSION_UNSUPPORTED",
            "The selected capability does not use the generic Connect protocol.",
        )
    if not capability.accepts_artifact(media_type, byte_size):
        raise ConnectError(
            "INPUT_ARTIFACT_INVALID",
            "The attachment type or size is not accepted by this capability.",
        )
    artifact = ArtifactIdentity(
        artifact_id=artifact_id,
        media_type=media_type.casefold(),
        byte_size=byte_size,
        sha256=sha256,
    )
    display_name = _safe_artifact_display_name(filename)
    validated_parameters = _validated_parameters(capability, parameters)
    try:
        request = _JobRequestV2.model_validate(
            {
                "protocol_version": GENERIC_PROTOCOL_VERSION,
                "job_id": job_id,
                "capability": {
                    "id": capability.capability_id,
                    "version": capability.capability_version,
                },
                "inputs": [
                    {
                        **artifact.public_dict(),
                        "display_name": display_name,
                        "source_app_id": SOURCE_APP_ID,
                    }
                ],
                "parameters": validated_parameters,
            }
        ).model_dump()
    except (ValueError, TypeError) as exc:
        raise ConnectError(
            "JOB_REQUEST_INVALID",
            "The capability job request is invalid.",
        ) from exc
    return PreparedCapabilityJob(
        job_id=job_id,
        provider_app_id=capability.app_id,
        provider_app_version=capability.app_version,
        provider_instance_id=capability.instance_id,
        capability_id=capability.capability_id,
        capability_version=capability.capability_version,
        artifact=artifact,
        display_name=display_name,
        parameters=tuple(validated_parameters.items()),
        request_json=json.dumps(
            request,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8"),
    )


def restore_capability_job(
    capability: DiscoveredCapability,
    *,
    job_id: str,
    artifact_id: str,
    media_type: str,
    byte_size: int,
    sha256: str,
    filename: str,
    parameters: dict[str, object] | None = None,
) -> PreparedCapabilityJob:
    return _build_capability_job(
        capability,
        job_id=job_id,
        artifact_id=artifact_id,
        media_type=media_type,
        byte_size=byte_size,
        sha256=sha256,
        filename=filename,
        parameters=parameters,
    )


def restore_persisted_capability_job(
    capability: DiscoveredCapability,
    request_json: bytes,
) -> PreparedCapabilityJob:
    """Restore the exact durable v2 request for reconciliation or resubmission."""
    if capability.protocol_version != GENERIC_PROTOCOL_VERSION:
        raise ConnectError(
            "PROTOCOL_VERSION_UNSUPPORTED",
            "The selected capability does not use the generic Connect protocol.",
        )
    try:
        request = _JobRequestV2.model_validate_json(request_json)
    except (ValueError, TypeError) as exc:
        raise ConnectError(
            "JOB_REQUEST_INVALID",
            "The durable capability job request is invalid.",
        ) from exc
    if (
        request.capability.id != capability.capability_id
        or request.capability.version != capability.capability_version
    ):
        raise ConnectError(
            "JOB_CAPABILITY_MISMATCH",
            "The durable job does not match the selected capability.",
        )
    artifact = request.inputs[0]
    if artifact.source_app_id != SOURCE_APP_ID:
        raise ConnectError(
            "JOB_REQUEST_INVALID",
            "The durable capability job source is invalid.",
        )
    parameters = _validated_parameters(capability, dict(request.parameters))
    return PreparedCapabilityJob(
        job_id=request.job_id,
        provider_app_id=capability.app_id,
        provider_app_version=capability.app_version,
        provider_instance_id=capability.instance_id,
        capability_id=request.capability.id,
        capability_version=request.capability.version,
        artifact=ArtifactIdentity(
            artifact_id=artifact.artifact_id,
            media_type=artifact.media_type,
            byte_size=artifact.byte_size,
            sha256=artifact.sha256,
        ),
        display_name=artifact.display_name,
        parameters=tuple(parameters.items()),
        request_json=request_json,
    )


def prepare_capability_job(
    capability: DiscoveredCapability,
    content: bytes,
    media_type: str,
    filename: str,
    *,
    parameters: dict[str, object] | None = None,
    confirmed: bool = False,
    job_id: str | None = None,
) -> PreparedCapabilityJob:
    if capability.confirmation_required and not confirmed:
        raise ConnectError(
            "CONFIRMATION_REQUIRED",
            "This capability requires explicit confirmation before invocation.",
        )
    if len(content) > MAX_INPUT_BYTES:
        raise ConnectError(
            "INPUT_ARTIFACT_INVALID",
            "The attachment size is unsupported.",
        )
    return _build_capability_job(
        capability,
        job_id=validate_job_id(job_id) if job_id is not None else str(uuid4()),
        artifact_id=str(uuid4()),
        media_type=media_type,
        byte_size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        filename=filename,
        parameters=parameters,
    )


def _summary_request(
    job_id: str, artifact: ArtifactIdentity, display_name: str
) -> dict[str, object]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "job_id": job_id,
        "capability": {"id": CAPABILITY_ID, "version": CAPABILITY_VERSION},
        "inputs": [
            {
                **artifact.public_dict(),
                "display_name": display_name,
                "source_app_id": SOURCE_APP_ID,
            }
        ],
    }


def restore_summary_job(
    *,
    job_id: str,
    artifact_id: str,
    media_type: str,
    byte_size: int,
    sha256: str,
    filename: str,
) -> PreparedSummaryJob:
    artifact = ArtifactIdentity(
        artifact_id=artifact_id,
        media_type=media_type,
        byte_size=byte_size,
        sha256=sha256,
    )
    display_name = _safe_display_name(filename)
    return PreparedSummaryJob(
        job_id,
        artifact,
        display_name,
        _summary_request(job_id, artifact, display_name),
    )


def prepare_summary_job(content: bytes, filename: str) -> PreparedSummaryJob:
    if not content or len(content) > MAX_INPUT_BYTES:
        raise ConnectError("INPUT_ARTIFACT_INVALID", "The PDF attachment size is unsupported.")
    return restore_summary_job(
        job_id=str(uuid4()),
        artifact_id=str(uuid4()),
        media_type=INPUT_MEDIA_TYPE,
        byte_size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        filename=filename,
    )


class ConnectClient:
    def __init__(
        self,
        provider: ProviderCapability,
        *,
        client: httpx.Client | None = None,
        poll_interval_seconds: float = 0.25,
        job_timeout_seconds: float = DEFAULT_JOB_TIMEOUT_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.provider = provider
        self._client = client
        self._poll_interval_seconds = poll_interval_seconds
        self._job_timeout_seconds = job_timeout_seconds
        self._sleep = sleep
        self._monotonic = monotonic

    def _request_client(self) -> tuple[httpx.Client, bool]:
        return (self._client, False) if self._client is not None else (_client(), True)

    def submit(self, job: PreparedSummaryJob, content: bytes) -> JobUpdate:
        if len(content) != job.artifact.byte_size or hashlib.sha256(content).hexdigest() != (
            job.artifact.sha256
        ):
            raise ConnectError(
                "INPUT_ARTIFACT_CHANGED", "The PDF attachment changed before handoff."
            )
        if job.artifact.byte_size > self.provider.max_input_bytes:
            raise ConnectError("INPUT_TOO_LARGE", "The PDF exceeds the provider's input limit.")
        encoded = json.dumps(
            job.request, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        active_client, owned = self._request_client()
        try:
            with active_client.stream(
                "POST",
                f"{self.provider.base_url}v1/jobs",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self.provider.token}",
                },
                files=[
                    ("request", ("request.json", encoded, "application/json")),
                    (
                        "artifact",
                        (job.display_name, BytesIO(content), INPUT_MEDIA_TYPE),
                    ),
                ],
            ) as response:
                if response.status_code not in {200, 202}:
                    raise _http_error(response)
                return self._job_update(response, job)
        except httpx.HTTPError as exc:
            raise ConnectError(
                "PROVIDER_UNAVAILABLE",
                "The local capability provider became unavailable.",
                retryable=True,
            ) from exc
        finally:
            if owned:
                active_client.close()

    def get(self, job: PreparedSummaryJob) -> JobUpdate:
        active_client, owned = self._request_client()
        try:
            with active_client.stream(
                "GET",
                f"{self.provider.base_url}v1/jobs/{job.job_id}",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self.provider.token}",
                },
            ) as response:
                if response.status_code != 200:
                    raise _http_error(response)
                return self._job_update(response, job)
        except httpx.HTTPError as exc:
            raise ConnectError(
                "PROVIDER_UNAVAILABLE",
                "The local capability provider became unavailable.",
                retryable=True,
            ) from exc
        finally:
            if owned:
                active_client.close()

    def wait_for_terminal(
        self,
        job: PreparedSummaryJob,
        initial: JobUpdate,
        on_update: Callable[[JobUpdate], None],
    ) -> JobUpdate:
        current = initial
        started = self._monotonic()
        while current.status not in {"completed", "failed"}:
            if self._monotonic() - started >= self._job_timeout_seconds:
                raise ConnectError(
                    "JOB_TIMEOUT",
                    "The local capability job did not finish before its timeout.",
                    retryable=True,
                )
            self._sleep(self._poll_interval_seconds)
            update = self.get(job)
            allowed = {
                "accepted": {"accepted", "processing", "completed", "failed"},
                "processing": {"processing", "completed", "failed"},
            }
            if update.status not in allowed.get(current.status, set()):
                raise ConnectError(
                    "JOB_STATE_INVALID",
                    "The local capability provider returned an invalid job transition.",
                )
            if update.status != current.status:
                on_update(update)
            current = update
        return current

    def _job_update(self, response: httpx.Response, job: PreparedSummaryJob) -> JobUpdate:
        try:
            status = _JobStatus.model_validate(_response_json(response, MAX_STATUS_BYTES))
        except (ValueError, TypeError) as exc:
            raise ConnectError("RESPONSE_INVALID", "Connect job status was invalid.") from exc
        expected = job.artifact
        if (
            status.job_id != job.job_id
            or status.capability.id != CAPABILITY_ID
            or status.capability.version != CAPABILITY_VERSION
            or status.provider.app_id != self.provider.app_id
            or status.provider.instance_id != self.provider.instance_id
            or len(status.input_artifacts) != 1
        ):
            raise ConnectError("RESPONSE_MISMATCH", "Connect job status did not match its request.")
        actual_input = status.input_artifacts[0]
        if actual_input.model_dump() != expected.public_dict():
            raise ConnectError("RESPONSE_MISMATCH", "Connect input provenance did not match.")

        result: SummaryResult | None = None
        error: ConnectError | None = None
        if status.result is not None:
            if len(status.result.outputs) != 1:
                raise ConnectError(
                    "RESPONSE_INVALID", "Connect returned an unsupported output set."
                )
            output = status.result.outputs[0]
            if output.content.input_artifact.model_dump() != expected.public_dict():
                raise ConnectError("RESPONSE_MISMATCH", "Summary provenance did not match.")
            content_bytes = json.dumps(
                output.content.model_dump(),
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
            if (
                output.byte_size != len(content_bytes)
                or output.sha256 != hashlib.sha256(content_bytes).hexdigest()
            ):
                raise ConnectError("OUTPUT_INTEGRITY_INVALID", "Summary integrity check failed.")
            result = SummaryResult(
                artifact_id=output.artifact_id,
                media_type=output.media_type,
                byte_size=output.byte_size,
                sha256=output.sha256,
                summary_version=output.content.summary_version,
                text=output.content.text,
                warnings=tuple(warning.model_dump() for warning in output.content.warnings),
            )
        if status.error is not None:
            error = ConnectError(
                status.error.code,
                status.error.message,
                retryable=status.error.retryable,
            )
        return JobUpdate(
            job_id=status.job_id,
            status=status.status,
            provider_app_id=status.provider.app_id,
            provider_instance_id=status.provider.instance_id,
            result=result,
            error=error,
        )


class ConnectV2Client:
    def __init__(
        self,
        capability: DiscoveredCapability,
        *,
        client: httpx.Client | None = None,
        poll_interval_seconds: float = 0.25,
        job_timeout_seconds: float = DEFAULT_JOB_TIMEOUT_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        if capability.protocol_version != GENERIC_PROTOCOL_VERSION:
            raise ValueError("ConnectV2Client requires a v2 capability")
        self.capability = capability
        self._client = client
        self._poll_interval_seconds = poll_interval_seconds
        self._job_timeout_seconds = job_timeout_seconds
        self._sleep = sleep
        self._monotonic = monotonic

    def _request_client(self) -> tuple[httpx.Client, bool]:
        return (self._client, False) if self._client is not None else (_client(), True)

    def _validate_job(
        self,
        job: PreparedCapabilityJob,
        *,
        require_compatible_input: bool,
    ) -> None:
        if (
            job.provider_app_id != self.capability.app_id
            or job.provider_app_version != self.capability.app_version
            or job.provider_instance_id != self.capability.instance_id
            or job.capability_id != self.capability.capability_id
            or job.capability_version != self.capability.capability_version
        ):
            raise ConnectError(
                "JOB_CAPABILITY_MISMATCH",
                "The prepared job does not match the selected capability.",
            )
        if require_compatible_input and not self.capability.accepts_artifact(
            job.artifact.media_type, job.artifact.byte_size
        ):
            raise ConnectError(
                "JOB_CAPABILITY_MISMATCH",
                "The prepared job input is not accepted by the selected capability.",
            )

    def submit(self, job: PreparedCapabilityJob, content: bytes) -> CapabilityJobUpdate:
        self._validate_job(job, require_compatible_input=True)
        if len(content) != job.artifact.byte_size or hashlib.sha256(content).hexdigest() != (
            job.artifact.sha256
        ):
            raise ConnectError(
                "INPUT_ARTIFACT_CHANGED",
                "The attachment changed before handoff.",
            )
        active_client, owned = self._request_client()
        try:
            with active_client.stream(
                "POST",
                f"{self.capability.base_url}v2/jobs",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self.capability.token}",
                },
                files=[
                    (
                        "request",
                        ("request.json", job.request_json, "application/json"),
                    ),
                    (
                        "artifact",
                        (job.display_name, BytesIO(content), job.artifact.media_type),
                    ),
                ],
            ) as response:
                if response.status_code not in {200, 202}:
                    raise _http_error_v2(response)
                return self._job_update(response, job)
        except httpx.HTTPError as exc:
            raise ConnectError(
                "PROVIDER_UNAVAILABLE",
                "The local capability provider became unavailable.",
                retryable=True,
            ) from exc
        finally:
            if owned:
                active_client.close()

    def get(self, job: PreparedCapabilityJob) -> CapabilityJobUpdate:
        self._validate_job(job, require_compatible_input=False)
        active_client, owned = self._request_client()
        try:
            with active_client.stream(
                "GET",
                f"{self.capability.base_url}v2/jobs/{job.job_id}",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self.capability.token}",
                },
            ) as response:
                if response.status_code != 200:
                    raise _http_error_v2(response)
                return self._job_update(response, job)
        except httpx.HTTPError as exc:
            raise ConnectError(
                "PROVIDER_UNAVAILABLE",
                "The local capability provider became unavailable.",
                retryable=True,
            ) from exc
        finally:
            if owned:
                active_client.close()

    def wait_for_terminal(
        self,
        job: PreparedCapabilityJob,
        initial: CapabilityJobUpdate,
        on_update: Callable[[CapabilityJobUpdate], None],
    ) -> CapabilityJobUpdate:
        current = initial
        started = self._monotonic()
        while current.status not in {"completed", "failed"}:
            if self._monotonic() - started >= self._job_timeout_seconds:
                raise ConnectError(
                    "JOB_TIMEOUT",
                    "The local capability job did not finish before its timeout.",
                    retryable=True,
                )
            self._sleep(self._poll_interval_seconds)
            update = self.get(job)
            allowed = {
                "accepted": {"accepted", "processing", "completed", "failed"},
                "processing": {"processing", "completed", "failed"},
            }
            if update.status not in allowed.get(current.status, set()):
                raise ConnectError(
                    "JOB_STATE_INVALID",
                    "The local capability provider returned an invalid job transition.",
                )
            if update.status != current.status:
                on_update(update)
            current = update
        return current

    def _job_update(
        self, response: httpx.Response, job: PreparedCapabilityJob
    ) -> CapabilityJobUpdate:
        try:
            status = _JobStatusV2.model_validate(
                _response_json(response, MAX_GENERIC_STATUS_BYTES)
            )
        except (ValueError, TypeError) as exc:
            raise ConnectError("RESPONSE_INVALID", "Connect job status was invalid.") from exc
        expected = job.artifact
        if (
            status.job_id != job.job_id
            or status.capability.id != job.capability_id
            or status.capability.version != job.capability_version
            or status.provider.app_id != self.capability.app_id
            or status.provider.instance_id != self.capability.instance_id
            or status.input_artifacts[0].model_dump() != expected.public_dict()
        ):
            raise ConnectError("RESPONSE_MISMATCH", "Connect job status did not match its request.")

        result: CapabilityResult | None = None
        error: ConnectError | None = None
        if status.result is not None:
            outputs: list[CapabilityOutput] = []
            for output in status.result.outputs:
                if output.artifact_id == expected.artifact_id:
                    raise ConnectError(
                        "RESPONSE_MISMATCH",
                        "Connect output identity cannot alias its input.",
                    )
                if output.media_type not in self.capability.produces:
                    raise ConnectError(
                        "RESPONSE_MISMATCH",
                        "Connect output type was not declared by the capability.",
                    )
                try:
                    payload = base64.b64decode(output.payload_base64, validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise ConnectError(
                        "OUTPUT_INTEGRITY_INVALID",
                        "Connect output encoding was invalid.",
                    ) from exc
                if (
                    base64.b64encode(payload).decode("ascii") != output.payload_base64
                    or len(payload) != output.byte_size
                    or hashlib.sha256(payload).hexdigest() != output.sha256
                ):
                    raise ConnectError(
                        "OUTPUT_INTEGRITY_INVALID",
                        "Connect output integrity check failed.",
                    )
                outputs.append(
                    CapabilityOutput(
                        artifact_id=output.artifact_id,
                        media_type=output.media_type,
                        display_name=output.display_name,
                        byte_size=output.byte_size,
                        sha256=output.sha256,
                        payload=payload,
                    )
                )
            result = CapabilityResult(tuple(outputs))
        if status.error is not None:
            error = ConnectError(
                status.error.code,
                status.error.message,
                retryable=status.error.retryable,
            )
        return CapabilityJobUpdate(
            job_id=status.job_id,
            status=status.status,
            provider_app_id=status.provider.app_id,
            provider_instance_id=status.provider.instance_id,
            result=result,
            error=error,
        )


def capability_matches_attachment(media_type: str, byte_size: int) -> bool:
    return media_type.casefold() == INPUT_MEDIA_TYPE and 0 < byte_size <= MAX_INPUT_BYTES
