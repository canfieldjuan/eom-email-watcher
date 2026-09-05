import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from eom_email_watcher import cli
from eom_email_watcher.db import Store
from eom_email_watcher.engine_api import ApiError
from eom_email_watcher.notifications import ChannelResult, DeliveryResult


def test_setup_reports_partial_notification_failure(tmp_path: Path, monkeypatch, capsys) -> None:
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
    monkeypatch.setattr(
        "eom_email_watcher.engine_api.dispatch",
        lambda request: {"baseline_initialized": True, "connected": True},
    )
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
    assert "Baseline initialized" in output.out


def test_doctor_uses_the_active_imap_provider_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir(mode=0o700)
    account = SimpleNamespace(provider="imap", account_id=f"imap-{'a' * 32}")

    class FakeStore:
        def mail_account(self, provider: str, account_id: str):
            assert (provider, account_id) == (account.provider, account.account_id)
            return account

        def state(self, *, provider: str, account_id: str):
            assert (provider, account_id) == (account.provider, account.account_id)
            return ("eom-imap-v1:44:7", "2026-09-05T00:00:00+00:00")

    config = SimpleNamespace(
        path=tmp_path / "config.toml",
        senders=("trusted@example.com",),
        database_file=state_directory / "watcher.sqlite3",
        gmail_send_token_file=state_directory / "send-token.json",
        monthly_hours_recipient=None,
        model_require_auth=False,
        model_api_token_file=None,
        model_base_url="http://127.0.0.1:11434/v1",
    )
    model = SimpleNamespace(health=lambda: (True, "ready"))
    monkeypatch.setattr(cli, "_runtime", lambda _path: (config, FakeStore(), model))
    monkeypatch.setattr(
        cli,
        "configured_mailbox_identity",
        lambda _store: (account.provider, account.account_id),
    )
    monkeypatch.setattr(cli, "mail_account_connected", lambda _config, _account: True)
    monkeypatch.setattr(cli.shutil, "which", lambda _name: "/usr/bin/notify-send")

    assert cli._doctor(config.path) == 0

    checks = json.loads(capsys.readouterr().out)
    assert checks["mail_provider_connection"] == {"ok": True, "provider": "imap"}
    assert checks["mail_account_credentials"] == {"ok": True, "provider": "imap"}
    assert "oauth_credentials" not in checks
    assert "oauth_token" not in checks


def test_setup_connects_a_replacement_for_unidentified_migrated_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = SimpleNamespace(
        notifications_enabled=False,
        ntfy_topic=None,
        ntfy_url="https://ntfy.sh",
    )
    operations: list[str] = []

    def dispatch(request: dict[str, object]) -> dict[str, object]:
        operation = str(request["operation"])
        operations.append(operation)
        if operation == "gmail.authorize":
            raise ApiError(
                "account_identity_unverified",
                "The existing mailbox identity cannot be verified; connect it as a new account",
            )
        assert request["payload"] == {"provider": "gmail"}
        return {"baseline_initialized": True, "account": {"connected": True}}

    monkeypatch.setattr(cli, "_runtime", lambda path: (config, object(), object()))
    monkeypatch.setattr("eom_email_watcher.engine_api.dispatch", dispatch)

    assert cli._setup(tmp_path / "config.toml") == 0
    assert operations == ["gmail.authorize", "mail.accounts.connect"]


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
        cli,
        "load_configured_mailbox",
        lambda *args: pytest.fail("blocked check must not access mail"),
    )

    with (
        cli._production_check_lock(config.database_file),
        pytest.raises(RuntimeError, match="production check is already running"),
    ):
        cli._check(tmp_path / "config.toml", dry_run=False)


def test_production_check_reloads_runtime_after_acquiring_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale_config = SimpleNamespace(
        **{
            **vars(_check_config(tmp_path)),
            "retention_days": 180,
        }
    )
    fresh_config = SimpleNamespace(
        **{
            **vars(stale_config),
            "gmail_credentials_file": tmp_path / "fresh-credentials.json",
            "gmail_token_file": tmp_path / "fresh-token.json",
            "retention_days": 1,
        }
    )
    runtimes = iter(
        (
            (stale_config, object(), object()),
            (fresh_config, object(), object()),
        )
    )
    monkeypatch.setattr(cli, "_runtime", lambda path: next(runtimes))

    @contextmanager
    def acquired_lock(database_file: Path):
        assert database_file == stale_config.database_file
        yield

    monkeypatch.setattr(cli, "_production_check_lock", acquired_lock)

    def gmail_from_token(credentials_file: Path, token_file: Path):
        assert credentials_file == fresh_config.gmail_credentials_file
        assert token_file == fresh_config.gmail_token_file
        return object()

    monkeypatch.setattr(
        cli,
        "load_configured_mailbox",
        lambda config, store: gmail_from_token(
            config.gmail_credentials_file, config.gmail_token_file
        ),
    )

    class FakeWatcher:
        def __init__(self, config, store, gmail, model):
            assert config is fresh_config

        def check(self, *, dry_run: bool):
            assert dry_run is False
            return {"retention_days": fresh_config.retention_days}

    monkeypatch.setattr(cli, "Watcher", FakeWatcher)

    assert cli._check(tmp_path / "config.toml", dry_run=False) == 0


def test_dry_run_does_not_take_production_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _check_config(tmp_path)
    monkeypatch.setattr(cli, "_runtime", lambda path: (config, object(), object()))
    monkeypatch.setattr(cli, "load_configured_mailbox", lambda config, store: object())

    class FakeWatcher:
        def __init__(self, *args):
            pass

        def check(self, *, dry_run: bool):
            assert dry_run is True
            return {"dry_run": True}

    monkeypatch.setattr(cli, "Watcher", FakeWatcher)

    with cli._production_check_lock(config.database_file):
        assert cli._check(tmp_path / "config.toml", dry_run=True) == 0


def test_zero_sender_production_check_locks_reloads_and_skips_gmail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    stale_config = SimpleNamespace(
        **{
            **vars(_check_config(tmp_path)),
            "senders": (),
            "retention_days": 180,
            "notifications_enabled": True,
        }
    )
    fresh_config = SimpleNamespace(
        **{
            **vars(stale_config),
            "retention_days": 1,
        }
    )

    class FakeStore:
        def purge(self, retention_days: int) -> int:
            assert retention_days == 1
            return 2

    runtimes = iter(
        (
            (stale_config, object(), object()),
            (fresh_config, FakeStore(), object()),
        )
    )
    monkeypatch.setattr(cli, "_runtime", lambda path: next(runtimes))

    @contextmanager
    def acquired_lock(database_file: Path):
        assert database_file == stale_config.database_file
        yield

    monkeypatch.setattr(cli, "_production_check_lock", acquired_lock)
    monkeypatch.setattr(
        cli,
        "load_configured_mailbox",
        lambda *args: pytest.fail("inactive check must not access mail"),
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
