from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import sqlite3
import uuid
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import MAX_RETENTION_DAYS
from .mailbox import DEFAULT_MAIL_ACCOUNT_ID, DEFAULT_MAIL_PROVIDER
from .mime import AttachmentDescriptor

SCHEMA_VERSION = 12
MAX_CONNECT_REQUEST_BYTES = 128 * 1024
MAX_CONNECT_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_CONNECT_RESULT_BYTES = 24 * 1024 * 1024
MAX_CONNECT_RESULT_METADATA_BYTES = 64 * 1024


def _sqlite_casefold(value: object) -> str:
    return value.casefold() if isinstance(value, str) else ""


def _sqlite_aware_iso_epoch(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return None
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        return (parsed.astimezone(UTC) - epoch).total_seconds()
    except (OverflowError, ValueError):
        return None


def _message_suppression_key(provider: str, account_id: str, provider_message_id: str) -> str:
    encoded = "\0".join((provider, account_id, provider_message_id)).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _legacy_message_suppression_key(provider_message_id: str) -> str:
    return hashlib.sha256(provider_message_id.encode("utf-8")).hexdigest()


def _suppression_expiry(received_at: str, now: datetime) -> str:
    try:
        received = datetime.fromisoformat(received_at)
        if received.tzinfo is None:
            raise ValueError("received_at must include a timezone")
        received = received.astimezone(UTC)
    except (OverflowError, ValueError):
        received = now
    # A future-dated source message must not create an effectively unbounded marker.
    return (min(received, now) + timedelta(days=MAX_RETENTION_DAYS)).isoformat()


_CONNECT_JOBS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS connect_attachment_jobs (
    job_id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL,
    part_id TEXT NOT NULL,
    protocol_version INTEGER NOT NULL DEFAULT 1 CHECK (protocol_version IN (1, 2)),
    capability_id TEXT NOT NULL,
    capability_version TEXT NOT NULL,
    provider_app_id TEXT,
    provider_app_version TEXT,
    provider_instance_id TEXT,
    invocation_fingerprint TEXT NOT NULL CHECK (
        (protocol_version = 1 AND invocation_fingerprint = 'v1')
        OR (protocol_version = 2 AND length(invocation_fingerprint) = 64)
    ),
    input_artifact_id TEXT NOT NULL,
    input_media_type TEXT NOT NULL,
    input_byte_size INTEGER NOT NULL CHECK (
        (protocol_version = 1 AND input_byte_size > 0)
        OR (protocol_version = 2 AND input_byte_size >= 0)
    ),
    input_sha256 TEXT NOT NULL CHECK (length(input_sha256) = 64),
    input_display_name TEXT,
    source_app_id TEXT,
    request_json BLOB CHECK (
        request_json IS NULL OR (
            typeof(request_json) = 'blob'
            AND length(request_json) BETWEEN 1 AND 131072
        )
    ),
    status TEXT NOT NULL CHECK (
        status IN ('requested', 'accepted', 'processing', 'completed', 'failed')
    ),
    output_artifact_id TEXT,
    output_media_type TEXT,
    output_byte_size INTEGER CHECK (
        output_byte_size IS NULL OR output_byte_size > 0
    ),
    output_sha256 TEXT CHECK (
        output_sha256 IS NULL OR length(output_sha256) = 64
    ),
    summary_version TEXT,
    summary_text TEXT,
    warnings_json TEXT,
    result_json BLOB CHECK (
        result_json IS NULL OR (
            typeof(result_json) = 'blob'
            AND length(result_json) BETWEEN 1 AND 25165824
        )
    ),
    result_metadata_json BLOB CHECK (
        result_metadata_json IS NULL OR (
            typeof(result_metadata_json) = 'blob'
            AND length(result_metadata_json) BETWEEN 1 AND 65536
        )
    ),
    error_code TEXT,
    error_message TEXT,
    error_retryable INTEGER CHECK (
        error_retryable IS NULL OR error_retryable IN (0, 1)
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        protocol_version = 1 OR (
            provider_app_id IS NOT NULL
            AND provider_app_version IS NOT NULL
            AND provider_instance_id IS NOT NULL
            AND input_display_name IS NOT NULL
            AND length(input_display_name) > 0
            AND source_app_id IS NOT NULL
            AND length(source_app_id) > 0
            AND request_json IS NOT NULL
        )
    ),
    CHECK (
        (status = 'completed'
            AND error_code IS NULL
            AND error_message IS NULL
            AND error_retryable IS NULL
            AND (
                (protocol_version = 1
                    AND output_artifact_id IS NOT NULL
                    AND output_media_type IS NOT NULL
                    AND output_byte_size IS NOT NULL
                    AND output_sha256 IS NOT NULL
                    AND summary_version IS NOT NULL
                    AND summary_text IS NOT NULL
                    AND length(summary_text) > 0
                    AND warnings_json IS NOT NULL
                    AND result_json IS NULL
                    AND result_metadata_json IS NULL)
                OR (protocol_version = 2
                    AND output_artifact_id IS NULL
                    AND output_media_type IS NULL
                    AND output_byte_size IS NULL
                    AND output_sha256 IS NULL
                    AND summary_version IS NULL
                    AND summary_text IS NULL
                    AND warnings_json IS NULL
                    AND result_json IS NOT NULL
                    AND result_metadata_json IS NOT NULL)
            ))
        OR (status = 'failed'
            AND error_code IS NOT NULL
            AND error_message IS NOT NULL
            AND error_retryable IS NOT NULL
            AND output_artifact_id IS NULL
            AND output_media_type IS NULL
            AND output_byte_size IS NULL
            AND output_sha256 IS NULL
            AND summary_version IS NULL
            AND summary_text IS NULL
            AND warnings_json IS NULL
            AND result_json IS NULL
            AND result_metadata_json IS NULL)
        OR (status IN ('requested', 'accepted', 'processing')
            AND output_artifact_id IS NULL
            AND output_media_type IS NULL
            AND output_byte_size IS NULL
            AND output_sha256 IS NULL
            AND summary_version IS NULL
            AND summary_text IS NULL
            AND warnings_json IS NULL
            AND result_json IS NULL
            AND result_metadata_json IS NULL
            AND error_code IS NULL
            AND error_message IS NULL
            AND error_retryable IS NULL)
    )
)
"""

_CONNECT_JOBS_LOOKUP_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_connect_attachment_jobs_lookup
ON connect_attachment_jobs(
    message_id, part_id, protocol_version, capability_id, capability_version,
    invocation_fingerprint, created_at
)
"""

_CONNECT_JOBS_ACTIVE_INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_connect_attachment_jobs_active
ON connect_attachment_jobs(
    message_id, part_id, protocol_version, capability_id, capability_version,
    invocation_fingerprint
)
WHERE status IN ('requested', 'accepted', 'processing')
  AND protocol_version = 1
"""

_CONNECT_JOBS_DELETE_TRIGGER_SQL = """
CREATE TRIGGER IF NOT EXISTS messages_delete_connect_attachment_jobs
AFTER DELETE ON messages
BEGIN
    DELETE FROM connect_attachment_jobs WHERE message_id = OLD.message_id;
END
"""


@dataclass(frozen=True)
class AnalysisRequest:
    request_id: str
    context_at: str
    body_char_limit: int


@dataclass(frozen=True)
class CalendarGrant:
    account_id: str
    profile: str
    state: str
    principal_key: str | None
    home_account_id: str | None
    tenant_id: str | None
    object_id: str | None
    email_address: str | None
    updated_at: str


@dataclass(frozen=True)
class CalendarWindow:
    account_id: str
    principal_key: str
    window_start: str
    window_end: str
    cursor: str
    updated_at: str


@dataclass(frozen=True)
class CalendarEventProjection:
    event_id: str
    subject: str
    start_date_time: str
    start_time_zone: str
    end_date_time: str
    end_time_zone: str
    is_all_day: bool
    location: str


@dataclass(frozen=True)
class CalendarEventMutation:
    event_id: str
    event: CalendarEventProjection | None


@dataclass(frozen=True)
class PendingMessage:
    message_id: str
    provider: str
    account_id: str
    provider_message_id: str
    thread_id: str | None
    sender: str
    sender_name: str | None
    subject: str
    received_at: str
    attempts: int
    fallback_notified_at: str | None
    analysis_request_id: str | None
    analysis_context_at: str | None
    analysis_body_char_limit: int | None


@dataclass(frozen=True)
class AnalyzedMessage:
    message_id: str
    sender: str
    sender_name: str | None
    subject: str
    received_at: str
    attempts: int
    fallback_notified_at: str | None
    category: str
    priority: str
    summary: str
    action_required: int
    suggested_action: str | None
    deadline_text: str | None
    deadline_iso: str | None
    confidence: float


@dataclass(frozen=True)
class NotificationIntent:
    message_id: str
    kind: str
    sender: str
    sender_name: str | None
    subject: str
    analysis_at: str | None
    priority: str | None
    summary: str | None
    suggested_action: str | None
    deadline_iso: str | None
    last_error: str | None


@dataclass(frozen=True)
class MessageSource:
    message_id: str
    provider: str
    account_id: str
    provider_message_id: str


@dataclass(frozen=True)
class MailAccount:
    provider: str
    account_id: str
    display_name: str
    address: str | None
    active: bool
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ConnectJob:
    job_id: str
    message_id: str
    part_id: str
    protocol_version: int
    capability_id: str
    capability_version: str
    provider_app_id: str | None
    provider_app_version: str | None
    provider_instance_id: str | None
    invocation_fingerprint: str
    input_artifact_id: str
    input_media_type: str
    input_byte_size: int
    input_sha256: str
    input_display_name: str | None
    source_app_id: str | None
    request_json: bytes | None = field(repr=False)
    status: str
    output_artifact_id: str | None
    output_media_type: str | None
    output_byte_size: int | None
    output_sha256: str | None
    summary_version: str | None
    summary_text: str | None
    warnings_json: str | None
    result_json: bytes | None = field(repr=False)
    result_metadata_json: bytes | None = field(repr=False)
    error_code: str | None
    error_message: str | None
    error_retryable: int | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ConnectOutput:
    artifact_id: str
    media_type: str
    display_name: str
    byte_size: int
    sha256: str
    payload: bytes = field(repr=False)

    def metadata(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "media_type": self.media_type,
            "display_name": self.display_name,
            "byte_size": self.byte_size,
            "sha256": self.sha256,
        }


def _ensure_connect_jobs_schema(db: sqlite3.Connection, current_version: int) -> None:
    table_exists = (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'connect_attachment_jobs'"
        ).fetchone()
        is not None
    )
    legacy_table = False
    if table_exists:
        columns = {
            str(row["name"])
            for row in db.execute("PRAGMA table_info(connect_attachment_jobs)").fetchall()
        }
        legacy_table = "protocol_version" not in columns
    if legacy_table:
        db.execute("DROP TRIGGER IF EXISTS messages_delete_connect_attachment_jobs")
        db.execute("DROP INDEX IF EXISTS idx_connect_attachment_jobs_lookup")
        db.execute("DROP INDEX IF EXISTS idx_connect_attachment_jobs_active")
        db.execute("ALTER TABLE connect_attachment_jobs RENAME TO connect_attachment_jobs_v5")
    elif table_exists and current_version < 7:
        db.execute("DROP INDEX IF EXISTS idx_connect_attachment_jobs_active")

    db.execute(_CONNECT_JOBS_TABLE_SQL)
    db.execute(_CONNECT_JOBS_LOOKUP_INDEX_SQL)
    db.execute(_CONNECT_JOBS_ACTIVE_INDEX_SQL)
    db.execute(_CONNECT_JOBS_DELETE_TRIGGER_SQL)

    if not legacy_table:
        return
    db.execute(
        """INSERT INTO connect_attachment_jobs(
            job_id, message_id, part_id, protocol_version,
            capability_id, capability_version,
            provider_app_id, provider_app_version, provider_instance_id,
            invocation_fingerprint,
            input_artifact_id, input_media_type, input_byte_size, input_sha256,
            input_display_name, source_app_id, request_json, status,
            output_artifact_id, output_media_type, output_byte_size, output_sha256,
            summary_version, summary_text, warnings_json, result_json,
            result_metadata_json,
            error_code, error_message, error_retryable, created_at, updated_at
        )
        SELECT
            job_id, message_id, part_id, 1,
            capability_id, capability_version,
            provider_app_id, NULL, provider_instance_id,
            'v1',
            input_artifact_id, input_media_type, input_byte_size, input_sha256,
            NULL, 'email-watcher', NULL, status,
            output_artifact_id, output_media_type, output_byte_size, output_sha256,
            summary_version, summary_text, warnings_json, NULL, NULL,
            error_code, error_message, error_retryable, created_at, updated_at
        FROM connect_attachment_jobs_v5
        ORDER BY rowid"""
    )
    db.execute("DROP TABLE connect_attachment_jobs_v5")


def _valid_uuid_v4(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return parsed.version == 4 and str(parsed) == value


def _decode_v2_request(request_json: bytes) -> dict[str, object]:
    if not 0 < len(request_json) <= MAX_CONNECT_REQUEST_BYTES:
        raise ValueError("Connect v2 request bytes are invalid")
    try:
        request = json.loads(request_json)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("Connect v2 request bytes are invalid") from exc
    if not isinstance(request, dict) or set(request) != {
        "protocol_version",
        "job_id",
        "capability",
        "inputs",
        "parameters",
    }:
        raise ValueError("Connect v2 request provenance is invalid")
    return request


def _canonical_v2_parameters(value: object) -> dict[str, str | int | bool]:
    if not isinstance(value, dict) or len(value) > 16:
        raise ValueError("Connect v2 request provenance is invalid")
    normalized: dict[str, str | int | bool] = {}
    for name, raw in value.items():
        if not isinstance(name, str) or not 0 < len(name) <= 100:
            raise ValueError("Connect v2 request provenance is invalid")
        if (
            isinstance(raw, str)
            and len(raw) <= 1000
            or type(raw) is bool
            or type(raw) is int
            and -9_007_199_254_740_991 <= raw <= 9_007_199_254_740_991
        ):
            normalized[name] = raw
        elif (
            type(raw) is float
            and math.isfinite(raw)
            and raw.is_integer()
            and -9_007_199_254_740_991 <= raw <= 9_007_199_254_740_991
        ):
            normalized[name] = int(raw)
        else:
            raise ValueError("Connect v2 request provenance is invalid")
    return normalized


def _v2_invocation_fingerprint(
    request: dict[str, object],
    *,
    capability_id: str,
    capability_version: str,
    provider_app_id: str,
    provider_app_version: str,
    provider_instance_id: str,
) -> str:
    capability = request.get("capability")
    inputs = request.get("inputs")
    if (
        request.get("protocol_version") != 2
        or capability != {"id": capability_id, "version": capability_version}
        or not isinstance(inputs, list)
        or len(inputs) != 1
        or not isinstance(inputs[0], dict)
        or set(inputs[0])
        != {
            "artifact_id",
            "media_type",
            "byte_size",
            "sha256",
            "display_name",
            "source_app_id",
        }
    ):
        raise ValueError("Connect v2 request provenance is invalid")
    input_artifact = inputs[0]
    identity = {
        "protocol_version": 2,
        "provider": {
            "app_id": provider_app_id,
            "version": provider_app_version,
            "instance_id": provider_instance_id,
        },
        "capability": capability,
        "input": {
            key: input_artifact[key]
            for key in (
                "media_type",
                "byte_size",
                "sha256",
                "display_name",
                "source_app_id",
            )
        },
        "parameters": _canonical_v2_parameters(request.get("parameters")),
    }
    encoded = json.dumps(
        identity,
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_v2_request_record(
    request_json: bytes,
    *,
    job_id: str,
    capability_id: str,
    capability_version: str,
    provider_app_id: str,
    provider_app_version: str,
    provider_instance_id: str,
    input_artifact_id: str,
    input_media_type: str,
    input_byte_size: int,
    input_sha256: str,
    input_display_name: str,
    source_app_id: str,
) -> str:
    request = _decode_v2_request(request_json)
    inputs = request.get("inputs")
    if (
        request.get("job_id") != job_id
        or not isinstance(inputs, list)
        or len(inputs) != 1
        or inputs[0]
        != {
            "artifact_id": input_artifact_id,
            "media_type": input_media_type,
            "byte_size": input_byte_size,
            "sha256": input_sha256,
            "display_name": input_display_name,
            "source_app_id": source_app_id,
        }
    ):
        raise ValueError("Connect v2 request provenance is invalid")
    return _v2_invocation_fingerprint(
        request,
        capability_id=capability_id,
        capability_version=capability_version,
        provider_app_id=provider_app_id,
        provider_app_version=provider_app_version,
        provider_instance_id=provider_instance_id,
    )


def _lookup_invocation_fingerprint(
    *,
    protocol_version: int,
    capability_id: str,
    capability_version: str,
    provider_app_id: str | None,
    provider_app_version: str | None,
    provider_instance_id: str | None,
    request_json: bytes | None,
) -> str:
    if protocol_version == 1:
        if any(
            value is not None
            for value in (
                provider_app_version,
                request_json,
            )
        ):
            raise ValueError("Connect v1 lookup cannot use v2 request identity")
        return "v1"
    if protocol_version != 2:
        raise ValueError("Connect protocol version is unsupported")
    if (
        not provider_app_id
        or not provider_app_version
        or not provider_instance_id
        or request_json is None
    ):
        raise ValueError("Connect v2 lookup identity is incomplete")
    return _v2_invocation_fingerprint(
        _decode_v2_request(request_json),
        capability_id=capability_id,
        capability_version=capability_version,
        provider_app_id=provider_app_id,
        provider_app_version=provider_app_version,
        provider_instance_id=provider_instance_id,
    )


def _validate_generic_result(value: object) -> tuple[ConnectOutput, ...]:
    if not isinstance(value, dict) or set(value) != {"outputs"}:
        raise ValueError("Completed Connect v2 result is invalid")
    raw_outputs = value["outputs"]
    if not isinstance(raw_outputs, list) or not 1 <= len(raw_outputs) <= 8:
        raise ValueError("Completed Connect v2 result is invalid")

    outputs: list[ConnectOutput] = []
    artifact_ids: set[str] = set()
    expected_keys = {
        "artifact_id",
        "media_type",
        "display_name",
        "byte_size",
        "sha256",
        "payload_base64",
    }
    for raw_output in raw_outputs:
        if not isinstance(raw_output, dict) or set(raw_output) != expected_keys:
            raise ValueError("Completed Connect v2 result is invalid")
        artifact_id = raw_output["artifact_id"]
        media_type = raw_output["media_type"]
        display_name = raw_output["display_name"]
        byte_size = raw_output["byte_size"]
        sha256 = raw_output["sha256"]
        payload_base64 = raw_output["payload_base64"]
        if (
            not _valid_uuid_v4(artifact_id)
            or artifact_id in artifact_ids
            or not isinstance(media_type, str)
            or not 0 < len(media_type) <= 127
            or media_type != media_type.casefold()
            or not isinstance(display_name, str)
            or not 0 < len(display_name) <= 255
            or type(byte_size) is not int
            or not 0 <= byte_size <= MAX_CONNECT_OUTPUT_BYTES
            or not isinstance(sha256, str)
            or len(sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256)
            or not isinstance(payload_base64, str)
        ):
            raise ValueError("Completed Connect v2 result is invalid")
        try:
            payload = base64.b64decode(payload_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("Completed Connect v2 result is invalid") from exc
        if (
            base64.b64encode(payload).decode("ascii") != payload_base64
            or len(payload) != byte_size
            or hashlib.sha256(payload).hexdigest() != sha256
        ):
            raise ValueError("Completed Connect v2 result failed integrity validation")
        artifact_ids.add(artifact_id)
        outputs.append(
            ConnectOutput(
                artifact_id=artifact_id,
                media_type=media_type,
                display_name=display_name,
                byte_size=byte_size,
                sha256=sha256,
                payload=payload,
            )
        )
    return tuple(outputs)


def _encode_generic_result(result: dict[str, object]) -> tuple[bytes, bytes]:
    outputs = _validate_generic_result(result)
    metadata = {"outputs": [output.metadata() for output in outputs]}
    encoded = json.dumps(
        {
            "outputs": [
                {
                    **output.metadata(),
                    "payload_base64": base64.b64encode(output.payload).decode("ascii"),
                }
                for output in outputs
            ]
        },
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    if len(encoded) > MAX_CONNECT_RESULT_BYTES:
        raise ValueError("Completed Connect v2 result is too large")
    encoded_metadata = json.dumps(
        metadata,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    if len(encoded_metadata) > MAX_CONNECT_RESULT_METADATA_BYTES:
        raise ValueError("Completed Connect v2 result metadata is too large")
    return encoded, encoded_metadata


def _decode_generic_result(result_json: bytes) -> tuple[ConnectOutput, ...]:
    if not 0 < len(result_json) <= MAX_CONNECT_RESULT_BYTES:
        raise RuntimeError("Completed Connect v2 result is invalid")
    try:
        value = json.loads(result_json)
        return _validate_generic_result(value)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError("Completed Connect v2 result is invalid") from exc


def _decode_generic_result_metadata(
    result_metadata_json: bytes,
) -> list[dict[str, object]]:
    if not 0 < len(result_metadata_json) <= MAX_CONNECT_RESULT_METADATA_BYTES:
        raise RuntimeError("Completed Connect v2 result metadata is invalid")
    try:
        value = json.loads(result_metadata_json)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("Completed Connect v2 result metadata is invalid") from exc
    if not isinstance(value, dict) or set(value) != {"outputs"}:
        raise RuntimeError("Completed Connect v2 result metadata is invalid")
    outputs = value["outputs"]
    if not isinstance(outputs, list) or not 1 <= len(outputs) <= 8:
        raise RuntimeError("Completed Connect v2 result metadata is invalid")
    artifact_ids: set[str] = set()
    for output in outputs:
        if not isinstance(output, dict) or set(output) != {
            "artifact_id",
            "media_type",
            "display_name",
            "byte_size",
            "sha256",
        }:
            raise RuntimeError("Completed Connect v2 result metadata is invalid")
        artifact_id = output["artifact_id"]
        media_type = output["media_type"]
        display_name = output["display_name"]
        byte_size = output["byte_size"]
        sha256 = output["sha256"]
        if (
            not _valid_uuid_v4(artifact_id)
            or artifact_id in artifact_ids
            or not isinstance(media_type, str)
            or not 0 < len(media_type) <= 127
            or media_type != media_type.casefold()
            or not isinstance(display_name, str)
            or not 0 < len(display_name) <= 255
            or type(byte_size) is not int
            or not 0 <= byte_size <= MAX_CONNECT_OUTPUT_BYTES
            or not isinstance(sha256, str)
            or len(sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256)
        ):
            raise RuntimeError("Completed Connect v2 result metadata is invalid")
        artifact_ids.add(artifact_id)
    return outputs


def _ensure_mailbox_scope_schema(db: sqlite3.Connection) -> None:
    message_columns = {
        str(row["name"]) for row in db.execute("PRAGMA table_info(messages)").fetchall()
    }
    if "provider" not in message_columns:
        db.execute("ALTER TABLE messages ADD COLUMN provider TEXT NOT NULL DEFAULT 'gmail'")
    if "account_id" not in message_columns:
        db.execute(
            "ALTER TABLE messages ADD COLUMN account_id TEXT NOT NULL DEFAULT 'gmail-default'"
        )
    if "provider_message_id" not in message_columns:
        db.execute("ALTER TABLE messages ADD COLUMN provider_message_id TEXT")
        db.execute("UPDATE messages SET provider_message_id = message_id")
    db.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_source_identity
        ON messages(provider, account_id, provider_message_id)"""
    )
    db.execute(
        """
        CREATE TRIGGER IF NOT EXISTS messages_require_source_identity_insert
        BEFORE INSERT ON messages
        WHEN NEW.provider = '' OR NEW.account_id = ''
          OR NEW.provider_message_id IS NULL OR NEW.provider_message_id = ''
        BEGIN
            SELECT RAISE(ABORT, 'message source identity is required');
        END
        """
    )
    db.execute(
        """
        CREATE TRIGGER IF NOT EXISTS messages_require_source_identity_update
        BEFORE UPDATE OF provider, account_id, provider_message_id ON messages
        WHEN NEW.provider = '' OR NEW.account_id = ''
          OR NEW.provider_message_id IS NULL OR NEW.provider_message_id = ''
        BEGIN
            SELECT RAISE(ABORT, 'message source identity is required');
        END
        """
    )

    state_columns = {
        str(row["name"]) for row in db.execute("PRAGMA table_info(mailbox_state)").fetchall()
    }
    if "provider" not in state_columns:
        db.execute("ALTER TABLE mailbox_state RENAME TO mailbox_state_v8")
        db.execute(
            """CREATE TABLE mailbox_state (
                provider TEXT NOT NULL,
                account_id TEXT NOT NULL,
                cursor TEXT NOT NULL,
                last_success_at TEXT NOT NULL,
                PRIMARY KEY (provider, account_id)
            )"""
        )
        db.execute(
            """INSERT INTO mailbox_state(provider, account_id, cursor, last_success_at)
            SELECT ?, ?, history_id, last_success_at FROM mailbox_state_v8 WHERE id = 1""",
            (DEFAULT_MAIL_PROVIDER, DEFAULT_MAIL_ACCOUNT_ID),
        )
        db.execute("DROP TABLE mailbox_state_v8")

    suppression_columns = {
        str(row["name"]) for row in db.execute("PRAGMA table_info(suppressed_messages)").fetchall()
    }
    if "provider" not in suppression_columns:
        db.execute("DROP INDEX IF EXISTS idx_suppressed_messages_expiry")
        db.execute("ALTER TABLE suppressed_messages RENAME TO suppressed_messages_v8")
        db.execute(
            """CREATE TABLE suppressed_messages (
                provider TEXT NOT NULL,
                account_id TEXT NOT NULL,
                message_key TEXT NOT NULL CHECK (length(message_key) = 64),
                expires_at TEXT NOT NULL,
                PRIMARY KEY (provider, account_id, message_key)
            )"""
        )
        db.execute(
            """INSERT INTO suppressed_messages(
                provider, account_id, message_key, expires_at
            ) SELECT ?, ?, message_key, expires_at FROM suppressed_messages_v8""",
            (DEFAULT_MAIL_PROVIDER, DEFAULT_MAIL_ACCOUNT_ID),
        )
        db.execute("DROP TABLE suppressed_messages_v8")
        db.execute(
            """CREATE INDEX idx_suppressed_messages_expiry
            ON suppressed_messages(expires_at)"""
        )


class Store:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.create_function("casefold", 1, _sqlite_casefold, deterministic=True)
        connection.create_function(
            "aware_iso_epoch",
            1,
            _sqlite_aware_iso_epoch,
            deterministic=True,
        )
        try:
            yield connection
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database schema version {version} is newer than supported "
                    f"version {SCHEMA_VERSION}"
                )
            db.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS mailbox_state (
                    provider TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    cursor TEXT NOT NULL,
                    last_success_at TEXT NOT NULL,
                    PRIMARY KEY (provider, account_id)
                );
                CREATE TABLE IF NOT EXISTS mail_accounts (
                    provider TEXT NOT NULL CHECK (provider <> ''),
                    account_id TEXT NOT NULL CHECK (account_id <> ''),
                    display_name TEXT NOT NULL CHECK (display_name <> ''),
                    address TEXT CHECK (address IS NULL OR address <> ''),
                    active INTEGER NOT NULL CHECK (active IN (0, 1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (provider, account_id),
                    UNIQUE (provider, address)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_mail_accounts_one_active
                    ON mail_accounts(active) WHERE active = 1;
                CREATE TABLE IF NOT EXISTS microsoft_calendar_grants (
                    account_id TEXT NOT NULL CHECK (account_id <> ''),
                    profile TEXT NOT NULL CHECK (profile IN ('read', 'proposal', 'write')),
                    state TEXT NOT NULL CHECK (
                        state IN (
                            'not_requested', 'consent_pending', 'ready', 'rejected', 'revoked'
                        )
                    ),
                    principal_key TEXT CHECK (
                        principal_key IS NULL OR length(principal_key) = 64
                    ),
                    home_account_id TEXT,
                    tenant_id TEXT,
                    object_id TEXT,
                    email_address TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (account_id, profile),
                    CHECK (
                        state <> 'ready' OR (
                            principal_key IS NOT NULL
                            AND home_account_id IS NOT NULL
                            AND tenant_id IS NOT NULL
                            AND object_id IS NOT NULL
                            AND email_address IS NOT NULL
                        )
                    )
                );
                CREATE TABLE IF NOT EXISTS microsoft_calendar_windows (
                    account_id TEXT PRIMARY KEY CHECK (account_id <> ''),
                    principal_key TEXT NOT NULL CHECK (length(principal_key) = 64),
                    window_start TEXT NOT NULL CHECK (window_start <> ''),
                    window_end TEXT NOT NULL CHECK (window_end <> ''),
                    cursor TEXT NOT NULL CHECK (
                        cursor <> '' AND length(CAST(cursor AS BLOB)) <= 32768
                    ),
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS microsoft_calendar_events (
                    account_id TEXT NOT NULL CHECK (account_id <> ''),
                    event_id TEXT NOT NULL CHECK (
                        event_id <> '' AND length(CAST(event_id AS BLOB)) <= 512
                    ),
                    subject TEXT NOT NULL CHECK (length(CAST(subject AS BLOB)) <= 512),
                    start_date_time TEXT NOT NULL CHECK (
                        start_date_time <> ''
                        AND length(CAST(start_date_time AS BLOB)) <= 64
                    ),
                    start_time_zone TEXT NOT NULL CHECK (
                        start_time_zone <> ''
                        AND length(CAST(start_time_zone AS BLOB)) <= 128
                    ),
                    end_date_time TEXT NOT NULL CHECK (
                        end_date_time <> ''
                        AND length(CAST(end_date_time AS BLOB)) <= 64
                    ),
                    end_time_zone TEXT NOT NULL CHECK (
                        end_time_zone <> ''
                        AND length(CAST(end_time_zone AS BLOB)) <= 128
                    ),
                    is_all_day INTEGER NOT NULL CHECK (is_all_day IN (0, 1)),
                    location TEXT NOT NULL CHECK (length(CAST(location AS BLOB)) <= 512),
                    PRIMARY KEY (account_id, event_id)
                );
                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    provider_message_id TEXT NOT NULL,
                    thread_id TEXT,
                    sender TEXT NOT NULL,
                    sender_name TEXT,
                    subject TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    discovered_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    analysis_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_retry_at TEXT,
                    analysis_request_id TEXT,
                    analysis_context_at TEXT,
                    analysis_body_char_limit INTEGER,
                    analysis_retryable INTEGER,
                    analysis_error_code TEXT,
                    analysis_retry_after_seconds INTEGER,
                    fallback_notified_at TEXT,
                    notified_at TEXT,
                    category TEXT,
                    priority TEXT,
                    summary TEXT,
                    action_required INTEGER,
                    suggested_action TEXT,
                    deadline_text TEXT,
                    deadline_iso TEXT,
                    confidence REAL,
                    last_error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_messages_pending
                    ON messages(status, next_retry_at);
                CREATE INDEX IF NOT EXISTS idx_messages_inbox_order
                    ON messages(received_at DESC, message_id DESC);
                CREATE TABLE IF NOT EXISTS suppressed_messages (
                    provider TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    message_key TEXT NOT NULL CHECK (length(message_key) = 64),
                    expires_at TEXT NOT NULL,
                    PRIMARY KEY (provider, account_id, message_key)
                );
                CREATE INDEX IF NOT EXISTS idx_suppressed_messages_expiry
                    ON suppressed_messages(expires_at);
                CREATE TABLE IF NOT EXISTS message_attachments (
                    message_id TEXT NOT NULL,
                    part_id TEXT NOT NULL,
                    attachment_id TEXT,
                    filename TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
                    position INTEGER NOT NULL CHECK (position >= 0),
                    PRIMARY KEY (message_id, part_id),
                    UNIQUE (message_id, position)
                );
                CREATE INDEX IF NOT EXISTS idx_message_attachments_message
                    ON message_attachments(message_id, position);
                CREATE TRIGGER IF NOT EXISTS messages_delete_attachments
                AFTER DELETE ON messages
                BEGIN
                    DELETE FROM message_attachments WHERE message_id = OLD.message_id;
                END;
                CREATE TABLE IF NOT EXISTS outbound_sends (
                    dedupe_key TEXT PRIMARY KEY,
                    recipient TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    gmail_message_id TEXT NOT NULL,
                    sent_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outbound_reservations (
                    dedupe_key TEXT PRIMARY KEY,
                    recipient TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('reserved', 'ambiguous')),
                    reserved_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_error TEXT
                );
                """
            )
            _ensure_mailbox_scope_schema(db)
            _ensure_connect_jobs_schema(db, version)
            stamp = datetime.now(UTC).isoformat()
            db.execute(
                """INSERT INTO mail_accounts(
                    provider, account_id, display_name, address, active,
                    created_at, updated_at
                ) SELECT ?, ?, 'Gmail', NULL, 1, ?, ?
                WHERE NOT EXISTS (SELECT 1 FROM mail_accounts)""",
                (DEFAULT_MAIL_PROVIDER, DEFAULT_MAIL_ACCOUNT_ID, stamp, stamp),
            )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(messages)").fetchall()}
            if "analysis_at" not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN analysis_at TEXT")
            migrations = {
                "analysis_request_id": "TEXT",
                "analysis_context_at": "TEXT",
                "analysis_body_char_limit": "INTEGER",
                "analysis_retryable": "INTEGER",
                "analysis_error_code": "TEXT",
                "analysis_retry_after_seconds": "INTEGER",
            }
            for column, definition in migrations.items():
                if column not in columns:
                    db.execute(f"ALTER TABLE messages ADD COLUMN {column} {definition}")
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.path.chmod(0o600)

    def mail_accounts(self) -> list[MailAccount]:
        with self.connection() as db:
            rows = db.execute(
                """SELECT provider, account_id, display_name, address, active,
                    created_at, updated_at
                FROM mail_accounts
                ORDER BY active DESC, casefold(display_name), provider, account_id"""
            ).fetchall()
        return [
            MailAccount(
                provider=str(row["provider"]),
                account_id=str(row["account_id"]),
                display_name=str(row["display_name"]),
                address=str(row["address"]) if row["address"] is not None else None,
                active=bool(row["active"]),
                created_at=str(row["created_at"]),
                updated_at=str(row["updated_at"]),
            )
            for row in rows
        ]

    def mail_account(self, provider: str, account_id: str) -> MailAccount | None:
        with self.connection() as db:
            row = db.execute(
                """SELECT provider, account_id, display_name, address, active,
                    created_at, updated_at
                FROM mail_accounts WHERE provider = ? AND account_id = ?""",
                (provider, account_id),
            ).fetchone()
        if row is None:
            return None
        return MailAccount(
            provider=str(row["provider"]),
            account_id=str(row["account_id"]),
            display_name=str(row["display_name"]),
            address=str(row["address"]) if row["address"] is not None else None,
            active=bool(row["active"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def mail_account_by_address(self, provider: str, address: str) -> MailAccount | None:
        with self.connection() as db:
            row = db.execute(
                """SELECT provider, account_id, display_name, address, active,
                    created_at, updated_at
                FROM mail_accounts WHERE provider = ? AND address = ?""",
                (provider, address),
            ).fetchone()
        if row is None:
            return None
        return MailAccount(
            provider=str(row["provider"]),
            account_id=str(row["account_id"]),
            display_name=str(row["display_name"]),
            address=str(row["address"]),
            active=bool(row["active"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def mail_account_has_history(self, provider: str, account_id: str) -> bool:
        with self.connection() as db:
            row = db.execute(
                """SELECT
                    EXISTS(
                        SELECT 1 FROM mailbox_state
                        WHERE provider = ?1 AND account_id = ?2
                    ) OR EXISTS(
                        SELECT 1 FROM messages
                        WHERE provider = ?1 AND account_id = ?2
                    ) AS has_history""",
                (provider, account_id),
            ).fetchone()
        return bool(row["has_history"])

    def active_mail_account(self) -> MailAccount | None:
        with self.connection() as db:
            row = db.execute(
                """SELECT provider, account_id, display_name, address, active,
                    created_at, updated_at
                FROM mail_accounts WHERE active = 1"""
            ).fetchone()
        if row is None:
            return None
        return MailAccount(
            provider=str(row["provider"]),
            account_id=str(row["account_id"]),
            display_name=str(row["display_name"]),
            address=str(row["address"]) if row["address"] is not None else None,
            active=bool(row["active"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def register_mail_account(
        self,
        provider: str,
        account_id: str,
        *,
        display_name: str,
        address: str | None = None,
        active: bool = False,
    ) -> MailAccount:
        if not provider or not account_id or not display_name:
            raise ValueError("Mail account identity and display name must be non-empty")
        stamp = datetime.now(UTC).isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if active:
                db.execute(
                    "UPDATE mail_accounts SET active = 0, updated_at = ? WHERE active = 1",
                    (stamp,),
                )
            db.execute(
                """INSERT INTO mail_accounts(
                    provider, account_id, display_name, address, active,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (provider, account_id, display_name, address, int(active), stamp, stamp),
            )
        account = self.mail_account(provider, account_id)
        assert account is not None
        return account

    def update_mail_account_identity(
        self,
        provider: str,
        account_id: str,
        *,
        display_name: str,
        address: str,
    ) -> MailAccount:
        stamp = datetime.now(UTC).isoformat()
        with self.connection() as db:
            cursor = db.execute(
                """UPDATE mail_accounts
                SET display_name = ?, address = ?, updated_at = ?
                WHERE provider = ? AND account_id = ?""",
                (display_name, address, stamp, provider, account_id),
            )
            if cursor.rowcount != 1:
                raise KeyError((provider, account_id))
        account = self.mail_account(provider, account_id)
        assert account is not None
        return account

    def activate_mail_account(self, provider: str, account_id: str) -> MailAccount:
        stamp = datetime.now(UTC).isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if (
                db.execute(
                    "SELECT 1 FROM mail_accounts WHERE provider = ? AND account_id = ?",
                    (provider, account_id),
                ).fetchone()
                is None
            ):
                raise KeyError((provider, account_id))
            db.execute(
                "UPDATE mail_accounts SET active = 0, updated_at = ? WHERE active = 1",
                (stamp,),
            )
            db.execute(
                """UPDATE mail_accounts SET active = 1, updated_at = ?
                WHERE provider = ? AND account_id = ?""",
                (stamp, provider, account_id),
            )
        account = self.mail_account(provider, account_id)
        assert account is not None
        return account

    def calendar_grant(
        self,
        account_id: str,
        profile: str = "read",
    ) -> CalendarGrant | None:
        with self.connection() as db:
            row = db.execute(
                """SELECT account_id, profile, state, principal_key,
                    home_account_id, tenant_id, object_id, email_address, updated_at
                FROM microsoft_calendar_grants
                WHERE account_id = ? AND profile = ?""",
                (account_id, profile),
            ).fetchone()
        return CalendarGrant(**dict(row)) if row is not None else None

    def set_calendar_grant(
        self,
        account_id: str,
        profile: str,
        state: str,
        *,
        principal_key: str | None = None,
        home_account_id: str | None = None,
        tenant_id: str | None = None,
        object_id: str | None = None,
        email_address: str | None = None,
    ) -> CalendarGrant:
        if profile not in {"read", "proposal", "write"}:
            raise ValueError("calendar grant profile is invalid")
        if state not in {"not_requested", "consent_pending", "ready", "rejected", "revoked"}:
            raise ValueError("calendar grant state is invalid")
        identity = (principal_key, home_account_id, tenant_id, object_id, email_address)
        if state == "ready" and any(value is None for value in identity):
            raise ValueError("a ready calendar grant requires an immutable principal")
        if state == "not_requested":
            identity = (None, None, None, None, None)
        stamp = datetime.now(UTC).isoformat()
        with self.connection() as db:
            db.execute(
                """INSERT INTO microsoft_calendar_grants(
                    account_id, profile, state, principal_key, home_account_id,
                    tenant_id, object_id, email_address, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, profile) DO UPDATE SET
                    state=excluded.state,
                    principal_key=excluded.principal_key,
                    home_account_id=excluded.home_account_id,
                    tenant_id=excluded.tenant_id,
                    object_id=excluded.object_id,
                    email_address=excluded.email_address,
                    updated_at=excluded.updated_at""",
                (account_id, profile, state, *identity, stamp),
            )
        grant = self.calendar_grant(account_id, profile)
        assert grant is not None
        return grant

    def revoke_calendar_grant_if_current(self, grant: CalendarGrant) -> bool:
        """Persist revocation only if validation still describes the stored grant."""
        stamp = datetime.now(UTC).isoformat()
        with self.connection() as db:
            cursor = db.execute(
                """UPDATE microsoft_calendar_grants
                SET state = 'revoked', updated_at = ?
                WHERE account_id = ? AND profile = ?
                    AND state = 'ready' AND updated_at = ?""",
                (stamp, grant.account_id, grant.profile, grant.updated_at),
            )
        return cursor.rowcount == 1

    def disconnect_calendar_grant(self, account_id: str, profile: str) -> CalendarGrant:
        if profile not in {"read", "proposal", "write"}:
            raise ValueError("calendar grant profile is invalid")
        stamp = datetime.now(UTC).isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """INSERT INTO microsoft_calendar_grants(
                    account_id, profile, state, principal_key, home_account_id,
                    tenant_id, object_id, email_address, updated_at
                ) VALUES (?, ?, 'not_requested', NULL, NULL, NULL, NULL, NULL, ?)
                ON CONFLICT(account_id, profile) DO UPDATE SET
                    state='not_requested', principal_key=NULL, home_account_id=NULL,
                    tenant_id=NULL, object_id=NULL, email_address=NULL,
                    updated_at=excluded.updated_at""",
                (account_id, profile, stamp),
            )
            if profile == "read":
                db.execute(
                    "DELETE FROM microsoft_calendar_events WHERE account_id = ?",
                    (account_id,),
                )
                db.execute(
                    "DELETE FROM microsoft_calendar_windows WHERE account_id = ?",
                    (account_id,),
                )
        grant = self.calendar_grant(account_id, profile)
        assert grant is not None
        return grant

    def disconnect_calendar_read(self, account_id: str) -> CalendarGrant:
        return self.disconnect_calendar_grant(account_id, "read")

    def calendar_window(self, account_id: str) -> CalendarWindow | None:
        with self.connection() as db:
            row = db.execute(
                """SELECT account_id, principal_key, window_start, window_end,
                    cursor, updated_at
                FROM microsoft_calendar_windows WHERE account_id = ?""",
                (account_id,),
            ).fetchone()
        return CalendarWindow(**dict(row)) if row is not None else None

    def calendar_events(self, account_id: str) -> list[CalendarEventProjection]:
        with self.connection() as db:
            rows = db.execute(
                """SELECT event_id, subject, start_date_time, start_time_zone,
                    end_date_time, end_time_zone, is_all_day, location
                FROM microsoft_calendar_events
                WHERE account_id = ?
                ORDER BY start_date_time, event_id""",
                (account_id,),
            ).fetchall()
        return [
            CalendarEventProjection(
                event_id=str(row["event_id"]),
                subject=str(row["subject"]),
                start_date_time=str(row["start_date_time"]),
                start_time_zone=str(row["start_time_zone"]),
                end_date_time=str(row["end_date_time"]),
                end_time_zone=str(row["end_time_zone"]),
                is_all_day=bool(row["is_all_day"]),
                location=str(row["location"]),
            )
            for row in rows
        ]

    def commit_calendar_round(
        self,
        *,
        account_id: str,
        principal_key: str,
        window_start: str,
        window_end: str,
        cursor: str,
        changes: Sequence[CalendarEventMutation],
        replace: bool,
    ) -> CalendarWindow:
        stamp = datetime.now(UTC).isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                """SELECT principal_key, window_start, window_end
                FROM microsoft_calendar_windows WHERE account_id = ?""",
                (account_id,),
            ).fetchone()
            if not replace and (
                previous is None
                or str(previous["principal_key"]) != principal_key
                or str(previous["window_start"]) != window_start
                or str(previous["window_end"]) != window_end
            ):
                raise ValueError("calendar delta does not match the completed projection")
            if replace:
                db.execute(
                    "DELETE FROM microsoft_calendar_events WHERE account_id = ?",
                    (account_id,),
                )
            for change in changes:
                if change.event is None:
                    db.execute(
                        """DELETE FROM microsoft_calendar_events
                        WHERE account_id = ? AND event_id = ?""",
                        (account_id, change.event_id),
                    )
                    continue
                event = change.event
                if event.event_id != change.event_id:
                    raise ValueError("calendar mutation identity is inconsistent")
                db.execute(
                    """INSERT INTO microsoft_calendar_events(
                        account_id, event_id, subject, start_date_time,
                        start_time_zone, end_date_time, end_time_zone,
                        is_all_day, location
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account_id, event_id) DO UPDATE SET
                        subject=excluded.subject,
                        start_date_time=excluded.start_date_time,
                        start_time_zone=excluded.start_time_zone,
                        end_date_time=excluded.end_date_time,
                        end_time_zone=excluded.end_time_zone,
                        is_all_day=excluded.is_all_day,
                        location=excluded.location""",
                    (
                        account_id,
                        event.event_id,
                        event.subject,
                        event.start_date_time,
                        event.start_time_zone,
                        event.end_date_time,
                        event.end_time_zone,
                        int(event.is_all_day),
                        event.location,
                    ),
                )
            event_count = int(
                db.execute(
                    """SELECT COUNT(*) FROM microsoft_calendar_events
                    WHERE account_id = ?""",
                    (account_id,),
                ).fetchone()[0]
            )
            if event_count > 3_200:
                raise ValueError("calendar projection exceeded its event limit")
            db.execute(
                """INSERT INTO microsoft_calendar_windows(
                    account_id, principal_key, window_start, window_end, cursor, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    principal_key=excluded.principal_key,
                    window_start=excluded.window_start,
                    window_end=excluded.window_end,
                    cursor=excluded.cursor,
                    updated_at=excluded.updated_at""",
                (account_id, principal_key, window_start, window_end, cursor, stamp),
            )
        window = self.calendar_window(account_id)
        assert window is not None
        return window

    def state(
        self,
        *,
        provider: str = DEFAULT_MAIL_PROVIDER,
        account_id: str = DEFAULT_MAIL_ACCOUNT_ID,
    ) -> tuple[str, str] | None:
        with self.connection() as db:
            row = db.execute(
                """SELECT cursor, last_success_at FROM mailbox_state
                WHERE provider = ? AND account_id = ?""",
                (provider, account_id),
            ).fetchone()
        return (row["cursor"], row["last_success_at"]) if row else None

    def set_state(
        self,
        cursor: str,
        at: datetime | None = None,
        *,
        provider: str = DEFAULT_MAIL_PROVIDER,
        account_id: str = DEFAULT_MAIL_ACCOUNT_ID,
    ) -> None:
        stamp = (at or datetime.now(UTC)).isoformat()
        with self.connection() as db:
            db.execute(
                """INSERT INTO mailbox_state(
                    provider, account_id, cursor, last_success_at
                ) VALUES(?, ?, ?, ?)
                ON CONFLICT(provider, account_id) DO UPDATE SET cursor=excluded.cursor,
                last_success_at=excluded.last_success_at""",
                (provider, account_id, cursor, stamp),
            )

    def has_message(self, message_id: str) -> bool:
        with self.connection() as db:
            return (
                db.execute("SELECT 1 FROM messages WHERE message_id = ?", (message_id,)).fetchone()
                is not None
            )

    def message_source(self, message_id: str) -> MessageSource:
        with self.connection() as db:
            row = db.execute(
                """SELECT message_id, provider, account_id, provider_message_id
                FROM messages WHERE message_id = ?""",
                (message_id,),
            ).fetchone()
        if row is None:
            raise KeyError(message_id)
        return MessageSource(**dict(row))

    def has_seen_message(
        self,
        provider_message_id: str,
        *,
        provider: str = DEFAULT_MAIL_PROVIDER,
        account_id: str = DEFAULT_MAIL_ACCOUNT_ID,
    ) -> bool:
        message_key = _message_suppression_key(provider, account_id, provider_message_id)
        legacy_message_key = _legacy_message_suppression_key(provider_message_id)
        with self.connection() as db:
            return (
                db.execute(
                    """SELECT 1 FROM messages
                    WHERE provider = ? AND account_id = ? AND provider_message_id = ?
                    UNION ALL
                    SELECT 1 FROM suppressed_messages
                    WHERE provider = ? AND account_id = ? AND message_key IN (?, ?)
                    LIMIT 1""",
                    (
                        provider,
                        account_id,
                        provider_message_id,
                        provider,
                        account_id,
                        message_key,
                        legacy_message_key,
                    ),
                ).fetchone()
                is not None
            )

    def add_message(
        self,
        *,
        message_id: str,
        thread_id: str | None,
        sender: str,
        sender_name: str | None,
        subject: str,
        received_at: str,
        provider: str = DEFAULT_MAIL_PROVIDER,
        account_id: str = DEFAULT_MAIL_ACCOUNT_ID,
        provider_message_id: str | None = None,
    ) -> bool:
        source_message_id = provider_message_id or message_id
        message_key = _message_suppression_key(provider, account_id, source_message_id)
        legacy_message_key = _legacy_message_suppression_key(source_message_id)
        with self.connection() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO messages(
                    message_id, provider, account_id, provider_message_id,
                    thread_id, sender, sender_name, subject, received_at, discovered_at
                ) SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                WHERE NOT EXISTS (
                    SELECT 1 FROM suppressed_messages
                    WHERE provider = ? AND account_id = ? AND message_key IN (?, ?)
                )""",
                (
                    message_id,
                    provider,
                    account_id,
                    source_message_id,
                    thread_id,
                    sender,
                    sender_name,
                    subject,
                    received_at,
                    datetime.now(UTC).isoformat(),
                    provider,
                    account_id,
                    message_key,
                    legacy_message_key,
                ),
            )
        return cursor.rowcount == 1

    def delete_message(self, message_id: str, *, now: datetime | None = None) -> bool:
        stamp = (now or datetime.now(UTC)).astimezone(UTC)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT provider, account_id, provider_message_id, received_at
                FROM messages WHERE message_id = ?""",
                (message_id,),
            ).fetchone()
            if row is None:
                return False
            db.execute(
                """INSERT INTO suppressed_messages(
                    provider, account_id, message_key, expires_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(provider, account_id, message_key)
                DO UPDATE SET expires_at = excluded.expires_at""",
                (
                    str(row["provider"]),
                    str(row["account_id"]),
                    _message_suppression_key(
                        str(row["provider"]),
                        str(row["account_id"]),
                        str(row["provider_message_id"]),
                    ),
                    _suppression_expiry(str(row["received_at"]), stamp),
                ),
            )
            cursor = db.execute("DELETE FROM messages WHERE message_id = ?", (message_id,))
        return cursor.rowcount == 1

    def clear_messages(self, *, now: datetime | None = None) -> int:
        stamp = (now or datetime.now(UTC)).astimezone(UTC)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                """SELECT provider, account_id, provider_message_id, received_at
                FROM messages"""
            ).fetchall()
            db.executemany(
                """INSERT INTO suppressed_messages(
                    provider, account_id, message_key, expires_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(provider, account_id, message_key)
                DO UPDATE SET expires_at = excluded.expires_at""",
                [
                    (
                        str(row["provider"]),
                        str(row["account_id"]),
                        _message_suppression_key(
                            str(row["provider"]),
                            str(row["account_id"]),
                            str(row["provider_message_id"]),
                        ),
                        _suppression_expiry(str(row["received_at"]), stamp),
                    )
                    for row in rows
                ],
            )
            cursor = db.execute("DELETE FROM messages")
        return cursor.rowcount

    def replace_attachments(
        self, message_id: str, attachments: Iterable[AttachmentDescriptor]
    ) -> None:
        items = tuple(attachments)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if (
                db.execute("SELECT 1 FROM messages WHERE message_id = ?", (message_id,)).fetchone()
                is None
            ):
                raise KeyError(message_id)
            db.execute("DELETE FROM message_attachments WHERE message_id = ?", (message_id,))
            db.executemany(
                """INSERT INTO message_attachments(
                    message_id, part_id, attachment_id, filename, media_type, byte_size, position
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        message_id,
                        item.part_id,
                        item.attachment_id,
                        item.filename,
                        item.media_type,
                        item.byte_size,
                        item.position,
                    )
                    for item in items
                ],
            )

    def attachment(self, message_id: str, part_id: str) -> AttachmentDescriptor:
        with self.connection() as db:
            row = db.execute(
                """SELECT part_id, attachment_id, filename, media_type, byte_size, position
                FROM message_attachments WHERE message_id = ? AND part_id = ?""",
                (message_id, part_id),
            ).fetchone()
        if row is None:
            raise KeyError((message_id, part_id))
        return AttachmentDescriptor(
            part_id=str(row["part_id"]),
            attachment_id=row["attachment_id"],
            filename=str(row["filename"]),
            media_type=str(row["media_type"]),
            byte_size=int(row["byte_size"]),
            position=int(row["position"]),
        )

    @staticmethod
    def _connect_job(row: sqlite3.Row) -> ConnectJob:
        return ConnectJob(**dict(row))

    def connect_job(self, job_id: str) -> ConnectJob | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM connect_attachment_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return self._connect_job(row) if row else None

    @staticmethod
    def completed_connect_warnings(job: ConnectJob) -> list[dict[str, str]]:
        if (
            job.status != "completed"
            or job.output_artifact_id is None
            or job.output_media_type != "application/vnd.local-connect.document-summary+json"
            or job.output_byte_size is None
            or job.output_sha256 is None
            or job.summary_version is None
            or job.summary_text is None
            or job.warnings_json is None
        ):
            raise RuntimeError("Completed Connect job is missing its durable result")
        try:
            warnings = json.loads(job.warnings_json)
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError("Completed Connect job warnings are invalid") from exc
        if not isinstance(warnings, list) or any(
            not isinstance(warning, dict)
            or set(warning) != {"code", "message"}
            or not isinstance(warning["code"], str)
            or not isinstance(warning["message"], str)
            for warning in warnings
        ):
            raise RuntimeError("Completed Connect job warnings are invalid")
        content = {
            "summary_version": job.summary_version,
            "text": job.summary_text,
            "warnings": warnings,
            "input_artifact": {
                "artifact_id": job.input_artifact_id,
                "media_type": job.input_media_type,
                "byte_size": job.input_byte_size,
                "sha256": job.input_sha256,
            },
        }
        encoded = json.dumps(content, separators=(",", ":"), ensure_ascii=False).encode()
        if (
            len(encoded) != job.output_byte_size
            or hashlib.sha256(encoded).hexdigest() != job.output_sha256
        ):
            raise RuntimeError("Completed Connect job failed its integrity check")
        return warnings

    @staticmethod
    def completed_connect_outputs(job: ConnectJob) -> tuple[ConnectOutput, ...]:
        if job.status != "completed" or job.protocol_version != 2 or job.result_json is None:
            raise RuntimeError("Completed Connect v2 job is missing its durable result")
        return _decode_generic_result(job.result_json)

    @staticmethod
    def canonical_connect_parameters(value: object) -> dict[str, str | int | bool]:
        return _canonical_v2_parameters(value)

    @staticmethod
    def connect_job_parameters(job: ConnectJob) -> dict[str, str | int | bool]:
        if (
            job.protocol_version != 2
            or job.provider_app_id is None
            or job.provider_app_version is None
            or job.provider_instance_id is None
            or job.input_display_name is None
            or job.source_app_id is None
            or job.request_json is None
        ):
            raise RuntimeError("Connect v2 job is missing its durable request")
        try:
            fingerprint = _validate_v2_request_record(
                job.request_json,
                job_id=job.job_id,
                capability_id=job.capability_id,
                capability_version=job.capability_version,
                provider_app_id=job.provider_app_id,
                provider_app_version=job.provider_app_version,
                provider_instance_id=job.provider_instance_id,
                input_artifact_id=job.input_artifact_id,
                input_media_type=job.input_media_type,
                input_byte_size=job.input_byte_size,
                input_sha256=job.input_sha256,
                input_display_name=job.input_display_name,
                source_app_id=job.source_app_id,
            )
            request = _decode_v2_request(job.request_json)
            parameters = _canonical_v2_parameters(request.get("parameters"))
        except ValueError as exc:
            raise RuntimeError("Connect v2 job has an invalid durable request") from exc
        if fingerprint != job.invocation_fingerprint:
            raise RuntimeError("Connect v2 job has an invalid durable request")
        return parameters

    def active_connect_job(
        self,
        *,
        message_id: str,
        part_id: str,
        capability_id: str,
        capability_version: str,
        protocol_version: int = 1,
        provider_app_id: str | None = None,
        provider_app_version: str | None = None,
        provider_instance_id: str | None = None,
        request_json: bytes | None = None,
    ) -> ConnectJob | None:
        invocation_fingerprint = _lookup_invocation_fingerprint(
            protocol_version=protocol_version,
            capability_id=capability_id,
            capability_version=capability_version,
            provider_app_id=provider_app_id,
            provider_app_version=provider_app_version,
            provider_instance_id=provider_instance_id,
            request_json=request_json,
        )
        with self.connection() as db:
            row = db.execute(
                """SELECT * FROM connect_attachment_jobs
                WHERE message_id = ? AND part_id = ?
                  AND capability_id = ? AND capability_version = ?
                  AND protocol_version = ?
                  AND invocation_fingerprint = ?
                  AND status IN ('requested', 'accepted', 'processing')
                ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                (
                    message_id,
                    part_id,
                    capability_id,
                    capability_version,
                    protocol_version,
                    invocation_fingerprint,
                ),
            ).fetchone()
        return self._connect_job(row) if row else None

    def completed_connect_job(
        self,
        *,
        message_id: str,
        part_id: str,
        capability_id: str,
        capability_version: str,
        protocol_version: int = 1,
        provider_app_id: str | None = None,
        provider_app_version: str | None = None,
        provider_instance_id: str | None = None,
        request_json: bytes | None = None,
    ) -> ConnectJob | None:
        invocation_fingerprint = _lookup_invocation_fingerprint(
            protocol_version=protocol_version,
            capability_id=capability_id,
            capability_version=capability_version,
            provider_app_id=provider_app_id,
            provider_app_version=provider_app_version,
            provider_instance_id=provider_instance_id,
            request_json=request_json,
        )
        with self.connection() as db:
            row = db.execute(
                """SELECT * FROM connect_attachment_jobs
                WHERE message_id = ? AND part_id = ?
                  AND capability_id = ? AND capability_version = ?
                  AND protocol_version = ?
                  AND invocation_fingerprint = ?
                  AND status = 'completed'
                ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                (
                    message_id,
                    part_id,
                    capability_id,
                    capability_version,
                    protocol_version,
                    invocation_fingerprint,
                ),
            ).fetchone()
        return self._connect_job(row) if row else None

    def create_connect_job(
        self,
        *,
        job_id: str,
        message_id: str,
        part_id: str,
        capability_id: str,
        capability_version: str,
        provider_app_id: str,
        provider_instance_id: str,
        input_artifact_id: str,
        input_media_type: str,
        input_byte_size: int,
        input_sha256: str,
        protocol_version: int = 1,
        provider_app_version: str | None = None,
        input_display_name: str | None = None,
        source_app_id: str | None = None,
        request_json: bytes | None = None,
    ) -> ConnectJob:
        if protocol_version not in {1, 2}:
            raise ValueError("Connect protocol version is unsupported")
        if protocol_version == 1:
            if request_json is not None:
                raise ValueError("Connect v1 jobs cannot store a v2 request")
            invocation_fingerprint = "v1"
        else:
            if (
                not provider_app_version
                or not input_display_name
                or not source_app_id
                or request_json is None
            ):
                raise ValueError("Connect v2 job provenance is incomplete")
            invocation_fingerprint = _validate_v2_request_record(
                request_json,
                job_id=job_id,
                capability_id=capability_id,
                capability_version=capability_version,
                provider_app_id=provider_app_id,
                provider_app_version=provider_app_version,
                provider_instance_id=provider_instance_id,
                input_artifact_id=input_artifact_id,
                input_media_type=input_media_type,
                input_byte_size=input_byte_size,
                input_sha256=input_sha256,
                input_display_name=input_display_name,
                source_app_id=source_app_id,
            )
        stamp = datetime.now(UTC).isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if (
                db.execute(
                    """SELECT 1 FROM message_attachments
                WHERE message_id = ? AND part_id = ?""",
                    (message_id, part_id),
                ).fetchone()
                is None
            ):
                raise KeyError((message_id, part_id))
            db.execute(
                """INSERT INTO connect_attachment_jobs(
                    job_id, message_id, part_id, protocol_version,
                    capability_id, capability_version,
                    provider_app_id, provider_app_version, provider_instance_id,
                    invocation_fingerprint,
                    input_artifact_id, input_media_type, input_byte_size, input_sha256,
                    input_display_name, source_app_id, request_json, status,
                    created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'requested', ?, ?
                )""",
                (
                    job_id,
                    message_id,
                    part_id,
                    protocol_version,
                    capability_id,
                    capability_version,
                    provider_app_id,
                    provider_app_version,
                    provider_instance_id,
                    invocation_fingerprint,
                    input_artifact_id,
                    input_media_type,
                    input_byte_size,
                    input_sha256,
                    input_display_name,
                    source_app_id,
                    request_json,
                    stamp,
                    stamp,
                ),
            )
            row = db.execute(
                "SELECT * FROM connect_attachment_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise RuntimeError("Connect job was not readable after creation")
        return self._connect_job(row)

    def transition_connect_job(
        self,
        *,
        job_id: str,
        expected_state: str,
        next_state: str,
        provider_app_id: str,
        provider_instance_id: str,
        result: dict[str, object] | None = None,
        error: dict[str, object] | None = None,
    ) -> ConnectJob:
        allowed = {
            "requested": {"accepted", "processing", "completed", "failed"},
            "accepted": {"processing", "completed", "failed"},
            "processing": {"completed", "failed"},
        }
        if next_state not in allowed.get(expected_state, set()):
            raise ValueError(f"Invalid Connect job transition: {expected_state} -> {next_state}")
        if next_state == "completed":
            if result is None or error is not None:
                raise ValueError("Completed Connect jobs require only a result")
        elif next_state == "failed":
            if error is None or result is not None:
                raise ValueError("Failed Connect jobs require only an error")
        elif result is not None or error is not None:
            raise ValueError("Active Connect jobs cannot contain terminal data")

        stamp = datetime.now(UTC).isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT * FROM connect_attachment_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if current is None or current["status"] != expected_state:
                raise RuntimeError("Connect job transition lost its expected-state race")
            current_job = self._connect_job(current)
            if current_job.protocol_version == 2 and (
                current_job.provider_app_id != provider_app_id
                or current_job.provider_instance_id != provider_instance_id
            ):
                raise ValueError("Connect v2 provider provenance cannot change")
            if next_state == "completed" and current_job.protocol_version == 1:
                assert result is not None
                output = result.get("output") if isinstance(result.get("output"), dict) else None
                if output is None:
                    raise ValueError("Completed Connect job result is invalid")
                warnings = output.get("warnings")
                if not isinstance(warnings, list):
                    raise ValueError("Completed Connect job warnings are invalid")
                values = (
                    output.get("artifact_id"),
                    output.get("media_type"),
                    output.get("byte_size"),
                    output.get("sha256"),
                    output.get("summary_version"),
                    output.get("text"),
                    json.dumps(warnings, separators=(",", ":"), ensure_ascii=False),
                    None,
                    None,
                    None,
                    None,
                    None,
                )
            elif next_state == "completed":
                assert result is not None
                result_json, result_metadata_json = _encode_generic_result(result)
                values = (None,) * 7 + (
                    result_json,
                    result_metadata_json,
                    None,
                    None,
                    None,
                )
            elif next_state == "failed":
                assert error is not None
                values = (None,) * 9 + (
                    error.get("code"),
                    error.get("message"),
                    int(bool(error.get("retryable"))),
                )
            else:
                values = (None,) * 12
            if next_state == "completed":
                candidate = ConnectJob(
                    **{
                        **dict(current),
                        "provider_app_id": provider_app_id,
                        "provider_instance_id": provider_instance_id,
                        "status": next_state,
                        "output_artifact_id": values[0],
                        "output_media_type": values[1],
                        "output_byte_size": values[2],
                        "output_sha256": values[3],
                        "summary_version": values[4],
                        "summary_text": values[5],
                        "warnings_json": values[6],
                        "result_json": values[7],
                        "result_metadata_json": values[8],
                        "error_code": values[9],
                        "error_message": values[10],
                        "error_retryable": values[11],
                        "updated_at": stamp,
                    }
                )
                if candidate.protocol_version == 1:
                    try:
                        self.completed_connect_warnings(candidate)
                    except RuntimeError as exc:
                        raise ValueError("Completed Connect job result failed validation") from exc
            cursor = db.execute(
                """UPDATE connect_attachment_jobs SET
                    provider_app_id = ?, provider_instance_id = ?, status = ?,
                    output_artifact_id = ?, output_media_type = ?, output_byte_size = ?,
                    output_sha256 = ?, summary_version = ?, summary_text = ?, warnings_json = ?,
                    result_json = ?, result_metadata_json = ?, error_code = ?, error_message = ?,
                    error_retryable = ?, updated_at = ?
                WHERE job_id = ? AND status = ?""",
                (
                    provider_app_id,
                    provider_instance_id,
                    next_state,
                    *values,
                    stamp,
                    job_id,
                    expected_state,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Connect job transition lost its expected-state race")
            row = db.execute(
                "SELECT * FROM connect_attachment_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise RuntimeError("Connect job was not readable after transition")
        return self._connect_job(row)

    def reset_connect_job_for_resubmission(
        self,
        *,
        job_id: str,
        expected_state: str,
        provider_app_id: str,
        provider_instance_id: str,
    ) -> ConnectJob:
        if expected_state not in {"requested", "accepted", "processing"}:
            raise ValueError("Only active Connect jobs can be reset for resubmission")
        stamp = datetime.now(UTC).isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                """UPDATE connect_attachment_jobs SET
                    status = 'requested',
                    output_artifact_id = NULL, output_media_type = NULL,
                    output_byte_size = NULL, output_sha256 = NULL,
                    summary_version = NULL, summary_text = NULL, warnings_json = NULL,
                    result_json = NULL, result_metadata_json = NULL,
                    error_code = NULL, error_message = NULL, error_retryable = NULL,
                    updated_at = ?
                WHERE job_id = ? AND status = ?
                  AND provider_app_id = ? AND provider_instance_id = ?""",
                (
                    stamp,
                    job_id,
                    expected_state,
                    provider_app_id,
                    provider_instance_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Connect job resubmission lost its expected-state race")
            row = db.execute(
                "SELECT * FROM connect_attachment_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise RuntimeError("Connect job was not readable after resubmission reset")
        return self._connect_job(row)

    def pending(
        self,
        now: datetime | None = None,
        limit: int = 25,
        *,
        provider: str | None = None,
        account_id: str | None = None,
    ) -> list[PendingMessage]:
        if (provider is None) != (account_id is None):
            raise ValueError("provider and account_id must be supplied together")
        stamp = (now or datetime.now(UTC)).isoformat()
        scope = ""
        parameters: list[object] = [stamp]
        if provider is not None and account_id is not None:
            scope = " AND provider = ? AND account_id = ?"
            parameters.extend((provider, account_id))
        parameters.append(limit)
        with self.connection() as db:
            rows = db.execute(
                f"""SELECT message_id, provider, account_id, provider_message_id,
                thread_id, sender, sender_name, subject, received_at,
                attempts, fallback_notified_at, analysis_request_id, analysis_context_at,
                analysis_body_char_limit FROM messages
                WHERE status = 'pending' AND COALESCE(analysis_retryable, 1) = 1
                AND (next_retry_at IS NULL OR next_retry_at <= ?){scope}
                ORDER BY received_at LIMIT ?""",
                parameters,
            ).fetchall()
        return [PendingMessage(**dict(row)) for row in rows]

    def reserve_analysis_request(
        self,
        message_id: str,
        body_char_limit: int,
        now: datetime | None = None,
    ) -> AnalysisRequest:
        if body_char_limit < 1:
            raise ValueError("body_char_limit must be positive")
        request_id = str(uuid.uuid4())
        context_at = (now or datetime.now(UTC)).isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT status, analysis_request_id, analysis_context_at,
                analysis_body_char_limit FROM messages WHERE message_id = ?""",
                (message_id,),
            ).fetchone()
            if row is None:
                raise KeyError(message_id)
            if row["status"] != "pending":
                raise RuntimeError("Analysis request can only be reserved for a pending message")
            values = (
                row["analysis_request_id"],
                row["analysis_context_at"],
                row["analysis_body_char_limit"],
            )
            if all(value is None for value in values):
                db.execute(
                    """UPDATE messages SET analysis_request_id = ?, analysis_context_at = ?,
                    analysis_body_char_limit = ? WHERE message_id = ? AND status = 'pending'""",
                    (request_id, context_at, body_char_limit, message_id),
                )
                return AnalysisRequest(request_id, context_at, body_char_limit)
            if any(value is None for value in values):
                raise RuntimeError("Pending message has incomplete analysis request state")
            return AnalysisRequest(str(values[0]), str(values[1]), int(values[2]))

    def pending_delivery(
        self, now: datetime | None = None, limit: int = 25
    ) -> list[AnalyzedMessage]:
        stamp = (now or datetime.now(UTC)).isoformat()
        with self.connection() as db:
            rows = db.execute(
                """SELECT message_id, sender, sender_name, subject, received_at, attempts,
                fallback_notified_at, category, priority, summary, action_required,
                suggested_action, deadline_text, deadline_iso, confidence FROM messages
                WHERE status = 'analyzed' AND (next_retry_at IS NULL OR next_retry_at <= ?)
                ORDER BY received_at LIMIT ?""",
                (stamp, limit),
            ).fetchall()
        return [AnalyzedMessage(**dict(row)) for row in rows]

    def record_failure(self, message_id: str, error: str, attempts: int) -> None:
        delays = (5, 15, 60, 360, 1440)
        delay = delays[min(attempts, len(delays) - 1)]
        retry = datetime.now(UTC) + timedelta(minutes=delay)
        with self.connection() as db:
            db.execute(
                """UPDATE messages SET attempts = attempts + 1, next_retry_at = ?,
                last_error = ? WHERE message_id = ?""",
                (retry.isoformat(), error[:500], message_id),
            )

    def record_analysis_failure(
        self,
        message_id: str,
        error: str,
        attempts: int,
        *,
        retryable: bool,
        error_code: str | None = None,
        retry_after_seconds: int | None = None,
        now: datetime | None = None,
    ) -> None:
        if retry_after_seconds is not None and retry_after_seconds < 1:
            raise ValueError("retry_after_seconds must be positive")
        current = now or datetime.now(UTC)
        if retryable:
            if retry_after_seconds is None:
                delays = (5, 15, 60, 360, 1440)
                next_retry = current + timedelta(minutes=delays[min(attempts, len(delays) - 1)])
            else:
                next_retry = current + timedelta(seconds=retry_after_seconds)
        else:
            next_retry = None
        with self.connection() as db:
            cursor = db.execute(
                """UPDATE messages SET attempts = attempts + 1, next_retry_at = ?,
                last_error = ?, analysis_retryable = ?, analysis_error_code = ?,
                analysis_retry_after_seconds = ?
                WHERE message_id = ? AND status = 'pending'""",
                (
                    next_retry.isoformat() if next_retry else None,
                    error[:500],
                    int(retryable),
                    error_code,
                    retry_after_seconds,
                    message_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Analysis failure target is no longer pending")

    def requeue_analysis(self, message_id: str) -> str:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT status, analysis_retryable
                FROM messages WHERE message_id = ?""",
                (message_id,),
            ).fetchone()
            if row is None:
                raise KeyError(message_id)
            if row["status"] != "pending" or row["analysis_retryable"] != 0:
                raise RuntimeError("Message does not have a permanently paused analysis")
            db.execute(
                """UPDATE messages SET attempts = 0, next_retry_at = NULL,
                analysis_request_id = NULL, analysis_context_at = NULL,
                analysis_body_char_limit = NULL, analysis_retryable = NULL,
                analysis_error_code = NULL, analysis_retry_after_seconds = NULL
                WHERE message_id = ?""",
                (message_id,),
            )
        return "requeued"

    def mark_fallback_notified(self, message_id: str) -> None:
        with self.connection() as db:
            db.execute(
                "UPDATE messages SET fallback_notified_at = ? WHERE message_id = ?",
                (datetime.now(UTC).isoformat(), message_id),
            )

    def mark_skipped(self, message_id: str) -> None:
        with self.connection() as db:
            db.execute(
                """UPDATE messages SET status='skipped', next_retry_at=NULL,
                last_error='message unavailable (skipped)', analysis_request_id=NULL,
                analysis_context_at=NULL, analysis_body_char_limit=NULL,
                analysis_retryable=NULL, analysis_error_code=NULL,
                analysis_retry_after_seconds=NULL WHERE message_id = ?""",
                (message_id,),
            )

    def mark_analyzed(self, message_id: str, result: dict[str, object]) -> None:
        with self.connection() as db:
            db.execute(
                """UPDATE messages SET status='analyzed', analysis_at=?, attempts=0,
                next_retry_at=NULL, last_error=NULL, analysis_request_id=NULL,
                analysis_context_at=NULL, analysis_body_char_limit=NULL,
                analysis_retryable=NULL, analysis_error_code=NULL,
                analysis_retry_after_seconds=NULL,
                category=?, priority=?, summary=?, action_required=?, suggested_action=?,
                deadline_text=?, deadline_iso=?, confidence=? WHERE message_id=?""",
                (
                    datetime.now(UTC).isoformat(),
                    result["category"],
                    result["priority"],
                    result["summary"],
                    int(bool(result["action_required"])),
                    result.get("suggested_action"),
                    result.get("deadline_text"),
                    result.get("deadline_iso"),
                    result["confidence"],
                    message_id,
                ),
            )

    def mark_delivery_complete(self, message_id: str, notified: bool) -> None:
        with self.connection() as db:
            db.execute(
                """UPDATE messages SET status='summarized', next_retry_at=NULL,
                last_error=NULL, notified_at=? WHERE message_id=?""",
                (datetime.now(UTC).isoformat() if notified else None, message_id),
            )

    def notification_intents(self, limit: int = 25) -> list[NotificationIntent]:
        with self.connection() as db:
            rows = db.execute(
                """SELECT message_id,
                CASE WHEN status = 'analyzed' THEN 'analysis' ELSE 'fallback' END AS kind,
                sender, sender_name, subject, analysis_at, priority, summary,
                suggested_action, deadline_iso, last_error
                FROM messages
                WHERE (status = 'analyzed' AND notified_at IS NULL)
                   OR (status = 'pending' AND last_error IS NOT NULL
                       AND fallback_notified_at IS NULL)
                ORDER BY received_at LIMIT ?""",
                (limit,),
            ).fetchall()
        return [NotificationIntent(**dict(row)) for row in rows]

    def notification_intent_count(self) -> int:
        with self.connection() as db:
            row = db.execute(
                """SELECT COUNT(*) AS count FROM messages
                WHERE (status = 'analyzed' AND notified_at IS NULL)
                   OR (status = 'pending' AND last_error IS NOT NULL
                       AND fallback_notified_at IS NULL)"""
            ).fetchone()
        return int(row["count"])

    def acknowledge_notification(
        self,
        *,
        message_id: str,
        kind: str,
        analysis_at: str | None = None,
    ) -> str:
        if kind not in {"analysis", "fallback"}:
            raise ValueError("Notification kind must be analysis or fallback")
        if kind == "analysis" and not analysis_at:
            raise ValueError("analysis_at is required for an analysis notification")

        stamp = datetime.now(UTC).isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT status, analysis_at, fallback_notified_at, notified_at, last_error
                FROM messages WHERE message_id = ?""",
                (message_id,),
            ).fetchone()
            if row is None:
                raise KeyError(message_id)

            if kind == "fallback":
                if row["fallback_notified_at"] is not None:
                    return "already_acknowledged"
                if row["status"] != "pending" or row["last_error"] is None:
                    raise RuntimeError("Fallback notification intent is no longer current")
                db.execute(
                    "UPDATE messages SET fallback_notified_at = ? WHERE message_id = ?",
                    (stamp, message_id),
                )
                return "acknowledged"

            if row["analysis_at"] != analysis_at:
                raise RuntimeError("Analysis notification intent is no longer current")
            if row["status"] == "summarized" and row["notified_at"] is not None:
                return "already_acknowledged"
            if row["status"] != "analyzed":
                raise RuntimeError("Analysis notification intent is no longer current")
            db.execute(
                """UPDATE messages SET status='summarized', next_retry_at=NULL,
                last_error=NULL, notified_at=? WHERE message_id=?""",
                (stamp, message_id),
            )
            return "acknowledged"

    def recent(self, limit: int) -> list[dict[str, object]]:
        items, _ = self.query_inbox(limit=limit)
        return items

    def query_inbox(
        self,
        *,
        limit: int,
        cursor: tuple[str, str] | None = None,
        sender_query: str | None = None,
        priority: str | None = None,
        category: str | None = None,
        status: str | None = None,
        keyword: str | None = None,
        provider: str | None = None,
        account_id: str | None = None,
    ) -> tuple[list[dict[str, object]], tuple[str, str] | None]:
        clauses: list[str] = []
        parameters: list[object] = []
        if cursor is not None:
            clauses.append("(received_at < ? OR (received_at = ? AND message_id < ?))")
            parameters.extend((cursor[0], cursor[0], cursor[1]))
        if sender_query is not None:
            folded_sender = sender_query.casefold()
            clauses.append(
                "(instr(casefold(sender), ?) > 0 "
                "OR instr(casefold(COALESCE(sender_name, '')), ?) > 0)"
            )
            parameters.extend((folded_sender, folded_sender))
        if priority == "untriaged":
            clauses.append("priority IS NULL")
        elif priority is not None:
            clauses.append("priority = ?")
            parameters.append(priority)
        if category == "unclassified":
            clauses.append("category IS NULL")
        elif category is not None:
            clauses.append("category = ?")
            parameters.append(category)
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status)
        if keyword is not None:
            folded_keyword = keyword.casefold()
            clauses.append(
                "(instr(casefold(subject), ?) > 0 OR instr(casefold(COALESCE(summary, '')), ?) > 0)"
            )
            parameters.extend((folded_keyword, folded_keyword))
        if provider is not None:
            clauses.append("provider = ?")
            parameters.append(provider)
        if account_id is not None:
            clauses.append("account_id = ?")
            parameters.append(account_id)

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit + 1)
        with self.connection() as db:
            rows = db.execute(
                f"""SELECT message_id, provider, account_id, received_at,
                sender, sender_name, subject, status,
                analysis_at, category, priority, summary, action_required, suggested_action,
                deadline_text, deadline_iso, confidence, attempts, next_retry_at,
                fallback_notified_at, notified_at, last_error, analysis_retryable,
                analysis_error_code, analysis_retry_after_seconds
                FROM messages{where}
                ORDER BY received_at DESC, message_id DESC LIMIT ?""",
                parameters,
            ).fetchall()
            has_more = len(rows) > limit
            page_rows = rows[:limit]
            items = self._hydrate_inbox_rows(db, page_rows)
        next_cursor = None
        if has_more and page_rows:
            last = page_rows[-1]
            next_cursor = (str(last["received_at"]), str(last["message_id"]))
        return items, next_cursor

    def _hydrate_inbox_rows(
        self, db: sqlite3.Connection, rows: list[sqlite3.Row]
    ) -> list[dict[str, object]]:
        items = [dict(row) for row in rows]
        for item in items:
            if item["analysis_retryable"] is not None:
                item["analysis_retryable"] = bool(item["analysis_retryable"])
        if not items:
            return []
        message_ids = [str(item["message_id"]) for item in items]
        placeholders = ",".join("?" for _ in message_ids)
        attachment_rows = db.execute(
            f"""SELECT message_id, part_id, attachment_id, filename, media_type, byte_size
            FROM message_attachments WHERE message_id IN ({placeholders})
            ORDER BY message_id, position""",
            message_ids,
        ).fetchall()
        connect_rows = db.execute(
            f"""SELECT
                job_id, message_id, part_id, protocol_version,
                capability_id, capability_version,
                provider_app_id, provider_app_version, provider_instance_id,
                invocation_fingerprint,
                input_artifact_id, input_media_type, input_byte_size, input_sha256,
                input_display_name, source_app_id,
                request_json, status,
                output_artifact_id, output_media_type, output_byte_size, output_sha256,
                summary_version, summary_text, warnings_json,
                NULL AS result_json, result_metadata_json,
                error_code, error_message, error_retryable, created_at, updated_at
            FROM (
                SELECT
                    job_id, message_id, part_id, protocol_version,
                    capability_id, capability_version,
                    provider_app_id, provider_app_version, provider_instance_id,
                    invocation_fingerprint,
                    input_artifact_id, input_media_type, input_byte_size, input_sha256,
                    input_display_name, source_app_id,
                    request_json, status,
                    output_artifact_id, output_media_type, output_byte_size, output_sha256,
                    summary_version, summary_text, warnings_json, result_metadata_json,
                    error_code, error_message, error_retryable, created_at, updated_at,
                    ROW_NUMBER() OVER (
                    PARTITION BY message_id, part_id, protocol_version,
                        capability_id, capability_version, invocation_fingerprint
                    ORDER BY created_at DESC, rowid DESC
                    ) AS recency
                FROM connect_attachment_jobs
                WHERE message_id IN ({placeholders})
            )
            WHERE recency = 1
            ORDER BY created_at DESC""",
            message_ids,
        ).fetchall()
        attachments_by_message: dict[str, list[dict[str, object]]] = {
            message_id: [] for message_id in message_ids
        }
        for row in attachment_rows:
            attachment = dict(row)
            message_id = str(attachment.pop("message_id"))
            attachments_by_message[message_id].append(attachment)
        connect_by_attachment: dict[tuple[str, str], list[dict[str, object]]] = {}
        for row in connect_rows:
            key = (
                str(row["message_id"]),
                str(row["part_id"]),
                str(row["capability_id"]),
                str(row["capability_version"]),
            )
            item: dict[str, object] = {
                "job_id": str(row["job_id"]),
                "capability_id": key[2],
                "capability_version": key[3],
                "status": str(row["status"]),
                "updated_at": str(row["updated_at"]),
            }
            if int(row["protocol_version"]) == 2:
                request_json = row["request_json"]
                if not isinstance(request_json, bytes):
                    raise RuntimeError("Connect v2 job is missing its durable request")
                request = _decode_v2_request(request_json)
                item["protocol_version"] = 2
                item["provider"] = {
                    "app_id": str(row["provider_app_id"]),
                    "version": str(row["provider_app_version"]),
                    "instance_id": str(row["provider_instance_id"]),
                }
                item["parameters"] = _canonical_v2_parameters(request.get("parameters"))
            if row["status"] == "completed":
                completed = self._connect_job(row)
                if completed.protocol_version == 1:
                    item["summary"] = {
                        "summary_version": completed.summary_version,
                        "text": completed.summary_text,
                        "warnings": self.completed_connect_warnings(completed),
                    }
                else:
                    metadata_json = row["result_metadata_json"]
                    if not isinstance(metadata_json, bytes):
                        raise RuntimeError(
                            "Completed Connect v2 job is missing its durable result metadata"
                        )
                    item["outputs"] = _decode_generic_result_metadata(metadata_json)
            elif row["status"] == "failed":
                item["error"] = {
                    "code": str(row["error_code"]),
                    "message": str(row["error_message"]),
                    "retryable": bool(row["error_retryable"]),
                }
            connect_by_attachment.setdefault((key[0], key[1]), []).append(item)
        for message_id, attachments in attachments_by_message.items():
            for attachment in attachments:
                capability_results = connect_by_attachment.get(
                    (message_id, str(attachment["part_id"]))
                )
                if capability_results:
                    attachment["capability_results"] = capability_results
        for item in items:
            item["attachments"] = attachments_by_message[str(item["message_id"])]
        return items

    def purge(self, retention_days: int, *, now: datetime | None = None) -> int:
        stamp = (now or datetime.now(UTC)).astimezone(UTC)
        cutoff = stamp - timedelta(days=retention_days)
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        stamp_epoch = (stamp - epoch).total_seconds()
        cutoff_epoch = (cutoff - epoch).total_seconds()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                """DELETE FROM messages
                WHERE aware_iso_epoch(received_at) IS NULL
                   OR (
                       aware_iso_epoch(received_at) > ?
                       AND (
                           aware_iso_epoch(discovered_at) IS NULL
                           OR aware_iso_epoch(discovered_at) < ?
                           OR aware_iso_epoch(discovered_at) > ?
                       )
                   )
                   OR (
                       aware_iso_epoch(received_at) <= ?
                       AND aware_iso_epoch(received_at) < ?
                   )""",
                (
                    stamp_epoch,
                    cutoff_epoch,
                    stamp_epoch,
                    stamp_epoch,
                    cutoff_epoch,
                ),
            )
            db.execute(
                """DELETE FROM suppressed_messages
                WHERE julianday(expires_at) IS NULL
                   OR julianday(expires_at) < julianday(?)""",
                (stamp.isoformat(),),
            )
        return cursor.rowcount

    def outbound_status(self, dedupe_key: str) -> str | None:
        with self.connection() as db:
            row = db.execute(
                """SELECT 'sent' AS status FROM outbound_sends WHERE dedupe_key = ?
                UNION ALL
                SELECT status FROM outbound_reservations WHERE dedupe_key = ?
                LIMIT 1""",
                (dedupe_key, dedupe_key),
            ).fetchone()
        return str(row["status"]) if row else None

    def outbound_details(self, dedupe_key: str) -> dict[str, object] | None:
        with self.connection() as db:
            row = db.execute(
                """SELECT dedupe_key, recipient, subject, 'sent' AS status,
                gmail_message_id, NULL AS reserved_at, sent_at AS updated_at,
                NULL AS last_error FROM outbound_sends WHERE dedupe_key = ?
                UNION ALL
                SELECT dedupe_key, recipient, subject, status, NULL AS gmail_message_id,
                reserved_at, updated_at, last_error FROM outbound_reservations
                WHERE dedupe_key = ?
                LIMIT 1""",
                (dedupe_key, dedupe_key),
            ).fetchone()
        return dict(row) if row else None

    def outbound_was_sent(self, dedupe_key: str) -> bool:
        return self.outbound_status(dedupe_key) == "sent"

    def reserve_outbound(self, *, dedupe_key: str, recipient: str, subject: str) -> bool:
        stamp = datetime.now(UTC).isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM outbound_sends WHERE dedupe_key = ?", (dedupe_key,)
            ).fetchone():
                return False
            cursor = db.execute(
                """INSERT OR IGNORE INTO outbound_reservations(
                    dedupe_key, recipient, subject, status, reserved_at, updated_at
                ) VALUES (?, ?, ?, 'reserved', ?, ?)""",
                (dedupe_key, recipient, subject, stamp, stamp),
            )
        return cursor.rowcount == 1

    def mark_outbound_ambiguous(self, dedupe_key: str, error: str) -> None:
        with self.connection() as db:
            cursor = db.execute(
                """UPDATE outbound_reservations SET status='ambiguous', updated_at=?,
                last_error=? WHERE dedupe_key=? AND status='reserved'""",
                (datetime.now(UTC).isoformat(), error[:500], dedupe_key),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Outbound reservation is not in the reserved state")

    def reconcile_outbound_sent(self, dedupe_key: str, gmail_message_id: str) -> None:
        message_id = gmail_message_id.strip()
        if not message_id:
            raise RuntimeError("A non-empty Gmail message ID is required")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            completed = db.execute(
                "SELECT gmail_message_id FROM outbound_sends WHERE dedupe_key = ?",
                (dedupe_key,),
            ).fetchone()
            if completed:
                if completed["gmail_message_id"] == message_id:
                    return
                raise RuntimeError("Completed outbound send has a different Gmail message ID")
            reservation = db.execute(
                """SELECT recipient, subject FROM outbound_reservations
                WHERE dedupe_key = ?""",
                (dedupe_key,),
            ).fetchone()
            if not reservation:
                raise RuntimeError("Outbound send has no unresolved reservation")
            db.execute(
                """INSERT INTO outbound_sends(
                    dedupe_key, recipient, subject, gmail_message_id, sent_at
                ) VALUES (?, ?, ?, ?, ?)""",
                (
                    dedupe_key,
                    reservation["recipient"],
                    reservation["subject"],
                    message_id,
                    datetime.now(UTC).isoformat(),
                ),
            )
            db.execute("DELETE FROM outbound_reservations WHERE dedupe_key = ?", (dedupe_key,))

    def release_outbound(self, dedupe_key: str) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM outbound_sends WHERE dedupe_key = ?", (dedupe_key,)
            ).fetchone():
                raise RuntimeError("Cannot release a completed outbound send")
            cursor = db.execute(
                "DELETE FROM outbound_reservations WHERE dedupe_key = ?", (dedupe_key,)
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Outbound send has no unresolved reservation")

    def record_outbound(
        self,
        *,
        dedupe_key: str,
        recipient: str,
        subject: str,
        gmail_message_id: str,
    ) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            reservation = db.execute(
                """SELECT recipient, subject, status FROM outbound_reservations
                WHERE dedupe_key = ?""",
                (dedupe_key,),
            ).fetchone()
            if (
                not reservation
                or reservation["status"] != "reserved"
                or reservation["recipient"] != recipient
                or reservation["subject"] != subject
            ):
                raise RuntimeError("Outbound send has no matching active reservation")
            db.execute(
                """INSERT INTO outbound_sends(
                    dedupe_key, recipient, subject, gmail_message_id, sent_at
                ) VALUES (?, ?, ?, ?, ?)""",
                (
                    dedupe_key,
                    recipient,
                    subject,
                    gmail_message_id,
                    datetime.now(UTC).isoformat(),
                ),
            )
            db.execute("DELETE FROM outbound_reservations WHERE dedupe_key = ?", (dedupe_key,))
