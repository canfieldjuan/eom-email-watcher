import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eom_email_watcher.db import Store
from eom_email_watcher.mime import AttachmentDescriptor


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
    assert recent["attachments"] == []
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
        db.execute("PRAGMA user_version = 2")
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2

    Store(database).initialize()

    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
        columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
        row = db.execute(
            "SELECT status, analysis_at FROM messages WHERE message_id = 'legacy-message'"
        ).fetchone()
        attachment_table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_attachments'"
        ).fetchone()
        connect_table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='connect_attachment_jobs'"
        ).fetchone()
    assert "analysis_at" in columns
    assert row == ("pending", None)
    assert attachment_table == (1,)
    assert connect_table == (1,)


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
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
        sent = db.execute(
            "SELECT gmail_message_id FROM outbound_sends WHERE dedupe_key = ?",
            ("monthly-hours:2026-07",),
        ).fetchone()
        reservation_table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='outbound_reservations'"
        ).fetchone()
    assert sent == ("gmail-id",)
    assert reservation_table == (1,)


def test_attachment_inventory_replaces_in_order_and_is_purged_with_message(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    store.add_message(
        message_id="m1",
        thread_id=None,
        sender="a@b.com",
        sender_name=None,
        subject="Documents",
        received_at="2026-07-18T14:00:00+00:00",
    )
    store.replace_attachments(
        "m1",
        (
            AttachmentDescriptor(
                "10", "gmail-b", "contract.pdf", "application/pdf", 20, 1
            ),
            AttachmentDescriptor(
                "2", "gmail-a", "invoice.pdf", "application/pdf", 10, 0
            ),
        ),
    )

    assert store.recent(1)[0]["attachments"] == [
        {
            "part_id": "2",
            "attachment_id": "gmail-a",
            "filename": "invoice.pdf",
            "media_type": "application/pdf",
            "byte_size": 10,
        },
        {
            "part_id": "10",
            "attachment_id": "gmail-b",
            "filename": "contract.pdf",
            "media_type": "application/pdf",
            "byte_size": 20,
        },
    ]
    assert store.attachment("m1", "2") == AttachmentDescriptor(
        "2", "gmail-a", "invoice.pdf", "application/pdf", 10, 0
    )
    with pytest.raises(KeyError):
        store.attachment("m1", "missing")

    store.replace_attachments(
        "m1",
        (AttachmentDescriptor("4", None, "notes.txt", "text/plain", 5, 0),),
    )
    assert [item["part_id"] for item in store.recent(1)[0]["attachments"]] == ["4"]

    old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    with store.connection() as db:
        db.execute("UPDATE messages SET discovered_at = ?", (old,))
    assert store.purge(1) == 1
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM message_attachments").fetchone()[0] == 0


def test_attachment_inventory_rejects_unknown_message_without_partial_rows(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()

    with pytest.raises(KeyError):
        store.replace_attachments(
            "missing",
            (
                AttachmentDescriptor(
                    "1", None, "invoice.pdf", "application/pdf", 10, 0
                ),
            ),
        )

    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM message_attachments").fetchone()[0] == 0


def seed_pdf_attachment(store: Store) -> None:
    store.add_message(
        message_id="m1",
        thread_id=None,
        sender="a@b.com",
        sender_name=None,
        subject="Document",
        received_at="2026-08-29T12:00:00+00:00",
    )
    store.replace_attachments(
        "m1",
        (AttachmentDescriptor("2", "gmail-a", "invoice.pdf", "application/pdf", 20, 0),),
    )


def create_connect_job(store: Store, job_id: str) -> None:
    store.create_connect_job(
        job_id=job_id,
        message_id="m1",
        part_id="2",
        capability_id="document.summarize",
        capability_version="1.0",
        provider_app_id="alternate-provider",
        provider_instance_id="11111111-1111-4111-8111-111111111111",
        input_artifact_id="22222222-2222-4222-8222-222222222222",
        input_media_type="application/pdf",
        input_byte_size=20,
        input_sha256="a" * 64,
    )


def test_connect_job_state_result_and_integrity_survive_reopen(tmp_path: Path) -> None:
    database = tmp_path / "state" / "watcher.sqlite3"
    store = Store(database)
    store.initialize()
    seed_pdf_attachment(store)
    job_id = "33333333-3333-4333-8333-333333333333"
    create_connect_job(store, job_id)
    accepted = store.transition_connect_job(
        job_id=job_id,
        expected_state="requested",
        next_state="accepted",
        provider_app_id="alternate-provider",
        provider_instance_id="11111111-1111-4111-8111-111111111111",
    )
    assert accepted.status == "accepted"

    with pytest.raises(RuntimeError, match="expected-state"):
        store.transition_connect_job(
            job_id=job_id,
            expected_state="requested",
            next_state="processing",
            provider_app_id="alternate-provider",
            provider_instance_id="11111111-1111-4111-8111-111111111111",
        )
    assert store.connect_job(job_id).status == "accepted"  # type: ignore[union-attr]

    store.transition_connect_job(
        job_id=job_id,
        expected_state="accepted",
        next_state="processing",
        provider_app_id="alternate-provider",
        provider_instance_id="11111111-1111-4111-8111-111111111111",
    )
    warnings = [{"code": "REVIEW", "message": "Human review required."}]
    content = {
        "summary_version": "1.0",
        "text": "Exact $1,247.17 summary.",
        "warnings": warnings,
        "input_artifact": {
            "artifact_id": "22222222-2222-4222-8222-222222222222",
            "media_type": "application/pdf",
            "byte_size": 20,
            "sha256": "a" * 64,
        },
    }
    encoded = json.dumps(content, separators=(",", ":"), ensure_ascii=False).encode()
    completed = store.transition_connect_job(
        job_id=job_id,
        expected_state="processing",
        next_state="completed",
        provider_app_id="alternate-provider",
        provider_instance_id="11111111-1111-4111-8111-111111111111",
        result={
            "output": {
                "artifact_id": "44444444-4444-4444-8444-444444444444",
                "media_type": "application/vnd.local-connect.document-summary+json",
                "byte_size": len(encoded),
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "summary_version": "1.0",
                "text": "Exact $1,247.17 summary.",
                "warnings": warnings,
            }
        },
    )
    assert completed.status == "completed"

    reopened = Store(database)
    reopened.initialize()
    restored = reopened.connect_job(job_id)
    assert restored == completed
    attachment = reopened.recent(1)[0]["attachments"][0]
    assert attachment["capability_results"] == [
        {
            "capability_id": "document.summarize",
            "capability_version": "1.0",
            "status": "completed",
            "updated_at": completed.updated_at,
            "summary": {
                "summary_version": "1.0",
                "text": "Exact $1,247.17 summary.",
                "warnings": [{"code": "REVIEW", "message": "Human review required."}],
            },
        }
    ]


def test_connect_job_atomic_constraints_and_single_active_request(tmp_path: Path) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    seed_pdf_attachment(store)
    first_job = "33333333-3333-4333-8333-333333333333"
    create_connect_job(store, first_job)

    with pytest.raises(sqlite3.IntegrityError):
        create_connect_job(store, "44444444-4444-4444-8444-444444444444")
    assert store.connect_job(first_job).status == "requested"  # type: ignore[union-attr]

    with pytest.raises(ValueError, match="failed validation"):
        store.transition_connect_job(
            job_id=first_job,
            expected_state="requested",
            next_state="completed",
            provider_app_id="alternate-provider",
            provider_instance_id="11111111-1111-4111-8111-111111111111",
            result={
                "output": {
                    "artifact_id": None,
                    "media_type": None,
                    "byte_size": None,
                    "sha256": None,
                    "summary_version": None,
                    "text": None,
                    "warnings": [],
                }
            },
        )
    assert store.connect_job(first_job).status == "requested"  # type: ignore[union-attr]
