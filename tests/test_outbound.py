from datetime import date
from pathlib import Path

from eom_email_watcher.db import Store
from eom_email_watcher.outbound import previous_month_email


def test_previous_month_email_crosses_year_boundary() -> None:
    email = previous_month_email(date(2027, 1, 1))
    assert email.period_key == "2026-12"
    assert email.subject == "Firefly Hours for December 2026"
    assert "Firefly hours for December 2026" in email.body
    assert "damn" not in email.body.casefold()


def test_outbound_dedupe(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    assert not store.outbound_was_sent("monthly-hours:2026-07")
    store.record_outbound(
        dedupe_key="monthly-hours:2026-07",
        recipient="maria@example.com",
        subject="Subject",
        gmail_message_id="gmail-id",
    )
    assert store.outbound_was_sent("monthly-hours:2026-07")
