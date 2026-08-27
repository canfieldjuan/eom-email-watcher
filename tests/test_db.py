import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eom_email_watcher.db import Store


def test_cursor_dedup_and_summary_lifecycle(tmp_path: Path) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    store.set_state("100", datetime(2026, 7, 18, tzinfo=UTC))
    assert store.state() == ("100", "2026-07-18T00:00:00+00:00")
    values = dict(
        message_id="m1",
        thread_id="t1",
        sender="trusted@example.com",
        sender_name="Trusted",
        subject="Invoice",
        received_at="2026-07-18T14:00:00+00:00",
    )
    assert store.add_message(**values)
    assert not store.add_message(**values)
    assert [item.message_id for item in store.pending()] == ["m1"]
    store.mark_analyzed(
        "m1",
        {
            "category": "invoice",
            "priority": "normal",
            "summary": "Invoice received.",
            "action_required": True,
            "suggested_action": "Review it.",
            "deadline_text": None,
            "deadline_iso": None,
            "confidence": 0.9,
        },
    )
    assert store.pending() == []
    assert store.pending_delivery()[0].summary == "Invoice received."
    store.mark_delivery_complete("m1", notified=True)
    assert store.pending_delivery() == []
    recent = store.recent(1)[0]
    assert recent["message_id"] == "m1"
    assert recent["summary"] == "Invoice received."
    assert "deadline_text" in recent
    assert recent["notified_at"] is not None


def test_analysis_notification_ack_is_state_checked_and_idempotent(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    store.add_message(
        message_id="m1",
        thread_id=None,
        sender="a@b.com",
        sender_name="A",
        subject="Action",
        received_at="2026-07-18T14:00:00+00:00",
    )
    store.mark_analyzed(
        "m1",
        {
            "category": "customer_request",
            "priority": "high",
            "summary": "Please respond.",
            "action_required": True,
            "suggested_action": "Reply.",
            "deadline_text": None,
            "deadline_iso": None,
            "confidence": 0.9,
        },
    )
    intent = store.notification_intents()[0]
    assert intent.kind == "analysis"
    assert intent.analysis_at is not None

    with pytest.raises(RuntimeError, match="no longer current"):
        store.acknowledge_notification(
            message_id="m1", kind="analysis", analysis_at="stale-version"
        )
    assert store.recent(1)[0]["status"] == "analyzed"

    assert (
        store.acknowledge_notification(
            message_id="m1", kind="analysis", analysis_at=intent.analysis_at
        )
        == "acknowledged"
    )
    assert store.notification_intents() == []
    assert (
        store.acknowledge_notification(
            message_id="m1", kind="analysis", analysis_at=intent.analysis_at
        )
        == "already_acknowledged"
    )


def test_fallback_ack_does_not_ack_later_analysis(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    store.add_message(
        message_id="m1",
        thread_id=None,
        sender="a@b.com",
        sender_name=None,
        subject="Update",
        received_at="2026-07-18T14:00:00+00:00",
    )
    store.record_failure("m1", "local model unavailable", 0)
    fallback = store.notification_intents()[0]
    assert fallback.kind == "fallback"
    assert store.acknowledge_notification(message_id="m1", kind="fallback") == "acknowledged"
    assert store.notification_intents() == []

    store.mark_analyzed(
        "m1",
        {
            "category": "informational",
            "priority": "normal",
            "summary": "Recovered summary.",
            "action_required": False,
            "suggested_action": None,
            "deadline_text": None,
            "deadline_iso": None,
            "confidence": 0.9,
        },
    )
    analysis = store.notification_intents()[0]
    assert analysis.kind == "analysis"
    assert analysis.summary == "Recovered summary."
    assert (
        store.acknowledge_notification(message_id="m1", kind="fallback")
        == "already_acknowledged"
    )
    assert store.notification_intents()[0].kind == "analysis"


def test_fallback_ack_requires_current_notification_intent(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    store.add_message(
        message_id="m1",
        thread_id=None,
        sender="a@b.com",
        sender_name=None,
        subject="Update",
        received_at="2026-07-18T14:00:00+00:00",
    )

    with pytest.raises(RuntimeError, match="no longer current"):
        store.acknowledge_notification(message_id="m1", kind="fallback")
    assert store.recent(1)[0]["fallback_notified_at"] is None

    store.record_failure("m1", "local model unavailable", 0)
    assert store.acknowledge_notification(message_id="m1", kind="fallback") == "acknowledged"


def test_notification_intent_count_is_not_limited_to_retrieval_page(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    stamp = datetime.now(UTC).isoformat()
    with store.connection() as db:
        db.executemany(
            """INSERT INTO messages (
                message_id, sender, subject, received_at, discovered_at, status, last_error
            ) VALUES (?, 'a@b.com', 'Update', ?, ?, 'pending', 'model unavailable')""",
            [(f"m{index}", stamp, stamp) for index in range(501)],
        )

    assert len(store.notification_intents(limit=500)) == 500
    assert store.notification_intent_count() == 501


def test_purge_preserves_only_unacknowledged_notification_intents(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    for message_id in ("analysis", "fallback", "ordinary"):
        store.add_message(
            message_id=message_id,
            thread_id=None,
            sender="a@b.com",
            sender_name=None,
            subject=message_id,
            received_at="2026-07-18T14:00:00+00:00",
        )
    store.mark_analyzed(
        "analysis",
        {
            "category": "informational",
            "priority": "normal",
            "summary": "Summary.",
            "action_required": False,
            "suggested_action": None,
            "deadline_text": None,
            "deadline_iso": None,
            "confidence": 0.9,
        },
    )
    store.record_failure("fallback", "local model unavailable", 0)
    old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    with store.connection() as db:
        db.execute("UPDATE messages SET discovered_at = ?", (old,))

    assert store.purge(1, preserve_notification_intents=True) == 1
    assert {row["message_id"] for row in store.recent(10)} == {"analysis", "fallback"}

    analysis = next(
        intent for intent in store.notification_intents() if intent.kind == "analysis"
    )
    store.acknowledge_notification(
        message_id="analysis", kind="analysis", analysis_at=analysis.analysis_at
    )
    store.acknowledge_notification(message_id="fallback", kind="fallback")

    assert store.purge(1, preserve_notification_intents=True) == 0
    assert (
        store.acknowledge_notification(
            message_id="analysis", kind="analysis", analysis_at=analysis.analysis_at
        )
        == "already_acknowledged"
    )
    assert (
        store.acknowledge_notification(message_id="fallback", kind="fallback")
        == "already_acknowledged"
    )

    with store.connection() as db:
        db.execute(
            """UPDATE messages SET notified_at = ?, fallback_notified_at = ?
            WHERE message_id IN ('analysis', 'fallback')""",
            (old, old),
        )

    assert store.purge(1, preserve_notification_intents=True) == 2
    assert store.recent(10) == []


def test_retry_is_not_immediately_due(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    store.add_message(
        message_id="m1",
        thread_id=None,
        sender="a@b.com",
        sender_name=None,
        subject="S",
        received_at=datetime.now(UTC).isoformat(),
    )
    store.record_failure("m1", "safe error", 0)
    assert store.pending() == []
    assert len(store.pending(now=datetime.now(UTC) + timedelta(minutes=6))) == 1


def test_initialize_migrates_current_schema_without_losing_messages(tmp_path: Path) -> None:
    database = tmp_path / "state" / "watcher.sqlite3"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as db:
        db.executescript(
            """
            CREATE TABLE mailbox_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                history_id TEXT NOT NULL,
                last_success_at TEXT NOT NULL
            );
            CREATE TABLE messages (
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
            CREATE INDEX idx_messages_pending ON messages(status, next_retry_at);
            CREATE TABLE outbound_sends (
                dedupe_key TEXT PRIMARY KEY,
                recipient TEXT NOT NULL,
                subject TEXT NOT NULL,
                gmail_message_id TEXT NOT NULL,
                sent_at TEXT NOT NULL
            );
            INSERT INTO messages(
                message_id, sender, subject, received_at, discovered_at
            ) VALUES (
                'legacy-message', 'trusted@example.com', 'Legacy',
                '2026-07-18T14:00:00+00:00', '2026-07-18T14:01:00+00:00'
            );
            """
        )
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0

    Store(database).initialize()

    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
        row = db.execute(
            "SELECT status, analysis_at FROM messages WHERE message_id = 'legacy-message'"
        ).fetchone()
    assert "analysis_at" in columns
    assert row == ("pending", None)


def test_initialize_migrates_v1_outbound_schema_without_losing_sends(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state" / "watcher.sqlite3"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as db:
        db.executescript(
            """
            PRAGMA user_version = 1;
            CREATE TABLE outbound_sends (
                dedupe_key TEXT PRIMARY KEY,
                recipient TEXT NOT NULL,
                subject TEXT NOT NULL,
                gmail_message_id TEXT NOT NULL,
                sent_at TEXT NOT NULL
            );
            INSERT INTO outbound_sends(
                dedupe_key, recipient, subject, gmail_message_id, sent_at
            ) VALUES (
                'monthly-hours:2026-07', 'maria@example.com', 'Subject',
                'gmail-id', '2026-08-01T12:00:00+00:00'
            );
            """
        )

    Store(database).initialize()

    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        sent = db.execute(
            "SELECT gmail_message_id FROM outbound_sends WHERE dedupe_key = ?",
            ("monthly-hours:2026-07",),
        ).fetchone()
        reservation_table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='outbound_reservations'"
        ).fetchone()
    assert sent == ("gmail-id",)
    assert reservation_table == (1,)
