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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import MAX_RETENTION_DAYS, normalize_validated_address
from .mailbox import DEFAULT_MAIL_ACCOUNT_ID, DEFAULT_MAIL_PROVIDER
from .mime import AttachmentDescriptor

SCHEMA_VERSION = 17
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


class AutomationSourceChanged(RuntimeError):
    """A recoverable automation source no longer matches its first extraction input."""


def _automation_run(row: sqlite3.Row) -> AutomationRun:
    return AutomationRun(**dict(row))


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
    created_at: str,
    expires_at: str,
) -> None:
    source_message_key = _message_suppression_key(provider, account_id, provider_message_id)
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
            JOIN messages AS m
              ON m.provider = r.provider
             AND m.account_id = r.account_id
             AND message_source_key(
                    m.provider, m.account_id, m.provider_message_id
                 ) = r.source_message_key
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
                    str(row["transaction_id"])
                    if row["transaction_id"] is not None
                    else None
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
        WHERE aware_iso_epoch(expires_at) IS NULL
           OR aware_iso_epoch(expires_at) <= aware_iso_epoch(?)
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
                + _AUTOMATION_TABLES_SQL
            )
            _ensure_mailbox_scope_schema(db)
            _ensure_connect_jobs_schema(db, version)
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
                    db.execute(
                        f"ALTER TABLE automation_events ADD COLUMN {column} {definition}"
                    )
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
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                """SELECT message_id, provider, account_id, provider_message_id, received_at
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
            _mark_automation_sources_unavailable(
                db,
                [str(row["message_id"]) for row in rows],
                updated_at=stamp.isoformat(),
            )
            cursor = db.execute("DELETE FROM messages")
            _purge_expired_automation_tombstones(db, now=stamp.isoformat())
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
                    p.organizer_address AS extraction_organizer_address
                FROM automation_runs AS r
                JOIN messages AS m
                  ON m.provider = r.provider
                 AND m.account_id = r.account_id
                 AND message_source_key(
                        m.provider, m.account_id, m.provider_message_id
                     ) = r.source_message_key
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
                JOIN messages AS m
                  ON m.provider = r.provider
                 AND m.account_id = r.account_id
                 AND message_source_key(
                        m.provider, m.account_id, m.provider_message_id
                     ) = r.source_message_key
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
                        str(row["next_retry_at"])
                        if row["next_retry_at"] is not None
                        else None
                    ),
                    last_error_code=(
                        str(row["last_error_code"])
                        if row["last_error_code"] is not None
                        else None
                    ),
                    result_sha256=(
                        str(row["result_sha256"])
                        if row["result_sha256"] is not None
                        else None
                    ),
                    result_json=(
                        bytes(row["result_json"])
                        if row["result_json"] is not None
                        else None
                    ),
                    violations_json=(
                        bytes(row["violations_json"])
                        if row["violations_json"] is not None
                        else None
                    ),
                    created_at=str(row["extraction_created_at"]),
                    completed_at=(
                        str(row["completed_at"])
                        if row["completed_at"] is not None
                        else None
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

    def automation_proposal_for_message(
        self, message_id: str
    ) -> AutomationProposalPayload | None:
        with self.connection() as db:
            row = db.execute(
                """SELECT p.* FROM automation_proposal_payloads AS p
                JOIN automation_runs AS r
                  ON r.run_id = p.run_id AND r.current_payload_id = p.payload_id
                JOIN messages AS m
                  ON m.provider = r.provider
                 AND m.account_id = r.account_id
                 AND message_source_key(
                        m.provider, m.account_id, m.provider_message_id
                     ) = r.source_message_key
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
        accepted = all(
            value is not None for value in (start, end, timezone, suggestion_reason)
        ) and bool(suggestion_reason) and empty_reason is None
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
            row = db.execute(
                "SELECT * FROM automation_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
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
            row = db.execute(
                "SELECT * FROM automation_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
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
                  AND aware_iso_epoch(r.expires_at) > ?
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
            row = db.execute(
                "SELECT * FROM automation_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
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
                """SELECT 1 FROM messages AS m WHERE m.provider = ? AND m.account_id = ?
                AND message_source_key(m.provider, m.account_id, m.provider_message_id) = ?""",
                (row["provider"], row["account_id"], row["source_message_key"]),
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
            row = db.execute(
                "SELECT * FROM automation_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            write = db.execute(
                "SELECT * FROM automation_calendar_writes WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None or write is None:
                raise KeyError(run_id)
            previous_state = str(row["state"])
            if (
                (previous_state, next_state) not in allowed
                or int(row["state_version"]) != expected_state_version
            ):
                raise RuntimeError("Automation write transition lost its expected-state race")
            if next_state == "completed":
                if not isinstance(graph_event_id, str) or not 1 <= len(
                    graph_event_id.encode("utf-8")
                ) <= 512:
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
                JOIN messages AS m
                  ON m.provider = r.provider
                 AND m.account_id = r.account_id
                 AND message_source_key(
                        m.provider, m.account_id, m.provider_message_id
                     ) = r.source_message_key
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
        scheduling_automation_principal_key: str | None = None,
        now: datetime | None = None,
    ) -> None:
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
                """SELECT status, provider, account_id, provider_message_id, discovered_at
                FROM messages WHERE message_id = ?""",
                (message_id,),
            ).fetchone()
            if source is None:
                raise KeyError(message_id)
            if source["status"] != "pending":
                raise RuntimeError("Analysis can only complete for a pending message")
            updated = db.execute(
                """UPDATE messages SET status='analyzed', analysis_at=?, attempts=0,
                next_retry_at=NULL, last_error=NULL, analysis_request_id=NULL,
                analysis_context_at=NULL, analysis_body_char_limit=NULL,
                analysis_retryable=NULL, analysis_error_code=NULL,
                analysis_retry_after_seconds=NULL,
                category=?, priority=?, summary=?, action_required=?, suggested_action=?,
                deadline_text=?, deadline_iso=?, confidence=?
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
                    message_id,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError("Analysis completion lost its expected-state race")
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
                LEFT JOIN messages AS m
                  ON m.provider = r.provider
                 AND m.account_id = r.account_id
                 AND message_source_key(
                        m.provider, m.account_id, m.provider_message_id
                     ) = r.source_message_key
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
        proposal_rows = db.execute(
            f"""SELECT p.*, r.state AS run_state, r.state_version, r.provider, r.account_id,
                a.display_name AS account_display_name,
                a.address AS account_address, m.message_id,
                w.status AS write_status, w.graph_event_id
            FROM automation_proposal_payloads AS p
            JOIN automation_runs AS r
              ON r.run_id = p.run_id AND r.current_payload_id = p.payload_id
            LEFT JOIN automation_calendar_writes AS w ON w.run_id = r.run_id
            JOIN messages AS m
              ON m.provider = r.provider
             AND m.account_id = r.account_id
             AND message_source_key(
                    m.provider, m.account_id, m.provider_message_id
                 ) = r.source_message_key
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
        proposal_by_message: dict[str, dict[str, object]] = {}
        proposal_fields = AutomationProposalPayload.__dataclass_fields__
        for row in proposal_rows:
            proposal = AutomationProposalPayload(
                **{name: row[name] for name in proposal_fields}
            )
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
                    str(row["account_address"])
                    if row["account_address"] is not None
                    else None
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
                    str(row["graph_event_id"])
                    if row["graph_event_id"] is not None
                    else None
                ),
            }
        for message_id, attachments in attachments_by_message.items():
            for attachment in attachments:
                capability_results = connect_by_attachment.get(
                    (message_id, str(attachment["part_id"]))
                )
                if capability_results:
                    attachment["capability_results"] = capability_results
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
            db.execute("BEGIN IMMEDIATE")
            expired = db.execute(
                f"SELECT message_id FROM messages WHERE {expiry_predicate}",
                expiry_parameters,
            ).fetchall()
            automation_review_required = _mark_automation_sources_unavailable(
                db,
                [str(row["message_id"]) for row in expired],
                updated_at=stamp.isoformat(),
            )
            cursor = db.execute(
                f"DELETE FROM messages WHERE {expiry_predicate}",
                expiry_parameters,
            )
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
        return PurgeOutcome(cursor.rowcount, automation_review_required)

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
