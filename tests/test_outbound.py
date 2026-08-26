import json
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


def _reserved_store(tmp_path: Path, *, ambiguous: bool = False) -> tuple[Store, str]:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    key = "monthly-hours:2026-07"
    assert store.reserve_outbound(
        dedupe_key=key,
        recipient="maria@example.com",
        subject="Subject",
    )
    if ambiguous:
        store.mark_outbound_ambiguous(key, "SendError")
    return store, key


def test_outbound_details_reports_ambiguous_metadata(tmp_path: Path) -> None:
    store, key = _reserved_store(tmp_path, ambiguous=True)

    details = store.outbound_details(key)

    assert details is not None
    assert details["dedupe_key"] == key
    assert details["recipient"] == "maria@example.com"
    assert details["subject"] == "Subject"
    assert details["status"] == "ambiguous"
    assert details["gmail_message_id"] is None
    assert details["reserved_at"]
    assert details["updated_at"]
    assert details["last_error"] == "SendError"


@pytest.mark.parametrize("ambiguous", [False, True])
def test_reconcile_outbound_sent_is_transactional_and_idempotent(
    tmp_path: Path, ambiguous: bool
) -> None:
    store, key = _reserved_store(tmp_path, ambiguous=ambiguous)

    store.reconcile_outbound_sent(key, "gmail-id")
    store.reconcile_outbound_sent(key, "gmail-id")

    details = store.outbound_details(key)
    assert details is not None
    assert details["status"] == "sent"
    assert details["recipient"] == "maria@example.com"
    assert details["subject"] == "Subject"
    assert details["gmail_message_id"] == "gmail-id"
    assert details["reserved_at"] is None
    assert details["last_error"] is None
    with pytest.raises(RuntimeError, match="different Gmail message ID"):
        store.reconcile_outbound_sent(key, "different-id")


@pytest.mark.parametrize("ambiguous", [False, True])
def test_release_outbound_only_removes_unresolved_state(
    tmp_path: Path, ambiguous: bool
) -> None:
    store, key = _reserved_store(tmp_path, ambiguous=ambiguous)

    store.release_outbound(key)

    assert store.outbound_details(key) is None
    assert store.reserve_outbound(
        dedupe_key=key,
        recipient="maria@example.com",
        subject="Subject",
    )


def test_reconciliation_rejects_missing_and_completed_state(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    key = "monthly-hours:2026-07"
    with pytest.raises(RuntimeError, match="non-empty Gmail message ID"):
        store.reconcile_outbound_sent(key, "   ")
    with pytest.raises(RuntimeError, match="no unresolved reservation"):
        store.reconcile_outbound_sent(key, "gmail-id")
    with pytest.raises(RuntimeError, match="no unresolved reservation"):
        store.release_outbound(key)

    assert store.reserve_outbound(
        dedupe_key=key,
        recipient="maria@example.com",
        subject="Subject",
    )
    store.reconcile_outbound_sent(key, "gmail-id")
    with pytest.raises(RuntimeError, match="completed outbound send"):
        store.release_outbound(key)


def test_outbound_reconciliation_cli_never_calls_gmail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store, key = _reserved_store(tmp_path, ambiguous=True)
    monkeypatch.setattr(cli, "_runtime", lambda path: (object(), store, object()))
    monkeypatch.setattr(
        cli.GmailSender,
        "from_token",
        lambda path: pytest.fail("manual reconciliation must not access Gmail"),
    )

    assert cli._outbound_status(tmp_path / "config.toml", key) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["status"] == "ambiguous"
    assert cli._outbound_resolve(
        tmp_path / "config.toml",
        key,
        confirm_sent="gmail-id",
        confirm_unsent=False,
    ) == 0
    resolved = json.loads(capsys.readouterr().out)
    assert resolved == {
        "action": "confirmed_sent",
        "dedupe_key": key,
        "gmail_message_id": "gmail-id",
        "status": "sent",
    }


def test_outbound_confirmed_unsent_cli_only_releases_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store, key = _reserved_store(tmp_path)
    monkeypatch.setattr(cli, "_runtime", lambda path: (object(), store, object()))
    monkeypatch.setattr(
        cli.GmailSender,
        "from_token",
        lambda path: pytest.fail("confirmed-unsent must not access Gmail"),
    )

    assert cli._outbound_resolve(
        tmp_path / "config.toml",
        key,
        confirm_sent=None,
        confirm_unsent=True,
    ) == 0

    output = json.loads(capsys.readouterr().out)
    assert output == {
        "action": "confirmed_unsent",
        "dedupe_key": key,
        "status": "released_for_future_retry",
    }
    assert store.outbound_details(key) is None


def test_outbound_resolve_stops_while_send_operation_is_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, key = _reserved_store(tmp_path)
    monkeypatch.setattr(cli, "_runtime", lambda path: (object(), store, object()))

    with cli._outbound_operation_lock(store.path), pytest.raises(
        RuntimeError, match="Another outbound operation is already running"
    ):
        cli._outbound_resolve(
            tmp_path / "config.toml",
            key,
            confirm_sent=None,
            confirm_unsent=True,
        )

    assert store.outbound_status(key) == "reserved"


def test_production_send_stops_while_reconciliation_is_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    config = _outbound_config(tmp_path)
    monkeypatch.setattr(cli, "_runtime", lambda path: (config, store, object()))
    monkeypatch.setattr(
        cli.GmailSender,
        "from_token",
        lambda path: pytest.fail("blocked production send must not access Gmail"),
    )

    with cli._outbound_operation_lock(store.path), pytest.raises(
        RuntimeError, match="Another outbound operation is already running"
    ):
        cli._send_hours(tmp_path / "config.toml", test_to=None, dry_run=False)


def test_outbound_resolve_parser_requires_exactly_one_confirmation() -> None:
    parser = cli._parser()
    sent = parser.parse_args(
        ["outbound-resolve", "key", "--confirm-sent", "gmail-id"]
    )
    assert sent.confirm_sent == "gmail-id"
    assert sent.confirm_unsent is False
    unsent = parser.parse_args(["outbound-resolve", "key", "--confirm-unsent"])
    assert unsent.confirm_sent is None
    assert unsent.confirm_unsent is True
    with pytest.raises(SystemExit):
        parser.parse_args(["outbound-resolve", "key"])
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["outbound-resolve", "key", "--confirm-sent", "gmail-id", "--confirm-unsent"]
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
