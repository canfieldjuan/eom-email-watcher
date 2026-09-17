"""Durable workflow record, lifecycle ledger, and stage engine for the Automate host.

This is the host's own application-private store (ADR-0005 keeps app databases outside
the Connect interoperability contract). It reuses the proven patterns from
``eom_email_watcher.db``: a ``BEGIN IMMEDIATE`` writer, an append-only event log with
``UNIQUE(record_id, sequence_no)`` and ``UNIQUE(record_id, state_version)``, a
``sequence_no = 0`` bootstrap row, immutable-log triggers, and optimistic concurrency on
``state_version``.

A workflow record is free-standing: it is not anchored to a single source message, unlike
``automation_runs``. Slice 1 provides create and a single transition primitive with
operation-key idempotency. Each operation key is bound to a canonical operation name and a
request fingerprint: an identical replay is a no-op that returns the prior outcome, and the
same key with a changed request is a conflict. Triggers, effects, and the rule control
plane arrive in later slices; the host gates admission of any transition behind
``AutomateHost.require_license``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

CREATE_OPERATION_NAME = "create"

_SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS workflow_records (
    record_id TEXT PRIMARY KEY CHECK (length(record_id) = 36),
    workflow TEXT NOT NULL CHECK (workflow <> ''),
    stage TEXT NOT NULL CHECK (stage <> ''),
    state_version INTEGER NOT NULL CHECK (state_version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workflow_events (
    event_id TEXT PRIMARY KEY CHECK (length(event_id) = 36),
    record_id TEXT NOT NULL CHECK (record_id <> ''),
    sequence_no INTEGER NOT NULL CHECK (sequence_no >= 0),
    previous_stage TEXT CHECK (previous_stage IS NULL OR previous_stage <> ''),
    next_stage TEXT NOT NULL CHECK (next_stage <> ''),
    state_version INTEGER NOT NULL CHECK (state_version >= 1),
    operation_name TEXT NOT NULL CHECK (operation_name <> ''),
    operation_key TEXT CHECK (operation_key IS NULL OR operation_key <> ''),
    request_fingerprint TEXT CHECK (
        request_fingerprint IS NULL OR length(request_fingerprint) = 64
    ),
    created_at TEXT NOT NULL,
    UNIQUE (record_id, sequence_no),
    UNIQUE (record_id, state_version),
    CHECK (
        (previous_stage IS NULL AND sequence_no = 0 AND state_version = 1
            AND operation_name = 'create' AND operation_key IS NULL)
        OR (previous_stage IS NOT NULL AND sequence_no >= 1
            AND state_version = sequence_no + 1)
    )
);
CREATE INDEX IF NOT EXISTS idx_workflow_events_record
    ON workflow_events(record_id, sequence_no);
CREATE TABLE IF NOT EXISTS workflow_operations (
    record_id TEXT NOT NULL CHECK (record_id <> ''),
    operation_key TEXT NOT NULL CHECK (operation_key <> ''),
    operation_name TEXT NOT NULL CHECK (operation_name <> ''),
    request_fingerprint TEXT NOT NULL CHECK (length(request_fingerprint) = 64),
    event_id TEXT NOT NULL CHECK (length(event_id) = 36),
    created_at TEXT NOT NULL,
    PRIMARY KEY (record_id, operation_key)
);
CREATE TRIGGER IF NOT EXISTS workflow_events_no_update
BEFORE UPDATE ON workflow_events
BEGIN
    SELECT RAISE(ABORT, 'workflow_events are immutable');
END;
CREATE TRIGGER IF NOT EXISTS workflow_events_no_delete
BEFORE DELETE ON workflow_events
BEGIN
    SELECT RAISE(ABORT, 'workflow_events are immutable');
END;
COMMIT;
"""


class WorkflowStoreError(RuntimeError):
    """Base class for workflow store errors."""


class UnknownRecord(WorkflowStoreError):
    """Raised when a transition names a record that does not exist."""


class StaleRecord(WorkflowStoreError):
    """Raised when the caller's expected state_version does not match the record.

    This is the optimistic-concurrency (compare-and-set) conflict.
    """

    def __init__(self, *, expected: int, actual: int):
        super().__init__(f"record is at state_version {actual}, not the expected {expected}")
        self.expected = expected
        self.actual = actual


class OperationConflict(WorkflowStoreError):
    """Raised when an operation key is reused with a different name or request.

    The portal's equivalent is a 409: a key is rotated for changed intent, so a mismatched
    fingerprint must be refused rather than silently replaying the earlier outcome.
    """

    def __init__(self, *, operation_key: str):
        super().__init__(
            f"operation key {operation_key!r} was already used with a different "
            "operation or request"
        )
        self.operation_key = operation_key


@dataclass(frozen=True)
class RecordView:
    record_id: str
    workflow: str
    stage: str
    state_version: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class TransitionOutcome:
    record: RecordView
    event_id: str
    # False when an idempotent replay returned the prior outcome without a new event.
    applied: bool


def request_fingerprint(request: Mapping[str, object]) -> str:
    """A stable SHA-256 over the canonical JSON of a request payload.

    Keys are sorted and separators are fixed so the same logical request always yields the
    same fingerprint, and any change to the request yields a different one.
    """
    canonical = json.dumps(dict(request), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _new_id() -> str:
    return str(uuid.uuid4())


def _restrict(path: Path, mode: int) -> None:
    """Best-effort chmod; tolerated where the platform cannot honor POSIX modes."""
    with contextlib.suppress(OSError, NotImplementedError):
        path.chmod(mode)


def _record_view(row: sqlite3.Row) -> RecordView:
    return RecordView(
        record_id=row["record_id"],
        workflow=row["workflow"],
        stage=row["stage"],
        state_version=row["state_version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class WorkflowStore:
    """SQLite-backed durable store for workflow records and their lifecycle ledger."""

    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.row_factory = sqlite3.Row
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
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # mkdir does not tighten an already-existing directory, and SQLite creates the
        # database file under the process umask (often 0644). This ledger holds private
        # record names and stages, so restrict both the directory and the file to the
        # owner, mirroring the entitlement store's 0700/0600 handling.
        _restrict(parent, 0o700)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(_SCHEMA)
        _restrict(self.path, 0o600)
        for suffix in ("-wal", "-shm"):
            sidecar = self.path.with_name(self.path.name + suffix)
            if sidecar.exists():
                _restrict(sidecar, 0o600)

    def create_record(
        self,
        workflow: str,
        initial_stage: str,
        *,
        now: datetime,
        allowed_stages: frozenset[str] | None = None,
    ) -> RecordView:
        """Create a workflow record with a bootstrap ledger event at state_version 1."""
        if allowed_stages is not None and initial_stage not in allowed_stages:
            raise ValueError(f"stage {initial_stage!r} is not in the allowed set")
        record_id = _new_id()
        timestamp = now.isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO workflow_records "
                "(record_id, workflow, stage, state_version, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?)",
                (record_id, workflow, initial_stage, timestamp, timestamp),
            )
            db.execute(
                "INSERT INTO workflow_events "
                "(event_id, record_id, sequence_no, previous_stage, next_stage, "
                "state_version, operation_name, operation_key, request_fingerprint, "
                "created_at) "
                "VALUES (?, ?, 0, NULL, ?, 1, ?, NULL, NULL, ?)",
                (_new_id(), record_id, initial_stage, CREATE_OPERATION_NAME, timestamp),
            )
            row = db.execute(
                "SELECT * FROM workflow_records WHERE record_id = ?", (record_id,)
            ).fetchone()
        return _record_view(row)

    def get_record(self, record_id: str) -> RecordView:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM workflow_records WHERE record_id = ?", (record_id,)
            ).fetchone()
        if row is None:
            raise UnknownRecord(record_id)
        return _record_view(row)

    def transition(
        self,
        record_id: str,
        to_stage: str,
        *,
        operation_key: str,
        operation_name: str,
        request: Mapping[str, object],
        expected_version: int,
        now: datetime,
        allowed_stages: frozenset[str] | None = None,
    ) -> TransitionOutcome:
        """Advance a record to ``to_stage`` under compare-and-set and operation-key rules.

        Ordering inside one ``BEGIN IMMEDIATE`` transaction:

        1. The operation key is looked up first. An exact match (same operation name and
           request fingerprint) is an idempotent replay and returns the prior outcome with
           ``applied=False`` and no new event, regardless of ``expected_version``. A
           mismatch raises :class:`OperationConflict`.
        2. Only a genuinely new operation enforces compare-and-set: ``expected_version``
           must equal the record's current ``state_version`` or :class:`StaleRecord` is
           raised.
        3. The transition then appends one immutable event, advances the record, and binds
           the operation key to its name, fingerprint, and event.
        """
        fingerprint = request_fingerprint(request)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            record = db.execute(
                "SELECT * FROM workflow_records WHERE record_id = ?", (record_id,)
            ).fetchone()
            if record is None:
                raise UnknownRecord(record_id)

            prior = db.execute(
                "SELECT operation_name, request_fingerprint, event_id "
                "FROM workflow_operations WHERE record_id = ? AND operation_key = ?",
                (record_id, operation_key),
            ).fetchone()
            if prior is not None:
                prior_event = db.execute(
                    "SELECT next_stage, state_version, created_at "
                    "FROM workflow_events WHERE event_id = ?",
                    (prior["event_id"],),
                ).fetchone()
                # The destination stage is part of the operation's identity: the same key
                # with a changed name, request, or target stage is a different intent and
                # must conflict rather than replay.
                if (
                    prior["operation_name"] != operation_name
                    or prior["request_fingerprint"] != fingerprint
                    or prior_event["next_stage"] != to_stage
                ):
                    raise OperationConflict(operation_key=operation_key)
                # Return the prior outcome reconstructed from the event, not the current
                # projection, so a replay after the record has advanced still reports the
                # stage and version this operation produced.
                replayed = RecordView(
                    record_id=record_id,
                    workflow=record["workflow"],
                    stage=prior_event["next_stage"],
                    state_version=prior_event["state_version"],
                    created_at=record["created_at"],
                    updated_at=prior_event["created_at"],
                )
                return TransitionOutcome(
                    record=replayed,
                    event_id=prior["event_id"],
                    applied=False,
                )

            if record["state_version"] != expected_version:
                raise StaleRecord(expected=expected_version, actual=record["state_version"])
            if allowed_stages is not None and to_stage not in allowed_stages:
                raise ValueError(f"stage {to_stage!r} is not in the allowed set")

            next_version = record["state_version"] + 1
            sequence_no = next_version - 1
            event_id = _new_id()
            timestamp = now.isoformat()
            db.execute(
                "INSERT INTO workflow_events "
                "(event_id, record_id, sequence_no, previous_stage, next_stage, "
                "state_version, operation_name, operation_key, request_fingerprint, "
                "created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    record_id,
                    sequence_no,
                    record["stage"],
                    to_stage,
                    next_version,
                    operation_name,
                    operation_key,
                    fingerprint,
                    timestamp,
                ),
            )
            # Compare-and-set again in SQL so a concurrent writer cannot race between the
            # read above and this update; BEGIN IMMEDIATE already serializes writers, and
            # this makes the guard explicit and self-documenting.
            updated = db.execute(
                "UPDATE workflow_records SET stage = ?, state_version = ?, updated_at = ? "
                "WHERE record_id = ? AND state_version = ?",
                (to_stage, next_version, timestamp, record_id, expected_version),
            )
            if updated.rowcount != 1:
                raise StaleRecord(expected=expected_version, actual=record["state_version"])
            db.execute(
                "INSERT INTO workflow_operations "
                "(record_id, operation_key, operation_name, request_fingerprint, "
                "event_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    record_id,
                    operation_key,
                    operation_name,
                    fingerprint,
                    event_id,
                    timestamp,
                ),
            )
            refreshed = db.execute(
                "SELECT * FROM workflow_records WHERE record_id = ?", (record_id,)
            ).fetchone()
        return TransitionOutcome(record=_record_view(refreshed), event_id=event_id, applied=True)
