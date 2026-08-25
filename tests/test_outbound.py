from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from eom_email_watcher import cli
from eom_email_watcher.db import Store
from eom_email_watcher.outbound import SendError, previous_month_email


def test_previous_month_email_crosses_year_boundary() -> None:
    email = previous_month_email(date(2027, 1, 1))
    assert email.period_key == "2026-12"
    assert email.subject == "Firefly Hours for December 2026"
    assert "Firefly hours for December 2026" in email.body
    assert "damn" not in email.body.casefold()


def test_outbound_dedupe(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    key = "monthly-hours:2026-07"
    assert store.outbound_status(key) is None
    assert store.reserve_outbound(
        dedupe_key=key,
        recipient="maria@example.com",
        subject="Subject",
    )
    assert store.outbound_status(key) == "reserved"
    assert not store.reserve_outbound(
        dedupe_key=key,
        recipient="maria@example.com",
        subject="Subject",
    )
    store.record_outbound(
        dedupe_key=key,
        recipient="maria@example.com",
        subject="Subject",
        gmail_message_id="gmail-id",
    )
    assert store.outbound_status(key) == "sent"


def test_ambiguous_outbound_cannot_be_reserved_again(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    key = "monthly-hours:2026-07"
    assert store.reserve_outbound(
        dedupe_key=key,
        recipient="maria@example.com",
        subject="Subject",
    )
    store.mark_outbound_ambiguous(key, "Gmail send failed")
    assert store.outbound_status(key) == "ambiguous"
    assert not store.reserve_outbound(
        dedupe_key=key,
        recipient="maria@example.com",
        subject="Subject",
    )


def _outbound_config(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        monthly_hours_recipient="maria@example.com",
        gmail_send_token_file=tmp_path / "send-token.json",
        zone=UTC,
    )


def test_send_hours_reserves_before_gmail_and_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    config = _outbound_config(tmp_path)
    period = previous_month_email(datetime.now(UTC).date()).period_key
    key = f"monthly-hours:{period}"
    monkeypatch.setattr(cli, "_runtime", lambda path: (config, store, object()))

    class FakeSender:
        def send(self, recipient: str, subject: str, body: str) -> str:
            assert store.outbound_status(key) == "reserved"
            return "gmail-id"

    monkeypatch.setattr(cli.GmailSender, "from_token", lambda path: FakeSender())

    assert cli._send_hours(tmp_path / "config.toml", test_to=None, dry_run=False) == 0
    assert store.outbound_status(key) == "sent"


def test_send_hours_failure_becomes_ambiguous_and_blocks_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    config = _outbound_config(tmp_path)
    period = previous_month_email(datetime.now(UTC).date()).period_key
    key = f"monthly-hours:{period}"
    monkeypatch.setattr(cli, "_runtime", lambda path: (config, store, object()))

    class FailingSender:
        def send(self, recipient: str, subject: str, body: str) -> str:
            assert store.outbound_status(key) == "reserved"
            raise SendError("Gmail send failed")

    monkeypatch.setattr(cli.GmailSender, "from_token", lambda path: FailingSender())

    with pytest.raises(SendError, match="Gmail send failed"):
        cli._send_hours(tmp_path / "config.toml", test_to=None, dry_run=False)
    assert store.outbound_status(key) == "ambiguous"


@pytest.mark.parametrize("ambiguous", [False, True])
def test_send_hours_blocks_existing_unresolved_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ambiguous: bool
) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    config = _outbound_config(tmp_path)
    period = previous_month_email(datetime.now(UTC).date()).period_key
    key = f"monthly-hours:{period}"
    content = previous_month_email(datetime.now(UTC).date())
    assert store.reserve_outbound(
        dedupe_key=key,
        recipient=config.monthly_hours_recipient,
        subject=content.subject,
    )
    if ambiguous:
        store.mark_outbound_ambiguous(key, "uncertain delivery")
    monkeypatch.setattr(cli, "_runtime", lambda path: (config, store, object()))

    monkeypatch.setattr(
        cli.GmailSender,
        "from_token",
        lambda path: pytest.fail("ambiguous send must not reach Gmail"),
    )
    with pytest.raises(SendError, match="requires manual reconciliation"):
        cli._send_hours(tmp_path / "config.toml", test_to=None, dry_run=False)


def test_dry_run_and_test_send_do_not_reserve_production_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    config = _outbound_config(tmp_path)
    period = previous_month_email(datetime.now(UTC).date()).period_key
    key = f"monthly-hours:{period}"
    monkeypatch.setattr(cli, "_runtime", lambda path: (config, store, object()))
    calls: list[str] = []

    class FakeSender:
        def send(self, recipient: str, subject: str, body: str) -> str:
            calls.append(recipient)
            return "test-gmail-id"

    monkeypatch.setattr(cli.GmailSender, "from_token", lambda path: FakeSender())

    assert cli._send_hours(tmp_path / "config.toml", test_to=None, dry_run=True) == 0
    assert store.outbound_status(key) is None
    assert calls == []

    assert (
        cli._send_hours(
            tmp_path / "config.toml", test_to="test@example.com", dry_run=False
        )
        == 0
    )
    assert store.outbound_status(key) is None
    assert calls == ["test@example.com"]
