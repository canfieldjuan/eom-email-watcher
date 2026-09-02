import base64
import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eom_email_watcher.config import MAX_RETENTION_DAYS
from eom_email_watcher.db import MAX_CONNECT_REQUEST_BYTES, Store
from eom_email_watcher.mailbox import scoped_message_id
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


def test_mailbox_state_identity_and_suppression_are_account_scoped(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    store.set_state("gmail-cursor", provider="gmail", account_id="gmail-default")
    store.set_state("graph-cursor", provider="microsoft365", account_id="account-2")

    assert store.state(provider="gmail", account_id="gmail-default")[0] == "gmail-cursor"
    assert store.state(provider="microsoft365", account_id="account-2")[0] == "graph-cursor"

    source_id = "same-provider-id"
    assert store.add_message(
        message_id=scoped_message_id("gmail", "gmail-default", source_id),
        provider="gmail",
        account_id="gmail-default",
        provider_message_id=source_id,
        thread_id=None,
        sender="first@example.com",
        sender_name=None,
        subject="Gmail",
        received_at="2026-08-31T12:00:00+00:00",
    )
    assert store.delete_message(source_id)
    assert store.has_seen_message(source_id, provider="gmail", account_id="gmail-default")
    assert not store.has_seen_message(source_id, provider="microsoft365", account_id="account-2")

    graph_local_id = scoped_message_id("microsoft365", "account-2", source_id)
    assert store.add_message(
        message_id=graph_local_id,
        provider="microsoft365",
        account_id="account-2",
        provider_message_id=source_id,
        thread_id=None,
        sender="second@example.com",
        sender_name=None,
        subject="Microsoft 365",
        received_at="2026-08-31T13:00:00+00:00",
    )
    assert not store.add_message(
        message_id="different-local-id",
        provider="microsoft365",
        account_id="account-2",
        provider_message_id=source_id,
        thread_id=None,
        sender="second@example.com",
        sender_name=None,
        subject="Duplicate source identity",
        received_at="2026-08-31T13:00:00+00:00",
    )
    assert [
        item.message_id for item in store.pending(provider="microsoft365", account_id="account-2")
    ] == [graph_local_id]
    items, _ = store.query_inbox(limit=10, account_id="account-2")
    assert [(item["provider"], item["account_id"], item["message_id"]) for item in items] == [
        ("microsoft365", "account-2", graph_local_id)
    ]


def test_inbox_query_keyset_paginates_equal_timestamps_without_gaps(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    stamp = "2026-08-31T12:00:00+00:00"
    with store.connection() as db:
        db.executemany(
            """INSERT INTO messages(
                    message_id, provider, account_id, provider_message_id,
                    sender, subject, received_at, discovered_at, category
                ) VALUES (
                    ?1, 'gmail', 'gmail-default', ?1,
                    'sender@example.com', 'Update', ?2, ?3, 'informational'
                )""",
            [(f"message-{index:03}", stamp, stamp) for index in range(55)],
        )

    cursor: tuple[str, str] | None = None
    message_ids: list[str] = []
    while True:
        items, cursor = store.query_inbox(limit=7, cursor=cursor)
        message_ids.extend(str(item["message_id"]) for item in items)
        assert all(item["category"] == "informational" for item in items)
        if cursor is None:
            break

    assert message_ids == [f"message-{index:03}" for index in reversed(range(55))]
    assert len(message_ids) == len(set(message_ids))


def test_inbox_query_combines_filters_before_limiting_and_matches_literals(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    with store.connection() as db:
        db.executemany(
            """INSERT INTO messages(
                    message_id, provider, account_id, provider_message_id,
                    sender, sender_name, subject, received_at, discovered_at,
                    status, category, priority, summary
                ) VALUES (
                    ?1, 'gmail', 'gmail-default', ?1,
                    ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10
                )""",
            [
                (
                    f"noise-{index:03}",
                    "noise@example.com",
                    "Noise",
                    "Routine update",
                    f"2026-08-31T13:{index:02}:00+00:00",
                    "2026-08-31T14:00:00+00:00",
                    "analyzed",
                    "invoice",
                    "high",
                    "Nothing actionable.",
                )
                for index in range(60)
            ]
            + [
                (
                    "target",
                    "billing@acme.example",
                    "ACME Billing",
                    "Overdue % balance",
                    "2026-08-30T12:00:00+00:00",
                    "2026-08-31T14:00:00+00:00",
                    "analyzed",
                    "invoice",
                    "high",
                    "Please review the balance.",
                ),
                (
                    "untriaged",
                    "billing@acme.example",
                    None,
                    "Waiting",
                    "2026-08-29T12:00:00+00:00",
                    "2026-08-31T14:00:00+00:00",
                    "pending",
                    None,
                    None,
                    None,
                ),
            ],
        )

    items, cursor = store.query_inbox(
        limit=10,
        sender_query="acme",
        priority="high",
        category="invoice",
        status="analyzed",
        keyword="OVERDUE % BALANCE",
    )
    assert [item["message_id"] for item in items] == ["target"]
    assert cursor is None
    percent_items, _ = store.query_inbox(limit=10, keyword="%")
    assert [item["message_id"] for item in percent_items] == ["target"]
    untriaged_items, _ = store.query_inbox(limit=10, priority="untriaged", category="unclassified")
    assert [item["message_id"] for item in untriaged_items] == ["untriaged"]


def test_analysis_notification_ack_is_state_checked_and_idempotent(
    tmp_path: Path,
) -> None:
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
        store.acknowledge_notification(message_id="m1", kind="fallback") == "already_acknowledged"
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


def test_notification_intent_count_is_not_limited_to_retrieval_page(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    stamp = datetime.now(UTC).isoformat()
    with store.connection() as db:
        db.executemany(
            """INSERT INTO messages (
                    message_id, provider, account_id, provider_message_id,
                    sender, subject, received_at, discovered_at, status, last_error
                ) VALUES (
                    ?1, 'gmail', 'gmail-default', ?1,
                    'a@b.com', 'Update', ?2, ?3, 'pending', 'model unavailable'
                )""",
            [(f"m{index}", stamp, stamp) for index in range(501)],
        )

    assert len(store.notification_intents(limit=500)) == 500
    assert store.notification_intent_count() == 501


def test_purge_is_a_hard_source_time_ceiling_including_notification_intents(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    for message_id in ("analysis", "fallback", "ordinary"):
        store.add_message(
            message_id=message_id,
            thread_id=None,
            sender="a@b.com",
            sender_name=None,
            subject=message_id,
            received_at="2026-08-30T12:00:00+00:00",
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
    assert store.notification_intent_count() == 2
    assert store.purge(1, now=datetime(2026, 9, 1, 12, tzinfo=UTC)) == 3
    assert store.recent(10) == []
    assert store.notification_intents() == []


def test_purge_uses_source_received_time_not_local_discovery_time(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    store.add_message(
        message_id="source-old",
        thread_id=None,
        sender="a@b.com",
        sender_name=None,
        subject="Old source",
        received_at="2026-08-01T00:00:00+00:00",
    )
    store.add_message(
        message_id="source-current",
        thread_id=None,
        sender="a@b.com",
        sender_name=None,
        subject="Current source",
        received_at="2026-09-01T00:00:00+00:00",
    )
    with store.connection() as db:
        db.execute(
            "UPDATE messages SET discovered_at = '2020-01-01T00:00:00+00:00' "
            "WHERE message_id = 'source-current'"
        )

    assert store.purge(7, now=datetime(2026, 9, 1, 12, tzinfo=UTC)) == 1
    assert [row["message_id"] for row in store.recent(10)] == ["source-current"]


def test_purge_rejects_timezone_less_source_timestamp(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    store.add_message(
        message_id="timezone-less",
        thread_id=None,
        sender="a@b.com",
        sender_name=None,
        subject="Malformed source time",
        received_at="2026-09-01T11:00:00",
    )
    store.mark_analyzed(
        "timezone-less",
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

    assert store.purge(7, now=datetime(2026, 9, 1, 12, tzinfo=UTC)) == 1
    assert store.recent(10) == []
    assert store.notification_intents() == []


def test_purge_uses_one_parser_for_second_precision_timezone_offsets(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    for message_id, received_at in (
        ("old-offset", "2026-08-01T12:00:00+00:00:30"),
        ("current-offset", "2026-09-01T11:00:00+00:00:30"),
    ):
        assert store.add_message(
            message_id=message_id,
            thread_id=None,
            sender="a@b.com",
            sender_name=None,
            subject=message_id,
            received_at=received_at,
        )

    assert store.purge(7, now=datetime(2026, 9, 1, 12, tzinfo=UTC)) == 1
    assert [row["message_id"] for row in store.recent(10)] == ["current-offset"]


def test_purge_bounds_legacy_future_source_time_by_discovery_time(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    store.add_message(
        message_id="future-source",
        thread_id=None,
        sender="a@b.com",
        sender_name=None,
        subject="Malformed future source time",
        received_at="2036-09-01T00:00:00+00:00",
    )
    with store.connection() as db:
        db.execute(
            "UPDATE messages SET discovered_at = ? WHERE message_id = ?",
            ("2026-08-01T00:00:00+00:00", "future-source"),
        )

    assert store.purge(7, now=datetime(2026, 9, 1, 12, tzinfo=UTC)) == 1
    assert store.recent(10) == []


def test_purge_rejects_future_source_and_discovery_times(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    store.add_message(
        message_id="future-source-and-discovery",
        thread_id=None,
        sender="a@b.com",
        sender_name=None,
        subject="Future local timestamps",
        received_at="2036-09-01T00:00:00+00:00",
    )
    with store.connection() as db:
        db.execute(
            "UPDATE messages SET discovered_at = ? WHERE message_id = ?",
            ("2036-09-01T00:00:00+00:00", "future-source-and-discovery"),
        )

    assert store.purge(7, now=datetime(2026, 9, 1, 12, tzinfo=UTC)) == 1
    assert store.recent(10) == []


def test_delete_message_cascades_local_state_and_prevents_rediscovery(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    store.set_state("100", datetime(2026, 9, 1, tzinfo=UTC))
    seed_pdf_attachment(store)
    create_connect_job(store, "33333333-3333-4333-8333-333333333333")
    store.record_failure("m1", "model unavailable", 0)

    assert store.delete_message("m1", now=datetime(2026, 9, 1, 12, tzinfo=UTC))
    assert not store.delete_message("m1")
    assert store.recent(10) == []
    assert store.notification_intents() == []
    assert store.state() == ("100", "2026-09-01T00:00:00+00:00")
    assert not store.add_message(
        message_id="m1",
        thread_id=None,
        sender="a@b.com",
        sender_name=None,
        subject="Rediscovered",
        received_at="2026-08-29T12:00:00+00:00",
    )
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM message_attachments").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM connect_attachment_jobs").fetchone()[0] == 0
        suppression = db.execute("SELECT message_key FROM suppressed_messages").fetchone()[0]
    assert suppression == hashlib.sha256(b"gmail\0gmail-default\0m1").hexdigest()
    assert "m1" not in suppression


def test_delete_message_bounds_suppression_when_source_time_conversion_overflows(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    assert store.add_message(
        message_id="overflowing-source-time",
        thread_id=None,
        sender="a@b.com",
        sender_name=None,
        subject="Malformed source time",
        received_at="0001-01-01T00:00:00+23:59",
    )
    now = datetime(2026, 9, 1, 12, tzinfo=UTC)

    assert store.delete_message("overflowing-source-time", now=now)

    with store.connection() as db:
        expires_at = db.execute("SELECT expires_at FROM suppressed_messages").fetchone()[0]
    assert expires_at == (now + timedelta(days=MAX_RETENTION_DAYS)).isoformat()


def test_clear_messages_preserves_mailbox_and_outbound_state(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    store.set_state("200", datetime(2026, 9, 1, tzinfo=UTC))
    for message_id in ("m1", "m2"):
        store.add_message(
            message_id=message_id,
            thread_id=None,
            sender="a@b.com",
            sender_name=None,
            subject=message_id,
            received_at="2026-09-01T00:00:00+00:00",
        )
    with store.connection() as db:
        db.execute(
            """INSERT INTO outbound_sends(
                dedupe_key, recipient, subject, gmail_message_id, sent_at
            ) VALUES ('monthly-hours:2026-08', 'owner@example.com', 'Hours', 'sent-id', ?)""",
            (datetime(2026, 9, 1, tzinfo=UTC).isoformat(),),
        )

    assert store.clear_messages(now=datetime(2026, 9, 1, 12, tzinfo=UTC)) == 2
    assert store.recent(10) == []
    assert store.state() == ("200", "2026-09-01T00:00:00+00:00")
    assert store.outbound_status("monthly-hours:2026-08") == "sent"
    assert not store.add_message(
        message_id="m2",
        thread_id=None,
        sender="a@b.com",
        sender_name=None,
        subject="Rediscovered",
        received_at="2026-09-01T00:00:00+00:00",
    )


def test_manual_delete_suppression_overlaps_maximum_retention_boundary(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    received_at = datetime(2020, 1, 1, tzinfo=UTC)
    expires_at = received_at + timedelta(days=MAX_RETENTION_DAYS)
    store.add_message(
        message_id="boundary-message",
        thread_id=None,
        sender="a@b.com",
        sender_name=None,
        subject="Boundary",
        received_at=received_at.isoformat(),
    )

    assert store.delete_message("boundary-message", now=received_at)
    assert store.purge(MAX_RETENTION_DAYS, now=expires_at) == 0
    assert not store.add_message(
        message_id="boundary-message",
        thread_id=None,
        sender="a@b.com",
        sender_name=None,
        subject="Boundary replay",
        received_at=received_at.isoformat(),
    )


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


def test_analysis_request_and_server_retry_delay_survive_reopen(tmp_path: Path) -> None:
    database = tmp_path / "db.sqlite3"
    store = Store(database)
    store.initialize()
    store.add_message(
        message_id="m1",
        thread_id=None,
        sender="a@b.com",
        sender_name=None,
        subject="S",
        received_at="2026-08-29T12:00:00+00:00",
    )
    now = datetime(2026, 8, 29, 13, 0, tzinfo=UTC)

    first = store.reserve_analysis_request("m1", 20_000, now)
    reopened = Store(database)
    reopened.initialize()
    second = reopened.reserve_analysis_request("m1", 99_999, now + timedelta(hours=1))

    assert second == first
    reopened.record_analysis_failure(
        "m1",
        "Inference gateway error: capacity_limited",
        0,
        retryable=True,
        error_code="capacity_limited",
        retry_after_seconds=90,
        now=now,
    )
    assert reopened.pending(now=now + timedelta(seconds=89)) == []
    due = reopened.pending(now=now + timedelta(seconds=90))[0]
    assert due.analysis_request_id == first.request_id
    recent = reopened.recent(1)[0]
    assert recent["analysis_retryable"] == 1
    assert recent["analysis_error_code"] == "capacity_limited"
    assert recent["analysis_retry_after_seconds"] == 90


def test_permanent_analysis_failure_requires_explicit_requeue(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    store.add_message(
        message_id="m1",
        thread_id=None,
        sender="a@b.com",
        sender_name=None,
        subject="S",
        received_at="2026-08-29T12:00:00+00:00",
    )
    first = store.reserve_analysis_request("m1", 20_000)
    store.record_analysis_failure(
        "m1",
        "Inference gateway error: forbidden",
        0,
        retryable=False,
        error_code="forbidden",
    )

    assert store.pending(now=datetime.now(UTC) + timedelta(days=365)) == []
    assert store.notification_intents()[0].kind == "fallback"
    assert store.requeue_analysis("m1") == "requeued"
    assert store.notification_intents()[0].kind == "fallback"
    assert store.pending()[0].analysis_request_id is None
    with pytest.raises(RuntimeError, match="permanently paused"):
        store.requeue_analysis("m1")
    second = store.reserve_analysis_request("m1", 20_000)
    assert second.request_id != first.request_id


def test_initialize_migrates_current_schema_without_losing_messages(
    tmp_path: Path,
) -> None:
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
            CREATE TABLE suppressed_messages (
                message_key TEXT PRIMARY KEY CHECK (length(message_key) = 64),
                expires_at TEXT NOT NULL
            );
            CREATE TABLE message_attachments (
                message_id TEXT NOT NULL,
                part_id TEXT NOT NULL,
                attachment_id TEXT,
                filename TEXT NOT NULL,
                media_type TEXT NOT NULL,
                byte_size INTEGER NOT NULL,
                position INTEGER NOT NULL,
                PRIMARY KEY (message_id, part_id),
                UNIQUE (message_id, position)
            );
            CREATE TABLE outbound_sends (
                dedupe_key TEXT PRIMARY KEY,
                recipient TEXT NOT NULL,
                subject TEXT NOT NULL,
                gmail_message_id TEXT NOT NULL,
                sent_at TEXT NOT NULL
            );
            INSERT INTO mailbox_state(id, history_id, last_success_at)
            VALUES (1, 'legacy-cursor', '2026-07-18T14:01:00+00:00');
            INSERT INTO messages(
                message_id, sender, subject, received_at, discovered_at
            ) VALUES (
                'legacy-message', 'trusted@example.com', 'Legacy',
                '2026-07-18T14:00:00+00:00', '2026-07-18T14:01:00+00:00'
            );
            INSERT INTO message_attachments(
                message_id, part_id, attachment_id, filename, media_type,
                byte_size, position
            ) VALUES (
                'legacy-message', '2', 'legacy-attachment', 'legacy.pdf',
                'application/pdf', 42, 0
            );
            """
        )
        db.execute(
            "INSERT INTO suppressed_messages(message_key, expires_at) VALUES (?, ?)",
            (
                hashlib.sha256(b"legacy-deleted-message").hexdigest(),
                "2030-01-01T00:00:00+00:00",
            ),
        )
        db.execute("PRAGMA user_version = 2")
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2

    Store(database).initialize()

    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 9
        columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
        row = db.execute(
            """SELECT status, analysis_at, provider, account_id, provider_message_id
            FROM messages WHERE message_id = 'legacy-message'"""
        ).fetchone()
        state = db.execute(
            """SELECT provider, account_id, cursor
            FROM mailbox_state"""
        ).fetchall()
        attachment_table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_attachments'"
        ).fetchone()
        connect_table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='connect_attachment_jobs'"
        ).fetchone()
        suppression_table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='suppressed_messages'"
        ).fetchone()
    assert "analysis_at" in columns
    assert row == (
        "pending",
        None,
        "gmail",
        "gmail-default",
        "legacy-message",
    )
    assert state == [("gmail", "gmail-default", "legacy-cursor")]
    assert attachment_table == (1,)
    assert connect_table == (1,)
    assert suppression_table == (1,)
    migrated = Store(database)
    assert migrated.attachment("legacy-message", "2").filename == "legacy.pdf"
    assert migrated.has_seen_message("legacy-deleted-message")


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
        assert db.execute("PRAGMA user_version").fetchone()[0] == 9
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
            AttachmentDescriptor("10", "gmail-b", "contract.pdf", "application/pdf", 20, 1),
            AttachmentDescriptor("2", "gmail-a", "invoice.pdf", "application/pdf", 10, 0),
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
        db.execute("UPDATE messages SET received_at = ?", (old,))
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
            (AttachmentDescriptor("1", None, "invoice.pdf", "application/pdf", 10, 0),),
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


def create_v2_connect_job(
    store: Store,
    job_id: str = "33333333-3333-4333-8333-333333333333",
    *,
    capability_id: str = "document.translate",
    provider_app_id: str = "translation-provider",
    provider_app_version: str = "0.1.0",
    provider_instance_id: str = "11111111-1111-4111-8111-111111111111",
    artifact_id: str = "22222222-2222-4222-8222-222222222222",
    input_byte_size: int = 20,
    input_sha256: str = "a" * 64,
    parameters: dict[str, object] | None = None,
) -> bytes:
    parameter_values = {"target-language": "Spanish"} if parameters is None else parameters
    request = {
        "protocol_version": 2,
        "job_id": job_id,
        "capability": {"id": capability_id, "version": "1.0"},
        "inputs": [
            {
                "artifact_id": artifact_id,
                "media_type": "application/pdf",
                "byte_size": input_byte_size,
                "sha256": input_sha256,
                "display_name": "invoice.pdf",
                "source_app_id": "email-watcher",
            }
        ],
        "parameters": parameter_values,
    }
    request_json = json.dumps(
        request,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    store.create_connect_job(
        job_id=job_id,
        message_id="m1",
        part_id="2",
        protocol_version=2,
        capability_id=capability_id,
        capability_version="1.0",
        provider_app_id=provider_app_id,
        provider_app_version=provider_app_version,
        provider_instance_id=provider_instance_id,
        input_artifact_id=artifact_id,
        input_media_type="application/pdf",
        input_byte_size=input_byte_size,
        input_sha256=input_sha256,
        input_display_name="invoice.pdf",
        source_app_id="email-watcher",
        request_json=request_json,
    )
    return request_json


def v2_result() -> tuple[dict[str, object], tuple[bytes, bytes]]:
    translated = b"Translated invoice: $1,247.17"
    empty = b""
    outputs = (
        {
            "artifact_id": "44444444-4444-4444-8444-444444444444",
            "media_type": "text/plain",
            "display_name": "invoice-es.txt",
            "byte_size": len(translated),
            "sha256": hashlib.sha256(translated).hexdigest(),
            "payload_base64": base64.b64encode(translated).decode(),
        },
        {
            "artifact_id": "55555555-5555-4555-8555-555555555555",
            "media_type": "text/plain",
            "display_name": "empty.txt",
            "byte_size": 0,
            "sha256": hashlib.sha256(empty).hexdigest(),
            "payload_base64": "",
        },
    )
    return {"outputs": list(outputs)}, (translated, empty)


def downgrade_connect_jobs_to_v5(database: Path) -> None:
    legacy_columns = """
        job_id, message_id, part_id, capability_id, capability_version,
        provider_app_id, provider_instance_id, input_artifact_id,
        input_media_type, input_byte_size, input_sha256, status,
        output_artifact_id, output_media_type, output_byte_size, output_sha256,
        summary_version, summary_text, warnings_json,
        error_code, error_message, error_retryable, created_at, updated_at
    """
    with sqlite3.connect(database) as db:
        db.execute("DROP TRIGGER messages_delete_connect_attachment_jobs")
        db.execute("DROP INDEX idx_connect_attachment_jobs_lookup")
        db.execute("DROP INDEX idx_connect_attachment_jobs_active")
        db.execute("ALTER TABLE connect_attachment_jobs RENAME TO connect_attachment_jobs_v6")
        db.execute(
            f"CREATE TABLE connect_attachment_jobs AS "
            f"SELECT {legacy_columns} FROM connect_attachment_jobs_v6"
        )
        db.execute("DROP TABLE connect_attachment_jobs_v6")
        db.execute("PRAGMA user_version = 5")


def test_initialize_migrates_v5_connect_jobs_without_losing_terminal_state(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state" / "watcher.sqlite3"
    store = Store(database)
    store.initialize()
    seed_pdf_attachment(store)
    job_id = "33333333-3333-4333-8333-333333333333"
    create_connect_job(store, job_id)
    failed = store.transition_connect_job(
        job_id=job_id,
        expected_state="requested",
        next_state="failed",
        provider_app_id="alternate-provider",
        provider_instance_id="11111111-1111-4111-8111-111111111111",
        error={"code": "PDF_MALFORMED", "message": "Invalid PDF", "retryable": False},
    )
    downgrade_connect_jobs_to_v5(database)

    Store(database).initialize()

    reopened = Store(database)
    restored = reopened.connect_job(job_id)
    assert restored is not None
    assert restored.protocol_version == 1
    assert restored.status == failed.status
    assert restored.error_code == "PDF_MALFORMED"
    assert restored.error_message == "Invalid PDF"
    assert restored.error_retryable == 0
    assert restored.source_app_id == "email-watcher"
    assert restored.provider_app_version is None
    assert restored.invocation_fingerprint == "v1"
    assert restored.request_json is None
    assert restored.result_json is None
    assert restored.result_metadata_json is None
    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 9
        assert (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'connect_attachment_jobs_v5'"
            ).fetchone()
            is None
        )
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'trigger' "
            "AND name = 'messages_delete_connect_attachment_jobs'"
        ).fetchone() == (1,)


def test_failed_v5_connect_migration_rolls_back_without_losing_legacy_rows(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state" / "watcher.sqlite3"
    store = Store(database)
    store.initialize()
    seed_pdf_attachment(store)
    job_id = "33333333-3333-4333-8333-333333333333"
    create_connect_job(store, job_id)
    downgrade_connect_jobs_to_v5(database)
    with sqlite3.connect(database) as db:
        db.execute(
            "UPDATE connect_attachment_jobs SET input_sha256 = 'invalid' WHERE job_id = ?",
            (job_id,),
        )

    with pytest.raises(sqlite3.IntegrityError):
        Store(database).initialize()

    with sqlite3.connect(database) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(connect_attachment_jobs)")}
        assert "protocol_version" not in columns
        assert db.execute("SELECT job_id FROM connect_attachment_jobs").fetchall() == [(job_id,)]
        assert db.execute("PRAGMA user_version").fetchone()[0] == 5
        assert (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'connect_attachment_jobs_v5'"
            ).fetchone()
            is None
        )


def test_connect_v2_request_and_generic_outputs_survive_reopen(tmp_path: Path) -> None:
    database = tmp_path / "state" / "watcher.sqlite3"
    store = Store(database)
    store.initialize()
    seed_pdf_attachment(store)
    job_id = "33333333-3333-4333-8333-333333333333"
    request_json = create_v2_connect_job(store, job_id)
    result, payloads = v2_result()
    lookup = {
        "message_id": "m1",
        "part_id": "2",
        "capability_id": "document.translate",
        "capability_version": "1.0",
    }
    v2_lookup = {
        **lookup,
        "protocol_version": 2,
        "provider_app_id": "translation-provider",
        "provider_app_version": "0.1.0",
        "provider_instance_id": "11111111-1111-4111-8111-111111111111",
        "request_json": request_json,
    }
    assert store.active_connect_job(**lookup) is None
    assert store.active_connect_job(**v2_lookup) is not None
    store.transition_connect_job(
        job_id=job_id,
        expected_state="requested",
        next_state="accepted",
        provider_app_id="translation-provider",
        provider_instance_id="11111111-1111-4111-8111-111111111111",
    )
    store.transition_connect_job(
        job_id=job_id,
        expected_state="accepted",
        next_state="processing",
        provider_app_id="translation-provider",
        provider_instance_id="11111111-1111-4111-8111-111111111111",
    )
    completed = store.transition_connect_job(
        job_id=job_id,
        expected_state="processing",
        next_state="completed",
        provider_app_id="translation-provider",
        provider_instance_id="11111111-1111-4111-8111-111111111111",
        result=result,
    )

    reopened = Store(database)
    reopened.initialize()
    restored = reopened.connect_job(job_id)
    assert restored == completed
    assert restored is not None
    assert restored.request_json == request_json
    assert restored.provider_app_version == "0.1.0"
    assert restored.input_display_name == "invoice.pdf"
    assert restored.source_app_id == "email-watcher"
    assert reopened.completed_connect_job(**lookup) is None
    assert reopened.completed_connect_job(**v2_lookup) == restored
    outputs = reopened.completed_connect_outputs(restored)
    assert tuple(output.payload for output in outputs) == payloads
    assert [output.byte_size for output in outputs] == [len(payloads[0]), 0]

    projected = reopened.recent(1)[0]["attachments"][0]["capability_results"][0]
    assert projected == {
        "job_id": job_id,
        "capability_id": "document.translate",
        "capability_version": "1.0",
        "status": "completed",
        "updated_at": completed.updated_at,
        "protocol_version": 2,
        "provider": {
            "app_id": "translation-provider",
            "version": "0.1.0",
            "instance_id": "11111111-1111-4111-8111-111111111111",
        },
        "parameters": {"target-language": "Spanish"},
        "outputs": [output.metadata() for output in outputs],
    }
    assert "payload_base64" not in json.dumps(projected)


def test_connect_v2_active_identity_scopes_protocol_provider_and_parameters(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    seed_pdf_attachment(store)
    create_connect_job(store, "33333333-3333-4333-8333-333333333333")
    spanish_request = create_v2_connect_job(
        store,
        "44444444-4444-4444-8444-444444444444",
        capability_id="document.summarize",
        parameters={"target-language": "Spanish"},
    )
    french_request = create_v2_connect_job(
        store,
        "55555555-5555-4555-8555-555555555555",
        capability_id="document.summarize",
        parameters={"target-language": "French"},
    )
    other_provider_request = create_v2_connect_job(
        store,
        "66666666-6666-4666-8666-666666666666",
        capability_id="document.summarize",
        provider_app_id="other-provider",
        provider_instance_id="99999999-9999-4999-8999-999999999999",
        parameters={"target-language": "Spanish"},
    )

    base_lookup = {
        "message_id": "m1",
        "part_id": "2",
        "capability_id": "document.summarize",
        "capability_version": "1.0",
    }
    assert store.active_connect_job(**base_lookup).job_id == (  # type: ignore[union-attr]
        "33333333-3333-4333-8333-333333333333"
    )
    for request_json, provider_app_id, provider_instance_id, expected_job_id in (
        (
            spanish_request,
            "translation-provider",
            "11111111-1111-4111-8111-111111111111",
            "44444444-4444-4444-8444-444444444444",
        ),
        (
            french_request,
            "translation-provider",
            "11111111-1111-4111-8111-111111111111",
            "55555555-5555-4555-8555-555555555555",
        ),
        (
            other_provider_request,
            "other-provider",
            "99999999-9999-4999-8999-999999999999",
            "66666666-6666-4666-8666-666666666666",
        ),
    ):
        restored = store.active_connect_job(
            **base_lookup,
            protocol_version=2,
            provider_app_id=provider_app_id,
            provider_app_version="0.1.0",
            provider_instance_id=provider_instance_id,
            request_json=request_json,
        )
        assert restored is not None
        assert restored.job_id == expected_job_id

    create_v2_connect_job(
        store,
        "77777777-7777-4777-8777-777777777777",
        capability_id="document.summarize",
        parameters={"target-language": "Spanish"},
    )
    assert store.connect_job("44444444-4444-4444-8444-444444444444") is not None
    assert store.connect_job("77777777-7777-4777-8777-777777777777") is not None

    projected = store.recent(1)[0]["attachments"][0]["capability_results"]
    v2_results = [result for result in projected if result.get("protocol_version") == 2]
    assert {result["job_id"] for result in v2_results} == {
        "55555555-5555-4555-8555-555555555555",
        "66666666-6666-4666-8666-666666666666",
        "77777777-7777-4777-8777-777777777777",
    }
    assert {json.dumps(result["parameters"], sort_keys=True) for result in v2_results} == {
        '{"target-language": "French"}',
        '{"target-language": "Spanish"}',
    }


def test_initialize_replaces_v6_active_index_without_losing_jobs(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state" / "watcher.sqlite3"
    store = Store(database)
    store.initialize()
    seed_pdf_attachment(store)
    create_v2_connect_job(
        store,
        "44444444-4444-4444-8444-444444444444",
        capability_id="document.summarize",
        parameters={"target-language": "Spanish"},
    )
    with store.connection() as db:
        db.execute("DROP INDEX idx_connect_attachment_jobs_active")
        db.execute(
            """CREATE UNIQUE INDEX idx_connect_attachment_jobs_active
            ON connect_attachment_jobs(
                message_id, part_id, protocol_version, capability_id,
                capability_version, invocation_fingerprint
            )
            WHERE status IN ('requested', 'accepted', 'processing')"""
        )
        db.execute("PRAGMA user_version = 6")

    Store(database).initialize()
    create_v2_connect_job(
        store,
        "77777777-7777-4777-8777-777777777777",
        capability_id="document.summarize",
        parameters={"target-language": "Spanish"},
    )

    with store.connection() as db:
        index_sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' "
            "AND name = 'idx_connect_attachment_jobs_active'"
        ).fetchone()[0]
        assert db.execute("PRAGMA user_version").fetchone()[0] == 9
        assert db.execute("SELECT COUNT(*) FROM connect_attachment_jobs").fetchone()[0] == 2
    assert "protocol_version = 1" in index_sql


def test_connect_v2_persists_maximum_generated_request_and_zero_byte_input(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    seed_pdf_attachment(store)
    parameters = {(f"p{index:02d}-" + "x" * 96): "😀" * 1000 for index in range(16)}
    large_request = create_v2_connect_job(
        store,
        "33333333-3333-4333-8333-333333333333",
        parameters=parameters,
    )
    assert 64 * 1024 < len(large_request) <= MAX_CONNECT_REQUEST_BYTES

    empty_request = create_v2_connect_job(
        store,
        "44444444-4444-4444-8444-444444444444",
        input_byte_size=0,
        input_sha256=hashlib.sha256(b"").hexdigest(),
        parameters={},
    )
    empty = store.connect_job("44444444-4444-4444-8444-444444444444")
    assert empty is not None
    assert empty.input_byte_size == 0
    assert empty.request_json == empty_request


def test_connect_v2_rejects_mismatched_request_and_corrupt_result(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state" / "watcher.sqlite3"
    store = Store(database)
    store.initialize()
    seed_pdf_attachment(store)
    job_id = "33333333-3333-4333-8333-333333333333"
    request_json = create_v2_connect_job(store, job_id)
    request = json.loads(request_json)
    request["job_id"] = "99999999-9999-4999-8999-999999999999"
    mismatched = json.dumps(request, separators=(",", ":")).encode()
    with pytest.raises(ValueError, match="provenance"):
        store.create_connect_job(
            job_id="66666666-6666-4666-8666-666666666666",
            message_id="m1",
            part_id="2",
            protocol_version=2,
            capability_id="document.translate",
            capability_version="1.0",
            provider_app_id="translation-provider",
            provider_app_version="0.1.0",
            provider_instance_id="11111111-1111-4111-8111-111111111111",
            input_artifact_id="22222222-2222-4222-8222-222222222222",
            input_media_type="application/pdf",
            input_byte_size=20,
            input_sha256="a" * 64,
            input_display_name="invoice.pdf",
            source_app_id="email-watcher",
            request_json=mismatched,
        )

    result, _ = v2_result()
    completed = store.transition_connect_job(
        job_id=job_id,
        expected_state="requested",
        next_state="completed",
        provider_app_id="translation-provider",
        provider_instance_id="11111111-1111-4111-8111-111111111111",
        result=result,
    )
    with store.connection() as db:
        db.execute(
            "UPDATE connect_attachment_jobs SET result_json = ? WHERE job_id = ?",
            (b'{"outputs":[]}', job_id),
        )
    corrupted = store.connect_job(job_id)
    assert corrupted is not None
    assert corrupted.result_json != completed.result_json
    projected = store.recent(1)[0]["attachments"][0]["capability_results"][0]
    assert projected["outputs"] == [
        output.metadata() for output in store.completed_connect_outputs(completed)
    ]
    with pytest.raises(RuntimeError, match="invalid"):
        store.completed_connect_outputs(corrupted)


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
            "job_id": job_id,
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


def test_connect_job_atomic_constraints_and_single_active_request(
    tmp_path: Path,
) -> None:
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


def test_connect_job_resubmission_reset_is_atomic_and_provider_scoped(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    seed_pdf_attachment(store)
    job_id = "33333333-3333-4333-8333-333333333333"
    create_connect_job(store, job_id)
    store.transition_connect_job(
        job_id=job_id,
        expected_state="requested",
        next_state="processing",
        provider_app_id="alternate-provider",
        provider_instance_id="11111111-1111-4111-8111-111111111111",
    )

    with pytest.raises(RuntimeError, match="expected-state"):
        store.reset_connect_job_for_resubmission(
            job_id=job_id,
            expected_state="processing",
            provider_app_id="alternate-provider",
            provider_instance_id="99999999-9999-4999-8999-999999999999",
        )
    assert store.connect_job(job_id).status == "processing"  # type: ignore[union-attr]

    reset = store.reset_connect_job_for_resubmission(
        job_id=job_id,
        expected_state="processing",
        provider_app_id="alternate-provider",
        provider_instance_id="11111111-1111-4111-8111-111111111111",
    )
    assert reset.status == "requested"

    with pytest.raises(RuntimeError, match="expected-state"):
        store.reset_connect_job_for_resubmission(
            job_id=job_id,
            expected_state="processing",
            provider_app_id="alternate-provider",
            provider_instance_id="11111111-1111-4111-8111-111111111111",
        )
