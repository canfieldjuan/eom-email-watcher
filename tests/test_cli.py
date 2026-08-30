import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from eom_email_watcher import cli
from eom_email_watcher.db import Store
from eom_email_watcher.notifications import ChannelResult, DeliveryResult


def test_setup_reports_partial_notification_failure(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    token = tmp_path / "token.json"
    token.touch()
    config = SimpleNamespace(
        gmail_token_file=token,
        gmail_credentials_file=tmp_path / "credentials.json",
        notifications_enabled=True,
        ntfy_topic="eom-email-watch-0123456789ab",
        ntfy_url="https://ntfy.sh",
    )
    monkeypatch.setattr(cli, "_runtime", lambda path: (config, object(), object()))
    monkeypatch.setattr(cli.GmailGateway, "from_token", lambda *args: object())

    class FakeWatcher:
        def __init__(self, *args):
            pass

        def bootstrap(self) -> str:
            return "200"

    monkeypatch.setattr(cli, "Watcher", FakeWatcher)
    monkeypatch.setattr(
        cli,
        "send_fallback",
        lambda *args, **kwargs: DeliveryResult(
            desktop=ChannelResult(attempted=True, delivered=True),
            ntfy=ChannelResult(
                attempted=True,
                delivered=False,
                error="ntfy delivery failed: ConnectError",
            ),
        ),
    )

    assert cli._setup(tmp_path / "config.toml") == 0

    output = capsys.readouterr()
    assert "ntfy delivery failed: ConnectError" in output.err
    assert "Baseline history cursor saved (200)" in output.out


def _check_config(tmp_path: Path) -> SimpleNamespace:
    state = tmp_path / "state"
    state.mkdir()
    return SimpleNamespace(
        database_file=state / "watcher.sqlite3",
        gmail_credentials_file=tmp_path / "credentials.json",
        gmail_token_file=tmp_path / "token.json",
        senders=("trusted@example.com",),
    )


def test_second_production_check_stops_before_gmail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _check_config(tmp_path)
    monkeypatch.setattr(cli, "_runtime", lambda path: (config, object(), object()))
    monkeypatch.setattr(
        cli.GmailGateway,
        "from_token",
        lambda *args: pytest.fail("blocked check must not access Gmail"),
    )

    with cli._production_check_lock(config.database_file), pytest.raises(
        RuntimeError, match="production check is already running"
    ):
        cli._check(tmp_path / "config.toml", dry_run=False)


def test_dry_run_does_not_take_production_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _check_config(tmp_path)
    monkeypatch.setattr(cli, "_runtime", lambda path: (config, object(), object()))
    monkeypatch.setattr(cli.GmailGateway, "from_token", lambda *args: object())

    class FakeWatcher:
        def __init__(self, *args):
            pass

        def check(self, *, dry_run: bool):
            assert dry_run is True
            return {"dry_run": True}

    monkeypatch.setattr(cli, "Watcher", FakeWatcher)

    with cli._production_check_lock(config.database_file):
        assert cli._check(tmp_path / "config.toml", dry_run=True) == 0


def test_zero_sender_check_stops_before_lock_and_gmail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    config = SimpleNamespace(
        **{
            **vars(_check_config(tmp_path)),
            "senders": (),
            "retention_days": 180,
            "notifications_enabled": True,
        }
    )

    class FakeStore:
        def purge(self, retention_days: int, *, preserve_notification_intents: bool) -> int:
            assert retention_days == 180
            assert preserve_notification_intents is True
            return 2

    monkeypatch.setattr(cli, "_runtime", lambda path: (config, FakeStore(), object()))
    monkeypatch.setattr(
        cli,
        "_production_check_lock",
        lambda path: pytest.fail("inactive check must not take the production lock"),
    )
    monkeypatch.setattr(
        cli.GmailGateway,
        "from_token",
        lambda *args: pytest.fail("inactive check must not access Gmail"),
    )

    assert cli._check(tmp_path / "config.toml", dry_run=False) == 0
    assert json.loads(capsys.readouterr().out) == {
        "active": False,
        "discovered": 0,
        "fallback_notified": 0,
        "purged": 2,
        "stale_cursor_recovered": False,
        "summarized": 0,
    }


def test_requeue_analysis_command_releases_only_permanent_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    store = Store(tmp_path / "watcher.sqlite3")
    store.initialize()
    store.add_message(
        message_id="message-1",
        thread_id=None,
        sender="trusted@example.com",
        sender_name=None,
        subject="Subject",
        received_at="2026-08-29T12:00:00+00:00",
    )
    store.reserve_analysis_request("message-1", 20_000)
    store.record_analysis_failure(
        "message-1",
        "Inference gateway error: forbidden",
        0,
        retryable=False,
        error_code="forbidden",
    )
    monkeypatch.setattr(cli, "_runtime", lambda path: (object(), store, object()))

    with pytest.raises(SystemExit) as exited:
        cli.main(["--config", str(tmp_path / "config.toml"), "requeue-analysis", "message-1"])

    assert exited.value.code == 0
    assert json.loads(capsys.readouterr().out) == {
        "message_id": "message-1",
        "status": "requeued",
    }
    assert store.pending()[0].message_id == "message-1"

    with pytest.raises(SystemExit) as missing:
        cli.main(["requeue-analysis", "missing-message"])

    assert missing.value.code == 2
    assert capsys.readouterr().err.strip() == "error: Message was not found"
