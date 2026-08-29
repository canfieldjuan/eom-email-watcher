from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .mime import AttachmentDescriptor

SCHEMA_VERSION = 4


@dataclass(frozen=True)
class PendingMessage:
    message_id: str
    thread_id: str | None
    sender: str
    sender_name: str | None
    subject: str
    received_at: str
    attempts: int
    fallback_notified_at: str | None


@dataclass(frozen=True)
class AnalyzedMessage:
    message_id: str
    sender: str
    sender_name: str | None
    subject: str
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
class ConnectJob:
    job_id: str
    message_id: str
    part_id: str
    capability_id: str
    capability_version: str
    provider_app_id: str | None
    provider_instance_id: str | None
    input_artifact_id: str
    input_media_type: str
    input_byte_size: int
    input_sha256: str
    status: str
    output_artifact_id: str | None
    output_media_type: str | None
    output_byte_size: int | None
    output_sha256: str | None
    summary_version: str | None
    summary_text: str | None
    warnings_json: str | None
    error_code: str | None
    error_message: str | None
    error_retryable: int | None
    created_at: str
    updated_at: str


class Store:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
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
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    history_id TEXT NOT NULL,
                    last_success_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT PRIMARY KEY,
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
                CREATE TABLE IF NOT EXISTS connect_attachment_jobs (
                    job_id TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL,
                    part_id TEXT NOT NULL,
                    capability_id TEXT NOT NULL,
                    capability_version TEXT NOT NULL,
                    provider_app_id TEXT,
                    provider_instance_id TEXT,
                    input_artifact_id TEXT NOT NULL,
                    input_media_type TEXT NOT NULL,
                    input_byte_size INTEGER NOT NULL CHECK (input_byte_size > 0),
                    input_sha256 TEXT NOT NULL CHECK (length(input_sha256) = 64),
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
                    error_code TEXT,
                    error_message TEXT,
                    error_retryable INTEGER CHECK (
                        error_retryable IS NULL OR error_retryable IN (0, 1)
                    ),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK (
                        (status = 'completed'
                            AND output_artifact_id IS NOT NULL
                            AND output_media_type IS NOT NULL
                            AND output_byte_size IS NOT NULL
                            AND output_sha256 IS NOT NULL
                            AND summary_version IS NOT NULL
                            AND summary_text IS NOT NULL
                            AND length(summary_text) > 0
                            AND warnings_json IS NOT NULL
                            AND error_code IS NULL
                            AND error_message IS NULL
                            AND error_retryable IS NULL)
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
                            AND warnings_json IS NULL)
                        OR (status IN ('requested', 'accepted', 'processing')
                            AND output_artifact_id IS NULL
                            AND output_media_type IS NULL
                            AND output_byte_size IS NULL
                            AND output_sha256 IS NULL
                            AND summary_version IS NULL
                            AND summary_text IS NULL
                            AND warnings_json IS NULL
                            AND error_code IS NULL
                            AND error_message IS NULL
                            AND error_retryable IS NULL)
                    )
                );
                CREATE INDEX IF NOT EXISTS idx_connect_attachment_jobs_lookup
                    ON connect_attachment_jobs(
                        message_id, part_id, capability_id, capability_version, created_at
                    );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_connect_attachment_jobs_active
                    ON connect_attachment_jobs(
                        message_id, part_id, capability_id, capability_version
                    )
                    WHERE status IN ('requested', 'accepted', 'processing');
                CREATE TRIGGER IF NOT EXISTS messages_delete_attachments
                AFTER DELETE ON messages
                BEGIN
                    DELETE FROM message_attachments WHERE message_id = OLD.message_id;
                END;
                CREATE TRIGGER IF NOT EXISTS messages_delete_connect_attachment_jobs
                AFTER DELETE ON messages
                BEGIN
                    DELETE FROM connect_attachment_jobs WHERE message_id = OLD.message_id;
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
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(messages)").fetchall()
            }
            if "analysis_at" not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN analysis_at TEXT")
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.path.chmod(0o600)

    def state(self) -> tuple[str, str] | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT history_id, last_success_at FROM mailbox_state WHERE id = 1"
            ).fetchone()
        return (row["history_id"], row["last_success_at"]) if row else None

    def set_state(self, history_id: str, at: datetime | None = None) -> None:
        stamp = (at or datetime.now(UTC)).isoformat()
        with self.connection() as db:
            db.execute(
                """INSERT INTO mailbox_state(id, history_id, last_success_at) VALUES(1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET history_id=excluded.history_id,
                last_success_at=excluded.last_success_at""",
                (history_id, stamp),
            )

    def has_message(self, message_id: str) -> bool:
        with self.connection() as db:
            return (
                db.execute("SELECT 1 FROM messages WHERE message_id = ?", (message_id,)).fetchone()
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
    ) -> bool:
        with self.connection() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO messages(
                    message_id, thread_id, sender, sender_name, subject, received_at, discovered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    message_id,
                    thread_id,
                    sender,
                    sender_name,
                    subject,
                    received_at,
                    datetime.now(UTC).isoformat(),
                ),
            )
        return cursor.rowcount == 1

    def replace_attachments(
        self, message_id: str, attachments: Iterable[AttachmentDescriptor]
    ) -> None:
        items = tuple(attachments)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM messages WHERE message_id = ?", (message_id,)
            ).fetchone() is None:
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
            or job.output_media_type
            != "application/vnd.local-connect.document-summary+json"
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

    def active_connect_job(
        self,
        *,
        message_id: str,
        part_id: str,
        capability_id: str,
        capability_version: str,
    ) -> ConnectJob | None:
        with self.connection() as db:
            row = db.execute(
                """SELECT * FROM connect_attachment_jobs
                WHERE message_id = ? AND part_id = ?
                  AND capability_id = ? AND capability_version = ?
                  AND status IN ('requested', 'accepted', 'processing')
                ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                (message_id, part_id, capability_id, capability_version),
            ).fetchone()
        return self._connect_job(row) if row else None

    def completed_connect_job(
        self,
        *,
        message_id: str,
        part_id: str,
        capability_id: str,
        capability_version: str,
    ) -> ConnectJob | None:
        with self.connection() as db:
            row = db.execute(
                """SELECT * FROM connect_attachment_jobs
                WHERE message_id = ? AND part_id = ?
                  AND capability_id = ? AND capability_version = ?
                  AND status = 'completed'
                ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                (message_id, part_id, capability_id, capability_version),
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
    ) -> ConnectJob:
        stamp = datetime.now(UTC).isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                """SELECT 1 FROM message_attachments
                WHERE message_id = ? AND part_id = ?""",
                (message_id, part_id),
            ).fetchone() is None:
                raise KeyError((message_id, part_id))
            db.execute(
                """INSERT INTO connect_attachment_jobs(
                    job_id, message_id, part_id, capability_id, capability_version,
                    provider_app_id, provider_instance_id, input_artifact_id,
                    input_media_type, input_byte_size, input_sha256, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'requested', ?, ?)""",
                (
                    job_id,
                    message_id,
                    part_id,
                    capability_id,
                    capability_version,
                    provider_app_id,
                    provider_instance_id,
                    input_artifact_id,
                    input_media_type,
                    input_byte_size,
                    input_sha256,
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
            output = (
                result.get("output") if isinstance(result.get("output"), dict) else None
            )
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
            )
        elif next_state == "failed":
            if error is None or result is not None:
                raise ValueError("Failed Connect jobs require only an error")
            values = (
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                error.get("code"),
                error.get("message"),
                int(bool(error.get("retryable"))),
            )
        else:
            if result is not None or error is not None:
                raise ValueError("Active Connect jobs cannot contain terminal data")
            values = (None,) * 10

        stamp = datetime.now(UTC).isoformat()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT * FROM connect_attachment_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if current is None or current["status"] != expected_state:
                raise RuntimeError("Connect job transition lost its expected-state race")
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
                        "error_code": values[7],
                        "error_message": values[8],
                        "error_retryable": values[9],
                        "updated_at": stamp,
                    }
                )
                try:
                    self.completed_connect_warnings(candidate)
                except RuntimeError as exc:
                    raise ValueError("Completed Connect job result failed validation") from exc
            cursor = db.execute(
                """UPDATE connect_attachment_jobs SET
                    provider_app_id = ?, provider_instance_id = ?, status = ?,
                    output_artifact_id = ?, output_media_type = ?, output_byte_size = ?,
                    output_sha256 = ?, summary_version = ?, summary_text = ?, warnings_json = ?,
                    error_code = ?, error_message = ?, error_retryable = ?, updated_at = ?
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

    def pending(self, now: datetime | None = None, limit: int = 25) -> list[PendingMessage]:
        stamp = (now or datetime.now(UTC)).isoformat()
        with self.connection() as db:
            rows = db.execute(
                """SELECT message_id, thread_id, sender, sender_name, subject, received_at,
                attempts, fallback_notified_at FROM messages
                WHERE status = 'pending' AND (next_retry_at IS NULL OR next_retry_at <= ?)
                ORDER BY received_at LIMIT ?""",
                (stamp, limit),
            ).fetchall()
        return [PendingMessage(**dict(row)) for row in rows]

    def pending_delivery(
        self, now: datetime | None = None, limit: int = 25
    ) -> list[AnalyzedMessage]:
        stamp = (now or datetime.now(UTC)).isoformat()
        with self.connection() as db:
            rows = db.execute(
                """SELECT message_id, sender, sender_name, subject, attempts,
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
                last_error='message unavailable (skipped)' WHERE message_id = ?""",
                (message_id,),
            )

    def mark_analyzed(self, message_id: str, result: dict[str, object]) -> None:
        with self.connection() as db:
            db.execute(
                """UPDATE messages SET status='analyzed', analysis_at=?, attempts=0,
                next_retry_at=NULL, last_error=NULL,
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
        with self.connection() as db:
            rows = db.execute(
                """SELECT message_id, received_at, sender, sender_name, subject, status,
                analysis_at, priority, summary, action_required, suggested_action,
                deadline_text, deadline_iso, confidence, attempts, next_retry_at,
                fallback_notified_at, notified_at, last_error
                FROM messages ORDER BY received_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            items = [dict(row) for row in rows]
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
                f"""SELECT * FROM connect_attachment_jobs
                WHERE message_id IN ({placeholders})
                ORDER BY created_at DESC, rowid DESC""",
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
        seen_connect: set[tuple[str, str, str, str]] = set()
        for row in connect_rows:
            key = (
                str(row["message_id"]),
                str(row["part_id"]),
                str(row["capability_id"]),
                str(row["capability_version"]),
            )
            if key in seen_connect:
                continue
            seen_connect.add(key)
            item: dict[str, object] = {
                "capability_id": key[2],
                "capability_version": key[3],
                "status": str(row["status"]),
                "updated_at": str(row["updated_at"]),
            }
            if row["status"] == "completed":
                completed = self._connect_job(row)
                item["summary"] = {
                    "summary_version": completed.summary_version,
                    "text": completed.summary_text,
                    "warnings": self.completed_connect_warnings(completed),
                }
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

    def purge(
        self, retention_days: int, *, preserve_notification_intents: bool = False
    ) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        with self.connection() as db:
            if preserve_notification_intents:
                cursor = db.execute(
                    """DELETE FROM messages WHERE discovered_at < ?
                    AND (notified_at IS NULL OR notified_at < ?)
                    AND (fallback_notified_at IS NULL OR fallback_notified_at < ?)
                    AND NOT (
                        (status = 'analyzed' AND notified_at IS NULL)
                        OR (status = 'pending' AND last_error IS NOT NULL
                            AND fallback_notified_at IS NULL)
                    )""",
                    (cutoff.isoformat(), cutoff.isoformat(), cutoff.isoformat()),
                )
            else:
                cursor = db.execute(
                    """DELETE FROM messages WHERE discovered_at < ?
                    AND (notified_at IS NULL OR notified_at < ?)
                    AND (fallback_notified_at IS NULL OR fallback_notified_at < ?)""",
                    (cutoff.isoformat(), cutoff.isoformat(), cutoff.isoformat()),
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
            db.execute(
                "DELETE FROM outbound_reservations WHERE dedupe_key = ?", (dedupe_key,)
            )

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
            db.execute(
                "DELETE FROM outbound_reservations WHERE dedupe_key = ?", (dedupe_key,)
            )
