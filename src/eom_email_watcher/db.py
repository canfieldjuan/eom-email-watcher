from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import sqlite3
import unicodedata
import uuid
from collections.abc import Iterable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from connect_automate.locking import connect_operation_lock, connect_source_lock_path

from .automation.rules import (
    MAX_AUTOMATION_ATTACHMENTS,
    MAX_AUTOMATION_RULES,
    AutomationFanoutLimit,
    MatchRule,
    RuleDefinition,
    RuleValidationError,
    canonical_rule_definition,
    match_rules,
    parse_rule_definition,
)
from .config import MAX_RETENTION_DAYS, normalize_validated_address
from .mailbox import DEFAULT_MAIL_ACCOUNT_ID, DEFAULT_MAIL_PROVIDER
from .mime import AttachmentDescriptor

SCHEMA_VERSION = 26
MAX_CONNECT_REQUEST_BYTES = 128 * 1024
MAX_CONNECT_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_CONNECT_RESULT_BYTES = 24 * 1024 * 1024
MAX_CONNECT_RESULT_METADATA_BYTES = 64 * 1024
AUTOMATION_CLEANUP_CHUNK_SIZE = 500
AUTOMATION_RETRY_DELAYS_SECONDS = (60, 300, 900, 3600)
MAX_AUTOMATION_PROPOSAL_ATTENDEES = 64
SCHEDULING_AUTOMATION_ID = "email.schedule_event"
SCHEDULING_AUTOMATION_VERSION = 1
SCHEDULING_EXTRACTION_SCHEMA_VERSION = 1
CONNECT_QUEUE_MAX_JOBS = 25
CONNECT_QUEUE_ADMISSION_WINDOW = timedelta(hours=2)
CONNECT_RETRY_DELAYS_SECONDS = (2, 4, 8, 16, 30)
CONNECT_PROVIDER_ABSENCE_DELAY_SECONDS = 30
MAX_CONNECT_DISPATCH_ERROR_CODE_BYTES = 128
MAX_CONNECT_DISPATCH_ERROR_MESSAGE_BYTES = 1024
SOURCE_CLEANUP_LOCK_BATCH_SIZE = 32
AUTOMATION_FIRE_STATES = (
    "pending_dispatch",
    "entitlement_paused",
    "awaiting_confirmation",
    "submitted",
    "completed",
    "failed",
    "declined",
    "manual_review",
    "source_unavailable",
)
AUTOMATION_FIRE_TERMINAL_STATES = frozenset(
    {"completed", "failed", "declined", "manual_review", "source_unavailable"}
)
AUTOMATION_FIRE_TRANSITIONS = frozenset(
    {
        ("pending_dispatch", "entitlement_paused"),
        ("pending_dispatch", "awaiting_confirmation"),
        ("pending_dispatch", "submitted"),
        ("pending_dispatch", "manual_review"),
        ("pending_dispatch", "source_unavailable"),
        ("entitlement_paused", "pending_dispatch"),
        ("entitlement_paused", "submitted"),
        ("entitlement_paused", "completed"),
        ("entitlement_paused", "failed"),
        ("entitlement_paused", "manual_review"),
        ("entitlement_paused", "source_unavailable"),
        ("awaiting_confirmation", "pending_dispatch"),
        ("awaiting_confirmation", "declined"),
        ("awaiting_confirmation", "manual_review"),
        ("awaiting_confirmation", "source_unavailable"),
        ("submitted", "completed"),
        ("submitted", "failed"),
        ("submitted", "entitlement_paused"),
        ("submitted", "pending_dispatch"),
        ("submitted", "manual_review"),
        ("submitted", "source_unavailable"),
    }
)
AUTOMATION_FIRE_MAX_ATTEMPTS = 2
AUTOMATION_FIRE_PENDING_WINDOW = timedelta(hours=2)
MAX_AUTOMATION_PREPARED_IDENTITY_BYTES = 32 * 1024
MAX_GMAIL_LABEL_SELECTORS = 100
MAX_GMAIL_LABEL_ID_BYTES = 512
MAX_GMAIL_LABEL_NAME_BYTES = 1024
MAX_GMAIL_RECOVERY_SNAPSHOT_BYTES = 1024 * 1024
MAX_GMAIL_RECOVERY_PAGE_JSON_BYTES = 512 * 1024
MAX_GMAIL_RECOVERY_PAGE_IDS = 200
MAX_GMAIL_RECOVERY_PAGE_TOKEN_BYTES = 8192
MAX_GMAIL_RECOVERY_CURSOR_BYTES = 4096
SQLITE_MAX_INTEGER = (1 << 63) - 1


class GmailLabelStoreError(RuntimeError):
    """Stable store failure surfaced through the engine protocol."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _utf8_size(value: str) -> int:
    return len(value.encode("utf-8"))


def _has_control_character(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def _require_bounded_text(
    value: object,
    *,
    maximum_bytes: int,
    field: str,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or _utf8_size(value) > maximum_bytes
        or _has_control_character(value)
    ):
        raise ValueError(f"{field} is invalid")
    return value


def _require_mailbox_identity_key(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("mailbox identity key must be a lower-case SHA-256 digest")
    return value


def _require_revision(value: object) -> int:
    if type(value) is not int or not 0 <= value <= SQLITE_MAX_INTEGER:
        raise ValueError("revision must be a non-negative SQLite integer")
    return value


def _validate_utc_timestamp(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or _utf8_size(value) > 64:
        raise ValueError(f"{field} must be a UTC ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be a UTC ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be a UTC ISO-8601 timestamp")
    return value


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _decode_json_bytes(value: object, *, field: str) -> object:
    if not isinstance(value, bytes):
        raise RuntimeError(f"{field} is invalid")

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        decoded: dict[str, object] = {}
        for key, item in pairs:
            if key in decoded:
                raise ValueError("duplicate JSON key")
            decoded[key] = item
        return decoded

    try:
        return json.loads(value.decode("utf-8"), object_pairs_hook=reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"{field} is invalid") from exc


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


def _message_suppression_key(
    provider: str,
    account_id: str,
    provider_message_id: str,
    mailbox_identity_key: str | None = None,
) -> str:
    identity = (
        (provider, account_id, mailbox_identity_key, provider_message_id)
        if mailbox_identity_key is not None
        else (provider, account_id, provider_message_id)
    )
    encoded = "\0".join(identity).encode("utf-8")
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


def _automation_expiry(observed_at: str, admitted_at: datetime) -> str:
    try:
        observed = datetime.fromisoformat(observed_at)
        if observed.tzinfo is None:
            raise ValueError("observed_at must include a timezone")
        observed = observed.astimezone(UTC)
    except (OverflowError, ValueError):
        observed = admitted_at
    return (min(observed, admitted_at) + timedelta(days=MAX_RETENTION_DAYS)).isoformat()


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

_CONNECT_DISPATCH_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS connect_job_dispatch (
    job_id TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK (
        state IN ('waiting', 'dispatching', 'reconciling', 'provider_owned', 'terminal')
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    reconciliation_failure_count INTEGER NOT NULL DEFAULT 0 CHECK (
        reconciliation_failure_count >= 0
    ),
    next_attempt_at TEXT,
    admission_deadline TEXT NOT NULL,
    submission_possible INTEGER NOT NULL DEFAULT 0 CHECK (submission_possible IN (0, 1)),
    source_available INTEGER NOT NULL DEFAULT 1 CHECK (source_available IN (0, 1)),
    capability_external_effects INTEGER NOT NULL DEFAULT 0 CHECK (
        capability_external_effects IN (0, 1)
    ),
    capability_confirmation_required INTEGER NOT NULL DEFAULT 0 CHECK (
        capability_confirmation_required IN (0, 1)
    ),
    capability_authority_known INTEGER NOT NULL DEFAULT 0 CHECK (
        capability_authority_known IN (0, 1)
    ),
    capability_produces_json TEXT NOT NULL DEFAULT '[]' CHECK (
        length(capability_produces_json) BETWEEN 2 AND 4096
    ),
    interactive_authorized_at TEXT,
    automation_paused_at TEXT,
    highest_provider_state TEXT NOT NULL DEFAULT 'requested' CHECK (
        highest_provider_state IN ('requested', 'accepted', 'processing')
    ),
    last_error_code TEXT,
    last_error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_CONNECT_DISPATCH_LANE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_connect_job_dispatch_lane
ON connect_attachment_jobs(
    protocol_version, provider_app_id, provider_instance_id, created_at, job_id
)
WHERE protocol_version = 2 AND status IN ('requested', 'accepted', 'processing')
"""

_CONNECT_JOBS_ACTIVE_V2_INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_connect_attachment_jobs_active_v2
ON connect_attachment_jobs(message_id, part_id, invocation_fingerprint)
WHERE protocol_version = 2 AND status IN ('requested', 'accepted', 'processing')
"""

_CONNECT_DISPATCH_DELETE_TRIGGER_SQL = """
CREATE TRIGGER IF NOT EXISTS connect_jobs_delete_dispatch
AFTER DELETE ON connect_attachment_jobs
BEGIN
    DELETE FROM connect_job_dispatch WHERE job_id = OLD.job_id;
END
"""

_CONNECT_TABLE_TRIGGER_NAMES = (
    "messages_delete_connect_attachment_jobs",
    "connect_jobs_delete_dispatch",
    "messages_delete_pending_automation_fires",
    "connect_jobs_delete_linked_automation_fires",
)

_CONNECT_JOBS_DELETE_TRIGGER_V19_SQL = """
CREATE TRIGGER messages_delete_connect_attachment_jobs
AFTER DELETE ON messages
BEGIN
    DELETE FROM connect_attachment_jobs
    WHERE message_id = OLD.message_id
      AND (
        protocol_version = 1
        OR (
          status IN ('completed', 'failed')
          AND NOT EXISTS (
            SELECT 1 FROM automation_fires AS fire
            WHERE fire.job_id = connect_attachment_jobs.job_id
              AND fire.state = 'entitlement_paused'
              AND (
                NOT EXISTS (
                  SELECT 1 FROM automation_fire_attempts AS attempt
                  WHERE attempt.dispatch_request_id = connect_attachment_jobs.job_id
                )
                OR EXISTS (
                  SELECT 1 FROM connect_job_dispatch AS dispatch
                  WHERE dispatch.job_id = connect_attachment_jobs.job_id
                    AND dispatch.interactive_authorized_at IS NOT NULL
                )
              )
          )
        )
        OR job_id IN (
            SELECT job_id FROM connect_job_dispatch
            WHERE state = 'waiting' AND submission_possible = 0
        )
      );
    UPDATE connect_job_dispatch
    SET source_available = 0,
        updated_at = strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
    WHERE job_id IN (
        SELECT job_id FROM connect_attachment_jobs WHERE message_id = OLD.message_id
    );
END
"""

_AUTOMATE_CORE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS automation_rule_set (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    revision INTEGER NOT NULL CHECK (revision >= 0)
);
INSERT OR IGNORE INTO automation_rule_set(id, revision) VALUES (1, 0);
CREATE TABLE IF NOT EXISTS legacy_mailbox_markers (
    provider TEXT NOT NULL CHECK (provider <> ''),
    account_id TEXT NOT NULL CHECK (account_id <> ''),
    message_key TEXT NOT NULL CHECK (length(message_key) = 64),
    expires_at TEXT NOT NULL,
    PRIMARY KEY (provider, account_id, message_key)
);
CREATE TABLE IF NOT EXISTS automation_rules (
    rule_id TEXT PRIMARY KEY CHECK (length(rule_id) = 36),
    current_version INTEGER NOT NULL CHECK (current_version >= 1),
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    system INTEGER NOT NULL DEFAULT 0 CHECK (system IN (0, 1)),
    deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS automation_rule_versions (
    rule_id TEXT NOT NULL CHECK (length(rule_id) = 36),
    version INTEGER NOT NULL CHECK (version >= 1),
    kind TEXT NOT NULL CHECK (kind IN ('definition', 'deleted')),
    definition_json BLOB CHECK (
        (kind = 'deleted' AND definition_json IS NULL)
        OR (kind = 'definition' AND typeof(definition_json) = 'blob'
            AND length(definition_json) BETWEEN 1 AND 16384)
    ),
    definition_sha256 TEXT CHECK (
        (kind = 'deleted' AND definition_sha256 IS NULL)
        OR (kind = 'definition' AND length(definition_sha256) = 64)
    ),
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    scope_mailbox_identity_key TEXT CHECK (
        scope_mailbox_identity_key IS NULL OR length(scope_mailbox_identity_key) = 64
    ),
    global_revision INTEGER NOT NULL UNIQUE CHECK (global_revision >= 1),
    accepted_at TEXT NOT NULL,
    PRIMARY KEY (rule_id, version)
);
CREATE INDEX IF NOT EXISTS idx_automation_rule_versions_revision
    ON automation_rule_versions(rule_id, global_revision);
CREATE TRIGGER IF NOT EXISTS automation_rule_versions_immutable_update
BEFORE UPDATE ON automation_rule_versions
BEGIN
    SELECT RAISE(ABORT, 'automation rule versions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS automation_rule_versions_immutable_delete
BEFORE DELETE ON automation_rule_versions
BEGIN
    SELECT RAISE(ABORT, 'automation rule versions are immutable');
END;
"""

_AUTOMATION_FIRE_TABLES_SQL = f"""
CREATE TABLE IF NOT EXISTS automation_fires (
    fire_id TEXT PRIMARY KEY CHECK (length(fire_id) = 36),
    event_id TEXT NOT NULL CHECK (length(event_id) = 64),
    rule_id TEXT NOT NULL CHECK (length(rule_id) = 36),
    rule_version INTEGER NOT NULL CHECK (rule_version >= 1),
    message_id TEXT NOT NULL CHECK (message_id <> ''),
    part_id TEXT NOT NULL,
    action_kind TEXT NOT NULL CHECK (action_kind = 'connect.invoke'),
    state TEXT NOT NULL CHECK (state IN (
        'pending_dispatch', 'entitlement_paused', 'awaiting_confirmation',
        'submitted', 'completed', 'failed', 'declined', 'manual_review',
        'source_unavailable'
    )),
    state_version INTEGER NOT NULL CHECK (state_version >= 1),
    reason TEXT CHECK (reason IS NULL OR (reason <> '' AND length(reason) <= 128)),
    current_attempt_no INTEGER NOT NULL DEFAULT 1 CHECK (current_attempt_no IN (1, 2)),
    job_id TEXT CHECK (job_id IS NULL OR length(job_id) = 36),
    prepared_identity_sha256 TEXT CHECK (
        prepared_identity_sha256 IS NULL OR length(prepared_identity_sha256) = 64
    ),
    prepared_identity_json BLOB CHECK (
        prepared_identity_json IS NULL OR (
            typeof(prepared_identity_json) = 'blob'
            AND length(prepared_identity_json) BETWEEN 2
                AND {MAX_AUTOMATION_PREPARED_IDENTITY_BYTES}
        )
    ),
    confirmed INTEGER NOT NULL DEFAULT 0 CHECK (confirmed IN (0, 1)),
    pending_since TEXT,
    authorized_pending_seconds INTEGER NOT NULL DEFAULT 0 CHECK (
        authorized_pending_seconds >= 0
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (prepared_identity_sha256 IS NULL AND prepared_identity_json IS NULL)
        OR (prepared_identity_sha256 IS NOT NULL AND prepared_identity_json IS NOT NULL)
    ),
    CHECK (state <> 'submitted' OR job_id IS NOT NULL),
    UNIQUE (message_id, part_id, rule_id, rule_version)
);
CREATE INDEX IF NOT EXISTS idx_automation_fires_state
    ON automation_fires(state, created_at, fire_id);
CREATE INDEX IF NOT EXISTS idx_automation_fires_message
    ON automation_fires(message_id, part_id);
CREATE TRIGGER IF NOT EXISTS automation_fires_require_sources
BEFORE INSERT ON automation_fires
WHEN NOT EXISTS (
        SELECT 1 FROM automation_rule_versions
        WHERE rule_id = NEW.rule_id AND version = NEW.rule_version
          AND kind = 'definition'
    ) OR NOT EXISTS (
        SELECT 1 FROM message_attachments
        WHERE message_id = NEW.message_id AND part_id = NEW.part_id
    )
BEGIN
    SELECT RAISE(ABORT, 'automation fire source is invalid');
END;
CREATE TABLE IF NOT EXISTS automation_fire_attempts (
    fire_id TEXT NOT NULL CHECK (length(fire_id) = 36),
    attempt_no INTEGER NOT NULL CHECK (attempt_no IN (1, 2)),
    dispatch_request_id TEXT NOT NULL UNIQUE CHECK (length(dispatch_request_id) = 36),
    job_id TEXT CHECK (job_id IS NULL OR length(job_id) = 36),
    created_at TEXT NOT NULL,
    PRIMARY KEY (fire_id, attempt_no)
);
CREATE TRIGGER IF NOT EXISTS automation_fire_attempts_immutable_update
BEFORE UPDATE ON automation_fire_attempts
WHEN NEW.fire_id <> OLD.fire_id OR NEW.attempt_no <> OLD.attempt_no
  OR NEW.dispatch_request_id <> OLD.dispatch_request_id
  OR NEW.created_at <> OLD.created_at
  OR (OLD.job_id IS NOT NULL AND NEW.job_id IS NOT OLD.job_id)
BEGIN
    SELECT RAISE(ABORT, 'automation fire attempts are immutable');
END;
CREATE TRIGGER IF NOT EXISTS automation_fire_attempts_require_fire
BEFORE INSERT ON automation_fire_attempts
WHEN NOT EXISTS (SELECT 1 FROM automation_fires WHERE fire_id = NEW.fire_id)
BEGIN
    SELECT RAISE(ABORT, 'automation fire attempt source is invalid');
END;
CREATE TABLE IF NOT EXISTS automation_fire_confirmations (
    fire_id TEXT PRIMARY KEY CHECK (length(fire_id) = 36),
    prepared_identity_sha256 TEXT NOT NULL CHECK (length(prepared_identity_sha256) = 64),
    confirmed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS automation_run_source_identities (
    run_id TEXT PRIMARY KEY CHECK (run_id <> ''),
    mailbox_identity_key TEXT NOT NULL CHECK (length(mailbox_identity_key) = 64)
);
CREATE TRIGGER IF NOT EXISTS messages_delete_pending_automation_fires
BEFORE DELETE ON messages
BEGIN
    UPDATE automation_fire_attempts
    SET job_id = dispatch_request_id
    WHERE job_id IS NULL
      AND fire_id IN (
        SELECT fire_id FROM automation_fires
        WHERE message_id = OLD.message_id
          AND state IN ('pending_dispatch', 'entitlement_paused')
      )
      AND EXISTS (
        SELECT 1
        FROM connect_attachment_jobs AS job
        JOIN connect_job_dispatch AS dispatch ON dispatch.job_id = job.job_id
        WHERE job.job_id = automation_fire_attempts.dispatch_request_id
          AND job.protocol_version = 2
          AND job.status IN ('requested', 'accepted', 'processing', 'completed', 'failed')
          AND dispatch.capability_authority_known = 1
          AND NOT (
            dispatch.state = 'waiting' AND dispatch.submission_possible = 0
          )
      );
    UPDATE automation_fires
    SET state = 'submitted',
        state_version = state_version + 1,
        reason = 'source_removed_after_provider_admission',
        job_id = (
            SELECT attempt.job_id
            FROM automation_fire_attempts AS attempt
            WHERE attempt.fire_id = automation_fires.fire_id
              AND attempt.attempt_no = automation_fires.current_attempt_no
        ),
        pending_since = NULL,
        updated_at = strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
    WHERE message_id = OLD.message_id
      AND state IN ('pending_dispatch', 'entitlement_paused')
      AND job_id IS NULL
      AND EXISTS (
        SELECT 1
        FROM automation_fire_attempts AS attempt
        WHERE attempt.fire_id = automation_fires.fire_id
          AND attempt.attempt_no = automation_fires.current_attempt_no
          AND attempt.job_id IS NOT NULL
      );
    DELETE FROM automation_fire_confirmations
    WHERE fire_id IN (
        SELECT fire_id FROM automation_fires
        WHERE message_id = OLD.message_id
          AND (
            state IN ('pending_dispatch', 'awaiting_confirmation')
            OR (state = 'entitlement_paused' AND job_id IS NULL)
          )
    );
    DELETE FROM automation_fire_attempts
    WHERE fire_id IN (
        SELECT fire_id FROM automation_fires
        WHERE message_id = OLD.message_id
          AND (
            state IN ('pending_dispatch', 'awaiting_confirmation')
            OR (state = 'entitlement_paused' AND job_id IS NULL)
          )
    );
    DELETE FROM automation_fires
    WHERE message_id = OLD.message_id
      AND (
        state IN ('pending_dispatch', 'awaiting_confirmation')
        OR (state = 'entitlement_paused' AND job_id IS NULL)
      );
END;
CREATE TRIGGER IF NOT EXISTS connect_jobs_delete_linked_automation_fires
AFTER DELETE ON connect_attachment_jobs
BEGIN
    UPDATE automation_fires SET
        state = CASE OLD.status
            WHEN 'completed' THEN 'completed'
            WHEN 'failed' THEN 'failed'
            ELSE 'source_unavailable'
        END,
        state_version = state_version + 1,
        reason = CASE OLD.status
            WHEN 'completed' THEN 'connect_completed'
            WHEN 'failed' THEN substr(COALESCE(OLD.error_code, 'connect_failed'), 1, 128)
            ELSE 'job_removed'
        END,
        job_id = CASE
            WHEN OLD.status IN ('completed', 'failed') THEN OLD.job_id
            ELSE NULL
        END,
        pending_since = NULL,
        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    WHERE job_id = OLD.job_id
      AND state IN ('submitted', 'entitlement_paused');
END;
"""

_AUTOMATION_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS automation_runs (
    run_id TEXT PRIMARY KEY CHECK (length(run_id) = 36),
    provider TEXT NOT NULL CHECK (provider <> ''),
    account_id TEXT NOT NULL CHECK (account_id <> ''),
    calendar_principal_key TEXT NOT NULL CHECK (length(calendar_principal_key) = 64),
    source_message_key TEXT NOT NULL CHECK (length(source_message_key) = 64),
    automation_id TEXT NOT NULL CHECK (automation_id <> ''),
    automation_version INTEGER NOT NULL CHECK (automation_version > 0),
    extraction_schema_version INTEGER NOT NULL CHECK (extraction_schema_version > 0),
    state TEXT NOT NULL CHECK (state IN (
        'detected', 'extracting', 'ambiguous', 'manual_review', 'proposing',
        'awaiting_confirmation', 'declined', 'write_authorized', 'writing',
        'completed', 'failed', 'unresolved', 'source_unavailable', 'reconciling'
    )),
    state_version INTEGER NOT NULL CHECK (state_version >= 1),
    failure_code TEXT,
    source_content_sha256 TEXT CHECK (
        source_content_sha256 IS NULL OR length(source_content_sha256) = 64
    ),
    extraction_context_at TEXT,
    extraction_timezone TEXT,
    extraction_body_char_limit INTEGER CHECK (
        extraction_body_char_limit IS NULL OR extraction_body_char_limit > 0
    ),
    current_payload_id TEXT CHECK (
        current_payload_id IS NULL OR length(current_payload_id) = 36
    ),
    current_payload_sha256 TEXT CHECK (
        current_payload_sha256 IS NULL OR length(current_payload_sha256) = 64
    ),
    review_notified_at TEXT,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (
        provider, account_id, source_message_key, automation_id, automation_version
    ),
    CHECK (state <> 'detected' OR failure_code IS NULL),
    CHECK (
        state <> 'source_unavailable'
        OR failure_code = 'source_unavailable'
    )
);
CREATE INDEX IF NOT EXISTS idx_automation_runs_recovery
    ON automation_runs(state, updated_at);
CREATE TABLE IF NOT EXISTS automation_events (
    event_id TEXT PRIMARY KEY CHECK (length(event_id) = 36),
    run_id TEXT NOT NULL CHECK (run_id <> ''),
    sequence_no INTEGER NOT NULL CHECK (sequence_no >= 0),
    previous_state TEXT CHECK (previous_state IS NULL OR previous_state IN (
        'detected', 'extracting', 'ambiguous', 'manual_review', 'proposing',
        'awaiting_confirmation', 'declined', 'write_authorized', 'writing',
        'completed', 'failed', 'unresolved', 'source_unavailable', 'reconciling'
    )),
    next_state TEXT NOT NULL CHECK (next_state IN (
        'detected', 'extracting', 'ambiguous', 'manual_review', 'proposing',
        'awaiting_confirmation', 'declined', 'write_authorized', 'writing',
        'completed', 'failed', 'unresolved', 'source_unavailable', 'reconciling'
    )),
    state_version INTEGER NOT NULL CHECK (state_version >= 1),
    automation_id TEXT NOT NULL CHECK (automation_id <> ''),
    automation_version INTEGER NOT NULL CHECK (automation_version > 0),
    extraction_schema_version INTEGER NOT NULL CHECK (extraction_schema_version > 0),
    calendar_principal_key TEXT NOT NULL CHECK (length(calendar_principal_key) = 64),
    transition_kind TEXT NOT NULL CHECK (transition_kind IN (
        'detected', 'extracting', 'ambiguous', 'manual_review', 'proposing',
        'awaiting_confirmation', 'declined', 'write_authorized', 'writing',
        'completed', 'failed', 'unresolved', 'source_unavailable', 'reconciling'
    )),
    failure_code TEXT,
    payload_id TEXT CHECK (payload_id IS NULL OR length(payload_id) = 36),
    payload_sha256 TEXT CHECK (payload_sha256 IS NULL OR length(payload_sha256) = 64),
    decision TEXT CHECK (decision IS NULL OR decision IN ('confirmed', 'declined')),
    transaction_id TEXT CHECK (transaction_id IS NULL OR length(transaction_id) = 36),
    graph_event_id TEXT CHECK (
        graph_event_id IS NULL OR length(CAST(graph_event_id AS BLOB)) BETWEEN 1 AND 512
    ),
    created_at TEXT NOT NULL,
    UNIQUE (run_id, sequence_no),
    UNIQUE (run_id, state_version),
    CHECK (
        (next_state = 'detected' AND previous_state IS NULL
            AND sequence_no = 0 AND state_version = 1
            AND transition_kind = 'detected' AND failure_code IS NULL)
        OR (next_state <> 'detected' AND previous_state IS NOT NULL
            AND sequence_no >= 1 AND state_version = sequence_no + 1)
    )
);
CREATE INDEX IF NOT EXISTS idx_automation_events_run
    ON automation_events(run_id, sequence_no);
CREATE TRIGGER IF NOT EXISTS automation_events_no_update
BEFORE UPDATE ON automation_events
BEGIN
    SELECT RAISE(ABORT, 'automation_events are immutable');
END;
CREATE TRIGGER IF NOT EXISTS automation_events_no_delete
BEFORE DELETE ON automation_events
WHEN EXISTS (SELECT 1 FROM automation_runs WHERE run_id = OLD.run_id)
BEGIN
    SELECT RAISE(ABORT, 'automation_events are immutable');
END;
CREATE TABLE IF NOT EXISTS automation_extraction_payloads (
    payload_id TEXT PRIMARY KEY CHECK (length(payload_id) = 36),
    run_id TEXT NOT NULL CHECK (run_id <> ''),
    attempt_no INTEGER NOT NULL CHECK (attempt_no IN (1, 2)),
    request_id TEXT NOT NULL UNIQUE CHECK (length(request_id) = 36),
    status TEXT NOT NULL CHECK (status IN ('reserved', 'accepted', 'rejected')),
    source_content_sha256 TEXT NOT NULL CHECK (length(source_content_sha256) = 64),
    context_at TEXT NOT NULL CHECK (context_at <> ''),
    timezone TEXT NOT NULL CHECK (timezone <> ''),
    body_char_limit INTEGER NOT NULL CHECK (body_char_limit > 0),
    organizer_address TEXT NOT NULL CHECK (
        organizer_address <> '' AND length(organizer_address) <= 320
    ),
    failure_count INTEGER NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
    next_retry_at TEXT,
    last_error_code TEXT CHECK (
        last_error_code IS NULL OR length(last_error_code) BETWEEN 1 AND 64
    ),
    result_sha256 TEXT CHECK (result_sha256 IS NULL OR length(result_sha256) = 64),
    result_json BLOB CHECK (
        result_json IS NULL OR (
            typeof(result_json) = 'blob' AND length(result_json) BETWEEN 1 AND 32768
        )
    ),
    violations_json BLOB CHECK (
        violations_json IS NULL OR (
            typeof(violations_json) = 'blob' AND length(violations_json) BETWEEN 2 AND 8192
        )
    ),
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE (run_id, attempt_no),
    CHECK (
        (status = 'reserved' AND result_sha256 IS NULL AND result_json IS NULL
            AND violations_json IS NULL AND completed_at IS NULL)
        OR (status = 'accepted' AND result_sha256 IS NOT NULL AND result_json IS NOT NULL
            AND violations_json = X'5B5D' AND completed_at IS NOT NULL)
        OR (status = 'rejected' AND result_sha256 IS NOT NULL AND result_json IS NOT NULL
            AND violations_json IS NOT NULL AND violations_json <> X'5B5D'
            AND completed_at IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_automation_extraction_payloads_run
    ON automation_extraction_payloads(run_id, attempt_no);
CREATE TRIGGER IF NOT EXISTS automation_runs_delete_extraction_payloads
AFTER DELETE ON automation_runs
BEGIN
    DELETE FROM automation_extraction_payloads WHERE run_id = OLD.run_id;
END;
CREATE TABLE IF NOT EXISTS automation_proposal_payloads (
    payload_id TEXT PRIMARY KEY CHECK (length(payload_id) = 36),
    run_id TEXT NOT NULL CHECK (run_id <> ''),
    proposal_version INTEGER NOT NULL CHECK (proposal_version > 0),
    status TEXT NOT NULL CHECK (status IN ('accepted', 'no_suggestions')),
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    proposal_sha256 TEXT NOT NULL CHECK (length(proposal_sha256) = 64),
    subject TEXT NOT NULL CHECK (length(CAST(subject AS BLOB)) <= 512),
    attendees_json BLOB NOT NULL CHECK (
        typeof(attendees_json) = 'blob'
        AND length(attendees_json) BETWEEN 2 AND 32768
    ),
    start TEXT CHECK (start IS NULL OR length(CAST(start AS BLOB)) <= 64),
    end TEXT CHECK (end IS NULL OR length(CAST(end AS BLOB)) <= 64),
    timezone TEXT CHECK (timezone IS NULL OR length(CAST(timezone AS BLOB)) <= 128),
    suggestion_reason TEXT CHECK (
        suggestion_reason IS NULL OR length(CAST(suggestion_reason AS BLOB)) <= 512
    ),
    empty_reason TEXT CHECK (
        empty_reason IS NULL OR length(CAST(empty_reason AS BLOB)) <= 512
    ),
    observed_at TEXT NOT NULL CHECK (observed_at <> ''),
    expires_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, proposal_version),
    CHECK (
        (status = 'accepted' AND start IS NOT NULL AND end IS NOT NULL
            AND timezone IS NOT NULL AND suggestion_reason IS NOT NULL
            AND empty_reason IS NULL AND expires_at IS NOT NULL)
        OR (status = 'no_suggestions' AND start IS NULL AND end IS NULL
            AND timezone IS NULL AND suggestion_reason IS NULL
            AND empty_reason IS NOT NULL AND expires_at IS NULL)
    )
);
CREATE TRIGGER IF NOT EXISTS automation_runs_delete_proposal_payloads
AFTER DELETE ON automation_runs
BEGIN
    DELETE FROM automation_proposal_payloads WHERE run_id = OLD.run_id;
END;
CREATE TABLE IF NOT EXISTS automation_calendar_writes (
    run_id TEXT PRIMARY KEY CHECK (run_id <> ''),
    transaction_id TEXT NOT NULL UNIQUE CHECK (length(transaction_id) = 36),
    proposal_version INTEGER NOT NULL CHECK (proposal_version > 0),
    proposal_sha256 TEXT NOT NULL CHECK (length(proposal_sha256) = 64),
    calendar_principal_key TEXT NOT NULL CHECK (length(calendar_principal_key) = 64),
    calendar_id TEXT NOT NULL CHECK (calendar_id = 'primary'),
    start TEXT NOT NULL CHECK (length(CAST(start AS BLOB)) <= 64),
    end TEXT NOT NULL CHECK (length(CAST(end AS BLOB)) <= 64),
    timezone TEXT NOT NULL CHECK (length(CAST(timezone AS BLOB)) <= 128),
    status TEXT NOT NULL CHECK (
        status IN (
            'authorized', 'writing', 'unresolved', 'reconciling',
            'completed', 'failed', 'cancelled'
        )
    ),
    graph_event_id TEXT CHECK (
        graph_event_id IS NULL OR length(CAST(graph_event_id AS BLOB)) BETWEEN 1 AND 512
    ),
    failure_code TEXT CHECK (
        failure_code IS NULL OR length(failure_code) BETWEEN 1 AND 64
    ),
    confirmed_at TEXT NOT NULL,
    submitted_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL,
    CHECK (
        (status = 'completed' AND graph_event_id IS NOT NULL AND completed_at IS NOT NULL)
        OR (status <> 'completed' AND graph_event_id IS NULL AND completed_at IS NULL)
    )
);
CREATE TRIGGER IF NOT EXISTS automation_runs_delete_calendar_writes
AFTER DELETE ON automation_runs
BEGIN
    DELETE FROM automation_calendar_writes WHERE run_id = OLD.run_id;
END;
"""


def _microsoft_principal_key_v1(
    home_account_id: str,
    tenant_id: str,
    object_id: str,
) -> str:
    value = "\0".join((home_account_id, tenant_id, object_id))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _microsoft_principal_key_v2(home_account_id: str, object_id: str) -> str:
    value = "\0".join(("msal-principal-v2", home_account_id, object_id.casefold()))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _migrate_microsoft_principal_keys_v2(db: sqlite3.Connection) -> None:
    rows = db.execute(
        """SELECT account_id, principal_key, home_account_id, tenant_id, object_id
        FROM microsoft_calendar_grants
        WHERE principal_key IS NOT NULL
            AND home_account_id IS NOT NULL
            AND tenant_id IS NOT NULL
            AND object_id IS NOT NULL"""
    ).fetchall()
    migrations: dict[tuple[str, str], str] = {}
    for row in rows:
        account_id = str(row["account_id"])
        old_key = str(row["principal_key"])
        home_account_id = str(row["home_account_id"])
        tenant_id = str(row["tenant_id"])
        object_id = str(row["object_id"])
        legacy_key = _microsoft_principal_key_v1(
            home_account_id,
            tenant_id,
            object_id,
        )
        new_key = _microsoft_principal_key_v2(home_account_id, object_id)
        if old_key not in {legacy_key, new_key}:
            continue
        source = (account_id, legacy_key)
        previous = migrations.setdefault(source, new_key)
        if previous != new_key:
            raise RuntimeError("Microsoft calendar grants contain conflicting principal identities")

    _rewrite_microsoft_principal_keys(db, migrations)


def _rewrite_microsoft_principal_keys(
    db: sqlite3.Connection,
    migrations: dict[tuple[str, str], str],
) -> None:
    if not migrations:
        return

    db.execute("DROP TRIGGER IF EXISTS automation_events_no_update")
    for (account_id, old_key), new_key in sorted(migrations.items()):
        run_ids = [
            str(row["run_id"])
            for row in db.execute(
                """SELECT run_id FROM automation_runs
                WHERE provider = 'microsoft365' AND account_id = ?
                    AND calendar_principal_key = ?""",
                (account_id, old_key),
            ).fetchall()
        ]
        for run_id in run_ids:
            db.execute(
                """UPDATE automation_events SET calendar_principal_key = ?
                WHERE run_id = ? AND calendar_principal_key = ?""",
                (new_key, run_id, old_key),
            )
            db.execute(
                """UPDATE automation_calendar_writes SET calendar_principal_key = ?
                WHERE run_id = ? AND calendar_principal_key = ?""",
                (new_key, run_id, old_key),
            )
        db.execute(
            """UPDATE automation_runs SET calendar_principal_key = ?
            WHERE provider = 'microsoft365' AND account_id = ?
                AND calendar_principal_key = ?""",
            (new_key, account_id, old_key),
        )
        db.execute(
            """UPDATE microsoft_calendar_windows SET principal_key = ?
            WHERE account_id = ? AND principal_key = ?""",
            (new_key, account_id, old_key),
        )
        db.execute(
            """UPDATE microsoft_calendar_grants SET principal_key = ?
            WHERE account_id = ? AND principal_key = ?""",
            (new_key, account_id, old_key),
        )
    db.execute(
        """CREATE TRIGGER automation_events_no_update
        BEFORE UPDATE ON automation_events
        BEGIN
            SELECT RAISE(ABORT, 'automation_events are immutable');
        END"""
    )


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
class PurgeOutcome:
    messages: int
    automation_review_required: int


@dataclass(frozen=True)
class AutomationRun:
    run_id: str
    provider: str
    account_id: str
    calendar_principal_key: str
    source_message_key: str
    automation_id: str
    automation_version: int
    extraction_schema_version: int
    state: str
    state_version: int
    failure_code: str | None
    source_content_sha256: str | None
    extraction_context_at: str | None
    extraction_timezone: str | None
    extraction_body_char_limit: int | None
    current_payload_id: str | None
    current_payload_sha256: str | None
    review_notified_at: str | None
    expires_at: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class AutomationEvent:
    event_id: str
    run_id: str
    sequence_no: int
    previous_state: str | None
    next_state: str
    state_version: int
    automation_id: str
    automation_version: int
    extraction_schema_version: int
    calendar_principal_key: str
    transition_kind: str
    failure_code: str | None
    payload_id: str | None
    payload_sha256: str | None
    decision: str | None
    transaction_id: str | None
    graph_event_id: str | None
    created_at: str


@dataclass(frozen=True)
class AutomationExtractionPayload:
    payload_id: str
    run_id: str
    attempt_no: int
    request_id: str
    status: str
    source_content_sha256: str
    context_at: str
    timezone: str
    body_char_limit: int
    organizer_address: str | None
    failure_count: int
    next_retry_at: str | None
    last_error_code: str | None
    result_sha256: str | None
    result_json: bytes | None
    violations_json: bytes | None
    created_at: str
    completed_at: str | None


@dataclass(frozen=True)
class AutomationProposalPayload:
    payload_id: str
    run_id: str
    proposal_version: int
    status: str
    request_sha256: str
    proposal_sha256: str
    subject: str
    attendees_json: bytes
    start: str | None
    end: str | None
    timezone: str | None
    suggestion_reason: str | None
    empty_reason: str | None
    observed_at: str
    expires_at: str | None
    created_at: str

    @property
    def attendees(self) -> tuple[str, ...]:
        value = json.loads(self.attendees_json)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise RuntimeError("Stored automation proposal attendees are invalid")
        return tuple(value)


@dataclass(frozen=True)
class AutomationWork:
    run: AutomationRun
    message_id: str
    provider_message_id: str
    sender: str
    sender_name: str | None
    subject: str
    received_at: str
    organizer_address: str
    extraction_organizer_address: str | None
    source_mailbox_identity_key: str | None = None


@dataclass(frozen=True)
class AutomationProposalWork:
    run: AutomationRun
    message_id: str
    subject: str
    organizer_address: str
    extraction_payload: AutomationExtractionPayload


@dataclass(frozen=True)
class AutomationCalendarWrite:
    run_id: str
    transaction_id: str
    proposal_version: int
    proposal_sha256: str
    calendar_principal_key: str
    calendar_id: str
    start: str
    end: str
    timezone: str
    status: str
    graph_event_id: str | None
    failure_code: str | None
    confirmed_at: str
    submitted_at: str | None
    completed_at: str | None
    updated_at: str


@dataclass(frozen=True)
class AutomationCalendarWriteWork:
    run: AutomationRun
    proposal: AutomationProposalPayload | None
    write: AutomationCalendarWrite


@dataclass(frozen=True)
class AutomationRuleSummary:
    rule_id: str
    version: int
    enabled: bool
    system: bool
    valid: bool
    name: str | None
    invalid_reason: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class AutomationRuleDetail:
    summary: AutomationRuleSummary
    definition: dict[str, object] | None


@dataclass(frozen=True)
class AutomationFire:
    fire_id: str
    event_id: str
    rule_id: str
    rule_version: int
    message_id: str
    part_id: str
    action_kind: str
    state: str
    state_version: int
    reason: str | None
    current_attempt_no: int
    job_id: str | None
    prepared_identity_sha256: str | None
    prepared_identity_json: bytes | None
    confirmed: bool
    pending_since: str | None
    authorized_pending_seconds: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class AutomationFireAttempt:
    fire_id: str
    attempt_no: int
    dispatch_request_id: str
    job_id: str | None
    created_at: str


class AutomationRuleNotFound(KeyError):
    """The requested live rule identity does not exist."""


class AutomationRuleStale(RuntimeError):
    """A mutation did not name the current rule version."""


class AutomationRuleLimitExceeded(RuntimeError):
    """The live non-deleted rule cap has been reached."""


class AutomationRuleSystemProtected(RuntimeError):
    """A system-owned rule cannot be edited or deleted."""


class MailboxIdentityChanged(RuntimeError):
    """A polling or mutation session no longer owns the active mailbox key."""


class AutomationSourceChanged(RuntimeError):
    """A recoverable automation source no longer matches its first extraction input."""


def _automation_run(row: sqlite3.Row) -> AutomationRun:
    return AutomationRun(**dict(row))


def _automation_fire(row: sqlite3.Row) -> AutomationFire:
    prepared = row["prepared_identity_json"]
    return AutomationFire(
        fire_id=str(row["fire_id"]),
        event_id=str(row["event_id"]),
        rule_id=str(row["rule_id"]),
        rule_version=int(row["rule_version"]),
        message_id=str(row["message_id"]),
        part_id=str(row["part_id"]),
        action_kind=str(row["action_kind"]),
        state=str(row["state"]),
        state_version=int(row["state_version"]),
        reason=row["reason"],
        current_attempt_no=int(row["current_attempt_no"]),
        job_id=row["job_id"],
        prepared_identity_sha256=row["prepared_identity_sha256"],
        prepared_identity_json=bytes(prepared) if prepared is not None else None,
        confirmed=bool(row["confirmed"]),
        pending_since=row["pending_since"],
        authorized_pending_seconds=int(row["authorized_pending_seconds"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _automation_event(row: sqlite3.Row) -> AutomationEvent:
    return AutomationEvent(**dict(row))


def _automation_extraction_payload(row: sqlite3.Row) -> AutomationExtractionPayload:
    return AutomationExtractionPayload(**dict(row))


def _automation_proposal_payload(row: sqlite3.Row) -> AutomationProposalPayload:
    return AutomationProposalPayload(**dict(row))


def _automation_calendar_write(row: sqlite3.Row) -> AutomationCalendarWrite:
    return AutomationCalendarWrite(**dict(row))


def _append_automation_event(
    db: sqlite3.Connection,
    *,
    run_id: str,
    sequence_no: int,
    previous_state: str | None,
    next_state: str,
    state_version: int,
    automation_id: str,
    automation_version: int,
    extraction_schema_version: int,
    calendar_principal_key: str,
    transition_kind: str,
    failure_code: str | None,
    created_at: str,
    payload_id: str | None = None,
    payload_sha256: str | None = None,
    decision: str | None = None,
    transaction_id: str | None = None,
    graph_event_id: str | None = None,
) -> None:
    db.execute(
        """INSERT INTO automation_events(
            event_id, run_id, sequence_no, previous_state, next_state, state_version,
            automation_id, automation_version, extraction_schema_version,
            calendar_principal_key, transition_kind, failure_code, payload_id,
            payload_sha256, decision, transaction_id, graph_event_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            str(uuid.uuid4()),
            run_id,
            sequence_no,
            previous_state,
            next_state,
            state_version,
            automation_id,
            automation_version,
            extraction_schema_version,
            calendar_principal_key,
            transition_kind,
            failure_code,
            payload_id,
            payload_sha256,
            decision,
            transaction_id,
            graph_event_id,
            created_at,
        ),
    )


def _admit_scheduling_automation(
    db: sqlite3.Connection,
    *,
    provider: str,
    account_id: str,
    calendar_principal_key: str,
    provider_message_id: str,
    mailbox_identity_key: str,
    created_at: str,
    expires_at: str,
) -> None:
    source_message_key = _message_suppression_key(
        provider,
        account_id,
        provider_message_id,
        mailbox_identity_key,
    )
    run_id = str(uuid.uuid4())
    inserted = db.execute(
        """INSERT INTO automation_runs(
            run_id, provider, account_id, calendar_principal_key, source_message_key,
            automation_id, automation_version, extraction_schema_version,
            state, state_version, failure_code, expires_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'detected', 1, NULL, ?, ?, ?)
        ON CONFLICT(
            provider, account_id, source_message_key, automation_id, automation_version
        ) DO NOTHING""",
        (
            run_id,
            provider,
            account_id,
            calendar_principal_key,
            source_message_key,
            SCHEDULING_AUTOMATION_ID,
            SCHEDULING_AUTOMATION_VERSION,
            SCHEDULING_EXTRACTION_SCHEMA_VERSION,
            expires_at,
            created_at,
            created_at,
        ),
    )
    if inserted.rowcount != 1:
        return
    db.execute(
        """INSERT INTO automation_run_source_identities(run_id, mailbox_identity_key)
        VALUES (?, ?)""",
        (run_id, mailbox_identity_key),
    )
    _append_automation_event(
        db,
        run_id=run_id,
        sequence_no=0,
        previous_state=None,
        next_state="detected",
        state_version=1,
        automation_id=SCHEDULING_AUTOMATION_ID,
        automation_version=SCHEDULING_AUTOMATION_VERSION,
        extraction_schema_version=SCHEDULING_EXTRACTION_SCHEMA_VERSION,
        calendar_principal_key=calendar_principal_key,
        transition_kind="detected",
        failure_code=None,
        created_at=created_at,
    )


def _mark_automation_sources_unavailable(
    db: sqlite3.Connection,
    message_ids: Sequence[str],
    *,
    updated_at: str,
) -> int:
    if not message_ids:
        return 0
    transitioned = 0
    for offset in range(0, len(message_ids), AUTOMATION_CLEANUP_CHUNK_SIZE):
        chunk = message_ids[offset : offset + AUTOMATION_CLEANUP_CHUNK_SIZE]
        placeholders = ", ".join("?" for _ in chunk)
        runs = db.execute(
            f"""SELECT r.run_id, r.automation_id, r.automation_version,
                r.extraction_schema_version, r.calendar_principal_key, r.state,
                r.state_version, r.current_payload_id, r.current_payload_sha256,
                w.transaction_id
            FROM automation_runs AS r
            LEFT JOIN automation_calendar_writes AS w ON w.run_id = r.run_id
            LEFT JOIN automation_run_source_identities AS i ON i.run_id = r.run_id
            JOIN messages AS m
              ON m.provider = r.provider
             AND m.account_id = r.account_id
             AND (
                 (i.mailbox_identity_key IS NOT NULL
                  AND m.mailbox_identity_key = i.mailbox_identity_key
                  AND message_source_key(
                        m.provider, m.account_id, m.provider_message_id,
                        m.mailbox_identity_key
                      ) = r.source_message_key)
                 OR (i.mailbox_identity_key IS NULL
                     AND m.mailbox_identity_key IS NULL
                     AND message_source_key(
                            m.provider, m.account_id, m.provider_message_id
                         ) = r.source_message_key)
             )
            WHERE m.message_id IN ({placeholders})
            ORDER BY r.run_id""",
            tuple(chunk),
        ).fetchall()
        for row in runs:
            run_id = str(row["run_id"])
            db.execute(
                "DELETE FROM automation_extraction_payloads WHERE run_id = ?",
                (run_id,),
            )
            db.execute(
                "DELETE FROM automation_proposal_payloads WHERE run_id = ?",
                (run_id,),
            )
            previous_state = str(row["state"])
            if previous_state not in {
                "detected",
                "extracting",
                "proposing",
                "awaiting_confirmation",
                "write_authorized",
            }:
                continue
            previous_version = int(row["state_version"])
            next_version = previous_version + 1
            if previous_state == "write_authorized":
                cancelled = db.execute(
                    """UPDATE automation_calendar_writes SET status = 'cancelled',
                        failure_code = 'source_unavailable', updated_at = ?
                    WHERE run_id = ? AND status = 'authorized'""",
                    (updated_at, run_id),
                )
                if cancelled.rowcount != 1:
                    raise RuntimeError("Automation source cleanup lost its write-state race")
            changed = db.execute(
                """UPDATE automation_runs SET
                    state = 'source_unavailable', state_version = ?,
                    failure_code = 'source_unavailable', review_notified_at = NULL,
                    updated_at = ?
                WHERE run_id = ? AND state = ? AND state_version = ?""",
                (next_version, updated_at, run_id, previous_state, previous_version),
            )
            if changed.rowcount != 1:
                raise RuntimeError("Automation source cleanup lost its expected-state race")
            transitioned += 1
            _append_automation_event(
                db,
                run_id=run_id,
                sequence_no=next_version - 1,
                previous_state=previous_state,
                next_state="source_unavailable",
                state_version=next_version,
                automation_id=str(row["automation_id"]),
                automation_version=int(row["automation_version"]),
                extraction_schema_version=int(row["extraction_schema_version"]),
                calendar_principal_key=str(row["calendar_principal_key"]),
                transition_kind="source_unavailable",
                failure_code="source_unavailable",
                created_at=updated_at,
                payload_id=(
                    str(row["current_payload_id"])
                    if row["current_payload_id"] is not None
                    else None
                ),
                payload_sha256=(
                    str(row["current_payload_sha256"])
                    if row["current_payload_sha256"] is not None
                    else None
                ),
                transaction_id=(
                    str(row["transaction_id"]) if row["transaction_id"] is not None else None
                ),
            )
    return transitioned


def _purge_expired_automation_tombstones(
    db: sqlite3.Connection,
    *,
    now: str,
) -> None:
    rows = db.execute(
        """SELECT run_id FROM automation_runs
        WHERE state NOT IN ('writing', 'reconciling')
          AND (
              aware_iso_epoch(expires_at) IS NULL
              OR aware_iso_epoch(expires_at) <= aware_iso_epoch(?)
          )
        ORDER BY run_id""",
        (now,),
    ).fetchall()
    run_ids = [str(row["run_id"]) for row in rows]
    for offset in range(0, len(run_ids), AUTOMATION_CLEANUP_CHUNK_SIZE):
        chunk = run_ids[offset : offset + AUTOMATION_CLEANUP_CHUNK_SIZE]
        placeholders = ", ".join("?" for _ in chunk)
        db.execute(
            f"DELETE FROM automation_runs WHERE run_id IN ({placeholders})",
            tuple(chunk),
        )
        db.execute(
            f"DELETE FROM automation_events WHERE run_id IN ({placeholders})",
            tuple(chunk),
        )


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
    mailbox_identity_key: str | None = None


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
    subject_type: str
    subject_id: str
    revision: str
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
    received_at: str
    discovered_at: str
    mailbox_identity_key: str | None = None


@dataclass(frozen=True)
class MailAccount:
    provider: str
    account_id: str
    display_name: str
    address: str | None
    active: bool
    created_at: str
    updated_at: str
    mailbox_identity_key: str | None = None
    legacy_identity_status: str = "unresolved"
    legacy_identity_key: str | None = None


@dataclass(frozen=True)
class GmailLabelSelectorSet:
    provider: str
    account_id: str
    current_mailbox_identity_key: str = field(repr=False)
    revision: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class GmailLabelSelector:
    selector_id: str
    provider: str
    account_id: str
    mailbox_identity_key: str = field(repr=False)
    label_id: str
    selected_display_name: str
    created_at: str


@dataclass(frozen=True)
class GmailLabelSelectorValidation:
    selector_id: str
    label_id: str
    status: str
    display_name: str


@dataclass(frozen=True)
class GmailLabelValidationSnapshot:
    provider: str
    account_id: str
    mailbox_identity_key: str = field(repr=False)
    selector_revision: int
    validated_at: str
    selectors: tuple[GmailLabelSelectorValidation, ...]


@dataclass(frozen=True)
class GmailLabelSelectorSnapshot:
    selector_id: str
    label_id: str
    display_name: str


@dataclass(frozen=True)
class AdmissionProvenance:
    kind: str
    selector_id: str
    display_name: str | None
    mailbox_identity_key: str = field(repr=False)
    admitted_at: str


@dataclass(frozen=True)
class GmailRecoveryMessage:
    message_id: str
    thread_id: str | None
    sender: str
    sender_name: str | None
    subject: str
    received_at: str


@dataclass(frozen=True)
class GmailRecoveryState:
    provider: str
    account_id: str
    mailbox_identity_key: str = field(repr=False)
    selector_revision: int
    sender_snapshot: tuple[tuple[str, str | None], ...]
    selector_snapshot: tuple[GmailLabelSelectorSnapshot, ...]
    recovery_after_exclusive_epoch: int
    recovery_before_exclusive_epoch: int
    retention_cutoff: str
    replacement_history_cursor: str
    page_token: str | None
    current_page_ids: tuple[str, ...]
    page_loaded: bool
    next_index: int
    page_count: int
    terminal_candidate_count: int
    invalid_page_token_count: int
    consecutive_retry_count: int
    state: str
    failure_code: str | None
    next_retry_at: str | None
    created_at: str
    updated_at: str


def _mail_account(row: sqlite3.Row) -> MailAccount:
    return MailAccount(
        provider=str(row["provider"]),
        account_id=str(row["account_id"]),
        display_name=str(row["display_name"]),
        address=str(row["address"]) if row["address"] is not None else None,
        active=bool(row["active"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        mailbox_identity_key=(
            str(row["mailbox_identity_key"]) if row["mailbox_identity_key"] is not None else None
        ),
        legacy_identity_status=str(row["legacy_identity_status"]),
        legacy_identity_key=(
            str(row["legacy_identity_key"]) if row["legacy_identity_key"] is not None else None
        ),
    )


def _gmail_label_selector_set(row: sqlite3.Row) -> GmailLabelSelectorSet:
    return GmailLabelSelectorSet(
        provider=str(row["provider"]),
        account_id=str(row["account_id"]),
        current_mailbox_identity_key=str(row["current_mailbox_identity_key"]),
        revision=int(row["revision"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _gmail_label_selector(row: sqlite3.Row) -> GmailLabelSelector:
    return GmailLabelSelector(
        selector_id=str(row["selector_id"]),
        provider=str(row["provider"]),
        account_id=str(row["account_id"]),
        mailbox_identity_key=str(row["mailbox_identity_key"]),
        label_id=str(row["label_id"]),
        selected_display_name=str(row["selected_display_name"]),
        created_at=str(row["created_at"]),
    )


def _gmail_recovery_state(row: sqlite3.Row) -> GmailRecoveryState:
    page_ids = decode_gmail_recovery_page(row["current_page_ids_json"])
    page_loaded = bool(row["page_loaded"])
    next_index = int(row["next_index"])
    if (not page_loaded and (page_ids or next_index != 0)) or next_index > len(page_ids):
        raise RuntimeError("gmail recovery page state is invalid")
    page_token = str(row["page_token"]) if row["page_token"] is not None else None
    if page_token is not None and _utf8_size(page_token) > MAX_GMAIL_RECOVERY_PAGE_TOKEN_BYTES:
        raise RuntimeError("gmail recovery page token is invalid")
    return GmailRecoveryState(
        provider=str(row["provider"]),
        account_id=str(row["account_id"]),
        mailbox_identity_key=str(row["mailbox_identity_key"]),
        selector_revision=int(row["selector_revision"]),
        sender_snapshot=decode_gmail_sender_snapshot(row["sender_snapshot_json"]),
        selector_snapshot=decode_gmail_selector_snapshot(row["selector_snapshot_json"]),
        recovery_after_exclusive_epoch=int(row["recovery_after_exclusive_epoch"]),
        recovery_before_exclusive_epoch=int(row["recovery_before_exclusive_epoch"]),
        retention_cutoff=_validate_utc_timestamp(
            row["retention_cutoff"], field="gmail recovery retention cutoff"
        ),
        replacement_history_cursor=str(row["replacement_history_cursor"]),
        page_token=page_token,
        current_page_ids=page_ids,
        page_loaded=page_loaded,
        next_index=next_index,
        page_count=int(row["page_count"]),
        terminal_candidate_count=int(row["terminal_candidate_count"]),
        invalid_page_token_count=int(row["invalid_page_token_count"]),
        consecutive_retry_count=int(row["consecutive_retry_count"]),
        state=str(row["state"]),
        failure_code=(str(row["failure_code"]) if row["failure_code"] is not None else None),
        next_retry_at=(str(row["next_retry_at"]) if row["next_retry_at"] is not None else None),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


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
class ConnectDispatch:
    job_id: str
    state: str
    attempt_count: int
    reconciliation_failure_count: int
    next_attempt_at: str | None
    admission_deadline: str
    submission_possible: bool
    source_available: bool
    capability_external_effects: bool
    capability_confirmation_required: bool
    capability_authority_known: bool
    capability_produces: tuple[str, ...]
    interactive_authorized_at: str | None
    automation_paused_at: str | None
    highest_provider_state: str
    last_error_code: str | None
    last_error_message: str | None
    created_at: str
    updated_at: str


class ConnectQueueFull(RuntimeError):
    pass


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
        for trigger in _CONNECT_TABLE_TRIGGER_NAMES:
            db.execute(f"DROP TRIGGER IF EXISTS {trigger}")
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


def _connect_admission_deadline(created_at: str) -> str:
    created = datetime.fromisoformat(created_at)
    if created.tzinfo is None:
        raise ValueError("Connect job creation time must include a timezone")
    return (created.astimezone(UTC) + CONNECT_QUEUE_ADMISSION_WINDOW).isoformat()


def _ensure_connect_dispatch_schema(db: sqlite3.Connection) -> None:
    db.execute(_CONNECT_DISPATCH_TABLE_SQL)
    dispatch_columns = {
        str(row["name"])
        for row in db.execute("PRAGMA table_info(connect_job_dispatch)").fetchall()
    }
    missing_capability_produces = "capability_produces_json" not in dispatch_columns
    for column, definition in {
        "capability_external_effects": (
            "INTEGER NOT NULL DEFAULT 0 CHECK (capability_external_effects IN (0, 1))"
        ),
        "capability_confirmation_required": (
            "INTEGER NOT NULL DEFAULT 0 CHECK (capability_confirmation_required IN (0, 1))"
        ),
        "capability_authority_known": (
            "INTEGER NOT NULL DEFAULT 0 CHECK (capability_authority_known IN (0, 1))"
        ),
        "capability_produces_json": (
            "TEXT NOT NULL DEFAULT '[]' "
            "CHECK (length(capability_produces_json) BETWEEN 2 AND 4096)"
        ),
        "interactive_authorized_at": "TEXT",
        "automation_paused_at": "TEXT",
    }.items():
        if column not in dispatch_columns:
            db.execute(f"ALTER TABLE connect_job_dispatch ADD COLUMN {column} {definition}")
    if missing_capability_produces:
        db.execute("UPDATE connect_job_dispatch SET capability_authority_known = 0")
    rows = db.execute(
        """SELECT job_id, status, created_at, updated_at
        FROM connect_attachment_jobs
        WHERE protocol_version = 2
        ORDER BY created_at, job_id"""
    ).fetchall()
    for row in rows:
        status = str(row["status"])
        state = {
            "requested": "reconciling",
            "accepted": "provider_owned",
            "processing": "provider_owned",
            "completed": "terminal",
            "failed": "terminal",
        }[status]
        highest = status if status in {"accepted", "processing"} else "requested"
        db.execute(
            """INSERT OR IGNORE INTO connect_job_dispatch(
                job_id, state, admission_deadline, submission_possible,
                source_available, highest_provider_state, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 1, ?, ?, ?)""",
            (
                str(row["job_id"]),
                state,
                _connect_admission_deadline(str(row["created_at"])),
                int(status in {"requested", "accepted", "processing"}),
                highest,
                str(row["created_at"]),
                str(row["updated_at"]),
            ),
        )
    legacy_duplicate = db.execute(
        """SELECT 1
        FROM connect_attachment_jobs
        WHERE protocol_version = 2
          AND status IN ('requested', 'accepted', 'processing')
        GROUP BY message_id, part_id, invocation_fingerprint
        HAVING COUNT(*) > 1
        LIMIT 1"""
    ).fetchone()
    if legacy_duplicate is None:
        db.execute(_CONNECT_JOBS_ACTIVE_V2_INDEX_SQL)
    db.execute(_CONNECT_DISPATCH_LANE_INDEX_SQL)
    db.execute(_CONNECT_DISPATCH_DELETE_TRIGGER_SQL)
    db.execute("DROP TRIGGER IF EXISTS messages_delete_connect_attachment_jobs")
    db.execute(_CONNECT_JOBS_DELETE_TRIGGER_V19_SQL)


def _execute_transactional_script(db: sqlite3.Connection, script: str) -> None:
    """Execute complete SQL statements without executescript's implicit commit."""
    pending: list[str] = []
    for line in script.splitlines():
        pending.append(line)
        statement = "\n".join(pending).strip()
        if statement and sqlite3.complete_statement(statement):
            db.execute(statement)
            pending.clear()
    if "\n".join(pending).strip():
        raise RuntimeError("automation schema script ended with an incomplete statement")


def _migrate_automation_fires_v21(db: sqlite3.Connection) -> None:
    for trigger in (
        "messages_delete_connect_attachment_jobs",
        "connect_jobs_delete_linked_automation_fires",
        "messages_delete_pending_automation_fires",
        "automation_fire_attempts_immutable_update",
        "automation_fire_attempts_require_fire",
        "automation_fires_require_sources",
    ):
        db.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    db.execute("DROP INDEX IF EXISTS idx_automation_fires_state")
    db.execute("DROP INDEX IF EXISTS idx_automation_fires_message")
    db.execute("ALTER TABLE automation_fire_attempts RENAME TO automation_fire_attempts_v20")
    db.execute("ALTER TABLE automation_fires RENAME TO automation_fires_v20")
    _execute_transactional_script(db, _AUTOMATION_FIRE_TABLES_SQL)
    migrated_at = datetime.now(UTC).isoformat()
    db.execute(
        """INSERT INTO automation_fires(
            fire_id, event_id, rule_id, rule_version, message_id, part_id,
            action_kind, state, state_version, reason, current_attempt_no, job_id,
            prepared_identity_sha256, prepared_identity_json, confirmed,
            pending_since, authorized_pending_seconds, created_at, updated_at
        ) SELECT fire_id, event_id, rule_id, rule_version, message_id, part_id,
            action_kind, state, state_version, NULL, 1, NULL, NULL, NULL, 0,
            ?, 0, created_at, updated_at
        FROM automation_fires_v20""",
        (migrated_at,),
    )
    db.execute(
        """INSERT INTO automation_fire_attempts(
            fire_id, attempt_no, dispatch_request_id, job_id, created_at
        ) SELECT fire_id, attempt_no, dispatch_request_id, NULL, created_at
        FROM automation_fire_attempts_v20"""
    )
    db.execute("DROP TABLE automation_fire_attempts_v20")
    db.execute("DROP TABLE automation_fires_v20")


def _migrate_automation_fires_v22(db: sqlite3.Connection) -> None:
    for trigger in (
        "messages_delete_connect_attachment_jobs",
        "connect_jobs_delete_linked_automation_fires",
        "messages_delete_pending_automation_fires",
        "automation_fire_attempts_immutable_update",
        "automation_fire_attempts_require_fire",
        "automation_fires_require_sources",
    ):
        db.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    db.execute("DROP INDEX IF EXISTS idx_automation_fires_state")
    db.execute("DROP INDEX IF EXISTS idx_automation_fires_message")
    db.execute("ALTER TABLE automation_fire_attempts RENAME TO automation_fire_attempts_v21")
    db.execute("ALTER TABLE automation_fires RENAME TO automation_fires_v21")
    _execute_transactional_script(db, _AUTOMATION_FIRE_TABLES_SQL)
    db.execute(
        """INSERT INTO automation_fires(
            fire_id, event_id, rule_id, rule_version, message_id, part_id,
            action_kind, state, state_version, reason, current_attempt_no, job_id,
            prepared_identity_sha256, prepared_identity_json, confirmed,
            pending_since, authorized_pending_seconds, created_at, updated_at
        ) SELECT fire_id, event_id, rule_id, rule_version, message_id, part_id,
            action_kind, state, state_version, reason, current_attempt_no, job_id,
            prepared_identity_sha256, prepared_identity_json, confirmed,
            pending_since, authorized_pending_seconds, created_at, updated_at
        FROM automation_fires_v21"""
    )
    db.execute(
        """INSERT INTO automation_fire_attempts(
            fire_id, attempt_no, dispatch_request_id, job_id, created_at
        ) SELECT fire_id, attempt_no, dispatch_request_id, job_id, created_at
        FROM automation_fire_attempts_v21"""
    )
    db.execute("DROP TABLE automation_fire_attempts_v21")
    db.execute("DROP TABLE automation_fires_v21")


def _ensure_automate_core_schema(db: sqlite3.Connection, current_version: int) -> None:
    """Install automation authority and cross-version completion fences."""
    _execute_transactional_script(db, _AUTOMATE_CORE_TABLES_SQL)
    _execute_transactional_script(db, _AUTOMATION_FIRE_TABLES_SQL)
    fire_columns = {
        str(row["name"]) for row in db.execute("PRAGMA table_info(automation_fires)").fetchall()
    }
    if "job_id" not in fire_columns:
        _migrate_automation_fires_v21(db)
    elif current_version == 21:
        _migrate_automation_fires_v22(db)
    db.execute("DROP TRIGGER IF EXISTS messages_delete_pending_automation_fires")
    db.execute("DROP TRIGGER IF EXISTS connect_jobs_delete_linked_automation_fires")
    _execute_transactional_script(db, _AUTOMATION_FIRE_TABLES_SQL)
    db.execute("DROP TRIGGER IF EXISTS messages_delete_connect_attachment_jobs")
    db.execute(_CONNECT_JOBS_DELETE_TRIGGER_V19_SQL)

    account_columns = {
        str(row["name"]) for row in db.execute("PRAGMA table_info(mail_accounts)").fetchall()
    }
    for column, definition in {
        "mailbox_identity_key": (
            "TEXT CHECK (mailbox_identity_key IS NULL OR length(mailbox_identity_key) = 64)"
        ),
        "legacy_identity_status": (
            "TEXT NOT NULL DEFAULT 'unresolved' CHECK "
            "(legacy_identity_status IN ('continuity_proven', 'replacement', 'unresolved'))"
        ),
        "legacy_identity_key": (
            "TEXT CHECK (legacy_identity_key IS NULL OR length(legacy_identity_key) = 64)"
        ),
    }.items():
        if column not in account_columns:
            db.execute(f"ALTER TABLE mail_accounts ADD COLUMN {column} {definition}")

    message_columns = {
        str(row["name"]) for row in db.execute("PRAGMA table_info(messages)").fetchall()
    }
    for column, definition in {
        "mailbox_identity_key": (
            "TEXT CHECK (mailbox_identity_key IS NULL OR length(mailbox_identity_key) = 64)"
        ),
        "rules_revision_at_analysis": (
            "INTEGER CHECK (rules_revision_at_analysis IS NULL OR rules_revision_at_analysis >= 0)"
        ),
        "rules_evaluation_error": (
            "TEXT CHECK (rules_evaluation_error IS NULL OR rules_evaluation_error = "
            "'automation_fanout_limit')"
        ),
    }.items():
        if column not in message_columns:
            db.execute(f"ALTER TABLE messages ADD COLUMN {column} {definition}")

    attachment_columns = {
        str(row["name"]) for row in db.execute("PRAGMA table_info(message_attachments)").fetchall()
    }
    if "byte_size_known" not in attachment_columns:
        db.execute(
            "ALTER TABLE message_attachments ADD COLUMN byte_size_known "
            "INTEGER NOT NULL DEFAULT 1 CHECK (byte_size_known IN (0, 1))"
        )

    if current_version < 20:
        analyzed_predicate = (
            "status <> 'pending' OR analysis_at IS NOT NULL"
            if "analysis_at" in message_columns
            else "status <> 'pending'"
        )
        db.execute(f"UPDATE messages SET rules_revision_at_analysis = 0 WHERE {analyzed_predicate}")
        db.execute(
            """INSERT OR IGNORE INTO legacy_mailbox_markers(
                provider, account_id, message_key, expires_at
            ) SELECT provider, account_id, message_key, expires_at
            FROM suppressed_messages"""
        )

    db.execute("DROP INDEX IF EXISTS idx_messages_source_identity")
    db.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_source_identity_v20
        ON messages(provider, account_id, mailbox_identity_key, provider_message_id)"""
    )
    _execute_transactional_script(
        db,
        """
        CREATE TRIGGER IF NOT EXISTS messages_require_mailbox_identity_insert
        BEFORE INSERT ON messages
        WHEN NEW.mailbox_identity_key IS NULL
        BEGIN
            SELECT RAISE(ABORT, 'message mailbox identity is required');
        END;
        CREATE TRIGGER IF NOT EXISTS messages_require_rule_revision_completion
        BEFORE UPDATE OF status ON messages
        WHEN OLD.status = 'pending' AND NEW.status = 'analyzed'
          AND NEW.rules_revision_at_analysis IS NULL
        BEGIN
            SELECT RAISE(ABORT, 'analysis rule revision is required');
        END;
        """,
    )


def _ensure_gmail_label_schema(db: sqlite3.Connection) -> None:
    recovery_table = db.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='gmail_recovery_state'"
    ).fetchone()
    legacy_recovery_table = False
    if recovery_table is not None:
        recovery_columns = {
            str(row["name"])
            for row in db.execute("PRAGMA table_info(gmail_recovery_state)").fetchall()
        }
        legacy_recovery_table = "retention_cutoff" not in recovery_columns
        if legacy_recovery_table:
            db.execute("DROP TRIGGER IF EXISTS gmail_recovery_state_immutable")
            db.execute(
                "ALTER TABLE gmail_recovery_state RENAME TO gmail_recovery_state_v24"
            )
    _execute_transactional_script(
        db,
        """
        CREATE TABLE IF NOT EXISTS gmail_label_selector_sets (
          provider TEXT NOT NULL CHECK (provider = 'gmail'),
          account_id TEXT NOT NULL CHECK (
            account_id <> '' AND length(CAST(account_id AS BLOB)) <= 128
          ),
          current_mailbox_identity_key TEXT NOT NULL CHECK (
            length(current_mailbox_identity_key) = 64
            AND current_mailbox_identity_key = lower(current_mailbox_identity_key)
            AND current_mailbox_identity_key NOT GLOB '*[^0-9a-f]*'
          ),
          revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          PRIMARY KEY (provider, account_id)
        );
        CREATE TABLE IF NOT EXISTS gmail_label_selectors (
          selector_id TEXT PRIMARY KEY CHECK (length(selector_id) = 36),
          provider TEXT NOT NULL CHECK (provider = 'gmail'),
          account_id TEXT NOT NULL CHECK (
            account_id <> '' AND length(CAST(account_id AS BLOB)) <= 128
          ),
          mailbox_identity_key TEXT NOT NULL CHECK (
            length(mailbox_identity_key) = 64
            AND mailbox_identity_key = lower(mailbox_identity_key)
            AND mailbox_identity_key NOT GLOB '*[^0-9a-f]*'
          ),
          label_id TEXT NOT NULL CHECK (
            label_id <> '' AND length(CAST(label_id AS BLOB)) <= 512
          ),
          selected_display_name TEXT NOT NULL CHECK (
            selected_display_name <> ''
            AND length(CAST(selected_display_name AS BLOB)) <= 1024
          ),
          created_at TEXT NOT NULL,
          UNIQUE (provider, account_id, mailbox_identity_key, label_id)
        );
        CREATE INDEX IF NOT EXISTS idx_gmail_label_selectors_account
          ON gmail_label_selectors(provider, account_id, selector_id);
        CREATE TRIGGER IF NOT EXISTS gmail_label_selectors_immutable
        BEFORE UPDATE ON gmail_label_selectors
        BEGIN
          SELECT RAISE(ABORT, 'gmail label selectors are immutable');
        END;
        CREATE TABLE IF NOT EXISTS gmail_label_validation_sets (
          provider TEXT NOT NULL CHECK (provider = 'gmail'),
          account_id TEXT NOT NULL CHECK (
            account_id <> '' AND length(CAST(account_id AS BLOB)) <= 128
          ),
          mailbox_identity_key TEXT NOT NULL CHECK (
            length(mailbox_identity_key) = 64
            AND mailbox_identity_key = lower(mailbox_identity_key)
            AND mailbox_identity_key NOT GLOB '*[^0-9a-f]*'
          ),
          selector_revision INTEGER NOT NULL CHECK (selector_revision >= 0),
          validated_at TEXT NOT NULL CHECK (validated_at <> ''),
          PRIMARY KEY (provider, account_id)
        );
        CREATE TABLE IF NOT EXISTS gmail_label_selector_validations (
          selector_id TEXT PRIMARY KEY CHECK (length(selector_id) = 36),
          provider TEXT NOT NULL CHECK (provider = 'gmail'),
          account_id TEXT NOT NULL CHECK (
            account_id <> '' AND length(CAST(account_id AS BLOB)) <= 128
          ),
          mailbox_identity_key TEXT NOT NULL CHECK (
            length(mailbox_identity_key) = 64
            AND mailbox_identity_key = lower(mailbox_identity_key)
            AND mailbox_identity_key NOT GLOB '*[^0-9a-f]*'
          ),
          selector_revision INTEGER NOT NULL CHECK (selector_revision >= 0),
          label_id TEXT NOT NULL CHECK (
            label_id <> '' AND length(CAST(label_id AS BLOB)) <= 512
          ),
          status TEXT NOT NULL CHECK (status IN ('active', 'deleted', 'not_user')),
          display_name TEXT NOT NULL CHECK (
            display_name <> '' AND length(CAST(display_name AS BLOB)) <= 1024
          ),
          UNIQUE (provider, account_id, mailbox_identity_key, selector_revision, label_id)
        );
        CREATE INDEX IF NOT EXISTS idx_gmail_label_selector_validations_account
          ON gmail_label_selector_validations(provider, account_id, selector_id);
        CREATE TRIGGER IF NOT EXISTS gmail_label_validation_sets_require_current_scope
        BEFORE INSERT ON gmail_label_validation_sets
        WHEN NOT EXISTS (
          SELECT 1
          FROM gmail_label_selector_sets AS s
          JOIN mail_accounts AS a
            ON a.provider = s.provider AND a.account_id = s.account_id
          WHERE s.provider = NEW.provider AND s.account_id = NEW.account_id
            AND s.current_mailbox_identity_key = NEW.mailbox_identity_key
            AND s.revision = NEW.selector_revision
            AND a.mailbox_identity_key = NEW.mailbox_identity_key
        )
        BEGIN
          SELECT RAISE(ABORT, 'gmail label validation scope is stale');
        END;
        CREATE TRIGGER IF NOT EXISTS gmail_label_validations_require_snapshot_selector
        BEFORE INSERT ON gmail_label_selector_validations
        WHEN NOT EXISTS (
          SELECT 1
          FROM gmail_label_validation_sets AS v
          JOIN gmail_label_selectors AS l
            ON l.provider = v.provider AND l.account_id = v.account_id
          WHERE v.provider = NEW.provider AND v.account_id = NEW.account_id
            AND v.mailbox_identity_key = NEW.mailbox_identity_key
            AND v.selector_revision = NEW.selector_revision
            AND l.selector_id = NEW.selector_id
            AND l.mailbox_identity_key = NEW.mailbox_identity_key
            AND l.label_id = NEW.label_id
        )
        BEGIN
          SELECT RAISE(ABORT, 'gmail label validation selector is stale');
        END;
        CREATE TRIGGER IF NOT EXISTS gmail_label_validation_sets_immutable
        BEFORE UPDATE ON gmail_label_validation_sets
        BEGIN
          SELECT RAISE(ABORT, 'gmail label validation sets are immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS gmail_label_selector_validations_immutable
        BEFORE UPDATE ON gmail_label_selector_validations
        BEGIN
          SELECT RAISE(ABORT, 'gmail label selector validations are immutable');
        END;
        CREATE TABLE IF NOT EXISTS gmail_recovery_state (
          provider TEXT NOT NULL CHECK (provider = 'gmail'),
          account_id TEXT NOT NULL CHECK (
            account_id <> '' AND length(CAST(account_id AS BLOB)) <= 128
          ),
          mailbox_identity_key TEXT NOT NULL CHECK (
            length(mailbox_identity_key) = 64
            AND mailbox_identity_key = lower(mailbox_identity_key)
            AND mailbox_identity_key NOT GLOB '*[^0-9a-f]*'
          ),
          selector_revision INTEGER NOT NULL CHECK (selector_revision >= 0),
          sender_snapshot_json BLOB NOT NULL CHECK (
            typeof(sender_snapshot_json) = 'blob'
            AND length(sender_snapshot_json) <= 1048576
          ),
          selector_snapshot_json BLOB NOT NULL CHECK (
            typeof(selector_snapshot_json) = 'blob'
            AND length(selector_snapshot_json) <= 1048576
          ),
          recovery_after_exclusive_epoch INTEGER NOT NULL CHECK (
            recovery_after_exclusive_epoch >= 0
          ),
          recovery_before_exclusive_epoch INTEGER NOT NULL CHECK (
            recovery_before_exclusive_epoch > recovery_after_exclusive_epoch
          ),
          retention_cutoff TEXT NOT NULL CHECK (
            aware_iso_epoch(retention_cutoff) IS NOT NULL
            AND aware_iso_epoch(retention_cutoff) >= 0
          ),
          replacement_history_cursor TEXT NOT NULL CHECK (
            replacement_history_cursor <> ''
            AND length(CAST(replacement_history_cursor AS BLOB)) <= 4096
          ),
          page_token TEXT CHECK (
            page_token IS NULL OR length(CAST(page_token AS BLOB)) <= 8192
          ),
          current_page_ids_json BLOB NOT NULL CHECK (
            typeof(current_page_ids_json) = 'blob'
            AND length(current_page_ids_json) <= 524288
          ),
          page_loaded INTEGER NOT NULL DEFAULT 0 CHECK (page_loaded IN (0, 1)),
          next_index INTEGER NOT NULL DEFAULT 0 CHECK (next_index BETWEEN 0 AND 200),
          page_count INTEGER NOT NULL DEFAULT 0 CHECK (page_count >= 0),
          terminal_candidate_count INTEGER NOT NULL DEFAULT 0 CHECK (
            terminal_candidate_count >= 0
          ),
          invalid_page_token_count INTEGER NOT NULL DEFAULT 0 CHECK (
            invalid_page_token_count >= 0
          ),
          consecutive_retry_count INTEGER NOT NULL DEFAULT 0 CHECK (
            consecutive_retry_count BETWEEN 0 AND 31
          ),
          state TEXT NOT NULL CHECK (state IN ('collecting', 'backoff', 'degraded')),
          failure_code TEXT,
          next_retry_at TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          PRIMARY KEY (provider, account_id)
        );
        CREATE TRIGGER IF NOT EXISTS gmail_recovery_state_immutable
        BEFORE UPDATE ON gmail_recovery_state
        WHEN NEW.provider IS NOT OLD.provider
          OR NEW.account_id IS NOT OLD.account_id
          OR NEW.mailbox_identity_key IS NOT OLD.mailbox_identity_key
          OR NEW.selector_revision IS NOT OLD.selector_revision
          OR NEW.sender_snapshot_json IS NOT OLD.sender_snapshot_json
          OR NEW.selector_snapshot_json IS NOT OLD.selector_snapshot_json
          OR NEW.recovery_after_exclusive_epoch IS NOT OLD.recovery_after_exclusive_epoch
          OR NEW.recovery_before_exclusive_epoch IS NOT OLD.recovery_before_exclusive_epoch
          OR NEW.retention_cutoff IS NOT OLD.retention_cutoff
          OR NEW.replacement_history_cursor IS NOT OLD.replacement_history_cursor
          OR NEW.created_at IS NOT OLD.created_at
          OR NEW.page_count < OLD.page_count
          OR NEW.terminal_candidate_count < OLD.terminal_candidate_count
          OR NEW.invalid_page_token_count < OLD.invalid_page_token_count
        BEGIN
          SELECT RAISE(ABORT, 'gmail recovery state is immutable');
        END;
        """,
    )
    if legacy_recovery_table:
        # The old row did not record the exact retention boundary, so it cannot
        # be resumed without changing admission based on the current setting.
        # Dropping only that row is fail closed: the unchanged mailbox cursor
        # will re-enter stale recovery with a complete schema-25 capture.
        db.execute("DROP TABLE gmail_recovery_state_v24")
    message_columns = {
        str(row["name"]) for row in db.execute("PRAGMA table_info(messages)").fetchall()
    }
    columns = {
        "admission_kind": (
            "TEXT CHECK (admission_kind IS NULL OR "
            "admission_kind IN ('exact_sender', 'gmail_user_label'))"
        ),
        "admission_selector_id": (
            "TEXT CHECK (admission_selector_id IS NULL OR "
            "(admission_selector_id <> '' AND "
            "length(CAST(admission_selector_id AS BLOB)) <= 512))"
        ),
        "admission_display_name": (
            "TEXT CHECK (admission_display_name IS NULL OR "
            "length(CAST(admission_display_name AS BLOB)) <= 1024)"
        ),
        "admission_mailbox_identity_key": (
            "TEXT CHECK (admission_mailbox_identity_key IS NULL OR "
            "(length(admission_mailbox_identity_key) = 64 "
            "AND admission_mailbox_identity_key = lower(admission_mailbox_identity_key) "
            "AND admission_mailbox_identity_key NOT GLOB '*[^0-9a-f]*'))"
        ),
        "admitted_at": (
            "TEXT CHECK (admitted_at IS NULL OR "
            "(admitted_at <> '' AND length(CAST(admitted_at AS BLOB)) <= 64))"
        ),
    }
    for column, definition in columns.items():
        if column not in message_columns:
            db.execute(f"ALTER TABLE messages ADD COLUMN {column} {definition}")
    _execute_transactional_script(
        db,
        """
        CREATE TRIGGER IF NOT EXISTS messages_require_admission_provenance_insert
        BEFORE INSERT ON messages
        WHEN NEW.admission_kind IS NULL
          OR NEW.admission_selector_id IS NULL
          OR NEW.admission_mailbox_identity_key IS NULL
          OR NEW.admitted_at IS NULL
          OR NEW.admission_mailbox_identity_key <> NEW.mailbox_identity_key
        BEGIN
          SELECT RAISE(ABORT, 'message admission provenance is required');
        END;
        CREATE TRIGGER IF NOT EXISTS messages_admission_provenance_immutable
        BEFORE UPDATE OF admission_kind, admission_selector_id,
          admission_display_name, admission_mailbox_identity_key, admitted_at,
          mailbox_identity_key ON messages
        WHEN (OLD.admission_kind IS NOT NULL
          OR OLD.admission_selector_id IS NOT NULL
          OR OLD.admission_display_name IS NOT NULL
          OR OLD.admission_mailbox_identity_key IS NOT NULL
          OR OLD.admitted_at IS NOT NULL)
          AND (NEW.admission_kind IS NOT OLD.admission_kind
            OR NEW.admission_selector_id IS NOT OLD.admission_selector_id
            OR NEW.admission_display_name IS NOT OLD.admission_display_name
            OR NEW.admission_mailbox_identity_key IS NOT OLD.admission_mailbox_identity_key
            OR NEW.admitted_at IS NOT OLD.admitted_at
            OR NEW.mailbox_identity_key IS NOT OLD.mailbox_identity_key)
        BEGIN
          SELECT RAISE(ABORT, 'message admission provenance is immutable');
        END;
        """,
    )


def _valid_uuid_v4(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return parsed.version == 4 and str(parsed) == value


def _canonical_connect_capability_produces(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= 16:
        raise ValueError("Connect capability output authority is invalid")
    if any(
        not isinstance(media_type, str)
        or not 0 < len(media_type) <= 127
        or media_type != media_type.casefold()
        or media_type.count("/") != 1
        for media_type in value
    ):
        raise ValueError("Connect capability output authority is invalid")
    return tuple(sorted(set(value)))


def _encode_connect_capability_produces(value: object) -> str:
    return json.dumps(
        _canonical_connect_capability_produces(value),
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _decode_connect_capability_produces(value: object) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise RuntimeError("Stored Connect capability output authority is invalid")
    if value == "[]":
        return ()
    try:
        decoded = json.loads(value)
        canonical = _canonical_connect_capability_produces(decoded)
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("Stored Connect capability output authority is invalid") from exc
    if _encode_connect_capability_produces(canonical) != value:
        raise RuntimeError("Stored Connect capability output authority is invalid")
    return canonical


def encode_gmail_selector_snapshot(
    selectors: Sequence[object],
) -> bytes:
    if len(selectors) > MAX_GMAIL_LABEL_SELECTORS:
        raise ValueError("gmail selector snapshot exceeds its item limit")
    items: list[dict[str, str]] = []
    selector_ids: set[str] = set()
    for selector in selectors:
        selector_id = getattr(selector, "selector_id", None)
        label_id = getattr(selector, "label_id", None)
        display_name = getattr(
            selector,
            "selected_display_name",
            getattr(selector, "display_name", None),
        )
        if not _valid_uuid_v4(selector_id) or selector_id in selector_ids:
            raise ValueError("gmail selector snapshot has an invalid selector id")
        selector_ids.add(selector_id)
        items.append(
            {
                "selector_id": selector_id,
                "label_id": _require_bounded_text(
                    label_id,
                    maximum_bytes=MAX_GMAIL_LABEL_ID_BYTES,
                    field="gmail label id",
                ),
                "display_name": _require_bounded_text(
                    display_name,
                    maximum_bytes=MAX_GMAIL_LABEL_NAME_BYTES,
                    field="gmail label display name",
                ),
            }
        )
    items.sort(key=lambda item: item["selector_id"])
    encoded = _canonical_json_bytes(items)
    if len(encoded) > MAX_GMAIL_RECOVERY_SNAPSHOT_BYTES:
        raise GmailLabelStoreError("gmail_recovery_snapshot_too_large")
    return encoded


def decode_gmail_selector_snapshot(value: object) -> tuple[GmailLabelSelectorSnapshot, ...]:
    if not isinstance(value, bytes) or len(value) > MAX_GMAIL_RECOVERY_SNAPSHOT_BYTES:
        raise RuntimeError("gmail selector snapshot is invalid")
    decoded = _decode_json_bytes(value, field="gmail selector snapshot")
    if not isinstance(decoded, list) or len(decoded) > MAX_GMAIL_LABEL_SELECTORS:
        raise RuntimeError("gmail selector snapshot is invalid")
    selectors: list[GmailLabelSelectorSnapshot] = []
    selector_ids: set[str] = set()
    for item in decoded:
        if not isinstance(item, dict) or set(item) != {
            "selector_id",
            "label_id",
            "display_name",
        }:
            raise RuntimeError("gmail selector snapshot is invalid")
        selector_id = item["selector_id"]
        try:
            label_id = _require_bounded_text(
                item["label_id"],
                maximum_bytes=MAX_GMAIL_LABEL_ID_BYTES,
                field="gmail label id",
            )
            display_name = _require_bounded_text(
                item["display_name"],
                maximum_bytes=MAX_GMAIL_LABEL_NAME_BYTES,
                field="gmail label display name",
            )
        except ValueError as exc:
            raise RuntimeError("gmail selector snapshot is invalid") from exc
        if not _valid_uuid_v4(selector_id) or selector_id in selector_ids:
            raise RuntimeError("gmail selector snapshot is invalid")
        selector_ids.add(selector_id)
        selectors.append(
            GmailLabelSelectorSnapshot(
                selector_id=str(selector_id),
                label_id=label_id,
                display_name=display_name,
            )
        )
    if tuple(selector.selector_id for selector in selectors) != tuple(
        sorted(selector_ids)
    ):
        raise RuntimeError("gmail selector snapshot is not canonical")
    if _canonical_json_bytes(decoded) != value:
        raise RuntimeError("gmail selector snapshot is not canonical")
    return tuple(selectors)


def encode_gmail_sender_snapshot(
    senders: Sequence[tuple[str, str | None]],
) -> bytes:
    items: list[dict[str, str | None]] = []
    seen: set[str] = set()
    for raw_email, raw_name in senders:
        email = normalize_validated_address(raw_email)
        if email != raw_email or email in seen:
            raise ValueError("gmail sender snapshot is invalid")
        seen.add(email)
        if raw_name is not None and (
            not isinstance(raw_name, str)
            or not raw_name.strip()
            or raw_name != raw_name.strip()
            or any(not character.isprintable() for character in raw_name)
        ):
            raise ValueError("gmail sender snapshot is invalid")
        items.append({"email": email, "name": raw_name})
    items.sort(key=lambda item: (str(item["email"]), str(item["name"] or "")))
    encoded = _canonical_json_bytes(items)
    if len(encoded) > MAX_GMAIL_RECOVERY_SNAPSHOT_BYTES:
        raise GmailLabelStoreError("gmail_recovery_snapshot_too_large")
    return encoded


def decode_gmail_sender_snapshot(value: object) -> tuple[tuple[str, str | None], ...]:
    if not isinstance(value, bytes) or len(value) > MAX_GMAIL_RECOVERY_SNAPSHOT_BYTES:
        raise RuntimeError("gmail sender snapshot is invalid")
    decoded = _decode_json_bytes(value, field="gmail sender snapshot")
    if not isinstance(decoded, list):
        raise RuntimeError("gmail sender snapshot is invalid")
    senders: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for item in decoded:
        if not isinstance(item, dict) or set(item) != {"email", "name"}:
            raise RuntimeError("gmail sender snapshot is invalid")
        email = item["email"]
        name = item["name"]
        try:
            if not isinstance(email, str) or normalize_validated_address(email) != email:
                raise ValueError("invalid email")
        except ValueError as exc:
            raise RuntimeError("gmail sender snapshot is invalid") from exc
        if email in seen or (
            name is not None
            and (
                not isinstance(name, str)
                or not name.strip()
                or name != name.strip()
                or any(not character.isprintable() for character in name)
            )
        ):
            raise RuntimeError("gmail sender snapshot is invalid")
        seen.add(email)
        senders.append((email, name))
    if senders != sorted(senders, key=lambda item: (item[0], item[1] or "")):
        raise RuntimeError("gmail sender snapshot is not canonical")
    if _canonical_json_bytes(decoded) != value:
        raise RuntimeError("gmail sender snapshot is not canonical")
    return tuple(senders)


def encode_gmail_recovery_page(message_ids: Sequence[str]) -> bytes:
    if len(message_ids) > MAX_GMAIL_RECOVERY_PAGE_IDS:
        raise GmailLabelStoreError("gmail_recovery_page_invalid")
    seen: set[str] = set()
    validated: list[str] = []
    for message_id in message_ids:
        try:
            bounded = _require_bounded_text(
                message_id,
                maximum_bytes=MAX_GMAIL_LABEL_ID_BYTES,
                field="gmail provider message id",
            )
        except ValueError as exc:
            raise GmailLabelStoreError("gmail_recovery_page_invalid") from exc
        if bounded in seen:
            raise GmailLabelStoreError("gmail_recovery_page_invalid")
        seen.add(bounded)
        validated.append(bounded)
    encoded = json.dumps(validated, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_GMAIL_RECOVERY_PAGE_JSON_BYTES:
        raise GmailLabelStoreError("gmail_recovery_page_invalid")
    return encoded


def decode_gmail_recovery_page(value: object) -> tuple[str, ...]:
    if not isinstance(value, bytes) or len(value) > MAX_GMAIL_RECOVERY_PAGE_JSON_BYTES:
        raise RuntimeError("gmail recovery page is invalid")
    decoded = _decode_json_bytes(value, field="gmail recovery page")
    if not isinstance(decoded, list) or len(decoded) > MAX_GMAIL_RECOVERY_PAGE_IDS:
        raise RuntimeError("gmail recovery page is invalid")
    try:
        encoded = encode_gmail_recovery_page(decoded)
    except (GmailLabelStoreError, ValueError) as exc:
        raise RuntimeError("gmail recovery page is invalid") from exc
    if encoded != value:
        raise RuntimeError("gmail recovery page is not canonical")
    return tuple(decoded)


def _validate_admission_provenance(
    admission: AdmissionProvenance,
    *,
    mailbox_identity_key: str,
) -> None:
    if admission.kind not in {"exact_sender", "gmail_user_label"}:
        raise ValueError("admission kind is invalid")
    _require_bounded_text(
        admission.selector_id,
        maximum_bytes=MAX_GMAIL_LABEL_ID_BYTES,
        field="admission selector id",
    )
    if admission.display_name is not None and _utf8_size(admission.display_name) > 1024:
        raise ValueError("admission display name is invalid")
    if admission.display_name is not None and _has_control_character(admission.display_name):
        raise ValueError("admission display name is invalid")
    if admission.kind == "gmail_user_label" and not _valid_uuid_v4(admission.selector_id):
        raise ValueError("gmail label admission selector id is invalid")
    if admission.kind == "exact_sender":
        prefix = "sender:"
        if not admission.selector_id.startswith(prefix):
            raise ValueError("exact sender admission selector id is invalid")
        address = admission.selector_id[len(prefix) :]
        if normalize_validated_address(address) != address:
            raise ValueError("exact sender admission selector id is invalid")
    if _require_mailbox_identity_key(admission.mailbox_identity_key) != mailbox_identity_key:
        raise MailboxIdentityChanged("mailbox identity changed")
    _validate_utc_timestamp(admission.admitted_at, field="admitted_at")


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


def _resume_automation_dispatch(
    db: sqlite3.Connection,
    *,
    job_id: str,
    observed_at: datetime,
    stamp: str,
) -> None:
    dispatch = db.execute(
        """SELECT admission_deadline, automation_paused_at
        FROM connect_job_dispatch WHERE job_id = ?""",
        (job_id,),
    ).fetchone()
    if dispatch is None:
        raise RuntimeError("Automation entitlement resume lost its Connect job")
    paused_at_value = dispatch["automation_paused_at"]
    if paused_at_value is None:
        return
    admission_deadline = datetime.fromisoformat(str(dispatch["admission_deadline"]))
    paused_at = datetime.fromisoformat(str(paused_at_value))
    if admission_deadline.tzinfo is None:
        raise RuntimeError("Connect admission deadline is missing its timezone")
    if paused_at.tzinfo is None:
        raise RuntimeError("Automation entitlement pause is missing its timezone")
    paused_seconds = max(
        0,
        int((observed_at - paused_at.astimezone(UTC)).total_seconds()),
    )
    extended = db.execute(
        """UPDATE connect_job_dispatch
        SET admission_deadline = ?, automation_paused_at = NULL, updated_at = ?
        WHERE job_id = ? AND automation_paused_at = ?""",
        (
            (
                admission_deadline.astimezone(UTC) + timedelta(seconds=paused_seconds)
            ).isoformat(),
            stamp,
            job_id,
            paused_at_value,
        ),
    )
    if extended.rowcount != 1:
        raise RuntimeError("Automation entitlement resume lost its pause interval")


def _pause_linked_automation_fires(
    db: sqlite3.Connection,
    *,
    job_id: str,
    stamp: str,
) -> int:
    paused = db.execute(
        """UPDATE automation_fires SET
            state = 'entitlement_paused', state_version = state_version + 1,
            reason = 'entitlement_inactive', pending_since = NULL, updated_at = ?
        WHERE job_id = ? AND state = 'submitted'""",
        (stamp, job_id),
    )
    return paused.rowcount


def _connect_job_requires_automation_entitlement(
    db: sqlite3.Connection,
    job_id: str,
) -> bool:
    row = db.execute(
        """SELECT 1
        FROM automation_fire_attempts AS attempt
        JOIN connect_job_dispatch AS dispatch
          ON dispatch.job_id = attempt.dispatch_request_id
        WHERE attempt.dispatch_request_id = ?
          AND dispatch.interactive_authorized_at IS NULL
        LIMIT 1""",
        (job_id,),
    ).fetchone()
    return row is not None


class Store:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA recursive_triggers = ON")
        connection.row_factory = sqlite3.Row
        connection.create_function("casefold", 1, _sqlite_casefold, deterministic=True)
        connection.create_function(
            "aware_iso_epoch",
            1,
            _sqlite_aware_iso_epoch,
            deterministic=True,
        )
        connection.create_function(
            "message_source_key",
            3,
            _message_suppression_key,
            deterministic=True,
        )
        connection.create_function(
            "message_source_key",
            4,
            _message_suppression_key,
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

    @contextmanager
    def _source_cleanup_locks(self, message_ids: Iterable[str]) -> Iterator[None]:
        identities = sorted(set(message_ids))
        with ExitStack() as stack:
            for message_id in identities:
                stack.enter_context(
                    connect_operation_lock(
                        connect_source_lock_path(self.path, message_id),
                        "The Connect source attachment is being handed off",
                        timeout_seconds=-1,
                    )
                )
            yield

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
                + _AUTOMATION_TABLES_SQL
            )
            _ensure_mailbox_scope_schema(db)
            _ensure_connect_jobs_schema(db, version)
            _ensure_connect_dispatch_schema(db)
            _ensure_automate_core_schema(db, version)
            _ensure_gmail_label_schema(db)
            if version < 18:
                _migrate_microsoft_principal_keys_v2(db)
            automation_run_columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(automation_runs)").fetchall()
            }
            automation_run_migrations = {
                "source_content_sha256": "TEXT",
                "extraction_context_at": "TEXT",
                "extraction_timezone": "TEXT",
                "extraction_body_char_limit": "INTEGER",
                "current_payload_id": "TEXT",
                "current_payload_sha256": "TEXT",
                "review_notified_at": "TEXT",
            }
            for column, definition in automation_run_migrations.items():
                if column not in automation_run_columns:
                    db.execute(f"ALTER TABLE automation_runs ADD COLUMN {column} {definition}")
            automation_event_columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(automation_events)").fetchall()
            }
            automation_event_migrations = {
                "payload_id": "TEXT",
                "payload_sha256": "TEXT",
                "decision": "TEXT",
                "transaction_id": "TEXT",
                "graph_event_id": "TEXT",
            }
            for column, definition in automation_event_migrations.items():
                if column not in automation_event_columns:
                    db.execute(f"ALTER TABLE automation_events ADD COLUMN {column} {definition}")
            if version < 17:
                db.execute("DROP TRIGGER IF EXISTS automation_runs_delete_proposal_payloads")
                db.execute(
                    "ALTER TABLE automation_proposal_payloads "
                    "RENAME TO automation_proposal_payloads_v16"
                )
                db.execute(
                    """CREATE TABLE automation_proposal_payloads (
                        payload_id TEXT PRIMARY KEY CHECK (length(payload_id) = 36),
                        run_id TEXT NOT NULL CHECK (run_id <> ''),
                        proposal_version INTEGER NOT NULL CHECK (proposal_version > 0),
                        status TEXT NOT NULL CHECK (status IN ('accepted', 'no_suggestions')),
                        request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
                        proposal_sha256 TEXT NOT NULL CHECK (length(proposal_sha256) = 64),
                        subject TEXT NOT NULL CHECK (length(CAST(subject AS BLOB)) <= 512),
                        attendees_json BLOB NOT NULL CHECK (
                            typeof(attendees_json) = 'blob'
                            AND length(attendees_json) BETWEEN 2 AND 32768
                        ),
                        start TEXT CHECK (start IS NULL OR length(CAST(start AS BLOB)) <= 64),
                        end TEXT CHECK (end IS NULL OR length(CAST(end AS BLOB)) <= 64),
                        timezone TEXT CHECK (
                            timezone IS NULL OR length(CAST(timezone AS BLOB)) <= 128
                        ),
                        suggestion_reason TEXT CHECK (
                            suggestion_reason IS NULL
                            OR length(CAST(suggestion_reason AS BLOB)) <= 512
                        ),
                        empty_reason TEXT CHECK (
                            empty_reason IS NULL
                            OR length(CAST(empty_reason AS BLOB)) <= 512
                        ),
                        observed_at TEXT NOT NULL CHECK (observed_at <> ''),
                        expires_at TEXT,
                        created_at TEXT NOT NULL,
                        UNIQUE (run_id, proposal_version),
                        CHECK (
                            (status = 'accepted' AND start IS NOT NULL AND end IS NOT NULL
                                AND timezone IS NOT NULL AND suggestion_reason IS NOT NULL
                                AND empty_reason IS NULL AND expires_at IS NOT NULL)
                            OR (status = 'no_suggestions' AND start IS NULL AND end IS NULL
                                AND timezone IS NULL AND suggestion_reason IS NULL
                                AND empty_reason IS NOT NULL AND expires_at IS NULL)
                        )
                    )"""
                )
                db.execute(
                    """INSERT INTO automation_proposal_payloads(
                        payload_id, run_id, proposal_version, status, request_sha256,
                        proposal_sha256, subject, attendees_json, start, end, timezone,
                        suggestion_reason, empty_reason, observed_at, expires_at, created_at
                    )
                    SELECT payload_id, run_id, proposal_version, status, request_sha256,
                        proposal_sha256, subject, attendees_json, start, end, timezone,
                        suggestion_reason, empty_reason, observed_at, expires_at, created_at
                    FROM automation_proposal_payloads_v16"""
                )
                db.execute("DROP TABLE automation_proposal_payloads_v16")
                db.execute(
                    """CREATE TRIGGER automation_runs_delete_proposal_payloads
                    AFTER DELETE ON automation_runs
                    BEGIN
                        DELETE FROM automation_proposal_payloads WHERE run_id = OLD.run_id;
                    END
                    """
                )
            automation_payload_columns = {
                str(row["name"])
                for row in db.execute(
                    "PRAGMA table_info(automation_extraction_payloads)"
                ).fetchall()
            }
            automation_payload_migrations = {
                "organizer_address": "TEXT",
                "failure_count": "INTEGER NOT NULL DEFAULT 0",
                "next_retry_at": "TEXT",
                "last_error_code": "TEXT",
            }
            for column, definition in automation_payload_migrations.items():
                if column not in automation_payload_columns:
                    db.execute(
                        "ALTER TABLE automation_extraction_payloads "
                        f"ADD COLUMN {column} {definition}"
                    )
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
                    created_at, updated_at, mailbox_identity_key,
                    legacy_identity_status, legacy_identity_key
                FROM mail_accounts
                ORDER BY active DESC, casefold(display_name), provider, account_id"""
            ).fetchall()
        return [_mail_account(row) for row in rows]

    def mail_account(self, provider: str, account_id: str) -> MailAccount | None:
        with self.connection() as db:
            row = db.execute(
                """SELECT provider, account_id, display_name, address, active,
                    created_at, updated_at, mailbox_identity_key,
                    legacy_identity_status, legacy_identity_key
                FROM mail_accounts WHERE provider = ? AND account_id = ?""",
                (provider, account_id),
            ).fetchone()
        if row is None:
            return None
        return _mail_account(row)

    def mail_account_by_address(self, provider: str, address: str) -> MailAccount | None:
        with self.connection() as db:
            row = db.execute(
                """SELECT provider, account_id, display_name, address, active,
                    created_at, updated_at, mailbox_identity_key,
                    legacy_identity_status, legacy_identity_key
                FROM mail_accounts WHERE provider = ? AND address = ?""",
                (provider, address),
            ).fetchone()
        if row is None:
            return None
        return _mail_account(row)

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
                    created_at, updated_at, mailbox_identity_key,
                    legacy_identity_status, legacy_identity_key
                FROM mail_accounts WHERE active = 1"""
            ).fetchone()
        if row is None:
            return None
        return _mail_account(row)

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

    def reconcile_mailbox_identity(
        self,
        provider: str,
        account_id: str,
        mailbox_identity_key: str,
        *,
        legacy_status: str | None = None,
        preserve_cursor: bool = False,
    ) -> MailAccount:
        _require_mailbox_identity_key(mailbox_identity_key)
        if legacy_status not in {None, "continuity_proven", "replacement", "unresolved"}:
            raise ValueError("legacy mailbox identity status is invalid")
        stamp = datetime.now(UTC).isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT mailbox_identity_key, legacy_identity_status,
                    legacy_identity_key FROM mail_accounts
                WHERE provider = ? AND account_id = ?""",
                (provider, account_id),
            ).fetchone()
            if row is None:
                raise KeyError((provider, account_id))
            previous = (
                str(row["mailbox_identity_key"])
                if row["mailbox_identity_key"] is not None
                else None
            )
            status = legacy_status or str(row["legacy_identity_status"])
            legacy_key = (
                str(row["legacy_identity_key"]) if row["legacy_identity_key"] is not None else None
            )
            if previous is not None and previous != mailbox_identity_key:
                status = "replacement"
                legacy_key = previous
            elif previous is None and status == "continuity_proven":
                legacy_key = mailbox_identity_key
            db.execute(
                """UPDATE mail_accounts
                SET mailbox_identity_key = ?, legacy_identity_status = ?,
                    legacy_identity_key = ?, updated_at = ?
                WHERE provider = ? AND account_id = ?""",
                (mailbox_identity_key, status, legacy_key, stamp, provider, account_id),
            )
            if previous != mailbox_identity_key and not preserve_cursor:
                db.execute(
                    "DELETE FROM mailbox_state WHERE provider = ? AND account_id = ?",
                    (provider, account_id),
                )
            if previous is None and status == "continuity_proven":
                db.execute(
                    """UPDATE messages SET mailbox_identity_key = ?
                    WHERE provider = ? AND account_id = ?
                      AND mailbox_identity_key IS NULL AND status = 'pending'""",
                    (mailbox_identity_key, provider, account_id),
                )
            if provider == "gmail":
                selector_set = db.execute(
                    """SELECT current_mailbox_identity_key, revision
                    FROM gmail_label_selector_sets
                    WHERE provider = 'gmail' AND account_id = ?""",
                    (account_id,),
                ).fetchone()
                if selector_set is None:
                    db.execute(
                        """INSERT INTO gmail_label_selector_sets(
                            provider, account_id, current_mailbox_identity_key,
                            revision, created_at, updated_at
                        ) VALUES ('gmail', ?, ?, 0, ?, ?)""",
                        (account_id, mailbox_identity_key, stamp, stamp),
                    )
                    self._delete_gmail_label_validation(db, account_id)
                elif selector_set["current_mailbox_identity_key"] != mailbox_identity_key:
                    revision = int(selector_set["revision"])
                    if revision == SQLITE_MAX_INTEGER:
                        raise GmailLabelStoreError("gmail_selector_revision_overflow")
                    db.execute(
                        """UPDATE gmail_label_selector_sets
                        SET current_mailbox_identity_key = ?, revision = ?, updated_at = ?
                        WHERE provider = 'gmail' AND account_id = ?""",
                        (mailbox_identity_key, revision + 1, stamp, account_id),
                    )
                    self._delete_gmail_label_validation(db, account_id)
                    db.execute(
                        """DELETE FROM gmail_recovery_state
                        WHERE provider = 'gmail' AND account_id = ?""",
                        (account_id,),
                    )
        account = self.mail_account(provider, account_id)
        assert account is not None
        return account

    def require_mailbox_identity(
        self, provider: str, account_id: str, mailbox_identity_key: str
    ) -> None:
        with self.connection() as db:
            row = db.execute(
                """SELECT 1 FROM mail_accounts
                WHERE provider = ? AND account_id = ? AND mailbox_identity_key = ?""",
                (provider, account_id, mailbox_identity_key),
            ).fetchone()
        if row is None:
            raise MailboxIdentityChanged("mailbox identity changed")

    def gmail_label_selector_set(self, account_id: str) -> GmailLabelSelectorSet | None:
        with self.connection() as db:
            row = db.execute(
                """SELECT provider, account_id, current_mailbox_identity_key,
                    revision, created_at, updated_at
                FROM gmail_label_selector_sets
                WHERE provider = 'gmail' AND account_id = ?""",
                (account_id,),
            ).fetchone()
        return _gmail_label_selector_set(row) if row is not None else None

    def gmail_label_selectors(
        self,
        account_id: str,
        *,
        mailbox_identity_key: str | None = None,
    ) -> tuple[GmailLabelSelector, ...]:
        parameters: list[object] = [account_id]
        identity_clause = ""
        if mailbox_identity_key is not None:
            _require_mailbox_identity_key(mailbox_identity_key)
            identity_clause = " AND mailbox_identity_key = ?"
            parameters.append(mailbox_identity_key)
        with self.connection() as db:
            rows = db.execute(
                """SELECT selector_id, provider, account_id, mailbox_identity_key,
                    label_id, selected_display_name, created_at
                FROM gmail_label_selectors
                WHERE provider = 'gmail' AND account_id = ?"""
                + identity_clause
                + " ORDER BY selector_id",
                parameters,
            ).fetchall()
        return tuple(_gmail_label_selector(row) for row in rows)

    def gmail_current_label_selectors(
        self,
        account_id: str,
        mailbox_identity_key: str,
    ) -> tuple[GmailLabelSelector, ...]:
        _require_mailbox_identity_key(mailbox_identity_key)
        with self.connection() as db:
            selector_set = db.execute(
                """SELECT current_mailbox_identity_key
                FROM gmail_label_selector_sets
                WHERE provider = 'gmail' AND account_id = ?""",
                (account_id,),
            ).fetchone()
            account = db.execute(
                """SELECT mailbox_identity_key FROM mail_accounts
                WHERE provider = 'gmail' AND account_id = ?""",
                (account_id,),
            ).fetchone()
            if (
                selector_set is None
                or account is None
                or selector_set["current_mailbox_identity_key"] != mailbox_identity_key
                or account["mailbox_identity_key"] != mailbox_identity_key
            ):
                raise MailboxIdentityChanged("mailbox identity changed")
            rows = db.execute(
                """SELECT selector_id, provider, account_id, mailbox_identity_key,
                    label_id, selected_display_name, created_at
                FROM gmail_label_selectors
                WHERE provider = 'gmail' AND account_id = ?
                  AND mailbox_identity_key = ?
                ORDER BY selector_id""",
                (account_id, mailbox_identity_key),
            ).fetchall()
        return tuple(_gmail_label_selector(row) for row in rows)

    @staticmethod
    def _delete_gmail_label_validation(
        db: sqlite3.Connection,
        account_id: str,
    ) -> None:
        db.execute(
            """DELETE FROM gmail_label_selector_validations
            WHERE provider = 'gmail' AND account_id = ?""",
            (account_id,),
        )
        db.execute(
            """DELETE FROM gmail_label_validation_sets
            WHERE provider = 'gmail' AND account_id = ?""",
            (account_id,),
        )

    def persist_gmail_label_validation(
        self,
        account_id: str,
        mailbox_identity_key: str,
        expected_revision: int,
        catalog_labels: Sequence[tuple[str, str, str]],
        *,
        now: datetime | None = None,
    ) -> GmailLabelValidationSnapshot:
        _require_mailbox_identity_key(mailbox_identity_key)
        expected = _require_revision(expected_revision)
        labels: dict[str, tuple[str, str]] = {}
        for raw_label_id, raw_display_name, raw_label_type in catalog_labels:
            label_id = _require_bounded_text(
                raw_label_id,
                maximum_bytes=MAX_GMAIL_LABEL_ID_BYTES,
                field="gmail label id",
            )
            display_name = _require_bounded_text(
                raw_display_name,
                maximum_bytes=MAX_GMAIL_LABEL_NAME_BYTES,
                field="gmail label display name",
            )
            if raw_label_type not in {"user", "system"}:
                raise ValueError("gmail label type is invalid")
            if label_id in labels:
                raise ValueError("gmail label ids must be unique")
            labels[label_id] = (display_name, raw_label_type)
        stamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            selector_set = self._require_current_gmail_selector_set(
                db,
                account_id=account_id,
                mailbox_identity_key=mailbox_identity_key,
            )
            if int(selector_set["revision"]) != expected:
                raise GmailLabelStoreError("stale_revision")
            previous_names = {
                str(row["selector_id"]): str(row["display_name"])
                for row in db.execute(
                    """SELECT selector_id, display_name
                    FROM gmail_label_selector_validations
                    WHERE provider = 'gmail' AND account_id = ?
                      AND mailbox_identity_key = ?""",
                    (account_id, mailbox_identity_key),
                ).fetchall()
            }
            selectors = tuple(
                _gmail_label_selector(row)
                for row in db.execute(
                    """SELECT selector_id, provider, account_id, mailbox_identity_key,
                        label_id, selected_display_name, created_at
                    FROM gmail_label_selectors
                    WHERE provider = 'gmail' AND account_id = ?
                      AND mailbox_identity_key = ?
                    ORDER BY selector_id""",
                    (account_id, mailbox_identity_key),
                ).fetchall()
            )
            validations: list[GmailLabelSelectorValidation] = []
            for selector in selectors:
                catalog_label = labels.get(selector.label_id)
                if catalog_label is None:
                    status = "deleted"
                    display_name = previous_names.get(
                        selector.selector_id,
                        selector.selected_display_name,
                    )
                else:
                    display_name, label_type = catalog_label
                    status = "active" if label_type == "user" else "not_user"
                validations.append(
                    GmailLabelSelectorValidation(
                        selector_id=selector.selector_id,
                        label_id=selector.label_id,
                        status=status,
                        display_name=display_name,
                    )
                )
            self._delete_gmail_label_validation(db, account_id)
            db.execute(
                """INSERT INTO gmail_label_validation_sets(
                    provider, account_id, mailbox_identity_key,
                    selector_revision, validated_at
                ) VALUES ('gmail', ?, ?, ?, ?)""",
                (account_id, mailbox_identity_key, expected, stamp),
            )
            db.executemany(
                """INSERT INTO gmail_label_selector_validations(
                    selector_id, provider, account_id, mailbox_identity_key,
                    selector_revision, label_id, status, display_name
                ) VALUES (?, 'gmail', ?, ?, ?, ?, ?, ?)""",
                (
                    (
                        validation.selector_id,
                        account_id,
                        mailbox_identity_key,
                        expected,
                        validation.label_id,
                        validation.status,
                        validation.display_name,
                    )
                    for validation in validations
                ),
            )
        return GmailLabelValidationSnapshot(
            provider="gmail",
            account_id=account_id,
            mailbox_identity_key=mailbox_identity_key,
            selector_revision=expected,
            validated_at=stamp,
            selectors=tuple(validations),
        )

    def gmail_label_validation_snapshot(
        self,
        account_id: str,
        mailbox_identity_key: str,
        selector_revision: int,
    ) -> GmailLabelValidationSnapshot | None:
        _require_mailbox_identity_key(mailbox_identity_key)
        revision = _require_revision(selector_revision)
        with self.connection() as db:
            snapshot = db.execute(
                """SELECT v.provider, v.account_id, v.mailbox_identity_key,
                    v.selector_revision, v.validated_at
                FROM gmail_label_validation_sets AS v
                JOIN gmail_label_selector_sets AS s
                  ON s.provider = v.provider AND s.account_id = v.account_id
                JOIN mail_accounts AS a
                  ON a.provider = v.provider AND a.account_id = v.account_id
                WHERE v.provider = 'gmail' AND v.account_id = ?
                  AND v.mailbox_identity_key = ? AND v.selector_revision = ?
                  AND s.current_mailbox_identity_key = v.mailbox_identity_key
                  AND s.revision = v.selector_revision
                  AND a.mailbox_identity_key = v.mailbox_identity_key""",
                (account_id, mailbox_identity_key, revision),
            ).fetchone()
            if snapshot is None:
                return None
            selectors = db.execute(
                """SELECT selector_id, label_id
                FROM gmail_label_selectors
                WHERE provider = 'gmail' AND account_id = ?
                  AND mailbox_identity_key = ?
                ORDER BY selector_id""",
                (account_id, mailbox_identity_key),
            ).fetchall()
            rows = db.execute(
                """SELECT selector_id, label_id, status, display_name
                FROM gmail_label_selector_validations
                WHERE provider = 'gmail' AND account_id = ?
                  AND mailbox_identity_key = ? AND selector_revision = ?
                ORDER BY selector_id""",
                (account_id, mailbox_identity_key, revision),
            ).fetchall()
        expected_selectors = [
            (str(row["selector_id"]), str(row["label_id"])) for row in selectors
        ]
        validated_selectors = [
            (str(row["selector_id"]), str(row["label_id"])) for row in rows
        ]
        if validated_selectors != expected_selectors:
            return None
        return GmailLabelValidationSnapshot(
            provider=str(snapshot["provider"]),
            account_id=str(snapshot["account_id"]),
            mailbox_identity_key=str(snapshot["mailbox_identity_key"]),
            selector_revision=int(snapshot["selector_revision"]),
            validated_at=str(snapshot["validated_at"]),
            selectors=tuple(
                GmailLabelSelectorValidation(
                    selector_id=str(row["selector_id"]),
                    label_id=str(row["label_id"]),
                    status=str(row["status"]),
                    display_name=str(row["display_name"]),
                )
                for row in rows
            ),
        )

    def invalidate_gmail_label_validation(
        self,
        account_id: str,
        mailbox_identity_key: str,
        selector_revision: int,
    ) -> bool:
        _require_mailbox_identity_key(mailbox_identity_key)
        revision = _require_revision(selector_revision)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            snapshot = db.execute(
                """SELECT 1 FROM gmail_label_validation_sets
                WHERE provider = 'gmail' AND account_id = ?
                  AND mailbox_identity_key = ? AND selector_revision = ?""",
                (account_id, mailbox_identity_key, revision),
            ).fetchone()
            if snapshot is None:
                return False
            self._delete_gmail_label_validation(db, account_id)
        return True

    @staticmethod
    def _require_current_gmail_selector_set(
        db: sqlite3.Connection,
        *,
        account_id: str,
        mailbox_identity_key: str,
    ) -> sqlite3.Row:
        row = db.execute(
            """SELECT s.current_mailbox_identity_key, s.revision
            FROM gmail_label_selector_sets AS s
            JOIN mail_accounts AS a
              ON a.provider = s.provider AND a.account_id = s.account_id
            WHERE s.provider = 'gmail' AND s.account_id = ?""",
            (account_id,),
        ).fetchone()
        if (
            row is None
            or row["current_mailbox_identity_key"] != mailbox_identity_key
            or db.execute(
                """SELECT 1 FROM mail_accounts
                WHERE provider = 'gmail' AND account_id = ?
                  AND mailbox_identity_key = ?""",
                (account_id, mailbox_identity_key),
            ).fetchone()
            is None
        ):
            raise MailboxIdentityChanged("mailbox identity changed")
        return row

    def add_gmail_label_selector(
        self,
        account_id: str,
        mailbox_identity_key: str,
        label_id: str,
        display_name: str,
        expected_revision: int,
        *,
        now: datetime | None = None,
    ) -> tuple[int, GmailLabelSelector]:
        _require_mailbox_identity_key(mailbox_identity_key)
        expected = _require_revision(expected_revision)
        bounded_label_id = _require_bounded_text(
            label_id,
            maximum_bytes=MAX_GMAIL_LABEL_ID_BYTES,
            field="gmail label id",
        )
        bounded_display_name = _require_bounded_text(
            display_name,
            maximum_bytes=MAX_GMAIL_LABEL_NAME_BYTES,
            field="gmail label display name",
        )
        stamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        selector_id = str(uuid.uuid4())
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            selector_set = self._require_current_gmail_selector_set(
                db,
                account_id=account_id,
                mailbox_identity_key=mailbox_identity_key,
            )
            revision = int(selector_set["revision"])
            if revision != expected:
                raise GmailLabelStoreError("stale_revision")
            if revision == SQLITE_MAX_INTEGER:
                raise GmailLabelStoreError("gmail_selector_revision_overflow")
            if (
                db.execute(
                    """SELECT 1 FROM gmail_label_selectors
                    WHERE provider = 'gmail' AND account_id = ?
                      AND mailbox_identity_key = ? AND label_id = ?""",
                    (account_id, mailbox_identity_key, bounded_label_id),
                ).fetchone()
                is not None
            ):
                raise GmailLabelStoreError("conflict")
            count = int(
                db.execute(
                    """SELECT COUNT(*) FROM gmail_label_selectors
                    WHERE provider = 'gmail' AND account_id = ?""",
                    (account_id,),
                ).fetchone()[0]
            )
            if count >= MAX_GMAIL_LABEL_SELECTORS:
                raise GmailLabelStoreError("limit_exceeded")
            db.execute(
                """INSERT INTO gmail_label_selectors(
                    selector_id, provider, account_id, mailbox_identity_key,
                    label_id, selected_display_name, created_at
                ) VALUES (?, 'gmail', ?, ?, ?, ?, ?)""",
                (
                    selector_id,
                    account_id,
                    mailbox_identity_key,
                    bounded_label_id,
                    bounded_display_name,
                    stamp,
                ),
            )
            new_revision = revision + 1
            changed = db.execute(
                """UPDATE gmail_label_selector_sets
                SET revision = ?, updated_at = ?
                WHERE provider = 'gmail' AND account_id = ?
                  AND current_mailbox_identity_key = ? AND revision = ?""",
                (new_revision, stamp, account_id, mailbox_identity_key, revision),
            )
            if changed.rowcount != 1:
                raise GmailLabelStoreError("stale_revision")
            self._delete_gmail_label_validation(db, account_id)
        selector = GmailLabelSelector(
            selector_id=selector_id,
            provider="gmail",
            account_id=account_id,
            mailbox_identity_key=mailbox_identity_key,
            label_id=bounded_label_id,
            selected_display_name=bounded_display_name,
            created_at=stamp,
        )
        return new_revision, selector

    def remove_gmail_label_selector(
        self,
        account_id: str,
        mailbox_identity_key: str,
        selector_id: str,
        expected_revision: int,
        *,
        now: datetime | None = None,
    ) -> int:
        _require_mailbox_identity_key(mailbox_identity_key)
        if not _valid_uuid_v4(selector_id):
            raise ValueError("selector id must be a canonical UUIDv4")
        expected = _require_revision(expected_revision)
        stamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            selector_set = self._require_current_gmail_selector_set(
                db,
                account_id=account_id,
                mailbox_identity_key=mailbox_identity_key,
            )
            revision = int(selector_set["revision"])
            if revision != expected:
                raise GmailLabelStoreError("stale_revision")
            if revision == SQLITE_MAX_INTEGER:
                raise GmailLabelStoreError("gmail_selector_revision_overflow")
            deleted = db.execute(
                """DELETE FROM gmail_label_selectors
                WHERE provider = 'gmail' AND account_id = ? AND selector_id = ?""",
                (account_id, selector_id),
            )
            if deleted.rowcount != 1:
                raise GmailLabelStoreError("not_found")
            new_revision = revision + 1
            changed = db.execute(
                """UPDATE gmail_label_selector_sets
                SET revision = ?, updated_at = ?
                WHERE provider = 'gmail' AND account_id = ?
                  AND current_mailbox_identity_key = ? AND revision = ?""",
                (new_revision, stamp, account_id, mailbox_identity_key, revision),
            )
            if changed.rowcount != 1:
                raise GmailLabelStoreError("stale_revision")
            self._delete_gmail_label_validation(db, account_id)
        return new_revision

    def gmail_label_selector_is_current(
        self,
        account_id: str,
        mailbox_identity_key: str,
        selector_id: str,
        label_id: str,
    ) -> bool:
        _require_mailbox_identity_key(mailbox_identity_key)
        if not _valid_uuid_v4(selector_id):
            return False
        with self.connection() as db:
            return (
                db.execute(
                    """SELECT 1 FROM gmail_label_selectors AS l
                    JOIN gmail_label_selector_sets AS s
                      ON s.provider = l.provider AND s.account_id = l.account_id
                    JOIN mail_accounts AS a
                      ON a.provider = l.provider AND a.account_id = l.account_id
                    WHERE l.provider = 'gmail' AND l.account_id = ?
                      AND l.mailbox_identity_key = ?
                      AND l.selector_id = ? AND l.label_id = ?
                      AND s.current_mailbox_identity_key = l.mailbox_identity_key
                      AND a.mailbox_identity_key = l.mailbox_identity_key""",
                    (account_id, mailbox_identity_key, selector_id, label_id),
                ).fetchone()
                is not None
            )

    def create_gmail_recovery_state(
        self,
        account_id: str,
        mailbox_identity_key: str,
        selector_revision: int,
        sender_snapshot: Sequence[tuple[str, str | None]],
        selector_snapshot: Sequence[object],
        recovery_after_exclusive_epoch: int,
        recovery_before_exclusive_epoch: int,
        replacement_history_cursor: str,
        *,
        retention_cutoff: datetime | None = None,
        now: datetime | None = None,
    ) -> GmailRecoveryState:
        _require_mailbox_identity_key(mailbox_identity_key)
        revision = _require_revision(selector_revision)
        if (
            type(recovery_after_exclusive_epoch) is not int
            or type(recovery_before_exclusive_epoch) is not int
            or recovery_after_exclusive_epoch < 0
            or recovery_before_exclusive_epoch <= recovery_after_exclusive_epoch
            or recovery_before_exclusive_epoch > SQLITE_MAX_INTEGER
        ):
            raise ValueError("gmail recovery window is invalid")
        cursor = _require_bounded_text(
            replacement_history_cursor,
            maximum_bytes=MAX_GMAIL_RECOVERY_CURSOR_BYTES,
            field="gmail replacement history cursor",
        )
        if retention_cutoff is None:
            retention_cutoff = datetime.fromtimestamp(
                recovery_after_exclusive_epoch, tz=UTC
            )
        if (
            not isinstance(retention_cutoff, datetime)
            or retention_cutoff.tzinfo is None
            or retention_cutoff.utcoffset() != timedelta(0)
            or retention_cutoff < datetime(1970, 1, 1, tzinfo=UTC)
        ):
            raise ValueError("gmail recovery retention cutoff is invalid")
        retention_cutoff_stamp = retention_cutoff.astimezone(UTC).isoformat()
        sender_json = encode_gmail_sender_snapshot(sender_snapshot)
        selector_json = encode_gmail_selector_snapshot(selector_snapshot)
        stamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            selector_set = self._require_current_gmail_selector_set(
                db,
                account_id=account_id,
                mailbox_identity_key=mailbox_identity_key,
            )
            active = db.execute(
                """SELECT active FROM mail_accounts
                WHERE provider = 'gmail' AND account_id = ?""",
                (account_id,),
            ).fetchone()
            if active is None or not bool(active["active"]):
                raise GmailLabelStoreError("account_not_active")
            if int(selector_set["revision"]) != revision:
                raise GmailLabelStoreError("stale_revision")
            for frozen_selector in decode_gmail_selector_snapshot(selector_json):
                if (
                    db.execute(
                        """SELECT 1 FROM gmail_label_selectors
                        WHERE provider = 'gmail' AND account_id = ?
                          AND mailbox_identity_key = ? AND selector_id = ?
                          AND label_id = ?""",
                        (
                            account_id,
                            mailbox_identity_key,
                            frozen_selector.selector_id,
                            frozen_selector.label_id,
                        ),
                    ).fetchone()
                    is None
                ):
                    raise GmailLabelStoreError("gmail_recovery_grant_revoked")
            try:
                db.execute(
                    """INSERT INTO gmail_recovery_state(
                        provider, account_id, mailbox_identity_key, selector_revision,
                        sender_snapshot_json, selector_snapshot_json,
                        recovery_after_exclusive_epoch, recovery_before_exclusive_epoch,
                        retention_cutoff, replacement_history_cursor,
                        page_token, current_page_ids_json,
                        page_loaded, next_index, page_count, terminal_candidate_count,
                        invalid_page_token_count, consecutive_retry_count,
                        state, failure_code, next_retry_at, created_at, updated_at
                    ) VALUES (
                        'gmail', ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, X'5B5D',
                        0, 0, 0, 0, 0, 0, 'collecting', NULL, NULL, ?, ?
                    )""",
                    (
                        account_id,
                        mailbox_identity_key,
                        revision,
                        sender_json,
                        selector_json,
                        recovery_after_exclusive_epoch,
                        recovery_before_exclusive_epoch,
                        retention_cutoff_stamp,
                        cursor,
                        stamp,
                        stamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise GmailLabelStoreError("conflict") from exc
            row = db.execute(
                "SELECT * FROM gmail_recovery_state WHERE provider='gmail' AND account_id=?",
                (account_id,),
            ).fetchone()
        assert row is not None
        return _gmail_recovery_state(row)

    def gmail_recovery_state(self, account_id: str) -> GmailRecoveryState | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM gmail_recovery_state WHERE provider='gmail' AND account_id=?",
                (account_id,),
            ).fetchone()
        return _gmail_recovery_state(row) if row is not None else None

    @staticmethod
    def _require_current_gmail_recovery(
        db: sqlite3.Connection,
        *,
        account_id: str,
        mailbox_identity_key: str,
    ) -> tuple[sqlite3.Row, GmailRecoveryState]:
        Store._require_current_gmail_selector_set(
            db,
            account_id=account_id,
            mailbox_identity_key=mailbox_identity_key,
        )
        row = db.execute(
            "SELECT * FROM gmail_recovery_state WHERE provider='gmail' AND account_id=?",
            (account_id,),
        ).fetchone()
        if row is None:
            raise GmailLabelStoreError("not_found")
        state = _gmail_recovery_state(row)
        if state.mailbox_identity_key != mailbox_identity_key:
            raise MailboxIdentityChanged("mailbox identity changed")
        active = db.execute(
            """SELECT active FROM mail_accounts
            WHERE provider = 'gmail' AND account_id = ?""",
            (account_id,),
        ).fetchone()
        if active is None or not bool(active["active"]):
            raise GmailLabelStoreError("account_not_active")
        return row, state

    def store_gmail_recovery_page(
        self,
        account_id: str,
        mailbox_identity_key: str,
        message_ids: Sequence[str],
        next_page_token: str | None,
        *,
        now: datetime | None = None,
    ) -> GmailRecoveryState:
        _require_mailbox_identity_key(mailbox_identity_key)
        page_json = encode_gmail_recovery_page(message_ids)
        if next_page_token is not None:
            next_page_token = _require_bounded_text(
                next_page_token,
                maximum_bytes=MAX_GMAIL_RECOVERY_PAGE_TOKEN_BYTES,
                field="gmail recovery page token",
            )
        stamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            _row, state = self._require_current_gmail_recovery(
                db,
                account_id=account_id,
                mailbox_identity_key=mailbox_identity_key,
            )
            if state.page_loaded:
                raise GmailLabelStoreError("gmail_recovery_page_already_loaded")
            if state.page_count == SQLITE_MAX_INTEGER:
                raise GmailLabelStoreError("gmail_recovery_counter_overflow")
            db.execute(
                """UPDATE gmail_recovery_state
                SET page_token = ?, current_page_ids_json = ?, page_loaded = 1,
                    next_index = 0, page_count = page_count + 1,
                    consecutive_retry_count = 0, state = 'collecting',
                    failure_code = NULL, next_retry_at = NULL, updated_at = ?
                WHERE provider = 'gmail' AND account_id = ?""",
                (next_page_token, page_json, stamp, account_id),
            )
            updated = db.execute(
                "SELECT * FROM gmail_recovery_state WHERE provider='gmail' AND account_id=?",
                (account_id,),
            ).fetchone()
        assert updated is not None
        return _gmail_recovery_state(updated)

    def finish_gmail_recovery_page(
        self,
        account_id: str,
        mailbox_identity_key: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Retire a drained non-final page; return True when the final page is ready."""
        _require_mailbox_identity_key(mailbox_identity_key)
        stamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            _row, state = self._require_current_gmail_recovery(
                db,
                account_id=account_id,
                mailbox_identity_key=mailbox_identity_key,
            )
            if not state.page_loaded or state.next_index != len(state.current_page_ids):
                raise GmailLabelStoreError("gmail_recovery_page_not_drained")
            if state.page_token is None:
                return True
            db.execute(
                """UPDATE gmail_recovery_state
                SET current_page_ids_json = X'5B5D', page_loaded = 0,
                    next_index = 0, updated_at = ?
                WHERE provider = 'gmail' AND account_id = ?""",
                (stamp, account_id),
            )
        return False

    def record_gmail_recovery_backoff(
        self,
        account_id: str,
        mailbox_identity_key: str,
        *,
        failure_code: str,
        next_retry_at: str,
        degraded: bool = False,
        now: datetime | None = None,
    ) -> GmailRecoveryState:
        if failure_code not in {
            "gmail_recovery_provider_unavailable",
            "gmail_recovery_page_invalid",
        }:
            raise ValueError("gmail recovery failure code is invalid")
        _require_mailbox_identity_key(mailbox_identity_key)
        _validate_utc_timestamp(next_retry_at, field="next_retry_at")
        stamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            _row, state = self._require_current_gmail_recovery(
                db,
                account_id=account_id,
                mailbox_identity_key=mailbox_identity_key,
            )
            retry_count = min(31, state.consecutive_retry_count + 1)
            db.execute(
                """UPDATE gmail_recovery_state
                SET consecutive_retry_count = ?, state = ?, failure_code = ?,
                    next_retry_at = ?, updated_at = ?
                WHERE provider = 'gmail' AND account_id = ?""",
                (
                    retry_count,
                    "degraded" if degraded else "backoff",
                    failure_code,
                    next_retry_at,
                    stamp,
                    account_id,
                ),
            )
            updated = db.execute(
                "SELECT * FROM gmail_recovery_state WHERE provider='gmail' AND account_id=?",
                (account_id,),
            ).fetchone()
        assert updated is not None
        return _gmail_recovery_state(updated)

    def record_gmail_recovery_invalid_page_token(
        self,
        account_id: str,
        mailbox_identity_key: str,
        *,
        next_retry_at: str,
        now: datetime | None = None,
    ) -> GmailRecoveryState:
        _require_mailbox_identity_key(mailbox_identity_key)
        _validate_utc_timestamp(next_retry_at, field="next_retry_at")
        stamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            _row, state = self._require_current_gmail_recovery(
                db,
                account_id=account_id,
                mailbox_identity_key=mailbox_identity_key,
            )
            if state.invalid_page_token_count == SQLITE_MAX_INTEGER:
                raise GmailLabelStoreError("gmail_recovery_counter_overflow")
            invalid_count = state.invalid_page_token_count + 1
            retry_count = min(31, state.consecutive_retry_count + 1)
            db.execute(
                """UPDATE gmail_recovery_state
                SET page_token = NULL, current_page_ids_json = X'5B5D',
                    page_loaded = 0, next_index = 0,
                    invalid_page_token_count = ?, consecutive_retry_count = ?,
                    state = ?, failure_code = 'gmail_recovery_page_token_invalid',
                    next_retry_at = ?, updated_at = ?
                WHERE provider = 'gmail' AND account_id = ?""",
                (
                    invalid_count,
                    retry_count,
                    "degraded" if invalid_count >= 5 else "backoff",
                    next_retry_at,
                    stamp,
                    account_id,
                ),
            )
            updated = db.execute(
                "SELECT * FROM gmail_recovery_state WHERE provider='gmail' AND account_id=?",
                (account_id,),
            ).fetchone()
        assert updated is not None
        return _gmail_recovery_state(updated)

    def clear_gmail_recovery_backoff(
        self,
        account_id: str,
        mailbox_identity_key: str,
        *,
        now: datetime | None = None,
    ) -> GmailRecoveryState:
        _require_mailbox_identity_key(mailbox_identity_key)
        stamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._require_current_gmail_recovery(
                db,
                account_id=account_id,
                mailbox_identity_key=mailbox_identity_key,
            )
            db.execute(
                """UPDATE gmail_recovery_state
                SET state = 'collecting',
                    failure_code = NULL, next_retry_at = NULL, updated_at = ?
                WHERE provider = 'gmail' AND account_id = ?""",
                (stamp, account_id),
            )
            updated = db.execute(
                "SELECT * FROM gmail_recovery_state WHERE provider='gmail' AND account_id=?",
                (account_id,),
            ).fetchone()
        assert updated is not None
        return _gmail_recovery_state(updated)

    def finish_gmail_recovery_candidate(
        self,
        account_id: str,
        mailbox_identity_key: str,
        provider_message_id: str,
        *,
        message: GmailRecoveryMessage | None = None,
        admission: AdmissionProvenance | None = None,
        metadata_label_ids: frozenset[str] | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Advance one terminal candidate, atomically inserting an admitted message."""
        _require_mailbox_identity_key(mailbox_identity_key)
        bounded_provider_message_id = _require_bounded_text(
            provider_message_id,
            maximum_bytes=MAX_GMAIL_LABEL_ID_BYTES,
            field="gmail provider message id",
        )
        if (message is None) != (admission is None) or (
            message is None and metadata_label_ids is not None
        ):
            raise ValueError("message, admission, and metadata labels are inconsistent")
        if message is not None and (
            not isinstance(metadata_label_ids, frozenset)
            or not all(isinstance(label_id, str) for label_id in metadata_label_ids)
            or "INBOX" not in metadata_label_ids
        ):
            raise ValueError("admitted recovery metadata must include INBOX labels")
        stamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            _row, state = self._require_current_gmail_recovery(
                db,
                account_id=account_id,
                mailbox_identity_key=mailbox_identity_key,
            )
            if (
                not state.page_loaded
                or state.next_index >= len(state.current_page_ids)
                or state.current_page_ids[state.next_index] != bounded_provider_message_id
            ):
                raise GmailLabelStoreError("gmail_recovery_candidate_mismatch")
            if state.terminal_candidate_count == SQLITE_MAX_INTEGER:
                raise GmailLabelStoreError("gmail_recovery_counter_overflow")
            inserted = False
            if message is not None and admission is not None:
                _validate_admission_provenance(
                    admission,
                    mailbox_identity_key=mailbox_identity_key,
                )
                if admission.kind == "gmail_user_label":
                    frozen_by_id = {
                        selector.selector_id: selector
                        for selector in state.selector_snapshot
                    }
                    current_rows = db.execute(
                        """SELECT selector_id, label_id
                        FROM gmail_label_selectors
                        WHERE provider = 'gmail' AND account_id = ?
                          AND mailbox_identity_key = ?
                        ORDER BY selector_id""",
                        (account_id, mailbox_identity_key),
                    ).fetchall()
                    matching_ids = [
                        str(row["selector_id"])
                        for row in current_rows
                        if str(row["selector_id"]) in frozen_by_id
                        and str(row["label_id"])
                        == frozen_by_id[str(row["selector_id"])].label_id
                        and str(row["label_id"]) in metadata_label_ids
                    ]
                    winning_id = min(matching_ids) if matching_ids else None
                    frozen = frozen_by_id.get(admission.selector_id)
                    if (
                        frozen is None
                        or winning_id != admission.selector_id
                        or frozen.display_name != admission.display_name
                    ):
                        raise GmailLabelStoreError("gmail_recovery_grant_revoked")
                else:
                    prefix = "sender:"
                    sender_address = (
                        admission.selector_id[len(prefix) :]
                        if admission.selector_id.startswith(prefix)
                        else ""
                    )
                    frozen_sender = next(
                        (
                            item
                            for item in state.sender_snapshot
                            if item[0] == sender_address
                        ),
                        None,
                    )
                    if frozen_sender is None or frozen_sender[1] != admission.display_name:
                        raise GmailLabelStoreError("gmail_recovery_grant_revoked")
                inserted = self._insert_message_in_transaction(
                    db,
                    message_id=message.message_id,
                    provider="gmail",
                    account_id=account_id,
                    provider_message_id=bounded_provider_message_id,
                    mailbox_identity_key=mailbox_identity_key,
                    thread_id=message.thread_id,
                    sender=message.sender,
                    sender_name=message.sender_name,
                    subject=message.subject,
                    received_at=message.received_at,
                    admission=admission,
                    discovered_at=stamp,
                )
            changed = db.execute(
                """UPDATE gmail_recovery_state
                SET next_index = next_index + 1,
                    terminal_candidate_count = terminal_candidate_count + 1,
                    consecutive_retry_count = 0, state = 'collecting',
                    failure_code = NULL, next_retry_at = NULL, updated_at = ?
                WHERE provider = 'gmail' AND account_id = ?
                  AND mailbox_identity_key = ? AND next_index = ?
                  AND terminal_candidate_count = ?""",
                (
                    stamp,
                    account_id,
                    mailbox_identity_key,
                    state.next_index,
                    state.terminal_candidate_count,
                ),
            )
            if changed.rowcount != 1:
                raise GmailLabelStoreError("gmail_recovery_candidate_mismatch")
        return inserted

    def complete_gmail_recovery(
        self,
        account_id: str,
        mailbox_identity_key: str,
        *,
        now: datetime | None = None,
    ) -> str:
        _require_mailbox_identity_key(mailbox_identity_key)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            _row, state = self._require_current_gmail_recovery(
                db,
                account_id=account_id,
                mailbox_identity_key=mailbox_identity_key,
            )
            if (
                not state.page_loaded
                or state.page_token is not None
                or state.next_index != len(state.current_page_ids)
            ):
                raise GmailLabelStoreError("gmail_recovery_page_not_drained")
            db.execute(
                """INSERT INTO mailbox_state(provider, account_id, cursor, last_success_at)
                VALUES ('gmail', ?, ?, ?)
                ON CONFLICT(provider, account_id) DO UPDATE SET
                  cursor=excluded.cursor, last_success_at=excluded.last_success_at""",
                (account_id, state.replacement_history_cursor, state.created_at),
            )
            deleted = db.execute(
                """DELETE FROM gmail_recovery_state
                WHERE provider = 'gmail' AND account_id = ?
                  AND mailbox_identity_key = ?""",
                (account_id, mailbox_identity_key),
            )
            if deleted.rowcount != 1:
                raise GmailLabelStoreError("not_found")
        return state.replacement_history_cursor

    @staticmethod
    def _automation_rule_detail(row: sqlite3.Row) -> AutomationRuleDetail:
        definition: dict[str, object] | None = None
        name: str | None = None
        invalid_reason: str | None = None
        try:
            if str(row["kind"]) != "definition":
                raise RuleValidationError("current rule version is not a definition")
            encoded = bytes(row["definition_json"])
            if hashlib.sha256(encoded).hexdigest() != str(row["definition_sha256"]):
                raise RuleValidationError("stored definition digest does not match")
            decoded = json.loads(encoded)
            parsed = parse_rule_definition(decoded)
            if canonical_rule_definition(parsed) != encoded:
                raise RuleValidationError("stored definition is not canonical")
            definition = parsed.model_dump(mode="json", exclude_defaults=False)
            name = parsed.name
        except (
            AttributeError,
            json.JSONDecodeError,
            TypeError,
            UnicodeDecodeError,
            UnicodeEncodeError,
            RuleValidationError,
        ) as exc:
            invalid_reason = str(exc)[:128] or "stored definition is invalid"
        summary = AutomationRuleSummary(
            rule_id=str(row["rule_id"]),
            version=int(row["current_version"]),
            enabled=bool(row["enabled"]),
            system=bool(row["system"]),
            valid=invalid_reason is None,
            name=name,
            invalid_reason=invalid_reason,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )
        return AutomationRuleDetail(summary=summary, definition=definition)

    @staticmethod
    def _next_automation_rules_revision(db: sqlite3.Connection) -> int:
        row = db.execute("SELECT revision FROM automation_rule_set WHERE id = 1").fetchone()
        if row is None:
            raise RuntimeError("automation rule-set authority is missing")
        current = int(row["revision"])
        changed = db.execute(
            "UPDATE automation_rule_set SET revision = ? WHERE id = 1 AND revision = ?",
            (current + 1, current),
        )
        if changed.rowcount != 1:
            raise RuntimeError("automation rule-set revision lost its transaction race")
        return current + 1

    @staticmethod
    def _automation_rule_scope_identity(
        db: sqlite3.Connection,
        definition: RuleDefinition,
        expected_account_identity: str | None,
    ) -> str | None:
        scope = definition.scope
        if scope.account_id is None:
            if expected_account_identity is not None:
                raise ValueError("mailbox identity is only valid for account-scoped rules")
            return None
        if expected_account_identity is None:
            raise MailboxIdentityChanged("account-scoped rule requires a mailbox identity")
        row = db.execute(
            """SELECT mailbox_identity_key FROM mail_accounts
            WHERE provider = ? AND account_id = ?""",
            (scope.provider, scope.account_id),
        ).fetchone()
        if row is None or row["mailbox_identity_key"] != expected_account_identity:
            raise MailboxIdentityChanged("mailbox identity changed")
        return expected_account_identity

    def automation_rules_snapshot(self) -> tuple[int, list[AutomationRuleSummary]]:
        with self.connection() as db:
            db.execute("BEGIN")
            revision = db.execute(
                "SELECT revision FROM automation_rule_set WHERE id = 1"
            ).fetchone()
            rows = db.execute(
                """SELECT r.*, v.kind, v.definition_json, v.definition_sha256,
                    v.scope_mailbox_identity_key, v.global_revision
                FROM automation_rules AS r
                JOIN automation_rule_versions AS v
                  ON v.rule_id = r.rule_id AND v.version = r.current_version
                WHERE r.deleted = 0
                ORDER BY r.created_at, r.rule_id"""
            ).fetchall()
        if revision is None:
            raise RuntimeError("automation rule-set authority is missing")
        return int(revision["revision"]), [
            self._automation_rule_detail(row).summary for row in rows
        ]

    def require_automation_rule_create_capacity(self) -> None:
        """Reject a full live rule set without mutating mailbox or rule state."""
        with self.connection() as db:
            count = db.execute(
                "SELECT COUNT(*) AS count FROM automation_rules WHERE deleted = 0"
            ).fetchone()
        if count is None or int(count["count"]) >= MAX_AUTOMATION_RULES:
            raise AutomationRuleLimitExceeded("automation rule limit exceeded")

    def automation_rule(self, rule_id: str) -> AutomationRuleDetail:
        with self.connection() as db:
            row = db.execute(
                """SELECT r.*, v.kind, v.definition_json, v.definition_sha256,
                    v.scope_mailbox_identity_key, v.global_revision
                FROM automation_rules AS r
                JOIN automation_rule_versions AS v
                  ON v.rule_id = r.rule_id AND v.version = r.current_version
                WHERE r.rule_id = ? AND r.deleted = 0""",
                (rule_id,),
            ).fetchone()
        if row is None:
            raise AutomationRuleNotFound(rule_id)
        return self._automation_rule_detail(row)

    def put_automation_rule(
        self,
        definition: RuleDefinition | object,
        *,
        rule_id: str | None = None,
        expected_version: int | None = None,
        expected_account_identity: str | None = None,
        now: datetime | None = None,
    ) -> AutomationRuleDetail:
        parsed = parse_rule_definition(
            definition.model_dump(mode="python", exclude_defaults=False)
            if isinstance(definition, RuleDefinition)
            else definition
        )
        encoded = canonical_rule_definition(parsed)
        digest = hashlib.sha256(encoded).hexdigest()
        stamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        identity = rule_id or str(uuid.uuid4())
        if not _valid_uuid_v4(identity):
            raise ValueError("automation rule id must be a lower-case UUIDv4")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM automation_rules WHERE rule_id = ?", (identity,)
            ).fetchone()
            scope_identity = self._automation_rule_scope_identity(
                db, parsed, expected_account_identity
            )
            if existing is None:
                if expected_version is not None:
                    raise AutomationRuleNotFound(identity)
                count = db.execute(
                    "SELECT COUNT(*) AS count FROM automation_rules WHERE deleted = 0"
                ).fetchone()
                if count is None or int(count["count"]) >= MAX_AUTOMATION_RULES:
                    raise AutomationRuleLimitExceeded("automation rule limit exceeded")
                version = 1
                version_enabled = 1
                global_revision = self._next_automation_rules_revision(db)
                db.execute(
                    """INSERT INTO automation_rules(
                        rule_id, current_version, enabled, system, deleted,
                        created_at, updated_at
                    ) VALUES (?, 1, 1, 0, 0, ?, ?)""",
                    (identity, stamp, stamp),
                )
            else:
                if bool(existing["deleted"]):
                    raise AutomationRuleNotFound(identity)
                current_version = int(existing["current_version"])
                if expected_version is None or expected_version != current_version:
                    raise AutomationRuleStale("automation rule version is stale")
                if bool(existing["system"]):
                    raise AutomationRuleSystemProtected("system automation rule is protected")
                version = current_version + 1
                version_enabled = int(existing["enabled"])
                global_revision = self._next_automation_rules_revision(db)
                changed = db.execute(
                    """UPDATE automation_rules
                    SET current_version = ?, updated_at = ?
                    WHERE rule_id = ? AND current_version = ? AND deleted = 0""",
                    (version, stamp, identity, current_version),
                )
                if changed.rowcount != 1:
                    raise AutomationRuleStale("automation rule version is stale")
            db.execute(
                """INSERT INTO automation_rule_versions(
                    rule_id, version, kind, definition_json, definition_sha256,
                    enabled, scope_mailbox_identity_key, global_revision, accepted_at
                ) VALUES (?, ?, 'definition', ?, ?, ?, ?, ?, ?)""",
                (
                    identity,
                    version,
                    encoded,
                    digest,
                    version_enabled,
                    scope_identity,
                    global_revision,
                    stamp,
                ),
            )
        return self.automation_rule(identity)

    def set_automation_rule_enabled(
        self,
        rule_id: str,
        expected_version: int,
        enabled: bool,
        *,
        now: datetime | None = None,
    ) -> AutomationRuleDetail:
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        stamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT r.*, v.kind, v.definition_json, v.definition_sha256,
                    v.scope_mailbox_identity_key
                FROM automation_rules AS r
                JOIN automation_rule_versions AS v
                  ON v.rule_id = r.rule_id AND v.version = r.current_version
                WHERE r.rule_id = ?""",
                (rule_id,),
            ).fetchone()
            if row is None or bool(row["deleted"]):
                raise AutomationRuleNotFound(rule_id)
            current_version = int(row["current_version"])
            if expected_version != current_version:
                raise AutomationRuleStale("automation rule version is stale")
            if bool(row["system"]):
                raise AutomationRuleSystemProtected("system automation rule is protected")
            if str(row["kind"]) != "definition":
                raise RuntimeError("live automation rule is not a definition")
            encoded = bytes(row["definition_json"])
            if hashlib.sha256(encoded).hexdigest() != str(row["definition_sha256"]):
                raise RuleValidationError("stored definition digest does not match")
            definition = parse_rule_definition(json.loads(encoded))
            if canonical_rule_definition(definition) != encoded:
                raise RuleValidationError("stored definition is not canonical")
            scope_identity = (
                str(row["scope_mailbox_identity_key"])
                if row["scope_mailbox_identity_key"] is not None
                else None
            )
            if bool(row["enabled"]) == enabled:
                return self._automation_rule_detail(row)
            version = current_version + 1
            global_revision = self._next_automation_rules_revision(db)
            changed = db.execute(
                """UPDATE automation_rules SET current_version = ?, enabled = ?, updated_at = ?
                WHERE rule_id = ? AND current_version = ? AND deleted = 0""",
                (version, int(enabled), stamp, rule_id, current_version),
            )
            if changed.rowcount != 1:
                raise AutomationRuleStale("automation rule version is stale")
            db.execute(
                """INSERT INTO automation_rule_versions(
                    rule_id, version, kind, definition_json, definition_sha256,
                    enabled, scope_mailbox_identity_key, global_revision, accepted_at
                ) VALUES (?, ?, 'definition', ?, ?, ?, ?, ?, ?)""",
                (
                    rule_id,
                    version,
                    encoded,
                    str(row["definition_sha256"]),
                    int(enabled),
                    scope_identity,
                    global_revision,
                    stamp,
                ),
            )
        return self.automation_rule(rule_id)

    def delete_automation_rule(
        self,
        rule_id: str,
        expected_version: int,
        *,
        now: datetime | None = None,
    ) -> int:
        stamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM automation_rules WHERE rule_id = ?", (rule_id,)
            ).fetchone()
            if row is None or bool(row["deleted"]):
                raise AutomationRuleNotFound(rule_id)
            current_version = int(row["current_version"])
            if expected_version != current_version:
                raise AutomationRuleStale("automation rule version is stale")
            if bool(row["system"]):
                raise AutomationRuleSystemProtected("system automation rule is protected")
            version = current_version + 1
            global_revision = self._next_automation_rules_revision(db)
            changed = db.execute(
                """UPDATE automation_rules
                SET current_version = ?, enabled = 0, deleted = 1, updated_at = ?
                WHERE rule_id = ? AND current_version = ? AND deleted = 0""",
                (version, stamp, rule_id, current_version),
            )
            if changed.rowcount != 1:
                raise AutomationRuleStale("automation rule version is stale")
            db.execute(
                """INSERT INTO automation_rule_versions(
                    rule_id, version, kind, definition_json, definition_sha256,
                    enabled, scope_mailbox_identity_key, global_revision, accepted_at
                ) VALUES (?, ?, 'deleted', NULL, NULL, 0, NULL, ?, ?)""",
                (rule_id, version, global_revision, stamp),
            )
        return version

    @staticmethod
    def _current_match_rules(db: sqlite3.Connection) -> list[MatchRule]:
        rows = db.execute(
            """SELECT r.rule_id, r.current_version, r.created_at,
                v.definition_json, v.definition_sha256, v.scope_mailbox_identity_key
            FROM automation_rules AS r
            JOIN automation_rule_versions AS v
              ON v.rule_id = r.rule_id AND v.version = r.current_version
            WHERE r.deleted = 0 AND r.enabled = 1 AND v.kind = 'definition'
            ORDER BY r.created_at, r.rule_id"""
        ).fetchall()
        rules: list[MatchRule] = []
        for row in rows:
            try:
                encoded = bytes(row["definition_json"])
                if hashlib.sha256(encoded).hexdigest() != str(row["definition_sha256"]):
                    continue
                definition = parse_rule_definition(json.loads(encoded))
                if canonical_rule_definition(definition) != encoded:
                    continue
            except (
                json.JSONDecodeError,
                TypeError,
                UnicodeDecodeError,
                UnicodeEncodeError,
                RuleValidationError,
            ):
                continue
            rules.append(
                MatchRule(
                    rule_id=str(row["rule_id"]),
                    version=int(row["current_version"]),
                    scope_mailbox_identity_key=(
                        str(row["scope_mailbox_identity_key"])
                        if row["scope_mailbox_identity_key"] is not None
                        else None
                    ),
                    definition=definition,
                )
            )
        return rules

    def automation_fires_for_message(self, message_id: str) -> list[AutomationFire]:
        with self.connection() as db:
            rows = db.execute(
                """SELECT * FROM automation_fires
                WHERE message_id = ? ORDER BY created_at, fire_id""",
                (message_id,),
            ).fetchall()
        return [_automation_fire(row) for row in rows]

    def automation_fire_attempts(self, fire_id: str) -> list[AutomationFireAttempt]:
        with self.connection() as db:
            rows = db.execute(
                """SELECT * FROM automation_fire_attempts
                WHERE fire_id = ? ORDER BY attempt_no""",
                (fire_id,),
            ).fetchall()
        return [AutomationFireAttempt(**dict(row)) for row in rows]

    def automation_fire(self, fire_id: str) -> AutomationFire | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM automation_fires WHERE fire_id = ?", (fire_id,)
            ).fetchone()
        return _automation_fire(row) if row is not None else None

    def automation_fires_in_states(
        self,
        states: Sequence[str],
        *,
        limit: int = CONNECT_QUEUE_MAX_JOBS,
    ) -> tuple[AutomationFire, ...]:
        normalized = tuple(states)
        if (
            not normalized
            or any(state not in AUTOMATION_FIRE_STATES for state in normalized)
            or isinstance(limit, bool)
            or not 1 <= limit <= CONNECT_QUEUE_MAX_JOBS
        ):
            raise ValueError("Automation fire query boundary is invalid")
        placeholders = ",".join("?" for _ in normalized)
        with self.connection() as db:
            rows = db.execute(
                f"""SELECT * FROM automation_fires
                WHERE state IN ({placeholders})
                ORDER BY updated_at, fire_id LIMIT ?""",
                (*normalized, limit),
            ).fetchall()
        return tuple(_automation_fire(row) for row in rows)

    def automation_fire_definition(self, fire: AutomationFire) -> RuleDefinition:
        with self.connection() as db:
            row = db.execute(
                """SELECT definition_json, definition_sha256
                FROM automation_rule_versions
                WHERE rule_id = ? AND version = ? AND kind = 'definition'""",
                (fire.rule_id, fire.rule_version),
            ).fetchone()
        if row is None:
            raise RuntimeError("Automation fire rule definition is unavailable")
        encoded = bytes(row["definition_json"])
        if hashlib.sha256(encoded).hexdigest() != str(row["definition_sha256"]):
            raise RuntimeError("Automation fire rule definition failed integrity validation")
        try:
            definition = parse_rule_definition(json.loads(encoded))
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError, RuleValidationError) as exc:
            raise RuntimeError("Automation fire rule definition is invalid") from exc
        if canonical_rule_definition(definition) != encoded:
            raise RuntimeError("Automation fire rule definition is noncanonical")
        return definition

    def touch_automation_fire(
        self,
        *,
        fire_id: str,
        expected_state: str,
        expected_version: int,
        now: datetime | None = None,
    ) -> bool:
        if expected_state not in AUTOMATION_FIRE_STATES:
            raise ValueError("Automation fire state is invalid")
        if type(expected_version) is not int or not 1 <= expected_version <= 2**63 - 1:
            raise ValueError("Automation fire version is invalid")
        stamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self.connection() as db:
            changed = db.execute(
                """UPDATE automation_fires SET updated_at = ?
                WHERE fire_id = ? AND state = ? AND state_version = ?""",
                (stamp, fire_id, expected_state, expected_version),
            )
        return changed.rowcount == 1

    @staticmethod
    def automation_pending_seconds(fire: AutomationFire, *, now: datetime | None = None) -> int:
        seconds = fire.authorized_pending_seconds
        if fire.state != "pending_dispatch" or fire.pending_since is None:
            return seconds
        observed_at = (now or datetime.now(UTC)).astimezone(UTC)
        started_at = datetime.fromisoformat(fire.pending_since)
        if started_at.tzinfo is None:
            raise RuntimeError("Automation pending time is missing its timezone")
        return seconds + max(0, int((observed_at - started_at.astimezone(UTC)).total_seconds()))

    def transition_automation_fire(
        self,
        *,
        fire_id: str,
        expected_state: str,
        expected_version: int,
        next_state: str,
        reason: str | None = None,
        job_id: str | None = None,
        prepared_identity: dict[str, object] | None = None,
        confirmed: bool | None = None,
        now: datetime | None = None,
    ) -> AutomationFire:
        if (expected_state, next_state) not in AUTOMATION_FIRE_TRANSITIONS:
            raise ValueError(
                f"Invalid automation fire transition: {expected_state} -> {next_state}"
            )
        if isinstance(expected_version, bool) or expected_version < 1:
            raise ValueError("Automation fire version is invalid")
        if reason is not None and (not reason or len(reason.encode("utf-8")) > 128):
            raise ValueError("Automation fire reason is invalid")
        if job_id is not None and not _valid_uuid_v4(job_id):
            raise ValueError("Automation fire job identity is invalid")
        prepared_json: bytes | None = None
        prepared_sha256: str | None = None
        if prepared_identity is not None:
            prepared_json = json.dumps(
                prepared_identity,
                separators=(",", ":"),
                sort_keys=True,
                ensure_ascii=False,
            ).encode("utf-8")
            if not 2 <= len(prepared_json) <= MAX_AUTOMATION_PREPARED_IDENTITY_BYTES:
                raise ValueError("Automation prepared identity is invalid")
            prepared_sha256 = hashlib.sha256(prepared_json).hexdigest()
        observed_at = (now or datetime.now(UTC)).astimezone(UTC)
        stamp = observed_at.isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM automation_fires WHERE fire_id = ?", (fire_id,)
            ).fetchone()
            if row is None:
                raise KeyError(fire_id)
            current = _automation_fire(row)
            if current.state != expected_state or current.state_version != expected_version:
                raise RuntimeError("Automation fire transition lost its expected-state race")
            accumulated = (
                current.authorized_pending_seconds
                if current.state == "pending_dispatch" and next_state == "entitlement_paused"
                else self.automation_pending_seconds(current, now=observed_at)
            )
            next_job_id = job_id if job_id is not None else current.job_id
            next_prepared_json = (
                prepared_json if prepared_json is not None else current.prepared_identity_json
            )
            next_prepared_sha256 = (
                prepared_sha256 if prepared_sha256 is not None else current.prepared_identity_sha256
            )
            next_confirmed = current.confirmed if confirmed is None else confirmed
            if next_state == "submitted" and next_job_id is None:
                raise ValueError("Submitted automation fire requires a Connect job")
            if next_state == "awaiting_confirmation" and next_prepared_json is None:
                raise ValueError("Confirmation requires a prepared invocation identity")
            if next_state == "entitlement_paused" and next_job_id is not None:
                dispatch = db.execute(
                    "SELECT 1 FROM connect_job_dispatch WHERE job_id = ?",
                    (next_job_id,),
                ).fetchone()
                if dispatch is None:
                    raise RuntimeError("Automation entitlement pause lost its Connect job")
                if _connect_job_requires_automation_entitlement(db, next_job_id):
                    db.execute(
                        """UPDATE connect_job_dispatch
                        SET automation_paused_at = COALESCE(automation_paused_at, ?)
                        WHERE job_id = ?""",
                        (stamp, next_job_id),
                    )
            if next_state == "submitted":
                assert next_job_id is not None
                _resume_automation_dispatch(
                    db,
                    job_id=next_job_id,
                    observed_at=observed_at,
                    stamp=stamp,
                )
                attempt = db.execute(
                    """UPDATE automation_fire_attempts SET job_id = ?
                    WHERE fire_id = ? AND attempt_no = ? AND job_id IS NULL""",
                    (next_job_id, fire_id, current.current_attempt_no),
                )
                if attempt.rowcount != 1:
                    bound = db.execute(
                        """SELECT job_id FROM automation_fire_attempts
                        WHERE fire_id = ? AND attempt_no = ?""",
                        (fire_id, current.current_attempt_no),
                    ).fetchone()
                    if bound is None or bound["job_id"] != next_job_id:
                        raise RuntimeError("Automation fire attempt job binding conflicted")
            cursor = db.execute(
                """UPDATE automation_fires SET
                    state = ?, state_version = state_version + 1, reason = ?, job_id = ?,
                    prepared_identity_sha256 = ?, prepared_identity_json = ?, confirmed = ?,
                    pending_since = ?, authorized_pending_seconds = ?, updated_at = ?
                WHERE fire_id = ? AND state = ? AND state_version = ?""",
                (
                    next_state,
                    reason,
                    next_job_id,
                    next_prepared_sha256,
                    next_prepared_json,
                    int(next_confirmed),
                    stamp if next_state == "pending_dispatch" else None,
                    accumulated,
                    stamp,
                    fire_id,
                    expected_state,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Automation fire transition lost its expected-state race")
            if next_state in {"completed", "failed"} and next_job_id is not None:
                db.execute(
                    """DELETE FROM connect_attachment_jobs
                    WHERE job_id = ? AND status IN ('completed', 'failed')
                      AND job_id IN (
                        SELECT job_id FROM connect_job_dispatch WHERE source_available = 0
                      )
                      AND NOT EXISTS (
                        SELECT 1 FROM automation_fires
                        WHERE job_id = ? AND state IN ('submitted', 'entitlement_paused')
                      )""",
                    (next_job_id, next_job_id),
                )
            updated = db.execute(
                "SELECT * FROM automation_fires WHERE fire_id = ?", (fire_id,)
            ).fetchone()
        if updated is None:
            raise RuntimeError("Automation fire disappeared after transition")
        return _automation_fire(updated)

    def decide_automation_fire(
        self,
        *,
        fire_id: str,
        expected_version: int,
        prepared_identity_sha256: str,
        confirmed: bool,
        now: datetime | None = None,
    ) -> AutomationFire:
        if len(prepared_identity_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in prepared_identity_sha256
        ):
            raise ValueError("Automation confirmation identity is invalid")
        observed_at = (now or datetime.now(UTC)).astimezone(UTC)
        stamp = observed_at.isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM automation_fires WHERE fire_id = ?", (fire_id,)
            ).fetchone()
            if row is None:
                raise KeyError(fire_id)
            current = _automation_fire(row)
            if (
                current.state != "awaiting_confirmation"
                or current.state_version != expected_version
                or current.prepared_identity_sha256 != prepared_identity_sha256
            ):
                raise RuntimeError("Automation confirmation lost its expected-state race")
            next_state = "pending_dispatch" if confirmed else "declined"
            if confirmed:
                db.execute(
                    """INSERT INTO automation_fire_confirmations(
                        fire_id, prepared_identity_sha256, confirmed_at
                    ) VALUES (?, ?, ?)""",
                    (fire_id, prepared_identity_sha256, stamp),
                )
            cursor = db.execute(
                """UPDATE automation_fires SET state = ?, state_version = state_version + 1,
                    reason = ?, confirmed = ?, pending_since = ?, updated_at = ?
                WHERE fire_id = ? AND state = 'awaiting_confirmation' AND state_version = ?""",
                (
                    next_state,
                    None if confirmed else "declined_by_user",
                    int(confirmed),
                    stamp if confirmed else None,
                    stamp,
                    fire_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Automation confirmation lost its expected-state race")
            updated = db.execute(
                "SELECT * FROM automation_fires WHERE fire_id = ?", (fire_id,)
            ).fetchone()
        if updated is None:
            raise RuntimeError("Automation fire disappeared after confirmation")
        return _automation_fire(updated)

    def automation_confirmation_matches(self, fire: AutomationFire) -> bool:
        if fire.prepared_identity_sha256 is None or not fire.confirmed:
            return False
        with self.connection() as db:
            row = db.execute(
                """SELECT 1 FROM automation_fire_confirmations
                WHERE fire_id = ? AND prepared_identity_sha256 = ?""",
                (fire.fire_id, fire.prepared_identity_sha256),
            ).fetchone()
        return row is not None

    def automation_fires_linked_to_job(self, job_id: str) -> tuple[AutomationFire, ...]:
        with self.connection() as db:
            rows = db.execute(
                """SELECT * FROM automation_fires
                WHERE job_id = ? AND state IN ('submitted', 'entitlement_paused')
                ORDER BY created_at, fire_id""",
                (job_id,),
            ).fetchall()
        return tuple(_automation_fire(row) for row in rows)

    def pause_interactive_job_automation_fires(
        self,
        job_id: str,
        *,
        now: datetime | None = None,
    ) -> int:
        stamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute(
                """SELECT dispatch.interactive_authorized_at
                FROM connect_attachment_jobs AS job
                JOIN connect_job_dispatch AS dispatch ON dispatch.job_id = job.job_id
                WHERE job.job_id = ?
                  AND job.status IN ('requested', 'accepted', 'processing')""",
                (job_id,),
            ).fetchone()
            if job is None:
                raise RuntimeError("Only active Connect jobs can pause joined automation fires")
            automation_origin = db.execute(
                """SELECT 1 FROM automation_fire_attempts
                WHERE dispatch_request_id = ? LIMIT 1""",
                (job_id,),
            ).fetchone()
            if automation_origin is not None and job["interactive_authorized_at"] is None:
                raise RuntimeError(
                    "Automation-origin jobs require durable interactive authority"
                )
            return _pause_linked_automation_fires(db, job_id=job_id, stamp=stamp)

    def authorize_connect_job_interactively(
        self,
        job_id: str,
        *,
        now: datetime | None = None,
    ) -> ConnectJob:
        observed_at = (now or datetime.now(UTC)).astimezone(UTC)
        stamp = observed_at.isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT job.* FROM connect_attachment_jobs AS job
                JOIN connect_job_dispatch AS dispatch ON dispatch.job_id = job.job_id
                WHERE job.job_id = ? AND job.protocol_version = 2""",
                (job_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("Only persisted Connect v2 jobs can be authorized interactively")
            current = self._connect_job(row)
            if current.status in {"completed", "failed"}:
                return current
            if current.status not in {"requested", "accepted", "processing"}:
                raise RuntimeError("Only active Connect v2 jobs can be authorized interactively")
            authorized = db.execute(
                """UPDATE connect_job_dispatch
                SET interactive_authorized_at = COALESCE(interactive_authorized_at, ?)
                WHERE job_id = ?""",
                (stamp, job_id),
            )
            if authorized.rowcount != 1:
                raise RuntimeError("Only active Connect v2 jobs can be authorized interactively")
            _resume_automation_dispatch(
                db,
                job_id=job_id,
                observed_at=observed_at,
                stamp=stamp,
            )
            updated = db.execute(
                "SELECT * FROM connect_attachment_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if updated is None:
                raise RuntimeError("Connect v2 job disappeared after interactive authorization")
            return self._connect_job(updated)

    def connect_job_requires_automation_entitlement(self, job_id: str) -> bool:
        with self.connection() as db:
            return _connect_job_requires_automation_entitlement(db, job_id)

    def automation_fire_settlement_due(self) -> bool:
        with self.connection() as db:
            row = db.execute(
                """SELECT 1
                FROM automation_fires AS fire
                LEFT JOIN connect_attachment_jobs AS job ON job.job_id = fire.job_id
                LEFT JOIN connect_job_dispatch AS dispatch ON dispatch.job_id = fire.job_id
                WHERE fire.state IN ('submitted', 'entitlement_paused')
                  AND fire.job_id IS NOT NULL
                  AND (
                    job.job_id IS NULL
                    OR (
                      job.status IN ('completed', 'failed')
                      AND NOT (
                        fire.state = 'entitlement_paused'
                        AND (
                          dispatch.interactive_authorized_at IS NOT NULL
                          OR NOT EXISTS (
                            SELECT 1 FROM automation_fire_attempts AS attempt
                            WHERE attempt.dispatch_request_id = fire.job_id
                          )
                        )
                      )
                      AND NOT (
                        job.status = 'failed'
                        AND
                        fire.state = 'entitlement_paused'
                        AND job.error_code = 'connect_queue_deadline_exceeded'
                      )
                    )
                  )
                LIMIT 1"""
            ).fetchone()
        return row is not None

    def resume_automation_fire_job(
        self,
        *,
        fire_id: str,
        expected_version: int,
        now: datetime | None = None,
    ) -> AutomationFire:
        observed_at = (now or datetime.now(UTC)).astimezone(UTC)
        stamp = observed_at.isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM automation_fires WHERE fire_id = ?", (fire_id,)
            ).fetchone()
            if row is None:
                raise KeyError(fire_id)
            current = _automation_fire(row)
            if (
                current.state != "entitlement_paused"
                or current.state_version != expected_version
                or current.job_id is None
            ):
                raise RuntimeError("Automation entitlement resume lost its expected-state race")
            _resume_automation_dispatch(
                db,
                job_id=current.job_id,
                observed_at=observed_at,
                stamp=stamp,
            )
            cursor = db.execute(
                """UPDATE automation_fires SET state = 'submitted',
                    state_version = state_version + 1, reason = NULL, updated_at = ?
                WHERE fire_id = ? AND state = 'entitlement_paused' AND state_version = ?""",
                (stamp, fire_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Automation entitlement resume lost its expected-state race")
            updated = db.execute(
                "SELECT * FROM automation_fires WHERE fire_id = ?", (fire_id,)
            ).fetchone()
        if updated is None:
            raise RuntimeError("Automation fire disappeared after entitlement resume")
        return _automation_fire(updated)

    def retry_automation_fire_after_deadline(
        self,
        *,
        fire_id: str,
        expected_version: int,
        now: datetime | None = None,
    ) -> AutomationFire:
        observed_at = (now or datetime.now(UTC)).astimezone(UTC)
        stamp = observed_at.isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT f.*, j.status AS linked_status, j.error_code AS linked_error_code,
                    d.highest_provider_state AS linked_highest_provider_state
                FROM automation_fires AS f
                JOIN connect_attachment_jobs AS j ON j.job_id = f.job_id
                JOIN connect_job_dispatch AS d ON d.job_id = j.job_id
                WHERE f.fire_id = ?""",
                (fire_id,),
            ).fetchone()
            if row is None:
                raise KeyError(fire_id)
            current = _automation_fire(row)
            if (
                current.state != "submitted"
                or current.state_version != expected_version
                or current.current_attempt_no != 1
                or row["linked_status"] != "failed"
                or row["linked_error_code"] != "connect_queue_deadline_exceeded"
                or row["linked_highest_provider_state"] != "requested"
            ):
                raise RuntimeError("Automation retry lacks proof of unaccepted provider work")
            dispatch_request_id = str(uuid.uuid4())
            db.execute(
                """INSERT INTO automation_fire_attempts(
                    fire_id, attempt_no, dispatch_request_id, job_id, created_at
                ) VALUES (?, 2, ?, NULL, ?)""",
                (fire_id, dispatch_request_id, stamp),
            )
            cursor = db.execute(
                """UPDATE automation_fires SET state = 'pending_dispatch',
                    state_version = state_version + 1, reason = 'first_admission_deadline',
                    current_attempt_no = 2, job_id = NULL, confirmed = 0,
                    prepared_identity_sha256 = NULL, prepared_identity_json = NULL,
                    pending_since = ?, authorized_pending_seconds = 0, updated_at = ?
                WHERE fire_id = ? AND state = 'submitted' AND state_version = ?""",
                (stamp, stamp, fire_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Automation retry lost its expected-state race")
            db.execute(
                "DELETE FROM automation_fire_confirmations WHERE fire_id = ?",
                (fire_id,),
            )
            updated = db.execute(
                "SELECT * FROM automation_fires WHERE fire_id = ?", (fire_id,)
            ).fetchone()
        if updated is None:
            raise RuntimeError("Automation fire disappeared after retry")
        return _automation_fire(updated)

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

    def migrate_calendar_principal_references(
        self,
        account_id: str,
        legacy_principal_keys: Iterable[str],
        current_principal_key: str,
    ) -> int:
        if not account_id:
            raise ValueError("calendar account ID is required")
        keys = set(legacy_principal_keys)
        if any(
            len(key) != 64 or any(character not in "0123456789abcdef" for character in key)
            for key in keys | {current_principal_key}
        ):
            raise ValueError("calendar principal keys must be SHA-256 hex digests")
        migrations = {
            (account_id, legacy_key): current_principal_key
            for legacy_key in keys
            if legacy_key != current_principal_key
        }
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            _rewrite_microsoft_principal_keys(db, migrations)
            row = db.execute(
                """SELECT COUNT(*) FROM automation_runs
                WHERE provider = 'microsoft365' AND account_id = ?
                    AND calendar_principal_key <> ?
                    AND state IN (
                        'detected', 'extracting', 'proposing', 'awaiting_confirmation',
                        'write_authorized', 'writing', 'unresolved', 'reconciling'
                    )""",
                (account_id, current_principal_key),
            ).fetchone()
        return int(row[0])

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

    @staticmethod
    def _calendar_event_rows(rows: Iterable[sqlite3.Row]) -> list[CalendarEventProjection]:
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
        return self._calendar_event_rows(rows)

    def calendar_projection(
        self,
        account_id: str,
    ) -> tuple[CalendarWindow | None, list[CalendarEventProjection]]:
        """Read one completed calendar window and its events from one snapshot."""
        with self.connection() as db:
            db.execute("BEGIN")
            window_row = db.execute(
                """SELECT account_id, principal_key, window_start, window_end,
                    cursor, updated_at
                FROM microsoft_calendar_windows WHERE account_id = ?""",
                (account_id,),
            ).fetchone()
            event_rows = db.execute(
                """SELECT event_id, subject, start_date_time, start_time_zone,
                    end_date_time, end_time_zone, is_all_day, location
                FROM microsoft_calendar_events
                WHERE account_id = ?
                ORDER BY start_date_time, event_id""",
                (account_id,),
            ).fetchall()
        window = CalendarWindow(**dict(window_row)) if window_row is not None else None
        return window, self._calendar_event_rows(event_rows)

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
        mailbox_identity_key: str | None = None,
    ) -> None:
        stamp = (at or datetime.now(UTC)).isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if mailbox_identity_key is not None:
                current = db.execute(
                    """SELECT mailbox_identity_key FROM mail_accounts
                    WHERE provider = ? AND account_id = ?""",
                    (provider, account_id),
                ).fetchone()
                if current is None or current["mailbox_identity_key"] != mailbox_identity_key:
                    raise MailboxIdentityChanged("mailbox identity changed")
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

    def has_unexpired_legacy_mailbox_markers(
        self,
        provider: str,
        account_id: str,
        *,
        retention_cutoff: datetime,
        now: datetime | None = None,
    ) -> bool:
        cutoff = retention_cutoff.astimezone(UTC)
        observed_at = (now or datetime.now(UTC)).astimezone(UTC)
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        cutoff_epoch = (cutoff - epoch).total_seconds()
        observed_epoch = (observed_at - epoch).total_seconds()
        with self.connection() as db:
            return (
                db.execute(
                    """SELECT 1 FROM messages
                    WHERE provider = ?1 AND account_id = ?2
                      AND mailbox_identity_key IS NULL
                      AND (
                          aware_iso_epoch(received_at) IS NULL
                          OR aware_iso_epoch(received_at) >= ?3
                      )
                    UNION ALL
                    SELECT 1 FROM legacy_mailbox_markers
                    WHERE provider = ?1 AND account_id = ?2
                      AND (
                          aware_iso_epoch(expires_at) IS NULL
                          OR aware_iso_epoch(expires_at) >= ?4
                      )
                    LIMIT 1""",
                    (provider, account_id, cutoff_epoch, observed_epoch),
                ).fetchone()
                is not None
            )

    def message_source(self, message_id: str) -> MessageSource:
        with self.connection() as db:
            row = db.execute(
                """SELECT message_id, provider, account_id, provider_message_id,
                    received_at, discovered_at, mailbox_identity_key
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
        mailbox_identity_key: str | None = None,
    ) -> bool:
        message_key = _message_suppression_key(
            provider, account_id, provider_message_id, mailbox_identity_key
        )
        legacy_message_key = _legacy_message_suppression_key(provider_message_id)
        with self.connection() as db:
            if mailbox_identity_key is not None:
                return (
                    db.execute(
                        """SELECT 1 FROM messages
                        WHERE provider = ?1 AND account_id = ?2
                          AND mailbox_identity_key = ?3 AND provider_message_id = ?4
                        UNION ALL
                        SELECT 1 FROM suppressed_messages
                        WHERE provider = ?1 AND account_id = ?2 AND message_key = ?5
                        UNION ALL
                        SELECT 1 FROM suppressed_messages
                        WHERE provider = ?1 AND account_id = ?2 AND message_key IN (?6, ?7)
                          AND EXISTS (
                              SELECT 1 FROM mail_accounts
                              WHERE provider = ?1 AND account_id = ?2
                                AND legacy_identity_status = 'continuity_proven'
                                AND legacy_identity_key = ?3
                          )
                        LIMIT 1""",
                        (
                            provider,
                            account_id,
                            mailbox_identity_key,
                            provider_message_id,
                            message_key,
                            _message_suppression_key(provider, account_id, provider_message_id),
                            legacy_message_key,
                        ),
                    ).fetchone()
                    is not None
                )
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

    @staticmethod
    def _insert_message_in_transaction(
        db: sqlite3.Connection,
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
        mailbox_identity_key: str | None = None,
        admission: AdmissionProvenance,
        discovered_at: str,
    ) -> bool:
        if mailbox_identity_key is None:
            raise ValueError("mailbox identity key is required")
        _validate_admission_provenance(
            admission,
            mailbox_identity_key=mailbox_identity_key,
        )
        source_message_id = provider_message_id or message_id
        message_key = _message_suppression_key(
            provider, account_id, source_message_id, mailbox_identity_key
        )
        account = db.execute(
            """SELECT mailbox_identity_key, legacy_identity_status, legacy_identity_key
            FROM mail_accounts WHERE provider = ? AND account_id = ?""",
            (provider, account_id),
        ).fetchone()
        if account is None or account["mailbox_identity_key"] != mailbox_identity_key:
            raise MailboxIdentityChanged("mailbox identity changed")
        if admission.kind == "gmail_user_label" and (
            db.execute(
                """SELECT 1 FROM gmail_label_selectors AS l
                JOIN gmail_label_selector_sets AS s
                  ON s.provider = l.provider AND s.account_id = l.account_id
                WHERE l.provider = ? AND l.account_id = ?
                  AND l.mailbox_identity_key = ? AND l.selector_id = ?
                  AND s.current_mailbox_identity_key = l.mailbox_identity_key""",
                (provider, account_id, mailbox_identity_key, admission.selector_id),
            ).fetchone()
            is None
        ):
            raise GmailLabelStoreError("gmail_recovery_grant_revoked")
        suppression_keys = [message_key]
        if (
            account["legacy_identity_status"] == "continuity_proven"
            and account["legacy_identity_key"] == mailbox_identity_key
        ):
            suppression_keys.extend(
                (
                    _message_suppression_key(provider, account_id, source_message_id),
                    _legacy_message_suppression_key(source_message_id),
                )
            )
        placeholders = ", ".join("?" for _ in suppression_keys)
        cursor = db.execute(
            f"""INSERT OR IGNORE INTO messages(
                message_id, provider, account_id, mailbox_identity_key, provider_message_id,
                thread_id, sender, sender_name, subject, received_at, discovered_at,
                admission_kind, admission_selector_id, admission_display_name,
                admission_mailbox_identity_key, admitted_at
            ) SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            WHERE NOT EXISTS (
                SELECT 1 FROM suppressed_messages
                WHERE provider = ? AND account_id = ?
                  AND message_key IN ({placeholders})
            )""",
            (
                message_id,
                provider,
                account_id,
                mailbox_identity_key,
                source_message_id,
                thread_id,
                sender,
                sender_name,
                subject,
                received_at,
                discovered_at,
                admission.kind,
                admission.selector_id,
                admission.display_name,
                admission.mailbox_identity_key,
                admission.admitted_at,
                provider,
                account_id,
                *suppression_keys,
            ),
        )
        return cursor.rowcount == 1

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
        mailbox_identity_key: str | None = None,
        admission: AdmissionProvenance,
    ) -> bool:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._insert_message_in_transaction(
                db,
                message_id=message_id,
                provider=provider,
                account_id=account_id,
                provider_message_id=provider_message_id,
                mailbox_identity_key=mailbox_identity_key,
                thread_id=thread_id,
                sender=sender,
                sender_name=sender_name,
                subject=subject,
                received_at=received_at,
                admission=admission,
                discovered_at=datetime.now(UTC).isoformat(),
            )

    def delete_message(self, message_id: str, *, now: datetime | None = None) -> bool:
        stamp = (now or datetime.now(UTC)).astimezone(UTC)
        with self._source_cleanup_locks((message_id,)), self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT provider, account_id, mailbox_identity_key,
                    provider_message_id, received_at
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
                        (
                            str(row["mailbox_identity_key"])
                            if row["mailbox_identity_key"] is not None
                            else None
                        ),
                    ),
                    _suppression_expiry(str(row["received_at"]), stamp),
                ),
            )
            _mark_automation_sources_unavailable(
                db,
                [message_id],
                updated_at=stamp.isoformat(),
            )
            cursor = db.execute("DELETE FROM messages WHERE message_id = ?", (message_id,))
            _purge_expired_automation_tombstones(db, now=stamp.isoformat())
        return cursor.rowcount == 1

    def clear_messages(self, *, now: datetime | None = None) -> int:
        stamp = (now or datetime.now(UTC)).astimezone(UTC)
        with self.connection() as db:
            message_ids = [
                str(row["message_id"])
                for row in db.execute(
                    "SELECT message_id FROM messages ORDER BY message_id"
                ).fetchall()
            ]
        deleted = 0
        for offset in range(0, len(message_ids), SOURCE_CLEANUP_LOCK_BATCH_SIZE):
            chunk = message_ids[offset : offset + SOURCE_CLEANUP_LOCK_BATCH_SIZE]
            with self._source_cleanup_locks(chunk), self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                placeholders = ", ".join("?" for _ in chunk)
                rows = db.execute(
                    f"""SELECT message_id, provider, account_id, mailbox_identity_key,
                        provider_message_id, received_at FROM messages
                        WHERE message_id IN ({placeholders})""",
                    tuple(chunk),
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
                                (
                                    str(row["mailbox_identity_key"])
                                    if row["mailbox_identity_key"] is not None
                                    else None
                                ),
                            ),
                            _suppression_expiry(str(row["received_at"]), stamp),
                        )
                        for row in rows
                    ],
                )
                current_ids = [str(row["message_id"]) for row in rows]
                _mark_automation_sources_unavailable(
                    db,
                    current_ids,
                    updated_at=stamp.isoformat(),
                )
                if current_ids:
                    current_placeholders = ", ".join("?" for _ in current_ids)
                    cursor = db.execute(
                        f"DELETE FROM messages WHERE message_id IN ({current_placeholders})",
                        tuple(current_ids),
                    )
                    deleted += cursor.rowcount
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            _purge_expired_automation_tombstones(db, now=stamp.isoformat())
        return deleted

    def replace_attachments(
        self, message_id: str, attachments: Iterable[AttachmentDescriptor]
    ) -> None:
        items = tuple(attachments)
        with self._source_cleanup_locks((message_id,)), self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if (
                db.execute("SELECT 1 FROM messages WHERE message_id = ?", (message_id,)).fetchone()
                is None
            ):
                raise KeyError(message_id)
            db.execute("DELETE FROM message_attachments WHERE message_id = ?", (message_id,))
            db.executemany(
                """INSERT INTO message_attachments(
                        message_id, part_id, attachment_id, filename,
                        media_type, byte_size, position, byte_size_known
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        message_id,
                        item.part_id,
                        item.attachment_id,
                        item.filename,
                        item.media_type,
                        item.byte_size,
                        item.position,
                        int(item.byte_size_known),
                    )
                    for item in items
                ],
            )

    def attachment(self, message_id: str, part_id: str) -> AttachmentDescriptor:
        with self.connection() as db:
            row = db.execute(
                """SELECT part_id, attachment_id, filename, media_type, byte_size, position,
                    byte_size_known
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
            byte_size_known=bool(row["byte_size_known"]),
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
    def _connect_dispatch(row: sqlite3.Row) -> ConnectDispatch:
        values = dict(row)
        values["submission_possible"] = bool(values["submission_possible"])
        values["source_available"] = bool(values["source_available"])
        values["capability_external_effects"] = bool(
            values["capability_external_effects"]
        )
        values["capability_confirmation_required"] = bool(
            values["capability_confirmation_required"]
        )
        values["capability_authority_known"] = bool(values["capability_authority_known"])
        values["capability_produces"] = _decode_connect_capability_produces(
            values.pop("capability_produces_json")
        )
        return ConnectDispatch(**values)

    def connect_dispatch(self, job_id: str) -> ConnectDispatch | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM connect_job_dispatch WHERE job_id = ?", (job_id,)
            ).fetchone()
        return self._connect_dispatch(row) if row else None

    def connect_queue_ahead(self, job_id: str) -> int:
        with self.connection() as db:
            row = db.execute(
                """WITH ranked AS (
                    SELECT j.job_id,
                        ROW_NUMBER() OVER (
                            PARTITION BY j.provider_app_id, j.provider_instance_id
                            ORDER BY CASE
                                WHEN d.state = 'dispatching' THEN 0
                                WHEN d.state IN ('reconciling', 'provider_owned') THEN 1
                                ELSE 2 END,
                                j.created_at, j.job_id
                        ) AS lane_rank
                    FROM connect_attachment_jobs AS j
                    JOIN connect_job_dispatch AS d ON d.job_id = j.job_id
                    WHERE j.protocol_version = 2
                      AND j.status IN ('requested', 'accepted', 'processing')
                      AND d.state IN (
                          'waiting', 'dispatching', 'reconciling', 'provider_owned'
                      )
                      AND (
                          d.automation_paused_at IS NULL
                          OR d.state IN ('dispatching', 'reconciling', 'provider_owned')
                      )
                )
                SELECT lane_rank - 1 AS queue_ahead
                FROM ranked WHERE job_id = ?""",
                (job_id,),
            ).fetchone()
        return int(row["queue_ahead"]) if row else 0

    def connect_queue_wakeups(
        self, *, now: datetime | None = None
    ) -> tuple[tuple[str, datetime], ...]:
        observed_at = (now or datetime.now(UTC)).astimezone(UTC)
        with self.connection() as db:
            head_rows = db.execute(
                """WITH ranked AS (
                    SELECT j.job_id,
                        ROW_NUMBER() OVER (
                            PARTITION BY j.provider_app_id, j.provider_instance_id
                            ORDER BY CASE
                                WHEN d.state = 'dispatching' THEN 0
                                WHEN d.state IN ('reconciling', 'provider_owned') THEN 1
                                ELSE 2 END,
                                j.created_at, j.job_id
                        ) AS lane_rank,
                        d.state, d.next_attempt_at, d.automation_paused_at
                    FROM connect_attachment_jobs AS j
                    JOIN connect_job_dispatch AS d ON d.job_id = j.job_id
                    WHERE j.protocol_version = 2
                      AND j.status IN ('requested', 'accepted', 'processing')
                      AND d.state IN (
                          'waiting', 'dispatching', 'reconciling', 'provider_owned'
                      )
                      AND (
                          d.automation_paused_at IS NULL
                          OR d.state IN ('dispatching', 'reconciling', 'provider_owned')
                      )
                )
                SELECT job_id, state, next_attempt_at FROM ranked
                WHERE lane_rank = 1
                  AND (
                      automation_paused_at IS NULL
                      OR state IN ('dispatching', 'reconciling', 'provider_owned')
                  )"""
            ).fetchall()
            deadline_rows = db.execute(
                """SELECT j.job_id, d.admission_deadline
                FROM connect_attachment_jobs AS j
                JOIN connect_job_dispatch AS d ON d.job_id = j.job_id
                WHERE j.protocol_version = 2
                  AND j.status = 'requested'
                  AND d.state = 'waiting'
                  AND d.submission_possible = 0
                  AND d.automation_paused_at IS NULL"""
            ).fetchall()

        candidates: list[tuple[str, datetime]] = []
        for row in head_rows:
            value = row["next_attempt_at"]
            if row["state"] == "dispatching" or value is None:
                candidates.append((str(row["job_id"]), observed_at))
                continue
            candidate = datetime.fromisoformat(str(value))
            if candidate.tzinfo is None:
                raise RuntimeError("Connect queue wakeup time is missing its timezone")
            candidates.append((str(row["job_id"]), candidate.astimezone(UTC)))
        for row in deadline_rows:
            candidate = datetime.fromisoformat(str(row["admission_deadline"]))
            if candidate.tzinfo is None:
                raise RuntimeError("Connect queue deadline is missing its timezone")
            candidates.append((str(row["job_id"]), candidate.astimezone(UTC)))
        return tuple(candidates)

    def due_connect_lane_heads(
        self,
        *,
        now: datetime | None = None,
        limit: int = CONNECT_QUEUE_MAX_JOBS,
    ) -> tuple[ConnectJob, ...]:
        if isinstance(limit, bool) or not 1 <= limit <= CONNECT_QUEUE_MAX_JOBS:
            raise ValueError(
                f"Connect queue pump limit must be between 1 and {CONNECT_QUEUE_MAX_JOBS}"
            )
        stamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._expire_waiting_connect_jobs_transaction(db, stamp)
            rows = db.execute(
                """WITH ranked AS (
                    SELECT j.job_id,
                        ROW_NUMBER() OVER (
                            PARTITION BY j.provider_app_id, j.provider_instance_id
                            ORDER BY CASE
                                WHEN d.state = 'dispatching' THEN 0
                                WHEN d.state IN ('reconciling', 'provider_owned') THEN 1
                                ELSE 2 END,
                                j.created_at, j.job_id
                        ) AS lane_rank,
                        d.state,
                        d.next_attempt_at,
                        d.automation_paused_at
                    FROM connect_attachment_jobs AS j
                    JOIN connect_job_dispatch AS d ON d.job_id = j.job_id
                    WHERE j.protocol_version = 2
                      AND j.status IN ('requested', 'accepted', 'processing')
                      AND d.state IN (
                          'waiting', 'dispatching', 'reconciling', 'provider_owned'
                      )
                      AND (
                          d.automation_paused_at IS NULL
                          OR d.state IN ('dispatching', 'reconciling', 'provider_owned')
                      )
                )
                SELECT j.*
                FROM ranked AS r
                JOIN connect_attachment_jobs AS j ON j.job_id = r.job_id
                WHERE r.lane_rank = 1
                  AND (
                      r.automation_paused_at IS NULL
                      OR r.state IN ('dispatching', 'reconciling', 'provider_owned')
                  )
                  AND (
                      r.state = 'dispatching'
                      OR r.next_attempt_at IS NULL
                      OR aware_iso_epoch(r.next_attempt_at) <= aware_iso_epoch(?)
                  )
                ORDER BY j.created_at, j.job_id
                LIMIT ?""",
                (stamp, limit),
            ).fetchall()
        return tuple(self._connect_job(row) for row in rows)

    def defer_connect_job(
        self,
        *,
        job_id: str,
        expected_dispatch_state: str,
        next_dispatch_state: str,
        error_code: str,
        error_message: str,
        delay_seconds: int,
        pause_automation_deadline: bool = False,
        pause_linked_automation_fires: bool = False,
        now: datetime | None = None,
    ) -> ConnectDispatch:
        if expected_dispatch_state not in {
            "dispatching",
            "reconciling",
            "provider_owned",
        }:
            raise ValueError("Connect retry expected state is invalid")
        if next_dispatch_state not in {"waiting", "reconciling", "provider_owned"}:
            raise ValueError("Connect retry next state is invalid")
        if isinstance(delay_seconds, bool) or not 0 <= delay_seconds <= 30:
            raise ValueError("Connect retry delay must be between 0 and 30 seconds")
        if type(pause_automation_deadline) is not bool:
            raise ValueError("Connect automation pause flag is invalid")
        if type(pause_linked_automation_fires) is not bool:
            raise ValueError("Connect linked automation pause flag is invalid")
        if pause_linked_automation_fires and not pause_automation_deadline:
            raise ValueError("Linked automation fires require a paused dispatch deadline")
        code = error_code.encode("utf-8")[:MAX_CONNECT_DISPATCH_ERROR_CODE_BYTES].decode(
            "utf-8", errors="ignore"
        )
        message = error_message.encode("utf-8")[:MAX_CONNECT_DISPATCH_ERROR_MESSAGE_BYTES].decode(
            "utf-8", errors="ignore"
        )
        if not code or not message:
            raise ValueError("Connect retry diagnostics cannot be empty")
        observed_at = (now or datetime.now(UTC)).astimezone(UTC)
        stamp = observed_at.isoformat()
        next_attempt_at = (observed_at + timedelta(seconds=delay_seconds)).isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT j.status, d.*
                FROM connect_attachment_jobs AS j
                JOIN connect_job_dispatch AS d ON d.job_id = j.job_id
                WHERE j.job_id = ?""",
                (job_id,),
            ).fetchone()
            if row is None or row["state"] != expected_dispatch_state:
                raise RuntimeError("Connect retry lost its expected dispatch-state race")
            if row["status"] not in {"requested", "accepted", "processing"}:
                raise RuntimeError("Only active Connect jobs can be deferred")
            if next_dispatch_state == "waiting" and (
                row["status"] != "requested" or row["highest_provider_state"] != "requested"
            ):
                raise RuntimeError("Provider-owned Connect work cannot return to waiting")
            if next_dispatch_state == "provider_owned" and row["status"] == "requested":
                raise RuntimeError("Requested Connect work cannot become provider-owned")
            if next_dispatch_state == "waiting":
                deadline = datetime.fromisoformat(str(row["admission_deadline"]))
                if deadline.tzinfo is None:
                    raise RuntimeError("Connect admission deadline must include a timezone")
                if deadline < datetime.fromisoformat(next_attempt_at):
                    next_attempt_at = deadline.astimezone(UTC).isoformat()
            cursor = db.execute(
                """UPDATE connect_job_dispatch SET
                    state = ?,
                    reconciliation_failure_count = reconciliation_failure_count + CASE
                        WHEN ? IN ('reconciling', 'provider_owned') THEN 1 ELSE 0 END,
                    next_attempt_at = ?,
                    submission_possible = CASE WHEN ? = 'waiting' THEN 0 ELSE 1 END,
                    automation_paused_at = CASE WHEN ?
                        THEN COALESCE(automation_paused_at, ?)
                        ELSE automation_paused_at END,
                    last_error_code = ?, last_error_message = ?, updated_at = ?
                WHERE job_id = ? AND state = ?""",
                (
                    next_dispatch_state,
                    next_dispatch_state,
                    next_attempt_at,
                    next_dispatch_state,
                    int(pause_automation_deadline),
                    stamp,
                    code,
                    message,
                    stamp,
                    job_id,
                    expected_dispatch_state,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Connect retry lost its expected dispatch-state race")
            if pause_linked_automation_fires:
                _pause_linked_automation_fires(db, job_id=job_id, stamp=stamp)
            if next_dispatch_state == "waiting":
                self._expire_waiting_connect_jobs_transaction(db, stamp)
            updated = db.execute(
                "SELECT * FROM connect_job_dispatch WHERE job_id = ?", (job_id,)
            ).fetchone()
        if updated is None:
            raise RuntimeError("Deferred Connect job is missing dispatch state")
        return self._connect_dispatch(updated)

    @staticmethod
    def _expire_waiting_connect_jobs_transaction(
        db: sqlite3.Connection, stamp: str
    ) -> tuple[str, ...]:
        rows = db.execute(
            """SELECT d.job_id
            FROM connect_job_dispatch AS d
            JOIN connect_attachment_jobs AS j ON j.job_id = d.job_id
            WHERE j.protocol_version = 2
              AND j.status = 'requested'
              AND d.state = 'waiting'
              AND d.submission_possible = 0
              AND d.automation_paused_at IS NULL
              AND aware_iso_epoch(d.admission_deadline) <= aware_iso_epoch(?)
            ORDER BY d.admission_deadline, d.job_id""",
            (stamp,),
        ).fetchall()
        job_ids = tuple(str(row["job_id"]) for row in rows)
        for job_id in job_ids:
            db.execute(
                """UPDATE connect_attachment_jobs
                SET status = 'failed', error_code = 'connect_queue_deadline_exceeded',
                    error_message = 'The provider queue admission deadline expired.',
                    error_retryable = 0, updated_at = ?
                WHERE job_id = ? AND status = 'requested'""",
                (stamp, job_id),
            )
            db.execute(
                """UPDATE connect_job_dispatch
                SET state = 'terminal', next_attempt_at = NULL, updated_at = ?
                WHERE job_id = ? AND state = 'waiting' AND submission_possible = 0""",
                (stamp, job_id),
            )
        return job_ids

    def expire_waiting_connect_jobs(self, *, now: datetime | None = None) -> tuple[str, ...]:
        stamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            job_ids = self._expire_waiting_connect_jobs_transaction(db, stamp)
        return job_ids

    def claim_connect_lane_head(
        self,
        *,
        provider_app_id: str,
        provider_instance_id: str,
        expected_job_id: str | None = None,
        now: datetime | None = None,
    ) -> tuple[ConnectJob, ConnectDispatch] | None:
        stamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._expire_waiting_connect_jobs_transaction(db, stamp)
            row = db.execute(
                """SELECT j.*, d.state AS claim_dispatch_state,
                    CASE WHEN d.next_attempt_at IS NULL
                              OR aware_iso_epoch(d.next_attempt_at) <= aware_iso_epoch(?)
                         THEN 1 ELSE 0 END AS claim_is_due
                FROM connect_attachment_jobs AS j
                JOIN connect_job_dispatch AS d ON d.job_id = j.job_id
                WHERE j.protocol_version = 2
                  AND j.provider_app_id = ? AND j.provider_instance_id = ?
                  AND j.status IN ('requested', 'accepted', 'processing')
                  AND d.state IN ('waiting', 'dispatching', 'reconciling', 'provider_owned')
                  AND (
                      d.automation_paused_at IS NULL
                      OR d.state IN ('dispatching', 'reconciling', 'provider_owned')
                  )
                ORDER BY CASE
                    WHEN d.state = 'dispatching' THEN 0
                    WHEN d.state IN ('reconciling', 'provider_owned') THEN 1
                    ELSE 2 END,
                    j.created_at, j.job_id
                LIMIT 1""",
                (stamp, provider_app_id, provider_instance_id),
            ).fetchone()
            if row is None:
                return None
            job_id = str(row["job_id"])
            if expected_job_id is not None and job_id != expected_job_id:
                return None
            if row["claim_dispatch_state"] != "dispatching" and not bool(row["claim_is_due"]):
                return None
            db.execute(
                """UPDATE connect_job_dispatch
                SET state = CASE
                        WHEN state = 'dispatching' THEN 'reconciling'
                        WHEN state = 'waiting' THEN 'dispatching'
                        ELSE state
                    END,
                    attempt_count = attempt_count + CASE
                        WHEN state = 'waiting' THEN 1
                        ELSE 0
                    END,
                    submission_possible = CASE
                        WHEN state IN ('waiting', 'dispatching') THEN 1
                        ELSE submission_possible
                    END,
                    next_attempt_at = NULL, updated_at = ?
                WHERE job_id = ?""",
                (stamp, job_id),
            )
            dispatch_row = db.execute(
                "SELECT * FROM connect_job_dispatch WHERE job_id = ?", (job_id,)
            ).fetchone()
            job_row = db.execute(
                "SELECT * FROM connect_attachment_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        if dispatch_row is None or job_row is None:
            raise RuntimeError("Claimed Connect job is missing dispatch state")
        return self._connect_job(job_row), self._connect_dispatch(dispatch_row)

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
        capability_produces: tuple[str, ...] = (),
        capability_external_effects: bool = False,
        capability_confirmation_required: bool = False,
        now: datetime | None = None,
    ) -> ConnectJob:
        if protocol_version not in {1, 2}:
            raise ValueError("Connect protocol version is unsupported")
        if type(capability_external_effects) is not bool or type(
            capability_confirmation_required
        ) is not bool:
            raise ValueError("Connect capability authority is invalid")
        if protocol_version == 1:
            if (
                request_json is not None
                or capability_produces
                or capability_external_effects
                or capability_confirmation_required
            ):
                raise ValueError("Connect v1 jobs cannot store a v2 request")
            invocation_fingerprint = "v1"
            capability_produces_json = "[]"
        else:
            if (
                not provider_app_version
                or not input_display_name
                or not source_app_id
                or request_json is None
            ):
                raise ValueError("Connect v2 job provenance is incomplete")
            capability_produces_json = _encode_connect_capability_produces(
                capability_produces
            )
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
        created_at = (now or datetime.now(UTC)).astimezone(UTC)
        stamp = created_at.isoformat()
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
            if protocol_version == 2:
                self._expire_waiting_connect_jobs_transaction(db, stamp)
                existing = db.execute(
                    """SELECT * FROM connect_attachment_jobs
                    WHERE message_id = ? AND part_id = ?
                      AND protocol_version = 2 AND invocation_fingerprint = ?
                      AND status IN ('requested', 'accepted', 'processing')
                    ORDER BY created_at, job_id LIMIT 1""",
                    (message_id, part_id, invocation_fingerprint),
                ).fetchone()
                if existing is not None:
                    if existing["job_id"] == job_id:
                        raise sqlite3.IntegrityError("Connect job identity already exists")
                    return self._connect_job(existing)
                lane_size = db.execute(
                    """SELECT COUNT(*)
                    FROM connect_attachment_jobs AS job
                    JOIN connect_job_dispatch AS dispatch ON dispatch.job_id = job.job_id
                    WHERE job.protocol_version = 2
                      AND job.provider_app_id = ? AND job.provider_instance_id = ?
                      AND job.status IN ('requested', 'accepted', 'processing')
                      AND dispatch.state IN (
                        'waiting', 'dispatching', 'reconciling', 'provider_owned'
                      )
                      AND (
                        dispatch.automation_paused_at IS NULL
                        OR dispatch.state IN ('dispatching', 'reconciling', 'provider_owned')
                      )""",
                    (provider_app_id, provider_instance_id),
                ).fetchone()[0]
                if int(lane_size) >= CONNECT_QUEUE_MAX_JOBS:
                    raise ConnectQueueFull("Connect provider queue is full")
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
            if protocol_version == 2:
                db.execute(
                    """INSERT INTO connect_job_dispatch(
                        job_id, state, admission_deadline, submission_possible,
                        source_available, capability_external_effects,
                        capability_confirmation_required, capability_authority_known,
                        capability_produces_json,
                        highest_provider_state,
                        created_at, updated_at
                    ) VALUES (?, 'waiting', ?, 0, 1, ?, ?, 1, ?, 'requested', ?, ?)""",
                    (
                        job_id,
                        (created_at + CONNECT_QUEUE_ADMISSION_WINDOW).isoformat(),
                        int(capability_external_effects),
                        int(capability_confirmation_required),
                        capability_produces_json,
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
            dispatch_before = None
            if current_job.protocol_version == 2:
                dispatch_before = db.execute(
                    "SELECT * FROM connect_job_dispatch WHERE job_id = ?", (job_id,)
                ).fetchone()
                if dispatch_before is None:
                    raise RuntimeError("Connect job transition is missing dispatch state")
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
            terminal_job = None
            if next_state in {"completed", "failed"}:
                terminal_job = ConnectJob(
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
                if next_state == "completed" and terminal_job.protocol_version == 1:
                    try:
                        self.completed_connect_warnings(terminal_job)
                    except RuntimeError as exc:
                        raise ValueError("Completed Connect job result failed validation") from exc
            deferred_interactive_fire = bool(
                terminal_job is not None
                and dispatch_before is not None
                and not bool(dispatch_before["source_available"])
                and db.execute(
                    """SELECT 1 FROM automation_fires AS fire
                    WHERE fire.job_id = ? AND fire.state = 'entitlement_paused'
                      AND (
                        EXISTS (
                          SELECT 1 FROM connect_job_dispatch AS dispatch
                          WHERE dispatch.job_id = ?
                            AND dispatch.interactive_authorized_at IS NOT NULL
                        )
                        OR NOT EXISTS (
                          SELECT 1 FROM automation_fire_attempts AS attempt
                          WHERE attempt.dispatch_request_id = ?
                        )
                      )
                    LIMIT 1""",
                    (job_id, job_id, job_id),
                ).fetchone()
                is not None
            )
            discard_terminal = bool(
                terminal_job is not None
                and dispatch_before is not None
                and not bool(dispatch_before["source_available"])
                and not deferred_interactive_fire
            )
            if discard_terminal:
                fire_state = "completed" if next_state == "completed" else "failed"
                fire_reason = (
                    "connect_completed"
                    if next_state == "completed"
                    else str((error or {}).get("code") or "connect_failed")[:128]
                )
                db.execute(
                    """UPDATE automation_fires SET
                        state = ?, state_version = state_version + 1, reason = ?,
                        pending_since = NULL, updated_at = ?
                    WHERE job_id = ? AND state IN ('submitted', 'entitlement_paused')""",
                    (fire_state, fire_reason, stamp, job_id),
                )
                cursor = db.execute(
                    "DELETE FROM connect_attachment_jobs WHERE job_id = ? AND status = ?",
                    (job_id, expected_state),
                )
            else:
                cursor = db.execute(
                    """UPDATE connect_attachment_jobs SET
                        provider_app_id = ?, provider_instance_id = ?, status = ?,
                        output_artifact_id = ?, output_media_type = ?, output_byte_size = ?,
                        output_sha256 = ?, summary_version = ?, summary_text = ?, warnings_json = ?,
                        result_json = ?, result_metadata_json = ?,
                        error_code = ?, error_message = ?,
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
            if current_job.protocol_version == 2 and not discard_terminal:
                dispatch_state = (
                    "provider_owned" if next_state in {"accepted", "processing"} else "terminal"
                )
                dispatch_cursor = db.execute(
                    """UPDATE connect_job_dispatch SET
                        state = ?,
                        submission_possible = CASE
                            WHEN ? IN ('accepted', 'processing', 'completed') THEN 1
                            ELSE submission_possible END,
                        highest_provider_state = CASE
                            WHEN ? = 'processing' THEN 'processing'
                            WHEN ? = 'accepted' AND highest_provider_state = 'requested'
                                THEN 'accepted'
                            ELSE highest_provider_state END,
                        reconciliation_failure_count = CASE
                            WHEN ? IN ('accepted', 'processing') THEN 0
                            ELSE reconciliation_failure_count END,
                        last_error_code = CASE
                            WHEN ? IN ('accepted', 'processing') THEN NULL
                            ELSE last_error_code END,
                        last_error_message = CASE
                            WHEN ? IN ('accepted', 'processing') THEN NULL
                            ELSE last_error_message END,
                        next_attempt_at = NULL, updated_at = ?
                    WHERE job_id = ?""",
                    (
                        dispatch_state,
                        next_state,
                        next_state,
                        next_state,
                        next_state,
                        next_state,
                        next_state,
                        stamp,
                        job_id,
                    ),
                )
                if dispatch_cursor.rowcount != 1:
                    raise RuntimeError("Connect job transition is missing dispatch state")
            row = None
            if not discard_terminal:
                row = db.execute(
                    "SELECT * FROM connect_attachment_jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
        if discard_terminal:
            assert terminal_job is not None
            return terminal_job
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
            current = db.execute(
                "SELECT * FROM connect_attachment_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if current is None or current["status"] != expected_state:
                raise RuntimeError("Connect job resubmission lost its expected-state race")
            is_v2 = int(current["protocol_version"]) == 2
            if is_v2:
                dispatch = db.execute(
                    "SELECT * FROM connect_job_dispatch WHERE job_id = ?", (job_id,)
                ).fetchone()
                if dispatch is None:
                    raise RuntimeError("Connect job resubmission is missing dispatch state")
                if dispatch["highest_provider_state"] != "requested":
                    raise RuntimeError(
                        "Connect job cannot be resubmitted after authoritative provider acceptance"
                    )
                if dispatch["state"] not in {"waiting", "dispatching", "reconciling"}:
                    raise RuntimeError("Connect job resubmission has incompatible dispatch state")
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
            if is_v2:
                dispatch_cursor = db.execute(
                    """UPDATE connect_job_dispatch SET
                        state = 'waiting', submission_possible = 0,
                        next_attempt_at = NULL, updated_at = ?
                    WHERE job_id = ?
                      AND highest_provider_state = 'requested'
                      AND state IN ('waiting', 'dispatching', 'reconciling')""",
                    (stamp, job_id),
                )
                if dispatch_cursor.rowcount != 1:
                    raise RuntimeError("Connect job resubmission lost its dispatch-state race")
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
                mailbox_identity_key, thread_id, sender, sender_name, subject, received_at,
                attempts, fallback_notified_at, analysis_request_id, analysis_context_at,
                analysis_body_char_limit FROM messages
                WHERE status = 'pending' AND COALESCE(analysis_retryable, 1) = 1
                AND (next_retry_at IS NULL OR next_retry_at <= ?){scope}
                ORDER BY received_at LIMIT ?""",
                parameters,
            ).fetchall()
        return [PendingMessage(**dict(row)) for row in rows]

    def recoverable_automation_runs(
        self,
        limit: int = 25,
        *,
        after: tuple[str, str] | None = None,
        now: datetime | None = None,
    ) -> list[AutomationWork]:
        if limit < 1:
            raise ValueError("limit must be positive")
        stamp = (now or datetime.now(UTC)).astimezone(UTC)
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        now_epoch = (stamp - epoch).total_seconds()
        page = ""
        parameters: list[object] = [now_epoch]
        if after is not None:
            page = " AND (r.created_at > ? OR (r.created_at = ? AND r.run_id > ?))"
            parameters.extend((after[0], after[0], after[1]))
        parameters.append(limit)
        with self.connection() as db:
            rows = db.execute(
                f"""SELECT r.*, m.message_id AS work_message_id,
                    m.provider_message_id AS work_provider_message_id,
                    m.sender AS work_sender, m.sender_name AS work_sender_name,
                    m.subject AS work_subject, m.received_at AS work_received_at,
                    a.address AS organizer_address,
                    p.organizer_address AS extraction_organizer_address,
                    i.mailbox_identity_key AS source_mailbox_identity_key
                FROM automation_runs AS r
                LEFT JOIN automation_run_source_identities AS i ON i.run_id = r.run_id
                JOIN messages AS m
                  ON m.provider = r.provider
                 AND m.account_id = r.account_id
                 AND (
                     (i.mailbox_identity_key IS NOT NULL
                      AND m.mailbox_identity_key = i.mailbox_identity_key
                      AND message_source_key(
                            m.provider, m.account_id, m.provider_message_id,
                            m.mailbox_identity_key
                          ) = r.source_message_key)
                     OR (i.mailbox_identity_key IS NULL
                         AND m.mailbox_identity_key IS NULL
                         AND message_source_key(
                                m.provider, m.account_id, m.provider_message_id
                             ) = r.source_message_key)
                 )
                JOIN mail_accounts AS a
                  ON a.provider = r.provider AND a.account_id = r.account_id
                LEFT JOIN automation_extraction_payloads AS p
                  ON p.payload_id = r.current_payload_id
                WHERE r.state IN ('detected', 'extracting')
                  AND a.address IS NOT NULL
                  AND (
                      p.next_retry_at IS NULL
                      OR aware_iso_epoch(p.next_retry_at) <= ?
                  )
                  {page}
                ORDER BY r.created_at, r.run_id
                LIMIT ?""",
                parameters,
            ).fetchall()
        names = AutomationRun.__dataclass_fields__
        return [
            AutomationWork(
                run=AutomationRun(**{name: row[name] for name in names}),
                message_id=str(row["work_message_id"]),
                provider_message_id=str(row["work_provider_message_id"]),
                sender=str(row["work_sender"]),
                sender_name=(
                    str(row["work_sender_name"]) if row["work_sender_name"] is not None else None
                ),
                subject=str(row["work_subject"]),
                received_at=str(row["work_received_at"]),
                organizer_address=str(row["organizer_address"]),
                extraction_organizer_address=(
                    str(row["extraction_organizer_address"])
                    if row["extraction_organizer_address"] is not None
                    else None
                ),
                source_mailbox_identity_key=(
                    str(row["source_mailbox_identity_key"])
                    if row["source_mailbox_identity_key"] is not None
                    else None
                ),
            )
            for row in rows
        ]

    def automation_extraction_payloads(self, run_id: str) -> list[AutomationExtractionPayload]:
        with self.connection() as db:
            rows = db.execute(
                """SELECT * FROM automation_extraction_payloads
                WHERE run_id = ? ORDER BY attempt_no""",
                (run_id,),
            ).fetchall()
        return [_automation_extraction_payload(row) for row in rows]

    def proposable_automation_runs(
        self,
        limit: int = 25,
        *,
        after: tuple[str, str] | None = None,
    ) -> list[AutomationProposalWork]:
        if limit < 1:
            raise ValueError("limit must be positive")
        page = ""
        parameters: list[object] = []
        if after is not None:
            page = " AND (r.created_at > ? OR (r.created_at = ? AND r.run_id > ?))"
            parameters.extend((after[0], after[0], after[1]))
        parameters.append(limit)
        with self.connection() as db:
            rows = db.execute(
                f"""SELECT r.*, m.message_id AS work_message_id,
                    m.subject AS work_subject, a.address AS organizer_address,
                    p.payload_id AS extraction_payload_id,
                    p.run_id AS extraction_run_id, p.attempt_no, p.request_id,
                    p.status AS extraction_status, p.source_content_sha256,
                    p.context_at, p.timezone AS extraction_payload_timezone,
                    p.body_char_limit, p.organizer_address AS extraction_organizer_address,
                    p.failure_count, p.next_retry_at, p.last_error_code,
                    p.result_sha256, p.result_json, p.violations_json,
                    p.created_at AS extraction_created_at, p.completed_at
                FROM automation_runs AS r
                LEFT JOIN automation_run_source_identities AS i ON i.run_id = r.run_id
                JOIN messages AS m
                  ON m.provider = r.provider
                 AND m.account_id = r.account_id
                 AND (
                     (i.mailbox_identity_key IS NOT NULL
                      AND m.mailbox_identity_key = i.mailbox_identity_key
                      AND message_source_key(
                            m.provider, m.account_id, m.provider_message_id,
                            m.mailbox_identity_key
                          ) = r.source_message_key)
                     OR (i.mailbox_identity_key IS NULL
                         AND m.mailbox_identity_key IS NULL
                         AND message_source_key(
                                m.provider, m.account_id, m.provider_message_id
                             ) = r.source_message_key)
                 )
                JOIN mail_accounts AS a
                  ON a.provider = r.provider AND a.account_id = r.account_id
                JOIN automation_extraction_payloads AS p
                  ON p.run_id = r.run_id AND p.status = 'accepted'
                WHERE r.state = 'proposing' AND p.status = 'accepted'
                  AND a.address IS NOT NULL
                  {page}
                ORDER BY r.created_at, r.run_id
                LIMIT ?""",
                parameters,
            ).fetchall()
        run_fields = AutomationRun.__dataclass_fields__
        return [
            AutomationProposalWork(
                run=AutomationRun(**{name: row[name] for name in run_fields}),
                message_id=str(row["work_message_id"]),
                subject=str(row["work_subject"]),
                organizer_address=str(row["organizer_address"]),
                extraction_payload=AutomationExtractionPayload(
                    payload_id=str(row["extraction_payload_id"]),
                    run_id=str(row["extraction_run_id"]),
                    attempt_no=int(row["attempt_no"]),
                    request_id=str(row["request_id"]),
                    status=str(row["extraction_status"]),
                    source_content_sha256=str(row["source_content_sha256"]),
                    context_at=str(row["context_at"]),
                    timezone=str(row["extraction_payload_timezone"]),
                    body_char_limit=int(row["body_char_limit"]),
                    organizer_address=(
                        str(row["extraction_organizer_address"])
                        if row["extraction_organizer_address"] is not None
                        else None
                    ),
                    failure_count=int(row["failure_count"]),
                    next_retry_at=(
                        str(row["next_retry_at"]) if row["next_retry_at"] is not None else None
                    ),
                    last_error_code=(
                        str(row["last_error_code"]) if row["last_error_code"] is not None else None
                    ),
                    result_sha256=(
                        str(row["result_sha256"]) if row["result_sha256"] is not None else None
                    ),
                    result_json=(
                        bytes(row["result_json"]) if row["result_json"] is not None else None
                    ),
                    violations_json=(
                        bytes(row["violations_json"])
                        if row["violations_json"] is not None
                        else None
                    ),
                    created_at=str(row["extraction_created_at"]),
                    completed_at=(
                        str(row["completed_at"]) if row["completed_at"] is not None else None
                    ),
                ),
            )
            for row in rows
        ]

    def automation_proposal(self, run_id: str) -> AutomationProposalPayload | None:
        with self.connection() as db:
            row = db.execute(
                """SELECT p.* FROM automation_proposal_payloads AS p
                JOIN automation_runs AS r
                  ON r.run_id = p.run_id AND r.current_payload_id = p.payload_id
                WHERE p.run_id = ?""",
                (run_id,),
            ).fetchone()
        return _automation_proposal_payload(row) if row is not None else None

    def automation_proposal_for_message(self, message_id: str) -> AutomationProposalPayload | None:
        with self.connection() as db:
            row = db.execute(
                """SELECT p.* FROM automation_proposal_payloads AS p
                JOIN automation_runs AS r
                  ON r.run_id = p.run_id AND r.current_payload_id = p.payload_id
                LEFT JOIN automation_run_source_identities AS i ON i.run_id = r.run_id
                JOIN messages AS m
                  ON m.provider = r.provider
                 AND m.account_id = r.account_id
                 AND (
                     (i.mailbox_identity_key IS NOT NULL
                      AND m.mailbox_identity_key = i.mailbox_identity_key
                      AND message_source_key(
                            m.provider, m.account_id, m.provider_message_id,
                            m.mailbox_identity_key
                          ) = r.source_message_key)
                     OR (i.mailbox_identity_key IS NULL
                         AND m.mailbox_identity_key IS NULL
                         AND message_source_key(
                                m.provider, m.account_id, m.provider_message_id
                             ) = r.source_message_key)
                 )
                WHERE m.message_id = ?""",
                (message_id,),
            ).fetchone()
        return _automation_proposal_payload(row) if row is not None else None

    def record_automation_proposal(
        self,
        run_id: str,
        expected_state_version: int,
        *,
        extraction_payload_id: str,
        request_sha256: str,
        subject: str,
        attendees: Sequence[str],
        start: str | None,
        end: str | None,
        timezone: str | None,
        suggestion_reason: str | None,
        empty_reason: str | None,
        observed_at: datetime,
    ) -> AutomationRun:
        try:
            valid_request_hash = (
                len(request_sha256) == 64 and len(bytes.fromhex(request_sha256)) == 32
            )
        except (TypeError, ValueError):
            valid_request_hash = False
        if not valid_request_hash:
            raise ValueError("proposal request hash must be a SHA-256 digest")
        try:
            subject_bytes = subject.encode("utf-8")
            normalized_attendees = tuple(
                normalize_validated_address(attendee) for attendee in attendees
            )
            attendees_json = json.dumps(
                list(normalized_attendees), ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        except (AttributeError, TypeError, UnicodeEncodeError, ValueError) as exc:
            raise ValueError("automation proposal content is invalid") from exc
        if (
            len(subject_bytes) > 512
            or len(normalized_attendees) > MAX_AUTOMATION_PROPOSAL_ATTENDEES
            or len(set(normalized_attendees)) != len(normalized_attendees)
            or not 2 <= len(attendees_json) <= 32768
        ):
            raise ValueError("automation proposal content is not bounded")
        for value, byte_limit in (
            (start, 64),
            (end, 64),
            (timezone, 128),
            (suggestion_reason, 512),
            (empty_reason, 512),
        ):
            if value is not None:
                try:
                    if len(value.encode("utf-8")) > byte_limit:
                        raise ValueError("automation proposal content is not bounded")
                except (AttributeError, UnicodeEncodeError) as exc:
                    raise ValueError("automation proposal content is invalid") from exc
        accepted = (
            all(value is not None for value in (start, end, timezone, suggestion_reason))
            and bool(suggestion_reason)
            and empty_reason is None
        )
        no_suggestions = (
            all(value is None for value in (start, end, timezone, suggestion_reason))
            and isinstance(empty_reason, str)
            and bool(empty_reason)
        )
        if accepted == no_suggestions:
            raise ValueError("automation proposal outcome is incomplete")
        if observed_at.tzinfo is None:
            raise ValueError("automation proposal observation time must be timezone-aware")
        stamp = observed_at.astimezone(UTC)
        expires_at: str | None = None
        status = "accepted" if accepted else "no_suggestions"
        next_state = "awaiting_confirmation" if accepted else "manual_review"
        failure_code = None if accepted else "proposal_no_suggestions"
        if accepted:
            assert start is not None and end is not None and timezone is not None
            try:
                parsed_start = datetime.fromisoformat(start)
                parsed_end = datetime.fromisoformat(end)
                zone = ZoneInfo(timezone)
            except (ValueError, ZoneInfoNotFoundError) as exc:
                raise ValueError("automation proposal time is invalid") from exc
            if (
                parsed_start.tzinfo is None
                or parsed_end.tzinfo is None
                or parsed_start.astimezone(UTC) <= stamp
                or parsed_end.astimezone(UTC) <= parsed_start.astimezone(UTC)
                or parsed_start.astimezone(zone).replace(tzinfo=None)
                != parsed_start.replace(tzinfo=None)
                or parsed_end.astimezone(zone).replace(tzinfo=None)
                != parsed_end.replace(tzinfo=None)
                or parsed_start.astimezone(zone).utcoffset() != parsed_start.utcoffset()
                or parsed_end.astimezone(zone).utcoffset() != parsed_end.utcoffset()
            ):
                raise ValueError("automation proposal time is invalid")
            expires_at = min(
                stamp + timedelta(minutes=15), parsed_start.astimezone(UTC)
            ).isoformat()
        proposal_document = {
            "attendees": list(normalized_attendees),
            "calendar": "primary",
            "empty_reason": empty_reason,
            "end": end,
            "online_meeting": False,
            "principal_key": None,
            "start": start,
            "subject": subject,
            "suggestion_reason": suggestion_reason,
            "timezone": timezone,
            "version": 1,
        }
        payload_id = str(uuid.uuid4())
        created_at = stamp.isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM automation_runs WHERE run_id = ?", (run_id,)).fetchone()
            extraction = db.execute(
                """SELECT status FROM automation_extraction_payloads
                WHERE payload_id = ? AND run_id = ?""",
                (extraction_payload_id, run_id),
            ).fetchone()
            if row is None or extraction is None:
                raise KeyError(run_id)
            if (
                row["state"] != "proposing"
                or int(row["state_version"]) != expected_state_version
                or extraction["status"] != "accepted"
            ):
                raise RuntimeError("Automation proposal lost its expected-state race")
            prior_proposal = db.execute(
                """SELECT COALESCE(MAX(proposal_version), 0) AS latest
                FROM automation_proposal_payloads WHERE run_id = ?""",
                (run_id,),
            ).fetchone()
            proposal_version = int(prior_proposal["latest"]) + 1
            proposal_document["principal_key"] = str(row["calendar_principal_key"])
            proposal_document["version"] = proposal_version
            proposal_json = json.dumps(
                proposal_document,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            proposal_sha256 = hashlib.sha256(proposal_json).hexdigest()
            db.execute(
                """INSERT INTO automation_proposal_payloads(
                    payload_id, run_id, proposal_version, status, request_sha256,
                    proposal_sha256, subject, attendees_json, start, end, timezone,
                    suggestion_reason, empty_reason, observed_at, expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    payload_id,
                    run_id,
                    proposal_version,
                    status,
                    request_sha256,
                    proposal_sha256,
                    subject,
                    sqlite3.Binary(attendees_json),
                    start,
                    end,
                    timezone,
                    suggestion_reason,
                    empty_reason,
                    created_at,
                    expires_at,
                    created_at,
                ),
            )
            next_version = expected_state_version + 1
            changed = db.execute(
                """UPDATE automation_runs SET state = ?, state_version = ?,
                    failure_code = ?, current_payload_id = ?, current_payload_sha256 = ?,
                    review_notified_at = NULL, updated_at = ?
                WHERE run_id = ? AND state = 'proposing' AND state_version = ?""",
                (
                    next_state,
                    next_version,
                    failure_code,
                    payload_id,
                    proposal_sha256,
                    created_at,
                    run_id,
                    expected_state_version,
                ),
            )
            if changed.rowcount != 1:
                raise RuntimeError("Automation proposal lost its expected-state race")
            _append_automation_event(
                db,
                run_id=run_id,
                sequence_no=next_version - 1,
                previous_state="proposing",
                next_state=next_state,
                state_version=next_version,
                automation_id=str(row["automation_id"]),
                automation_version=int(row["automation_version"]),
                extraction_schema_version=int(row["extraction_schema_version"]),
                calendar_principal_key=str(row["calendar_principal_key"]),
                transition_kind=next_state,
                failure_code=failure_code,
                created_at=created_at,
                payload_id=payload_id,
                payload_sha256=proposal_sha256,
            )
            updated = db.execute(
                "SELECT * FROM automation_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if updated is None:
            raise RuntimeError("Automation proposal was not readable")
        return _automation_run(updated)

    def decide_automation_proposal(
        self,
        run_id: str,
        expected_state_version: int,
        *,
        proposal_version: int,
        proposal_sha256: str,
        decision: str,
        write_authorized: bool = True,
        now: datetime | None = None,
    ) -> AutomationRun:
        if decision not in {"confirm", "decline"}:
            raise ValueError("automation proposal decision is invalid")
        if proposal_version < 1 or len(proposal_sha256) != 64:
            raise ValueError("automation proposal identity is invalid")
        selected_now = now or datetime.now(UTC)
        if selected_now.tzinfo is None:
            raise ValueError("automation proposal decision time must be timezone-aware")
        stamp = selected_now.astimezone(UTC)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM automation_runs WHERE run_id = ?", (run_id,)).fetchone()
            proposal = db.execute(
                """SELECT * FROM automation_proposal_payloads
                WHERE run_id = ? AND proposal_version = ? AND proposal_sha256 = ?""",
                (run_id, proposal_version, proposal_sha256),
            ).fetchone()
            if row is None or proposal is None:
                raise KeyError(run_id)
            existing_write = db.execute(
                "SELECT * FROM automation_calendar_writes WHERE run_id = ?", (run_id,)
            ).fetchone()
            if decision == "confirm" and existing_write is not None:
                if (
                    int(existing_write["proposal_version"]) == proposal_version
                    and existing_write["proposal_sha256"] == proposal_sha256
                ):
                    return _automation_run(row)
                raise RuntimeError("Automation confirmation conflicts with its durable write")
            if decision == "decline" and row["state"] == "declined":
                return _automation_run(row)
            if (
                row["state"] != "awaiting_confirmation"
                or int(row["state_version"]) != expected_state_version
                or row["current_payload_id"] != proposal["payload_id"]
                or row["current_payload_sha256"] != proposal_sha256
                or proposal["status"] != "accepted"
            ):
                raise RuntimeError("Automation proposal decision lost its expected-state race")

            previous_version = int(row["state_version"])
            next_version = previous_version + 1
            if decision == "decline":
                next_state = "declined"
                failure_code = None
                transaction_id = None
                event_decision = "declined"
                next_payload_id = str(proposal["payload_id"])
                next_payload_sha256 = proposal_sha256
            else:
                start_value = proposal["start"]
                end_value = proposal["end"]
                timezone_value = proposal["timezone"]
                expires_value = proposal["expires_at"]
                if not all(
                    isinstance(value, str) and value
                    for value in (start_value, end_value, timezone_value, expires_value)
                ):
                    raise RuntimeError("Stored automation proposal is incomplete")
                try:
                    start_time = datetime.fromisoformat(str(start_value)).astimezone(UTC)
                    expires_at = datetime.fromisoformat(str(expires_value)).astimezone(UTC)
                except (OverflowError, ValueError) as exc:
                    raise RuntimeError("Stored automation proposal time is invalid") from exc
                if stamp >= expires_at or stamp >= start_time:
                    extraction = db.execute(
                        """SELECT payload_id, result_sha256
                        FROM automation_extraction_payloads
                        WHERE run_id = ? AND status = 'accepted'""",
                        (run_id,),
                    ).fetchone()
                    if extraction is None or extraction["result_sha256"] is None:
                        raise RuntimeError("Accepted automation extraction is unavailable")
                    next_state = "proposing"
                    failure_code = "proposal_expired"
                    transaction_id = None
                    event_decision = None
                    next_payload_id = str(extraction["payload_id"])
                    next_payload_sha256 = str(extraction["result_sha256"])
                else:
                    if not write_authorized:
                        raise PermissionError("Calendar write authorization is required")
                    next_state = "write_authorized"
                    failure_code = None
                    transaction_id = str(uuid.uuid4())
                    event_decision = "confirmed"
                    next_payload_id = str(proposal["payload_id"])
                    next_payload_sha256 = proposal_sha256
                    db.execute(
                        """INSERT INTO automation_calendar_writes(
                            run_id, transaction_id, proposal_version, proposal_sha256,
                            calendar_principal_key, calendar_id, start, end, timezone,
                            status, graph_event_id, failure_code, confirmed_at,
                            submitted_at, completed_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 'primary', ?, ?, ?, 'authorized',
                            NULL, NULL, ?, NULL, NULL, ?)""",
                        (
                            run_id,
                            transaction_id,
                            proposal_version,
                            proposal_sha256,
                            str(row["calendar_principal_key"]),
                            str(start_value),
                            str(end_value),
                            str(timezone_value),
                            stamp.isoformat(),
                            stamp.isoformat(),
                        ),
                    )
            changed = db.execute(
                """UPDATE automation_runs SET state = ?, state_version = ?,
                    failure_code = ?, current_payload_id = ?, current_payload_sha256 = ?,
                    review_notified_at = NULL, updated_at = ?
                WHERE run_id = ? AND state = 'awaiting_confirmation'
                  AND state_version = ? AND current_payload_id = ?
                  AND current_payload_sha256 = ?""",
                (
                    next_state,
                    next_version,
                    failure_code,
                    next_payload_id,
                    next_payload_sha256,
                    stamp.isoformat(),
                    run_id,
                    previous_version,
                    str(proposal["payload_id"]),
                    proposal_sha256,
                ),
            )
            if changed.rowcount != 1:
                raise RuntimeError("Automation proposal decision lost its expected-state race")
            _append_automation_event(
                db,
                run_id=run_id,
                sequence_no=next_version - 1,
                previous_state="awaiting_confirmation",
                next_state=next_state,
                state_version=next_version,
                automation_id=str(row["automation_id"]),
                automation_version=int(row["automation_version"]),
                extraction_schema_version=int(row["extraction_schema_version"]),
                calendar_principal_key=str(row["calendar_principal_key"]),
                transition_kind=next_state,
                failure_code=failure_code,
                created_at=stamp.isoformat(),
                payload_id=str(proposal["payload_id"]),
                payload_sha256=proposal_sha256,
                decision=event_decision,
                transaction_id=transaction_id,
            )
            updated = db.execute(
                "SELECT * FROM automation_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if updated is None:
            raise RuntimeError("Automation proposal decision was not readable")
        return _automation_run(updated)

    def automation_calendar_write(self, run_id: str) -> AutomationCalendarWrite | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM automation_calendar_writes WHERE run_id = ?", (run_id,)
            ).fetchone()
        return _automation_calendar_write(row) if row is not None else None

    def pending_automation_calendar_writes(
        self,
        limit: int = 25,
        *,
        run_id: str | None = None,
        now: datetime | None = None,
    ) -> list[AutomationCalendarWriteWork]:
        if limit < 1:
            raise ValueError("limit must be positive")
        stamp = (now or datetime.now(UTC)).astimezone(UTC)
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        now_epoch = (stamp - epoch).total_seconds()
        run_filter = " AND r.run_id = ?" if run_id is not None else ""
        parameters: list[object] = [now_epoch]
        if run_id is not None:
            parameters.append(run_id)
        parameters.append(limit)
        with self.connection() as db:
            rows = db.execute(
                f"""SELECT r.* FROM automation_runs AS r
                JOIN automation_calendar_writes AS w ON w.run_id = r.run_id
                WHERE r.state IN ('write_authorized', 'writing', 'unresolved', 'reconciling')
                  AND (
                      r.state IN ('writing', 'reconciling')
                      OR aware_iso_epoch(r.expires_at) > ?
                  )
                {run_filter}
                ORDER BY r.updated_at, r.run_id LIMIT ?""",
                parameters,
            ).fetchall()
            work: list[AutomationCalendarWriteWork] = []
            for row in rows:
                write_row = db.execute(
                    "SELECT * FROM automation_calendar_writes WHERE run_id = ?",
                    (str(row["run_id"]),),
                ).fetchone()
                proposal_row = db.execute(
                    """SELECT * FROM automation_proposal_payloads
                    WHERE payload_id = ? AND run_id = ?""",
                    (row["current_payload_id"], str(row["run_id"])),
                ).fetchone()
                if write_row is None:
                    raise RuntimeError("Automation write projection is incomplete")
                work.append(
                    AutomationCalendarWriteWork(
                        run=_automation_run(row),
                        proposal=(
                            _automation_proposal_payload(proposal_row)
                            if proposal_row is not None
                            else None
                        ),
                        write=_automation_calendar_write(write_row),
                    )
                )
        return work

    def begin_automation_calendar_write(
        self,
        run_id: str,
        expected_state_version: int,
        *,
        now: datetime | None = None,
    ) -> AutomationRun:
        stamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM automation_runs WHERE run_id = ?", (run_id,)).fetchone()
            write = db.execute(
                "SELECT * FROM automation_calendar_writes WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None or write is None:
                raise KeyError(run_id)
            if (
                row["state"] != "write_authorized"
                or int(row["state_version"]) != expected_state_version
                or write["status"] != "authorized"
            ):
                raise RuntimeError("Automation write lost its expected-state race")
            source_exists = db.execute(
                """SELECT 1 FROM messages AS m
                LEFT JOIN automation_run_source_identities AS i ON i.run_id = ?
                WHERE m.provider = ? AND m.account_id = ? AND (
                    (i.mailbox_identity_key IS NOT NULL
                     AND m.mailbox_identity_key = i.mailbox_identity_key
                     AND message_source_key(
                            m.provider, m.account_id, m.provider_message_id,
                            m.mailbox_identity_key
                         ) = ?)
                    OR (i.mailbox_identity_key IS NULL
                        AND m.mailbox_identity_key IS NULL
                        AND message_source_key(
                               m.provider, m.account_id, m.provider_message_id
                            ) = ?)
                )""",
                (
                    run_id,
                    row["provider"],
                    row["account_id"],
                    row["source_message_key"],
                    row["source_message_key"],
                ),
            ).fetchone()
            proposal_exists = db.execute(
                """SELECT 1 FROM automation_proposal_payloads
                WHERE run_id = ? AND payload_id = ? AND proposal_version = ?
                  AND proposal_sha256 = ?""",
                (
                    run_id,
                    row["current_payload_id"],
                    int(write["proposal_version"]),
                    str(write["proposal_sha256"]),
                ),
            ).fetchone()
            next_version = expected_state_version + 1
            if source_exists is None or proposal_exists is None:
                db.execute("DELETE FROM automation_extraction_payloads WHERE run_id = ?", (run_id,))
                db.execute("DELETE FROM automation_proposal_payloads WHERE run_id = ?", (run_id,))
                next_state = "source_unavailable"
                failure_code = "source_unavailable"
            else:
                next_state = "writing"
                failure_code = None
                changed_write = db.execute(
                    """UPDATE automation_calendar_writes SET status = 'writing',
                        submitted_at = ?, failure_code = NULL, updated_at = ?
                    WHERE run_id = ? AND status = 'authorized'""",
                    (stamp, stamp, run_id),
                )
                if changed_write.rowcount != 1:
                    raise RuntimeError("Automation write lost its expected-state race")
            changed = db.execute(
                """UPDATE automation_runs SET state = ?, state_version = ?,
                    failure_code = ?, review_notified_at = NULL, updated_at = ?
                WHERE run_id = ? AND state = 'write_authorized' AND state_version = ?""",
                (next_state, next_version, failure_code, stamp, run_id, expected_state_version),
            )
            if changed.rowcount != 1:
                raise RuntimeError("Automation write lost its expected-state race")
            _append_automation_event(
                db,
                run_id=run_id,
                sequence_no=next_version - 1,
                previous_state="write_authorized",
                next_state=next_state,
                state_version=next_version,
                automation_id=str(row["automation_id"]),
                automation_version=int(row["automation_version"]),
                extraction_schema_version=int(row["extraction_schema_version"]),
                calendar_principal_key=str(row["calendar_principal_key"]),
                transition_kind=next_state,
                failure_code=failure_code,
                created_at=stamp,
                payload_id=(str(row["current_payload_id"]) if proposal_exists else None),
                payload_sha256=(str(row["current_payload_sha256"]) if proposal_exists else None),
                transaction_id=str(write["transaction_id"]),
            )
            updated = db.execute(
                "SELECT * FROM automation_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if updated is None:
            raise RuntimeError("Automation write transition was not readable")
        return _automation_run(updated)

    def transition_automation_calendar_write(
        self,
        run_id: str,
        expected_state_version: int,
        *,
        next_state: str,
        failure_code: str | None = None,
        graph_event_id: str | None = None,
        now: datetime | None = None,
    ) -> AutomationRun:
        allowed = {
            ("writing", "completed"),
            ("writing", "failed"),
            ("writing", "unresolved"),
            ("unresolved", "reconciling"),
            ("reconciling", "completed"),
            ("reconciling", "unresolved"),
        }
        stamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM automation_runs WHERE run_id = ?", (run_id,)).fetchone()
            write = db.execute(
                "SELECT * FROM automation_calendar_writes WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None or write is None:
                raise KeyError(run_id)
            previous_state = str(row["state"])
            if (previous_state, next_state) not in allowed or int(
                row["state_version"]
            ) != expected_state_version:
                raise RuntimeError("Automation write transition lost its expected-state race")
            if next_state == "completed":
                if (
                    not isinstance(graph_event_id, str)
                    or not 1 <= len(graph_event_id.encode("utf-8")) <= 512
                ):
                    raise ValueError("Graph event identity is invalid")
                write_status = "completed"
                completed_at = stamp
                stored_failure = None
            else:
                if graph_event_id is not None:
                    raise ValueError("Only completed writes may record a Graph event identity")
                if next_state in {"failed", "unresolved"}:
                    if not isinstance(failure_code, str) or not 1 <= len(failure_code) <= 64:
                        raise ValueError("Automation write failure code is invalid")
                else:
                    failure_code = None
                write_status = next_state
                completed_at = None
                stored_failure = failure_code
            next_version = expected_state_version + 1
            changed_write = db.execute(
                """UPDATE automation_calendar_writes SET status = ?, graph_event_id = ?,
                    failure_code = ?, completed_at = ?, updated_at = ?
                WHERE run_id = ? AND status = ?""",
                (
                    write_status,
                    graph_event_id,
                    stored_failure,
                    completed_at,
                    stamp,
                    run_id,
                    write["status"],
                ),
            )
            if changed_write.rowcount != 1:
                raise RuntimeError("Automation write transition lost its expected-state race")
            changed = db.execute(
                """UPDATE automation_runs SET state = ?, state_version = ?,
                    failure_code = ?, review_notified_at = NULL, updated_at = ?
                WHERE run_id = ? AND state = ? AND state_version = ?""",
                (
                    next_state,
                    next_version,
                    stored_failure,
                    stamp,
                    run_id,
                    previous_state,
                    expected_state_version,
                ),
            )
            if changed.rowcount != 1:
                raise RuntimeError("Automation write transition lost its expected-state race")
            _append_automation_event(
                db,
                run_id=run_id,
                sequence_no=next_version - 1,
                previous_state=previous_state,
                next_state=next_state,
                state_version=next_version,
                automation_id=str(row["automation_id"]),
                automation_version=int(row["automation_version"]),
                extraction_schema_version=int(row["extraction_schema_version"]),
                calendar_principal_key=str(row["calendar_principal_key"]),
                transition_kind=next_state,
                failure_code=stored_failure,
                created_at=stamp,
                payload_id=(
                    str(row["current_payload_id"])
                    if row["current_payload_id"] is not None
                    else None
                ),
                payload_sha256=(
                    str(row["current_payload_sha256"])
                    if row["current_payload_sha256"] is not None
                    else None
                ),
                transaction_id=str(write["transaction_id"]),
                graph_event_id=graph_event_id,
            )
            updated = db.execute(
                "SELECT * FROM automation_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if updated is None:
            raise RuntimeError("Automation write outcome was not readable")
        return _automation_run(updated)

    def reserve_automation_extraction(
        self,
        run_id: str,
        expected_state_version: int,
        *,
        source_content_sha256: str,
        context_at: str,
        timezone: str,
        body_char_limit: int,
        organizer_address: str,
        now: datetime | None = None,
    ) -> AutomationExtractionPayload:
        if len(source_content_sha256) != 64:
            raise ValueError("source_content_sha256 must be a SHA-256 digest")
        if (
            not context_at
            or not timezone
            or body_char_limit < 1
            or not organizer_address
            or len(organizer_address) > 320
        ):
            raise ValueError("extraction context must be complete")
        stamp = (now or datetime.now(UTC)).isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM automation_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            if (
                str(row["state"]) not in {"detected", "extracting"}
                or int(row["state_version"]) != expected_state_version
            ):
                raise RuntimeError("Automation extraction lost its expected-state race")
            previous_source_hash = row["source_content_sha256"]
            if previous_source_hash is not None and previous_source_hash != source_content_sha256:
                raise AutomationSourceChanged("Automation source content changed after admission")
            payload_rows = db.execute(
                """SELECT * FROM automation_extraction_payloads
                WHERE run_id = ? ORDER BY attempt_no""",
                (run_id,),
            ).fetchall()
            if payload_rows and (
                payload_rows[0]["organizer_address"] is None
                or str(payload_rows[0]["organizer_address"]) != organizer_address
            ):
                raise AutomationSourceChanged(
                    "Automation organizer changed or was not pinned after reservation"
                )
            if payload_rows and str(payload_rows[-1]["status"]) == "reserved":
                reserved = payload_rows[-1]
                if (
                    reserved["source_content_sha256"] != source_content_sha256
                    or reserved["context_at"] != context_at
                    or reserved["timezone"] != timezone
                    or int(reserved["body_char_limit"]) != body_char_limit
                    or reserved["organizer_address"] != organizer_address
                ):
                    raise AutomationSourceChanged(
                        "Automation source or extraction context changed after reservation"
                    )
                return _automation_extraction_payload(reserved)
            if payload_rows and str(payload_rows[-1]["status"]) != "rejected":
                raise RuntimeError("Automation extraction already has a terminal result")
            attempt_no = len(payload_rows) + 1
            if attempt_no not in {1, 2}:
                raise RuntimeError("Automation extraction retry budget is exhausted")
            if row["extraction_context_at"] is not None and (
                row["extraction_context_at"] != context_at
                or row["extraction_timezone"] != timezone
                or int(row["extraction_body_char_limit"]) != body_char_limit
            ):
                raise AutomationSourceChanged(
                    "Automation extraction context changed after admission"
                )
            payload_id = str(uuid.uuid4())
            request_id = str(uuid.uuid4())
            db.execute(
                """INSERT INTO automation_extraction_payloads(
                    payload_id, run_id, attempt_no, request_id, status,
                    source_content_sha256, context_at, timezone, body_char_limit,
                    organizer_address, failure_count, next_retry_at, last_error_code,
                    result_sha256, result_json, violations_json, created_at, completed_at
                ) VALUES (
                    ?, ?, ?, ?, 'reserved', ?, ?, ?, ?, ?, 0, NULL, NULL,
                    NULL, NULL, NULL, ?, NULL
                )""",
                (
                    payload_id,
                    run_id,
                    attempt_no,
                    request_id,
                    source_content_sha256,
                    context_at,
                    timezone,
                    body_char_limit,
                    organizer_address,
                    stamp,
                ),
            )
            previous_state = str(row["state"])
            next_version = expected_state_version + 1
            changed = db.execute(
                """UPDATE automation_runs SET state = 'extracting', state_version = ?,
                    failure_code = NULL, source_content_sha256 = ?, extraction_context_at = ?,
                    extraction_timezone = ?, extraction_body_char_limit = ?,
                    current_payload_id = ?, current_payload_sha256 = NULL, updated_at = ?
                WHERE run_id = ? AND state = ? AND state_version = ?""",
                (
                    next_version,
                    source_content_sha256,
                    context_at,
                    timezone,
                    body_char_limit,
                    payload_id,
                    stamp,
                    run_id,
                    previous_state,
                    expected_state_version,
                ),
            )
            if changed.rowcount != 1:
                raise RuntimeError("Automation extraction lost its expected-state race")
            _append_automation_event(
                db,
                run_id=run_id,
                sequence_no=next_version - 1,
                previous_state=previous_state,
                next_state="extracting",
                state_version=next_version,
                automation_id=str(row["automation_id"]),
                automation_version=int(row["automation_version"]),
                extraction_schema_version=int(row["extraction_schema_version"]),
                calendar_principal_key=str(row["calendar_principal_key"]),
                transition_kind="extracting",
                failure_code=None,
                created_at=stamp,
                payload_id=payload_id,
            )
            reserved = db.execute(
                "SELECT * FROM automation_extraction_payloads WHERE payload_id = ?",
                (payload_id,),
            ).fetchone()
        if reserved is None:
            raise RuntimeError("Automation extraction reservation was not readable")
        return _automation_extraction_payload(reserved)

    def record_automation_extraction_failure(
        self,
        run_id: str,
        expected_state_version: int,
        *,
        payload_id: str,
        error_code: str,
        retryable: bool,
        retry_after_seconds: int | None = None,
        now: datetime | None = None,
    ) -> AutomationRun:
        if (
            not error_code
            or len(error_code) > 64
            or not error_code[0].islower()
            or not error_code.replace("_", "").isalnum()
            or error_code != error_code.casefold()
        ):
            raise ValueError("automation extraction error code is invalid")
        if retry_after_seconds is not None and not 1 <= retry_after_seconds <= 86_400:
            raise ValueError("retry_after_seconds must be between 1 and 86400")
        if not retryable and retry_after_seconds is not None:
            raise ValueError("permanent extraction failures cannot carry a retry delay")
        stamp = (now or datetime.now(UTC)).astimezone(UTC)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM automation_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            payload = db.execute(
                "SELECT * FROM automation_extraction_payloads WHERE payload_id = ?",
                (payload_id,),
            ).fetchone()
            if (
                row is None
                or payload is None
                or str(row["state"]) != "extracting"
                or int(row["state_version"]) != expected_state_version
                or row["current_payload_id"] != payload_id
                or payload["run_id"] != run_id
                or payload["status"] != "reserved"
            ):
                raise RuntimeError("Automation extraction failure lost its expected-state race")
            failure_count = int(payload["failure_count"]) + 1
            if retryable:
                delay = (
                    retry_after_seconds
                    if retry_after_seconds is not None
                    else AUTOMATION_RETRY_DELAYS_SECONDS[
                        min(failure_count - 1, len(AUTOMATION_RETRY_DELAYS_SECONDS) - 1)
                    ]
                )
                next_retry_at = (stamp + timedelta(seconds=delay)).isoformat()
                next_state = "extracting"
            else:
                next_retry_at = None
                next_state = "manual_review"
            updated_payload = db.execute(
                """UPDATE automation_extraction_payloads SET
                    failure_count = ?, next_retry_at = ?, last_error_code = ?
                WHERE payload_id = ? AND run_id = ? AND status = 'reserved'""",
                (failure_count, next_retry_at, error_code, payload_id, run_id),
            )
            if updated_payload.rowcount != 1:
                raise RuntimeError("Automation extraction payload was not reserved")
            next_version = expected_state_version + 1
            failure_code = f"model_{error_code}"
            changed = db.execute(
                """UPDATE automation_runs SET state = ?, state_version = ?,
                    failure_code = ?, review_notified_at = NULL, updated_at = ?
                WHERE run_id = ? AND state = 'extracting' AND state_version = ?
                  AND current_payload_id = ?""",
                (
                    next_state,
                    next_version,
                    failure_code,
                    stamp.isoformat(),
                    run_id,
                    expected_state_version,
                    payload_id,
                ),
            )
            if changed.rowcount != 1:
                raise RuntimeError("Automation extraction failure lost its expected-state race")
            _append_automation_event(
                db,
                run_id=run_id,
                sequence_no=next_version - 1,
                previous_state="extracting",
                next_state=next_state,
                state_version=next_version,
                automation_id=str(row["automation_id"]),
                automation_version=int(row["automation_version"]),
                extraction_schema_version=int(row["extraction_schema_version"]),
                calendar_principal_key=str(row["calendar_principal_key"]),
                transition_kind=next_state,
                failure_code=failure_code,
                created_at=stamp.isoformat(),
                payload_id=payload_id,
            )
            updated = db.execute(
                "SELECT * FROM automation_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if updated is None:
            raise RuntimeError("Automation extraction failure was not readable")
        return _automation_run(updated)

    @staticmethod
    def _encoded_automation_violations(
        violations: Sequence[dict[str, str]],
    ) -> bytes:
        document = [
            {"code": str(item.get("code", ""))[:64], "path": str(item.get("path", ""))[:256]}
            for item in violations[:32]
        ]
        encoded = json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")
        if not 2 <= len(encoded) <= 8192:
            raise ValueError("automation extraction violations are not bounded")
        return encoded

    def record_automation_extraction(
        self,
        run_id: str,
        expected_state_version: int,
        *,
        payload_id: str,
        result_sha256: str,
        result_json: bytes,
        violations: Sequence[dict[str, str]],
        accepted_state: str | None = None,
        accepted_code: str | None = None,
        now: datetime | None = None,
    ) -> AutomationRun:
        if (
            len(result_sha256) != 64
            or hashlib.sha256(result_json).hexdigest() != result_sha256
            or not 1 <= len(result_json) <= 32768
        ):
            raise ValueError("automation extraction result is not bounded")
        violations_json = self._encoded_automation_violations(violations)
        accepted = not violations
        if accepted != (accepted_state is not None):
            raise ValueError("accepted extraction state does not match validation outcome")
        if accepted_state not in {None, "proposing", "manual_review", "ambiguous"}:
            raise ValueError("accepted extraction state is not supported")
        stamp = (now or datetime.now(UTC)).isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM automation_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            payload = db.execute(
                "SELECT * FROM automation_extraction_payloads WHERE payload_id = ?",
                (payload_id,),
            ).fetchone()
            if row is None or payload is None:
                raise KeyError(run_id)
            if (
                str(row["state"]) != "extracting"
                or int(row["state_version"]) != expected_state_version
                or row["current_payload_id"] != payload_id
                or payload["run_id"] != run_id
                or payload["status"] != "reserved"
            ):
                raise RuntimeError("Automation extraction completion lost its expected-state race")
            attempt_no = int(payload["attempt_no"])
            if accepted:
                next_state = str(accepted_state)
                failure_code = accepted_code
                payload_status = "accepted"
            elif attempt_no == 1:
                next_state = "extracting"
                failure_code = "validation_rejected_once"
                payload_status = "rejected"
            else:
                next_state = "manual_review"
                failure_code = "validation_rejected"
                payload_status = "rejected"
            finalized = db.execute(
                """UPDATE automation_extraction_payloads SET
                    status = ?, result_sha256 = ?, result_json = ?, violations_json = ?,
                    next_retry_at = NULL, completed_at = ?
                WHERE payload_id = ? AND run_id = ? AND status = 'reserved'""",
                (
                    payload_status,
                    result_sha256,
                    sqlite3.Binary(result_json),
                    sqlite3.Binary(violations_json),
                    stamp,
                    payload_id,
                    run_id,
                ),
            )
            if finalized.rowcount != 1:
                raise RuntimeError("Automation extraction payload was not reserved")
            next_version = expected_state_version + 1
            changed = db.execute(
                """UPDATE automation_runs SET state = ?, state_version = ?, failure_code = ?,
                    current_payload_sha256 = ?, review_notified_at = NULL, updated_at = ?
                WHERE run_id = ? AND state = 'extracting' AND state_version = ?
                  AND current_payload_id = ?""",
                (
                    next_state,
                    next_version,
                    failure_code,
                    result_sha256,
                    stamp,
                    run_id,
                    expected_state_version,
                    payload_id,
                ),
            )
            if changed.rowcount != 1:
                raise RuntimeError("Automation extraction completion lost its expected-state race")
            _append_automation_event(
                db,
                run_id=run_id,
                sequence_no=next_version - 1,
                previous_state="extracting",
                next_state=next_state,
                state_version=next_version,
                automation_id=str(row["automation_id"]),
                automation_version=int(row["automation_version"]),
                extraction_schema_version=int(row["extraction_schema_version"]),
                calendar_principal_key=str(row["calendar_principal_key"]),
                transition_kind=next_state,
                failure_code=failure_code,
                created_at=stamp,
                payload_id=payload_id,
                payload_sha256=result_sha256,
            )
            updated = db.execute(
                "SELECT * FROM automation_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if updated is None:
            raise RuntimeError("Automation extraction result was not readable")
        return _automation_run(updated)

    def transition_automation_to_review(
        self,
        run_id: str,
        expected_state_version: int,
        *,
        next_state: str,
        failure_code: str,
        now: datetime | None = None,
    ) -> AutomationRun:
        if next_state not in {"manual_review", "source_unavailable"}:
            raise ValueError("automation review transition is not supported")
        stamp = (now or datetime.now(UTC)).isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM automation_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            previous_state = str(row["state"])
            if (
                previous_state not in {"detected", "extracting", "proposing"}
                or int(row["state_version"]) != expected_state_version
            ):
                raise RuntimeError("Automation review transition lost its expected-state race")
            if next_state == "source_unavailable":
                db.execute(
                    "DELETE FROM automation_extraction_payloads WHERE run_id = ?",
                    (run_id,),
                )
            next_version = expected_state_version + 1
            changed = db.execute(
                """UPDATE automation_runs SET state = ?, state_version = ?, failure_code = ?,
                    review_notified_at = NULL, updated_at = ?
                WHERE run_id = ? AND state = ? AND state_version = ?""",
                (
                    next_state,
                    next_version,
                    failure_code,
                    stamp,
                    run_id,
                    previous_state,
                    expected_state_version,
                ),
            )
            if changed.rowcount != 1:
                raise RuntimeError("Automation review transition lost its expected-state race")
            _append_automation_event(
                db,
                run_id=run_id,
                sequence_no=next_version - 1,
                previous_state=previous_state,
                next_state=next_state,
                state_version=next_version,
                automation_id=str(row["automation_id"]),
                automation_version=int(row["automation_version"]),
                extraction_schema_version=int(row["extraction_schema_version"]),
                calendar_principal_key=str(row["calendar_principal_key"]),
                transition_kind=next_state,
                failure_code=failure_code,
                created_at=stamp,
                payload_id=(
                    str(row["current_payload_id"])
                    if row["current_payload_id"] is not None
                    else None
                ),
                payload_sha256=(
                    str(row["current_payload_sha256"])
                    if row["current_payload_sha256"] is not None
                    else None
                ),
            )
            updated = db.execute(
                "SELECT * FROM automation_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if updated is None:
            raise RuntimeError("Automation review transition was not readable")
        return _automation_run(updated)

    def automation_run_for_message(self, message_id: str) -> AutomationRun | None:
        with self.connection() as db:
            row = db.execute(
                """SELECT r.* FROM automation_runs AS r
                LEFT JOIN automation_run_source_identities AS i ON i.run_id = r.run_id
                JOIN messages AS m
                  ON m.provider = r.provider
                 AND m.account_id = r.account_id
                 AND (
                     (i.mailbox_identity_key IS NOT NULL
                      AND m.mailbox_identity_key = i.mailbox_identity_key
                      AND message_source_key(
                            m.provider, m.account_id, m.provider_message_id,
                            m.mailbox_identity_key
                          ) = r.source_message_key)
                     OR (i.mailbox_identity_key IS NULL
                         AND m.mailbox_identity_key IS NULL
                         AND message_source_key(
                                m.provider, m.account_id, m.provider_message_id
                             ) = r.source_message_key)
                 )
                WHERE m.message_id = ?""",
                (message_id,),
            ).fetchone()
        return _automation_run(row) if row is not None else None

    def automation_run(self, run_id: str) -> AutomationRun | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM automation_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return _automation_run(row) if row is not None else None

    def automation_events(self, run_id: str) -> list[AutomationEvent]:
        with self.connection() as db:
            rows = db.execute(
                """SELECT * FROM automation_events
                WHERE run_id = ? ORDER BY sequence_no""",
                (run_id,),
            ).fetchall()
        return [_automation_event(row) for row in rows]

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

    def mark_analyzed(
        self,
        message_id: str,
        result: dict[str, object],
        *,
        mailbox_identity_key: str,
        scheduling_automation_principal_key: str | None = None,
        now: datetime | None = None,
    ) -> None:
        if len(mailbox_identity_key) != 64 or any(
            character not in "0123456789abcdef" for character in mailbox_identity_key
        ):
            raise ValueError("mailbox identity key must be a lower-case SHA-256 digest")
        if scheduling_automation_principal_key is not None and (
            not isinstance(scheduling_automation_principal_key, str)
            or len(scheduling_automation_principal_key) != 64
        ):
            raise ValueError("scheduling automation principal key must be a 64-character string")
        admitted_at = (now or datetime.now(UTC)).astimezone(UTC)
        stamp = admitted_at.isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            source = db.execute(
                """SELECT status, provider, account_id, provider_message_id, discovered_at,
                    mailbox_identity_key, sender, sender_name, subject
                FROM messages WHERE message_id = ?""",
                (message_id,),
            ).fetchone()
            if source is None:
                raise KeyError(message_id)
            if source["status"] != "pending":
                raise RuntimeError("Analysis can only complete for a pending message")
            current_identity = db.execute(
                """SELECT mailbox_identity_key FROM mail_accounts
                WHERE provider = ? AND account_id = ?""",
                (source["provider"], source["account_id"]),
            ).fetchone()
            if (
                current_identity is None
                or current_identity["mailbox_identity_key"] != mailbox_identity_key
                or source["mailbox_identity_key"] != mailbox_identity_key
            ):
                raise MailboxIdentityChanged("mailbox identity changed")
            revision_row = db.execute(
                "SELECT revision FROM automation_rule_set WHERE id = 1"
            ).fetchone()
            if revision_row is None:
                raise RuntimeError("automation rule-set authority is missing")
            rules_revision = int(revision_row["revision"])
            rules = self._current_match_rules(db)
            attachment_count = db.execute(
                """SELECT COUNT(*) AS count FROM message_attachments
                WHERE message_id = ?""",
                (message_id,),
            ).fetchone()
            if attachment_count is None:
                raise RuntimeError("automation attachment count is unavailable")
            evaluation_error: str | None = None
            matched = []
            if int(attachment_count["count"]) > MAX_AUTOMATION_ATTACHMENTS:
                evaluation_error = "automation_fanout_limit"
            else:
                attachment_rows = db.execute(
                    """SELECT part_id, attachment_id, filename, media_type, byte_size,
                        position, byte_size_known
                    FROM message_attachments WHERE message_id = ?
                    ORDER BY position, part_id""",
                    (message_id,),
                ).fetchall()
                attachments = [
                    AttachmentDescriptor(
                        part_id=str(row["part_id"]),
                        attachment_id=(
                            str(row["attachment_id"]) if row["attachment_id"] is not None else None
                        ),
                        filename=str(row["filename"]),
                        media_type=str(row["media_type"]),
                        byte_size=int(row["byte_size"]),
                        position=int(row["position"]),
                        byte_size_known=bool(row["byte_size_known"]),
                    )
                    for row in attachment_rows
                ]
                try:
                    matched = match_rules(
                        rules,
                        provider=str(source["provider"]),
                        account_id=str(source["account_id"]),
                        mailbox_identity_key=mailbox_identity_key,
                        sender=str(source["sender"]),
                        sender_name=(
                            str(source["sender_name"])
                            if source["sender_name"] is not None
                            else None
                        ),
                        subject=str(source["subject"]),
                        result=result,
                        attachments=attachments,
                    )
                except AutomationFanoutLimit:
                    evaluation_error = "automation_fanout_limit"
            updated = db.execute(
                """UPDATE messages SET status='analyzed', analysis_at=?, attempts=0,
                next_retry_at=NULL, last_error=NULL, analysis_request_id=NULL,
                analysis_context_at=NULL, analysis_body_char_limit=NULL,
                analysis_retryable=NULL, analysis_error_code=NULL,
                analysis_retry_after_seconds=NULL,
                category=?, priority=?, summary=?, action_required=?, suggested_action=?,
                deadline_text=?, deadline_iso=?, confidence=?,
                rules_revision_at_analysis=?, rules_evaluation_error=?
                WHERE message_id=? AND status='pending'""",
                (
                    stamp,
                    result["category"],
                    result["priority"],
                    result["summary"],
                    int(bool(result["action_required"])),
                    result.get("suggested_action"),
                    result.get("deadline_text"),
                    result.get("deadline_iso"),
                    result["confidence"],
                    rules_revision,
                    evaluation_error,
                    message_id,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError("Analysis completion lost its expected-state race")
            if evaluation_error is None:
                event_id = _message_suppression_key(
                    str(source["provider"]),
                    str(source["account_id"]),
                    str(source["provider_message_id"]),
                    mailbox_identity_key,
                )
                for fire in matched:
                    fire_id = str(uuid.uuid4())
                    db.execute(
                        """INSERT INTO automation_fires(
                            fire_id, event_id, rule_id, rule_version, message_id, part_id,
                            action_kind, state, state_version, pending_since, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'connect.invoke',
                            'pending_dispatch', 1, ?, ?, ?)""",
                        (
                            fire_id,
                            event_id,
                            fire.rule_id,
                            fire.rule_version,
                            message_id,
                            fire.part_id,
                            stamp,
                            stamp,
                            stamp,
                        ),
                    )
                    db.execute(
                        """INSERT INTO automation_fire_attempts(
                            fire_id, attempt_no, dispatch_request_id, created_at
                        ) VALUES (?, 1, ?, ?)""",
                        (fire_id, str(uuid.uuid4()), stamp),
                    )
            if (
                scheduling_automation_principal_key is not None
                and result["category"] == "scheduling"
            ):
                _admit_scheduling_automation(
                    db,
                    provider=str(source["provider"]),
                    account_id=str(source["account_id"]),
                    calendar_principal_key=scheduling_automation_principal_key,
                    provider_message_id=str(source["provider_message_id"]),
                    mailbox_identity_key=mailbox_identity_key,
                    created_at=stamp,
                    expires_at=_automation_expiry(str(source["discovered_at"]), admitted_at),
                )

    def mark_delivery_complete(self, message_id: str, notified: bool) -> None:
        with self.connection() as db:
            db.execute(
                """UPDATE messages SET status='summarized', next_retry_at=NULL,
                last_error=NULL, notified_at=? WHERE message_id=?""",
                (datetime.now(UTC).isoformat() if notified else None, message_id),
            )

    def notification_intents(
        self,
        limit: int = 25,
        *,
        kind: str | None = None,
    ) -> list[NotificationIntent]:
        with self.connection() as db:
            rows = db.execute(
                """SELECT subject_type, subject_id, revision, message_id, kind,
                    sender, sender_name, subject, analysis_at, priority, summary,
                    suggested_action, deadline_iso, last_error
                FROM (
                SELECT 'message' AS subject_type, message_id AS subject_id,
                COALESCE(analysis_at, 'fallback') AS revision, message_id,
                CASE WHEN status = 'analyzed' THEN 'analysis' ELSE 'fallback' END AS kind,
                sender, sender_name, subject, analysis_at, priority, summary,
                suggested_action, deadline_iso, last_error, received_at AS sort_at
                FROM messages
                WHERE (status = 'analyzed' AND notified_at IS NULL)
                   OR (status = 'pending' AND last_error IS NOT NULL
                       AND fallback_notified_at IS NULL)
                UNION ALL
                SELECT 'automation_run' AS subject_type, r.run_id AS subject_id,
                    CAST(r.state_version AS TEXT) AS revision, r.run_id AS message_id,
                    'automation_review' AS kind,
                    COALESCE(m.sender, 'Email Watcher') AS sender,
                    m.sender_name,
                    COALESCE(m.subject, 'Scheduling request') AS subject,
                    r.updated_at AS analysis_at, 'normal' AS priority,
                    CASE r.failure_code
                        WHEN 'validation_rejected' THEN
                            'A scheduling mention could not be validated safely.'
                        WHEN 'reschedule_not_supported' THEN
                            'A reschedule request needs manual review.'
                        WHEN 'cancellation_not_supported' THEN
                            'A cancellation request needs manual review.'
                        WHEN 'source_unavailable' THEN
                            'The scheduling source is no longer available for review.'
                        WHEN 'source_changed' THEN
                            'The scheduling source changed before extraction completed.'
                        WHEN 'source_invalid' THEN
                            'The scheduling source could not be processed safely.'
                        ELSE 'A scheduling mention needs manual review.'
                    END AS summary,
                    NULL AS suggested_action, NULL AS deadline_iso,
                    r.failure_code AS last_error, r.updated_at AS sort_at
                FROM automation_runs AS r
                LEFT JOIN automation_run_source_identities AS i ON i.run_id = r.run_id
                LEFT JOIN messages AS m
                  ON m.provider = r.provider
                 AND m.account_id = r.account_id
                 AND (
                     (i.mailbox_identity_key IS NOT NULL
                      AND m.mailbox_identity_key = i.mailbox_identity_key
                      AND message_source_key(
                            m.provider, m.account_id, m.provider_message_id,
                            m.mailbox_identity_key
                          ) = r.source_message_key)
                     OR (i.mailbox_identity_key IS NULL
                         AND m.mailbox_identity_key IS NULL
                         AND message_source_key(
                                m.provider, m.account_id, m.provider_message_id
                             ) = r.source_message_key)
                 )
                WHERE r.state IN ('ambiguous', 'manual_review', 'source_unavailable')
                  AND r.review_notified_at IS NULL
                ) AS intents
                WHERE (? IS NULL OR kind = ?)
                ORDER BY sort_at LIMIT ?""",
                (kind, kind, limit),
            ).fetchall()
        return [NotificationIntent(**dict(row)) for row in rows]

    def notification_intent_count(self) -> int:
        with self.connection() as db:
            row = db.execute(
                """SELECT
                    (SELECT COUNT(*) FROM messages
                     WHERE (status = 'analyzed' AND notified_at IS NULL)
                        OR (status = 'pending' AND last_error IS NOT NULL
                            AND fallback_notified_at IS NULL))
                    +
                    (SELECT COUNT(*) FROM automation_runs
                     WHERE state IN ('ambiguous', 'manual_review', 'source_unavailable')
                       AND review_notified_at IS NULL) AS count"""
            ).fetchone()
        return int(row["count"])

    def acknowledge_notification(
        self,
        *,
        message_id: str,
        kind: str,
        analysis_at: str | None = None,
        subject_type: str | None = None,
        subject_id: str | None = None,
        revision: str | None = None,
    ) -> str:
        if kind not in {"analysis", "fallback", "automation_review"}:
            raise ValueError("Notification kind is not supported")
        if subject_type is not None or subject_id is not None or revision is not None:
            if not subject_type or not subject_id or revision is None:
                raise ValueError("Notification subject identity must be complete")
            if message_id != subject_id:
                raise ValueError("Notification compatibility identity does not match its subject")
        if kind == "automation_review":
            if subject_type not in {None, "automation_run"}:
                raise ValueError("Automation notification subject is invalid")
            run_id = subject_id or message_id
            state_version_text = revision or analysis_at
            try:
                state_version = int(state_version_text or "")
            except ValueError as exc:
                raise ValueError("Automation notification revision is invalid") from exc
            stamp = datetime.now(UTC).isoformat()
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    """SELECT state, state_version, review_notified_at
                    FROM automation_runs WHERE run_id = ?""",
                    (run_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(run_id)
                if int(row["state_version"]) != state_version or str(row["state"]) not in {
                    "ambiguous",
                    "manual_review",
                    "source_unavailable",
                }:
                    raise RuntimeError("Automation notification intent is no longer current")
                if row["review_notified_at"] is not None:
                    return "already_acknowledged"
                changed = db.execute(
                    """UPDATE automation_runs SET review_notified_at = ?
                    WHERE run_id = ? AND state_version = ? AND review_notified_at IS NULL""",
                    (stamp, run_id, state_version),
                )
                if changed.rowcount != 1:
                    raise RuntimeError("Automation notification acknowledgement lost its race")
            return "acknowledged"
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
                analysis_error_code, analysis_retry_after_seconds,
                admission_kind, admission_selector_id, admission_display_name, admitted_at
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
            admission_kind = item.pop("admission_kind")
            admission_selector_id = item.pop("admission_selector_id")
            admission_display_name = item.pop("admission_display_name")
            admitted_at = item.pop("admitted_at")
            item["admission"] = (
                {
                    "kind": admission_kind,
                    "selector_id": admission_selector_id,
                    "display_name": admission_display_name,
                    "admitted_at": admitted_at,
                }
                if admission_kind is not None
                else None
            )
        if not items:
            return []
        message_ids = [str(item["message_id"]) for item in items]
        placeholders = ",".join("?" for _ in message_ids)
        attachment_rows = db.execute(
            f"""SELECT message_id, part_id, attachment_id, filename, media_type, byte_size,
                byte_size_known
            FROM message_attachments WHERE message_id IN ({placeholders})
            ORDER BY message_id, position""",
            message_ids,
        ).fetchall()
        fire_rows = db.execute(
            f"""SELECT fire_id, rule_id, rule_version, message_id, part_id,
                state, state_version, reason, job_id, prepared_identity_sha256, updated_at
            FROM automation_fires WHERE message_id IN ({placeholders})
            ORDER BY created_at, fire_id""",
            message_ids,
        ).fetchall()
        connect_rows = db.execute(
            f"""SELECT latest.*,
                d.state AS dispatch_state,
                d.next_attempt_at AS dispatch_next_attempt_at,
                d.last_error_code AS dispatch_error_code,
                d.last_error_message AS dispatch_error_message
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
                ) AS ranked
                WHERE recency = 1
                   OR EXISTS (
                        SELECT 1 FROM automation_fires AS fire
                        WHERE fire.job_id = ranked.job_id
                   )
            ) AS latest
            LEFT JOIN connect_job_dispatch AS d ON d.job_id = latest.job_id
            ORDER BY latest.created_at DESC""",
            message_ids,
        ).fetchall()
        queue_rows = db.execute(
            """SELECT j.job_id, j.provider_app_id, j.provider_instance_id,
                j.created_at, d.state
            FROM connect_attachment_jobs AS j
            JOIN connect_job_dispatch AS d ON d.job_id = j.job_id
            WHERE j.protocol_version = 2
              AND j.status IN ('requested', 'accepted', 'processing')
              AND d.state IN ('waiting', 'dispatching', 'reconciling', 'provider_owned')
              AND (
                  d.automation_paused_at IS NULL
                  OR d.state IN ('dispatching', 'reconciling', 'provider_owned')
              )
            ORDER BY j.provider_app_id, j.provider_instance_id,
                CASE
                    WHEN d.state = 'dispatching' THEN 0
                    WHEN d.state IN ('reconciling', 'provider_owned') THEN 1
                    ELSE 2 END,
                j.created_at, j.job_id"""
        ).fetchall()
        queue_positions: dict[str, int] = {}
        current_lane: tuple[str, str] | None = None
        lane_position = 0
        for row in queue_rows:
            lane = (str(row["provider_app_id"]), str(row["provider_instance_id"]))
            if lane != current_lane:
                current_lane = lane
                lane_position = 0
            queue_positions[str(row["job_id"])] = lane_position
            lane_position += 1
        proposal_rows = db.execute(
            f"""SELECT p.*, r.state AS run_state, r.state_version, r.provider, r.account_id,
                a.display_name AS account_display_name,
                a.address AS account_address, m.message_id,
                w.status AS write_status, w.graph_event_id
            FROM automation_proposal_payloads AS p
            JOIN automation_runs AS r
              ON r.run_id = p.run_id AND r.current_payload_id = p.payload_id
            LEFT JOIN automation_calendar_writes AS w ON w.run_id = r.run_id
            LEFT JOIN automation_run_source_identities AS i ON i.run_id = r.run_id
            JOIN messages AS m
              ON m.provider = r.provider
             AND m.account_id = r.account_id
             AND (
                 (i.mailbox_identity_key IS NOT NULL
                  AND m.mailbox_identity_key = i.mailbox_identity_key
                  AND message_source_key(
                        m.provider, m.account_id, m.provider_message_id,
                        m.mailbox_identity_key
                      ) = r.source_message_key)
                 OR (i.mailbox_identity_key IS NULL
                     AND m.mailbox_identity_key IS NULL
                     AND message_source_key(
                            m.provider, m.account_id, m.provider_message_id
                         ) = r.source_message_key)
             )
            JOIN mail_accounts AS a
              ON a.provider = r.provider AND a.account_id = r.account_id
            WHERE m.message_id IN ({placeholders})
              AND (
                  (r.state IN (
                      'awaiting_confirmation', 'declined', 'write_authorized', 'writing',
                      'unresolved', 'reconciling', 'completed', 'failed'
                  ) AND p.status = 'accepted')
                  OR (r.state = 'manual_review' AND p.status = 'no_suggestions')
              )""",
            message_ids,
        ).fetchall()
        attachments_by_message: dict[str, list[dict[str, object]]] = {
            message_id: [] for message_id in message_ids
        }
        for row in attachment_rows:
            attachment = dict(row)
            message_id = str(attachment.pop("message_id"))
            attachment.pop("byte_size_known", None)
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
                if row["dispatch_state"] is None:
                    raise RuntimeError("Connect v2 inbox result is missing dispatch state")
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
                item["dispatch_state"] = str(row["dispatch_state"])
                item["queue_ahead"] = queue_positions.get(str(row["job_id"]), 0)
                item["next_attempt_at"] = row["dispatch_next_attempt_at"]
                if (
                    row["dispatch_error_code"] is not None
                    and row["dispatch_error_message"] is not None
                ):
                    item["dispatch_error"] = {
                        "code": str(row["dispatch_error_code"]),
                        "message": str(row["dispatch_error_message"]),
                    }
            if row["status"] == "completed":
                completed = ConnectJob(
                    **{name: row[name] for name in ConnectJob.__dataclass_fields__}
                )
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
        fires_by_attachment: dict[tuple[str, str], list[dict[str, object]]] = {}
        for row in fire_rows:
            key = (str(row["message_id"]), str(row["part_id"]))
            fires_by_attachment.setdefault(key, []).append(
                {
                    "fire_id": str(row["fire_id"]),
                    "rule_id": str(row["rule_id"]),
                    "rule_version": int(row["rule_version"]),
                    "state": str(row["state"]),
                    "state_version": int(row["state_version"]),
                    "reason": str(row["reason"]) if row["reason"] is not None else None,
                    "job_id": str(row["job_id"]) if row["job_id"] is not None else None,
                    "prepared_identity_sha256": (
                        str(row["prepared_identity_sha256"])
                        if row["prepared_identity_sha256"] is not None
                        else None
                    ),
                    "updated_at": str(row["updated_at"]),
                }
            )
        proposal_by_message: dict[str, dict[str, object]] = {}
        proposal_fields = AutomationProposalPayload.__dataclass_fields__
        for row in proposal_rows:
            proposal = AutomationProposalPayload(**{name: row[name] for name in proposal_fields})
            proposal_by_message[str(row["message_id"])] = {
                "run_id": proposal.run_id,
                "state": str(row["run_state"]),
                "state_version": int(row["state_version"]),
                "proposal_version": proposal.proposal_version,
                "proposal_sha256": proposal.proposal_sha256,
                "status": proposal.status,
                "provider": str(row["provider"]),
                "account_id": str(row["account_id"]),
                "account_display_name": str(row["account_display_name"]),
                "account_address": (
                    str(row["account_address"]) if row["account_address"] is not None else None
                ),
                "subject": proposal.subject,
                "attendees": list(proposal.attendees),
                "start": proposal.start,
                "end": proposal.end,
                "timezone": proposal.timezone,
                "suggestion_reason": proposal.suggestion_reason,
                "empty_reason": proposal.empty_reason,
                "observed_at": proposal.observed_at,
                "expires_at": proposal.expires_at,
                "write_status": (
                    str(row["write_status"]) if row["write_status"] is not None else None
                ),
                "graph_event_id": (
                    str(row["graph_event_id"]) if row["graph_event_id"] is not None else None
                ),
            }
        for message_id, attachments in attachments_by_message.items():
            for attachment in attachments:
                capability_results = connect_by_attachment.get(
                    (message_id, str(attachment["part_id"]))
                )
                if capability_results:
                    attachment["capability_results"] = capability_results
                automation_fires = fires_by_attachment.get(
                    (message_id, str(attachment["part_id"]))
                )
                if automation_fires:
                    attachment["automation_fires"] = automation_fires
        for item in items:
            message_id = str(item["message_id"])
            item["attachments"] = attachments_by_message[message_id]
            item["calendar_proposal"] = proposal_by_message.get(message_id)
        return items

    def purge_with_outcome(
        self,
        retention_days: int,
        *,
        now: datetime | None = None,
    ) -> PurgeOutcome:
        stamp = (now or datetime.now(UTC)).astimezone(UTC)
        cutoff = stamp - timedelta(days=retention_days)
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        stamp_epoch = (stamp - epoch).total_seconds()
        cutoff_epoch = (cutoff - epoch).total_seconds()
        expiry_predicate = """aware_iso_epoch(received_at) IS NULL
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
                   )"""
        expiry_parameters = (
            stamp_epoch,
            cutoff_epoch,
            stamp_epoch,
            stamp_epoch,
            cutoff_epoch,
        )
        with self.connection() as db:
            expired_ids = [
                str(row["message_id"])
                for row in db.execute(
                    f"""SELECT message_id FROM messages
                    WHERE {expiry_predicate} ORDER BY message_id""",
                    expiry_parameters,
                ).fetchall()
            ]
        deleted = 0
        automation_review_required = 0
        for offset in range(0, len(expired_ids), SOURCE_CLEANUP_LOCK_BATCH_SIZE):
            chunk = expired_ids[offset : offset + SOURCE_CLEANUP_LOCK_BATCH_SIZE]
            with self._source_cleanup_locks(chunk), self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                placeholders = ", ".join("?" for _ in chunk)
                current_expired = [
                    str(row["message_id"])
                    for row in db.execute(
                        f"""SELECT message_id FROM messages
                            WHERE message_id IN ({placeholders})
                              AND ({expiry_predicate})""",
                        (*chunk, *expiry_parameters),
                    ).fetchall()
                ]
                automation_review_required += _mark_automation_sources_unavailable(
                    db,
                    current_expired,
                    updated_at=stamp.isoformat(),
                )
                if current_expired:
                    current_placeholders = ", ".join("?" for _ in current_expired)
                    cursor = db.execute(
                        f"DELETE FROM messages WHERE message_id IN ({current_placeholders})",
                        tuple(current_expired),
                    )
                    deleted += cursor.rowcount
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            _purge_expired_automation_tombstones(
                db,
                now=stamp.isoformat(),
            )
            db.execute(
                """DELETE FROM suppressed_messages
                    WHERE julianday(expires_at) IS NULL
                       OR julianday(expires_at) < julianday(?)""",
                (stamp.isoformat(),),
            )
        return PurgeOutcome(deleted, automation_review_required)

    def purge(self, retention_days: int, *, now: datetime | None = None) -> int:
        return self.purge_with_outcome(retention_days, now=now).messages

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
