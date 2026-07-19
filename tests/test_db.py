from datetime import UTC, datetime, timedelta
from pathlib import Path

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
    store.mark_summarized(
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
        notified=True,
    )
    assert store.pending() == []
    assert store.recent(1)[0]["summary"] == "Invoice received."


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
