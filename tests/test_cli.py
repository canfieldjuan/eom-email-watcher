import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from eom_email_watcher import cli
from eom_email_watcher.db import Store
from eom_email_watcher.engine_api import ApiError
from eom_email_watcher.mailbox import DEFAULT_MAIL_ACCOUNT_ID, DEFAULT_MAIL_PROVIDER
from eom_email_watcher.notifications import ChannelResult, DeliveryResult

TEST_MAILBOX_IDENTITY_KEY = "a" * 64


def test_setup_reports_partial_notification_failure(tmp_path: Path, monkeypatch, capsys) -> None:
    token = tmp_path / "token.json"
    token.touch()
    config = SimpleNamespace(
        gmail_token_file=token,
        gmail_credentials_file=tmp_path / "credentials.json",
        notifications_enabled=True,
        ntfy_topic="eom-email-watch-0123456789ab",
        ntfy_url="https://ntfy.sh",
        ntfy_content_disclosure_acknowledged=True,
    )
    monkeypatch.setattr(cli, "_runtime", lambda path: (config, object(), object()))
    monkeypatch.setattr(
        "eom_email_watcher.engine_api.dispatch",
        lambda request: {"baseline_initialized": True, "connected": True},
    )
    delivered = []

    def capture_fallback(*args, **kwargs):
        delivered.append(kwargs)
        return DeliveryResult(
            desktop=ChannelResult(attempted=True, delivered=True),
            ntfy=ChannelResult(
                attempted=True,
                delivered=False,
                error="ntfy delivery failed: ConnectError",
            ),
        )

    monkeypatch.setattr(cli, "send_fallback", capture_fallback)

    assert cli._setup(tmp_path / "config.toml") == 0

    output = capsys.readouterr()
    assert delivered[0]["ntfy_content_disclosure_acknowledged"] is True
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


def test_recent_uses_entitlement_gated_engine_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.toml"
    requests: list[dict[str, object]] = []

    def dispatch(request: dict[str, object]) -> dict[str, object]:
        requests.append(request)
        return {"items": [{"message_id": "message-1", "calendar_proposal": None}]}

    monkeypatch.setattr("eom_email_watcher.engine_api.dispatch", dispatch)

    assert cli._recent(config_path, 20) == 0

    assert requests == [
        {
            "protocol": 1,
            "operation": "inbox.recent",
            "config_path": str(config_path),
            "payload": {"limit": 20},
        }
    ]
    assert json.loads(capsys.readouterr().out) == [
        {"message_id": "message-1", "calendar_proposal": None}
    ]


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
    monkeypatch.setattr(cli, "load_config", lambda path: config, raising=False)
    monkeypatch.setattr(
        cli,
        "_runtime",
        lambda path: pytest.fail("contended check must not construct runtime"),
    )
    monkeypatch.setattr(
        cli,
        "run_watcher_check",
        lambda *args, **kwargs: pytest.fail("blocked check must not run the watcher"),
    )

    with (
        cli._production_check_lock(config.database_file),
        pytest.raises(RuntimeError, match="production check is already running"),
    ):
        cli._check(tmp_path / "config.toml", dry_run=False)


def test_production_check_loads_runtime_after_acquiring_lock(
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
    lock_held = False
    monkeypatch.setattr(cli, "load_config", lambda path: stale_config, raising=False)

    def runtime(path: Path):
        assert lock_held is True
        return fresh_config, object(), object()

    monkeypatch.setattr(cli, "_runtime", runtime)

    @contextmanager
    def acquired_lock(database_file: Path):
        nonlocal lock_held
        assert database_file == stale_config.database_file
        lock_held = True
        try:
            yield
        finally:
            lock_held = False

    monkeypatch.setattr(cli, "_production_check_lock", acquired_lock)

    def run_watcher_check(config, store, model, *, dry_run: bool):
        assert config is fresh_config
        assert dry_run is False
        return {"retention_days": fresh_config.retention_days}

    monkeypatch.setattr(cli, "run_watcher_check", run_watcher_check)

    assert cli._check(tmp_path / "config.toml", dry_run=False) == 0


def test_dry_run_loads_runtime_after_acquiring_production_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _check_config(tmp_path)
    lock_held = False
    monkeypatch.setattr(cli, "load_config", lambda path: config, raising=False)

    def runtime(path: Path):
        assert lock_held is True
        return config, object(), object()

    @contextmanager
    def acquired_lock(database_file: Path):
        nonlocal lock_held
        assert database_file == config.database_file
        lock_held = True
        try:
            yield
        finally:
            lock_held = False

    monkeypatch.setattr(cli, "_runtime", runtime)
    monkeypatch.setattr(cli, "_production_check_lock", acquired_lock)
    monkeypatch.setattr(
        cli,
        "run_watcher_check",
        lambda config, store, model, *, dry_run: {"dry_run": dry_run},
    )

    assert cli._check(tmp_path / "config.toml", dry_run=True) == 0
    assert lock_held is False


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

    lock_held = False
    monkeypatch.setattr(cli, "load_config", lambda path: stale_config, raising=False)

    def runtime(path: Path):
        assert lock_held is True
        return fresh_config, FakeStore(), object()

    monkeypatch.setattr(cli, "_runtime", runtime)

    @contextmanager
    def acquired_lock(database_file: Path):
        nonlocal lock_held
        assert database_file == stale_config.database_file
        lock_held = True
        try:
            yield
        finally:
            lock_held = False

    monkeypatch.setattr(cli, "_production_check_lock", acquired_lock)

    def run_watcher_check(config, store, model, *, dry_run: bool):
        assert config is fresh_config
        assert dry_run is False
        return {
            "active": False,
            "automation_processed": 0,
            "automation_review_required": 0,
            "discovered": 0,
            "fallback_notified": 0,
            "purged": store.purge(config.retention_days),
            "stale_cursor_recovered": False,
            "summarized": 0,
        }

    monkeypatch.setattr(cli, "run_watcher_check", run_watcher_check)

    assert cli._check(tmp_path / "config.toml", dry_run=False) == 0
    assert json.loads(capsys.readouterr().out) == {
        "active": False,
        "automation_processed": 0,
        "automation_review_required": 0,
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
    store.reconcile_mailbox_identity(
        DEFAULT_MAIL_PROVIDER,
        DEFAULT_MAIL_ACCOUNT_ID,
        TEST_MAILBOX_IDENTITY_KEY,
        legacy_status="replacement",
    )
    store.add_message(
        message_id="message-1",
        thread_id=None,
        sender="trusted@example.com",
        sender_name=None,
        subject="Subject",
        received_at="2026-08-29T12:00:00+00:00",
        mailbox_identity_key=TEST_MAILBOX_IDENTITY_KEY,
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
