from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path


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


class Store:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connection() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
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
                CREATE TABLE IF NOT EXISTS outbound_sends (
                    dedupe_key TEXT PRIMARY KEY,
                    recipient TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    gmail_message_id TEXT NOT NULL,
                    sent_at TEXT NOT NULL
                );
                """
            )
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

    def mark_summarized(self, message_id: str, result: dict[str, object], notified: bool) -> None:
        with self.connection() as db:
            db.execute(
                """UPDATE messages SET status='summarized', next_retry_at=NULL, last_error=NULL,
                category=?, priority=?, summary=?, action_required=?, suggested_action=?,
                deadline_text=?, deadline_iso=?, confidence=?, notified_at=? WHERE message_id=?""",
                (
                    result["category"],
                    result["priority"],
                    result["summary"],
                    int(bool(result["action_required"])),
                    result.get("suggested_action"),
                    result.get("deadline_text"),
                    result.get("deadline_iso"),
                    result["confidence"],
                    datetime.now(UTC).isoformat() if notified else None,
                    message_id,
                ),
            )

    def recent(self, limit: int) -> list[dict[str, object]]:
        with self.connection() as db:
            rows = db.execute(
                """SELECT received_at, sender, sender_name, subject, status, priority, summary,
                action_required, suggested_action, deadline_iso, confidence, last_error
                FROM messages ORDER BY received_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def purge(self, retention_days: int) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        with self.connection() as db:
            cursor = db.execute(
                "DELETE FROM messages WHERE discovered_at < ?", (cutoff.isoformat(),)
            )
        return cursor.rowcount

    def outbound_was_sent(self, dedupe_key: str) -> bool:
        with self.connection() as db:
            return (
                db.execute(
                    "SELECT 1 FROM outbound_sends WHERE dedupe_key = ?", (dedupe_key,)
                ).fetchone()
                is not None
            )

    def record_outbound(
        self,
        *,
        dedupe_key: str,
        recipient: str,
        subject: str,
        gmail_message_id: str,
    ) -> None:
        with self.connection() as db:
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
