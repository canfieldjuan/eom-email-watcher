import io
import json
import logging
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from eom_email_watcher import engine_api
from eom_email_watcher.config import load_config
from eom_email_watcher.gmail import (
    GmailAuthorizationRejected,
    GmailError,
    MessageMetadata,
)
from eom_email_watcher.mailbox import (
    DEFAULT_MAIL_ACCOUNT_ID,
    DEFAULT_MAIL_PROVIDER,
    MailboxChanges,
    MailboxSession,
    MessageContent,
    scoped_message_id,
)
from eom_email_watcher.mime import AttachmentDescriptor, extract_body
from eom_email_watcher.model import Analysis
from eom_email_watcher.runtime import Runtime, load_runtime


def write_config(
    path: Path,
    *,
    ntfy_topic: str | None = None,
    notifications_enabled: bool = True,
    extra_settings: str = "",
    timezone: str = "America/Chicago",
    include_senders: bool = True,
) -> None:
    ntfy_setting = f'ntfy_topic = "{ntfy_topic}"\n' if ntfy_topic else ""
    notifications_setting = str(notifications_enabled).lower()
    credentials_file = (path.parent / "credentials.json").as_posix()
    token_file = (path.parent / "token.json").as_posix()
    send_token_file = (path.parent / "send-token.json").as_posix()
    database_file = (path.parent / "watcher.sqlite3").as_posix()
    senders = (
        """[[senders]]
email = "z@example.com"
name = "Zed"

[[senders]]
email = "A@Example.com"
name = "Trusted A"
"""
        if include_senders
        else ""
    )
    path.write_text(
        f'''timezone = "{timezone}"
gmail_credentials_file = "{credentials_file}"
gmail_token_file = "{token_file}"
gmail_send_token_file = "{send_token_file}"
database_file = "{database_file}"
model_base_url = "http://127.0.0.1:1234/v1"
model_name = "local-model"
model_require_auth = false
notifications_enabled = {notifications_setting}
{ntfy_setting}
{extra_settings}
{senders}
''',
        encoding="utf-8",
    )


def request(config_path: Path, operation: str, payload: dict[str, object] | None = None):
    return {
        "protocol": 1,
        "operation": operation,
        "config_path": str(config_path),
        "payload": payload or {},
    }


def test_read_operations_are_versioned_and_do_not_expose_token_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    monkeypatch.setattr(
        "eom_email_watcher.model.LocalModel.health", lambda self: (True, "HTTP 200")
    )

    watchlist = engine_api._response(request(config_path, "watchlist.list"))
    assert watchlist == {
        "protocol": 1,
        "ok": True,
        "operation": "watchlist.list",
        "data": {
            "items": [
                {"email": "a@example.com", "name": "Trusted A"},
                {"email": "z@example.com", "name": "Zed"},
            ]
        },
    }

    settings = engine_api._response(request(config_path, "settings.get"))
    assert settings["ok"] is True
    assert settings["data"]["poll_interval_minutes"] == 120
    assert settings["data"]["polling_supported"] is True
    encoded = json.dumps(settings)
    assert "model_api_token_file" not in encoded
    assert "send-token.json" not in encoded

    health = engine_api._response(request(config_path, "health.get"))
    assert health["data"]["local_model"]["ok"] is True
    assert health["data"]["notifications"] == {
        "delivery": "host",
        "enabled": True,
        "host_delivery_ready": True,
        "ntfy_configured": False,
    }
    assert health["data"]["production_check_supported"] is True

    runtime = load_runtime(config_path)
    runtime.store.add_message(
        message_id="m1",
        thread_id=None,
        sender="a@example.com",
        sender_name=None,
        subject="Subject",
        received_at="2026-07-18T14:00:00+00:00",
    )
    inbox = engine_api._response(request(config_path, "inbox.recent", {"limit": 1}))
    assert inbox["data"]["items"][0]["message_id"] == "m1"
    assert inbox["data"]["items"][0]["provider"] == "gmail"
    assert inbox["data"]["items"][0]["account_id"] == "gmail-default"
    assert inbox["data"]["items"][0]["attachments"] == []


def test_inbox_query_returns_opaque_cursor_and_uses_only_local_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    for message_id in ("message-1", "message-2"):
        runtime.store.add_message(
            message_id=message_id,
            thread_id=None,
            sender="billing@example.com",
            sender_name="Billing",
            subject="Invoice status",
            received_at="2026-08-31T12:00:00+00:00",
        )
    runtime.store.mark_analyzed(
        "message-2",
        {
            "category": "invoice",
            "priority": "high",
            "summary": "Invoice needs review.",
            "action_required": True,
            "suggested_action": "Review it.",
            "deadline_text": None,
            "deadline_iso": None,
            "confidence": 0.9,
        },
    )
    monkeypatch.setattr(engine_api, "load_runtime", lambda _path: runtime)

    def reject_external_access(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Inbox query attempted external provider access")

    monkeypatch.setattr(engine_api.GmailGateway, "from_token", reject_external_access)
    monkeypatch.setattr(runtime.model, "analyze", reject_external_access)
    monkeypatch.setattr(engine_api.connect, "discover_capabilities", reject_external_access)
    monkeypatch.setattr(engine_api.connect, "discover_summary_capability", reject_external_access)
    first = engine_api._response(
        request(
            config_path,
            "inbox.query",
            {
                "account_id": "gmail-default",
                "category": "invoice",
                "limit": 1,
                "provider": "gmail",
                "sender_query": "BILL",
            },
        )
    )
    assert first["ok"] is True
    assert first["data"]["items"][0]["message_id"] == "message-2"
    assert first["data"]["items"][0]["category"] == "invoice"
    assert first["data"]["next_cursor"] is None

    unfiltered = engine_api._response(request(config_path, "inbox.query", {"limit": 1}))
    cursor = unfiltered["data"]["next_cursor"]
    assert isinstance(cursor, str) and cursor
    second = engine_api._response(
        request(config_path, "inbox.query", {"cursor": cursor, "limit": 1})
    )
    assert [item["message_id"] for item in unfiltered["data"]["items"]] == ["message-2"]
    assert [item["message_id"] for item in second["data"]["items"]] == ["message-1"]
    assert second["data"]["next_cursor"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {"limit": False},
        {"limit": 0},
        {"limit": 101},
        {"cursor": "not-valid-base64!"},
        {"cursor": "eyJtZXNzYWdlX2lkIjoibTEiLCJyZWNlaXZlZF9hdCI6InQiLCJ2Ijp0cnVlfQ"},
        {"sender_query": ""},
        {"keyword": "x" * 201},
        {"priority": "critical"},
        {"category": "payments"},
        {"status": "deleted"},
    ],
)
def test_inbox_query_rejects_invalid_bounds_and_cursors(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    response = engine_api._response(request(tmp_path / "unused.toml", "inbox.query", payload))

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


def test_inbox_delete_and_clear_are_local_only_and_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.store.set_state("100", datetime.now(UTC))
    for message_id in ("message-1", "message-2"):
        runtime.store.add_message(
            message_id=message_id,
            thread_id=None,
            sender="billing@example.com",
            sender_name="Billing",
            subject="Invoice status",
            received_at=datetime.now(UTC).isoformat(),
        )
    monkeypatch.setattr(engine_api, "load_runtime", lambda _path: runtime)

    def reject_external_access(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Local inbox mutation attempted external provider access")

    monkeypatch.setattr(engine_api.GmailGateway, "from_token", reject_external_access)
    monkeypatch.setattr(runtime.model, "analyze", reject_external_access)
    monkeypatch.setattr(engine_api.connect, "discover_capabilities", reject_external_access)

    deleted = engine_api._response(
        request(config_path, "inbox.delete", {"message_id": "message-1"})
    )
    missing = engine_api._response(request(config_path, "inbox.delete", {"message_id": "missing"}))
    cleared = engine_api._response(request(config_path, "inbox.clear"))

    assert deleted["data"] == {"deleted": True, "message_id": "message-1"}
    assert missing["error"]["code"] == "not_found"
    assert cleared["data"] == {"deleted": 1}
    assert runtime.store.recent(10) == []
    assert runtime.store.state()[0] == "100"


@pytest.mark.parametrize("message_id", [None, "", "x" * 513])
def test_inbox_delete_rejects_invalid_message_identity(tmp_path: Path, message_id: object) -> None:
    response = engine_api._response(
        request(tmp_path / "unused.toml", "inbox.delete", {"message_id": message_id})
    )

    assert response["error"]["code"] == "invalid_request"


def test_health_recognizes_bundled_desktop_oauth_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    bundle_root = tmp_path / "bundle"
    bundled_file = bundle_root / "eom_email_watcher_data/google-oauth-client.json"
    bundled_file.parent.mkdir(parents=True)
    bundled_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("eom_email_watcher.gmail.sys._MEIPASS", str(bundle_root), raising=False)
    monkeypatch.setattr(
        "eom_email_watcher.model.LocalModel.health", lambda self: (True, "HTTP 200")
    )

    health = engine_api._response(request(config_path, "health.get"))

    assert health["data"]["gmail"]["credentials_configured"] is True


def test_config_initialize_creates_safe_first_run_contract(tmp_path: Path) -> None:
    config_path = tmp_path / "new" / "config.toml"

    response = engine_api._response(
        request(
            config_path,
            "config.initialize",
            {
                "model_base_url": "http://127.0.0.1:8080/v1",
                "model_name": "local-model",
                "timezone": "UTC",
            },
        )
    )

    assert response["ok"] is True
    assert response["data"]["created"] is True
    assert response["data"]["settings"]["timezone"] == "UTC"
    assert response["data"]["settings"]["local_model"] == {
        "authentication_required": False,
        "editable": True,
        "endpoint": "http://127.0.0.1:8080/v1",
        "model": "local-model",
        "timeout_seconds": 60.0,
        "token_configured": False,
    }
    assert load_config(config_path).senders == ()
    encoded = json.dumps(response)
    assert "token.json" not in encoded
    assert "send-token.json" not in encoded


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"model_base_url": "http://127.0.0.1:8080/v1", "model_name": "model"},
        {
            "model_base_url": "http://127.0.0.1:8080/v1",
            "model_name": "model",
            "timezone": "UTC",
            "unknown": True,
        },
        {
            "model_base_url": "https://models.example.com/v1",
            "model_name": "model",
            "timezone": "UTC",
        },
    ],
)
def test_config_initialize_rejects_invalid_payload_without_creating_file(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    config_path = tmp_path / "config.toml"

    response = engine_api._response(request(config_path, "config.initialize", payload))

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert not config_path.exists()


def test_config_initialize_reports_conflict_without_reading_or_replacing(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    original = b"operator-owned config\n"
    config_path.write_bytes(original)

    response = engine_api._response(
        request(
            config_path,
            "config.initialize",
            {
                "model_base_url": "http://127.0.0.1:8080/v1",
                "model_name": "local-model",
                "timezone": "UTC",
            },
        )
    )

    assert response["ok"] is False
    assert response["error"] == {
        "code": "conflict",
        "message": "Configuration already exists",
    }
    assert config_path.read_bytes() == original


def test_gmail_authorize_creates_current_baseline_without_exposing_identifiers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)

    class AuthorizedGmail:
        def profile_history_id(self) -> str:
            return "private-history-id"

    monkeypatch.setattr(
        engine_api.GmailGateway,
        "authorize_with_status",
        lambda credentials_file, token_file: (AuthorizedGmail(), True),
    )

    response = engine_api._response(request(config_path, "gmail.authorize"))

    assert response == {
        "data": {"baseline_initialized": True, "connected": True},
        "ok": True,
        "operation": "gmail.authorize",
        "protocol": 1,
    }
    assert "private-history-id" not in json.dumps(response)
    assert load_runtime(config_path).store.state()[0] == "private-history-id"


def test_gmail_authorize_preserves_existing_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.store.set_state("preserved-history-id", datetime(2026, 7, 18, tzinfo=UTC))
    runtime.config.gmail_token_file.write_text("existing token", encoding="utf-8")

    class ExistingGmail:
        def profile_history_id(self) -> str:
            return "current-probe-history-id"

    monkeypatch.setattr(
        engine_api.GmailGateway,
        "authorize_with_status",
        lambda credentials_file, token_file: (ExistingGmail(), False),
    )

    response = engine_api._response(request(config_path, "gmail.authorize"))

    assert response["data"] == {"baseline_initialized": False, "connected": True}
    assert load_runtime(config_path).store.state()[0] == "preserved-history-id"


def test_gmail_authorize_replaces_a_token_rejected_by_gmail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.store.set_state("old-history-id", datetime(2026, 7, 18, tzinfo=UTC))
    authorization_calls: list[bool] = []

    class RejectedGmail:
        def profile_history_id(self) -> str:
            raise GmailAuthorizationRejected("rejected")

    class ReauthorizedGmail:
        def profile_history_id(self) -> str:
            return "new-history-id"

    def authorize_with_status(credentials_file, token_file, *, force_reauthorize: bool = False):
        authorization_calls.append(force_reauthorize)
        if force_reauthorize:
            return ReauthorizedGmail(), True
        return RejectedGmail(), False

    monkeypatch.setattr(
        engine_api.GmailGateway,
        "authorize_with_status",
        authorize_with_status,
    )

    response = engine_api._response(request(config_path, "gmail.authorize"))

    assert response["data"] == {"baseline_initialized": True, "connected": True}
    assert authorization_calls == [False, True]
    assert load_runtime(config_path).store.state()[0] == "new-history-id"


def test_gmail_authorize_initializes_missing_baseline_with_existing_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    token_file = tmp_path / "token.json"
    token_file.write_text("existing token", encoding="utf-8")

    class ExistingGmail:
        def profile_history_id(self) -> str:
            return "current-history-id"

    monkeypatch.setattr(
        engine_api.GmailGateway,
        "authorize_with_status",
        lambda credentials_file, configured_token_file: (ExistingGmail(), False),
    )

    response = engine_api._response(request(config_path, "gmail.authorize"))

    assert response["data"] == {"baseline_initialized": True, "connected": True}
    assert load_runtime(config_path).store.state()[0] == "current-history-id"


def test_gmail_authorize_holds_operation_lock_through_baseline_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    lock_held = False

    class AuthorizationLock:
        def __init__(self, path: str, timeout: int):
            assert path.endswith("watcher.sqlite3.gmail-authorize.lock")
            assert timeout == engine_api.GMAIL_AUTHORIZATION_LOCK_TIMEOUT_SECONDS

        def __enter__(self):
            nonlocal lock_held
            lock_held = True

        def __exit__(self, exc_type, exc_value, traceback):
            nonlocal lock_held
            lock_held = False

    class AuthorizedGmail:
        def profile_history_id(self) -> str:
            assert lock_held
            return "serialized-history-id"

    original_state = runtime.store.state
    original_set_state = runtime.store.set_state

    def state(*, provider: str, account_id: str):
        assert lock_held
        assert (provider, account_id) == (DEFAULT_MAIL_PROVIDER, DEFAULT_MAIL_ACCOUNT_ID)
        return original_state(provider=provider, account_id=account_id)

    def set_state(history_id: str, *, provider: str, account_id: str):
        assert lock_held
        assert (provider, account_id) == (DEFAULT_MAIL_PROVIDER, DEFAULT_MAIL_ACCOUNT_ID)
        original_set_state(history_id, provider=provider, account_id=account_id)

    monkeypatch.setattr(engine_api, "FileLock", AuthorizationLock)
    monkeypatch.setattr(engine_api, "_runtime", lambda request: runtime)
    monkeypatch.setattr(runtime.store, "state", state)
    monkeypatch.setattr(runtime.store, "set_state", set_state)
    monkeypatch.setattr(
        engine_api.GmailGateway,
        "authorize_with_status",
        lambda credentials_file, token_file: (AuthorizedGmail(), True),
    )

    response = engine_api._response(request(config_path, "gmail.authorize"))

    assert response["data"] == {"baseline_initialized": True, "connected": True}
    assert lock_held is False
    assert original_state()[0] == "serialized-history-id"


def test_settings_update_is_allowlisted_atomic_and_secret_free(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path, extra_settings="# preserve this\nextension_key = 7")

    response = engine_api._response(
        request(
            config_path,
            "settings.update",
            {
                "model_base_url": "http://localhost:8080/v1/",
                "model_name": " replacement-model ",
                "poll_interval_minutes": 45,
                "retention_days": 365,
                "notifications_enabled": False,
            },
        )
    )

    assert response["ok"] is True
    assert response["data"]["poll_interval_minutes"] == 45
    assert response["data"]["retention_days"] == 365
    assert response["data"]["notifications_enabled"] is False
    assert response["data"]["local_model"] == {
        "authentication_required": False,
        "editable": True,
        "endpoint": "http://localhost:8080/v1",
        "model": "replacement-model",
        "timeout_seconds": 60.0,
        "token_configured": False,
    }
    assert "# preserve this" in config_path.read_text(encoding="utf-8")
    assert "extension_key = 7" in config_path.read_text(encoding="utf-8")
    encoded = json.dumps(response)
    assert "token.json" not in encoded
    assert "send-token.json" not in encoded


def test_settings_update_applies_shortened_retention_immediately(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path, extra_settings="retention_days = 180")
    runtime = load_runtime(config_path)
    for message_id, received_at in (
        ("expired", datetime.now(UTC).replace(microsecond=0) - timedelta(days=2)),
        ("current", datetime.now(UTC).replace(microsecond=0)),
    ):
        runtime.store.add_message(
            message_id=message_id,
            thread_id=None,
            sender="billing@example.com",
            sender_name="Billing",
            subject=message_id,
            received_at=received_at.isoformat(),
        )

    response = engine_api._response(request(config_path, "settings.update", {"retention_days": 1}))

    assert response["ok"] is True
    assert response["data"]["retention_days"] == 1
    assert [row["message_id"] for row in runtime.store.recent(10)] == ["current"]


def test_production_check_reloads_retention_after_acquiring_operation_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path, extra_settings="retention_days = 180")
    stale_runtime = load_runtime(config_path)
    stale_runtime.store.set_state("100", datetime.now(UTC))
    expired_at = (datetime.now(UTC) - timedelta(days=2)).isoformat()

    class ExpiredGmail:
        def history_message_ids(self, cursor: str):
            return ["expired"], "200"

        def changes_since(self, cursor: str) -> MailboxChanges:
            message_ids, newest = self.history_message_ids(cursor)
            return MailboxChanges(tuple(message_ids), newest)

        def metadata(self, message_id: str) -> MessageMetadata:
            return MessageMetadata(
                message_id,
                None,
                "a@example.com",
                "Trusted A",
                "Expired message",
                expired_at,
                frozenset({"INBOX"}),
            )

        def full_payload(self, message_id: str):
            raise AssertionError("expired message must not reach full-body retrieval")

    real_load_runtime = engine_api.load_runtime
    load_count = 0

    def load_runtime_with_stale_first(path: Path) -> Runtime:
        nonlocal load_count
        load_count += 1
        if load_count == 1:
            return stale_runtime
        return real_load_runtime(path)

    @contextmanager
    def shorten_retention_before_lock_entry(lock_path: Path, busy_message: str):
        engine_api.update_settings(config_path, {"retention_days": 1})
        stale_runtime.store.purge(1)
        yield

    monkeypatch.setattr(engine_api, "load_runtime", load_runtime_with_stale_first)
    monkeypatch.setattr(engine_api, "operation_lock", shorten_retention_before_lock_entry)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: ExpiredGmail())

    response = engine_api._response(request(config_path, "watcher.check"))

    assert response["ok"] is True
    assert response["data"]["discovered"] == 0
    assert load_count == 2
    assert load_config(config_path).retention_days == 1
    assert stale_runtime.store.recent(10) == []


@pytest.mark.parametrize(
    "operation",
    ["notifications.pending", "notifications.pending_under_host_lock"],
)
def test_notifications_pending_purges_expired_intents_before_host_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path, extra_settings="retention_days = 1")
    runtime = load_runtime(config_path)
    runtime.store.add_message(
        message_id="expired",
        thread_id=None,
        sender="a@example.com",
        sender_name="Trusted A",
        subject="Expired message",
        received_at=(datetime.now(UTC) - timedelta(days=2)).isoformat(),
    )
    runtime.store.mark_analyzed(
        "expired",
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
    monkeypatch.setattr(engine_api, "load_runtime", lambda _path: runtime)

    count_response = None
    if operation == "notifications.pending_under_host_lock":
        lock_path = engine_api._production_check_lock_path(runtime.config)
        with engine_api.operation_lock(lock_path, "test operation busy"):
            response = engine_api._response(request(config_path, operation))
            count_response = engine_api._response(
                request(config_path, "notifications.count_under_host_lock")
            )
    else:
        response = engine_api._response(request(config_path, operation))

    assert response["ok"] is True
    assert response["data"]["items"] == []
    assert runtime.store.notification_intents() == []
    assert runtime.store.recent(10) == []
    if count_response is not None:
        assert count_response["data"] == {"count": 0}


def test_host_operation_lock_returns_configured_database_lock_path(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    config = load_config(config_path)

    response = engine_api._response(request(config_path, "host.operation_lock"))

    assert response["ok"] is True
    assert response["data"] == {
        "path": str(config.database_file.with_name(f"{config.database_file.name}.check.lock"))
    }


def test_settings_reports_gateway_model_as_read_only_and_rejects_mutation(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(
        config_path,
        extra_settings=(
            'model_backend = "gateway"\n'
            f'model_api_token_file = "{tmp_path / "model-token"}"\n'
            f'model_ca_file = "{tmp_path / "gateway-ca.pem"}"'
        ),
    )
    gateway_config = (
        config_path.read_text(encoding="utf-8")
        .replace(
            'model_base_url = "http://127.0.0.1:1234/v1"',
            'model_base_url = "https://inference.office.internal:8443"',
        )
        .replace("model_require_auth = false", "model_require_auth = true")
    )
    config_path.write_text(gateway_config, encoding="utf-8")
    original = config_path.read_bytes()

    settings = engine_api._response(request(config_path, "settings.get"))
    changed = engine_api._response(
        request(
            config_path,
            "settings.update",
            {"model_name": "operator-selected-model"},
        )
    )

    assert settings["data"]["local_model"]["editable"] is False
    assert settings["data"]["local_model"]["model"] == "Managed by inference gateway"
    assert changed["error"]["code"] == "invalid_request"
    assert "managed" in changed["error"]["message"]
    assert config_path.read_bytes() == original


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"unknown": True},
        {"poll_interval_minutes": False},
        {"retention_days": 3651},
        {"notifications_enabled": "false"},
        {"model_base_url": "https://models.example.com/v1"},
        {"model_name": "   "},
    ],
)
def test_settings_update_rejects_invalid_payload_without_mutation(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    original = config_path.read_bytes()

    response = engine_api._response(request(config_path, "settings.update", payload))

    assert response["error"]["code"] == "invalid_request"
    assert config_path.read_bytes() == original


@pytest.mark.parametrize(
    ("extra_settings", "timezone", "message"),
    [
        (
            'retention_days = "seven"',
            "America/Chicago",
            "retention_days must be an integer",
        ),
        ("", "/tmp/foo", "Unknown timezone: /tmp/foo"),
    ],
)
def test_settings_update_preserves_existing_configuration_errors(
    tmp_path: Path, extra_settings: str, timezone: str, message: str
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path, extra_settings=extra_settings, timezone=timezone)
    original = config_path.read_bytes()

    response = engine_api._response(
        request(
            config_path,
            "settings.update",
            {"poll_interval_minutes": 45},
        )
    )

    assert response["error"] == {
        "code": "configuration_error",
        "message": message,
    }
    assert config_path.read_bytes() == original


@pytest.mark.parametrize("missing_parent", [False, True])
def test_settings_update_preserves_missing_configuration_error(
    tmp_path: Path, missing_parent: bool
) -> None:
    config_path = (
        tmp_path / "absent" / "missing.toml" if missing_parent else tmp_path / "missing.toml"
    )

    response = engine_api._response(
        request(config_path, "settings.update", {"poll_interval_minutes": 45})
    )

    assert response["error"]["code"] == "configuration_error"
    assert "Configuration not found" in response["error"]["message"]
    assert not config_path.exists()


def test_permanent_analysis_failure_is_visible_and_explicitly_requeueable(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.store.add_message(
        message_id="m1",
        thread_id=None,
        sender="a@example.com",
        sender_name=None,
        subject="Subject",
        received_at="2026-07-18T14:00:00+00:00",
    )
    runtime.store.reserve_analysis_request("m1", 20_000)
    runtime.store.record_analysis_failure(
        "m1",
        "Inference gateway error: forbidden",
        0,
        retryable=False,
        error_code="forbidden",
    )

    inbox = engine_api._response(request(config_path, "inbox.recent", {"limit": 1}))
    assert inbox["data"]["items"][0]["analysis_retryable"] is False
    assert inbox["data"]["items"][0]["analysis_error_code"] == "forbidden"

    requeued = engine_api._response(request(config_path, "analysis.requeue", {"message_id": "m1"}))
    assert requeued["data"] == {"status": "requeued"}
    assert runtime.store.pending()[0].analysis_request_id is None
    refreshed = engine_api._response(request(config_path, "inbox.recent", {"limit": 1}))
    assert refreshed["data"]["items"][0]["analysis_retryable"] is None

    duplicate = engine_api._response(request(config_path, "analysis.requeue", {"message_id": "m1"}))
    assert duplicate["error"]["code"] == "conflict"

    missing = engine_api._response(
        request(config_path, "analysis.requeue", {"message_id": "missing"})
    )
    assert missing["error"]["code"] == "not_found"


def test_watchlist_mutations_are_normalized_and_return_explicit_errors(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path, include_senders=False)

    added = engine_api._response(
        request(
            config_path,
            "watchlist.add",
            {"email": "New Person <NEW@Example.com>", "name": "  New Person  "},
        )
    )
    duplicate = engine_api._response(
        request(config_path, "watchlist.add", {"email": "new@example.com"})
    )
    missing = engine_api._response(
        request(config_path, "watchlist.remove", {"email": "missing@example.com"})
    )
    removed = engine_api._response(
        request(config_path, "watchlist.remove", {"email": "NEW@example.com"})
    )
    listed = engine_api._response(request(config_path, "watchlist.list"))

    assert added["data"]["item"] == {
        "email": "new@example.com",
        "name": "New Person",
    }
    assert duplicate["error"]["code"] == "conflict"
    assert missing["error"]["code"] == "not_found"
    assert removed["data"]["item"] == added["data"]["item"]
    assert listed["data"]["items"] == []


def test_attachment_export_uses_stored_identity_and_safe_private_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    local_message_id = scoped_message_id("microsoft365", "account-2", "provider-message")
    runtime.store.add_message(
        message_id=local_message_id,
        provider="microsoft365",
        account_id="account-2",
        provider_message_id="provider-message",
        thread_id=None,
        sender="a@example.com",
        sender_name=None,
        subject="Attachment",
        received_at="2026-07-18T14:00:00+00:00",
    )
    runtime.store.replace_attachments(
        local_message_id,
        (
            AttachmentDescriptor(
                "", "gmail-attachment", "../../private.PDF", "application/pdf", 4, 0
            ),
        ),
    )
    destination = tmp_path / "exports"
    destination.mkdir()

    class FakeAttachmentGmail:
        def attachment_bytes(
            self, message_id: str, part_id: str, attachment_id: str | None
        ) -> bytes:
            assert (message_id, part_id, attachment_id) == (
                "provider-message",
                "",
                "gmail-attachment",
            )
            return b"%PDF"

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api,
        "configured_mailbox_identity",
        lambda _config: ("microsoft365", "account-2"),
    )
    monkeypatch.setattr(
        engine_api,
        "load_configured_mailbox",
        lambda _config: MailboxSession(
            "microsoft365",
            "account-2",
            FakeAttachmentGmail(),
        ),
    )

    response = engine_api._response(
        request(
            config_path,
            "attachment.export",
            {
                "message_id": local_message_id,
                "part_id": "",
                "destination_dir": str(destination),
            },
        )
    )

    assert response["ok"] is True
    exported = Path(response["data"]["path"])
    assert exported.parent == destination.resolve()
    assert exported.name.startswith("email-watcher-attachment-")
    assert exported.name != "private.PDF"
    assert exported.suffix == ".pdf"
    assert exported.read_bytes() == b"%PDF"
    assert exported.stat().st_mode & 0o777 == 0o600
    assert response["data"] == {
        "byte_size": 4,
        "filename": "../../private.PDF",
        "media_type": "application/pdf",
        "path": str(exported),
    }


def test_attachment_export_rejects_an_unconfigured_account_before_provider_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    local_message_id = scoped_message_id("microsoft365", "account-2", "provider-message")
    runtime.store.add_message(
        message_id=local_message_id,
        provider="microsoft365",
        account_id="account-2",
        provider_message_id="provider-message",
        thread_id=None,
        sender="a@example.com",
        sender_name=None,
        subject="Attachment",
        received_at="2026-07-18T14:00:00+00:00",
    )
    runtime.store.replace_attachments(
        local_message_id,
        (AttachmentDescriptor("2", "attachment", "file.pdf", "application/pdf", 4, 0),),
    )
    destination = tmp_path / "exports"
    destination.mkdir()
    monkeypatch.setattr(engine_api, "load_runtime", lambda _path: runtime)
    monkeypatch.setattr(
        engine_api,
        "load_configured_mailbox",
        lambda _config: pytest.fail("An unavailable account must not be opened"),
    )

    response = engine_api._response(
        request(
            config_path,
            "attachment.export",
            {
                "message_id": local_message_id,
                "part_id": "2",
                "destination_dir": str(destination),
            },
        )
    )

    assert response["error"]["code"] == "account_unavailable"
    assert list(destination.iterdir()) == []


@pytest.mark.parametrize(
    ("operation", "operation_payload"),
    [
        (
            "connect.attachment.capabilities",
            {"message_id": "MESSAGE_ID", "part_id": "2"},
        ),
        (
            "connect.attachment.invoke",
            {
                "request_id": "11111111-1111-4111-8111-111111111111",
                "message_id": "MESSAGE_ID",
                "part_id": "2",
                "provider": {
                    "app_id": "provider",
                    "version": "1.0.0",
                    "instance_id": "provider-instance",
                },
                "capability": {"id": "document.summarize", "version": "1.0"},
                "parameters": {},
                "confirmed": False,
            },
        ),
        (
            "connect.attachment.summarize",
            {"message_id": "MESSAGE_ID", "part_id": "2"},
        ),
    ],
)
def test_connect_paths_reject_an_unconfigured_account_before_provider_interaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    operation_payload: dict[str, object],
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    local_message_id = scoped_message_id("microsoft365", "account-2", "provider-message")
    runtime.store.add_message(
        message_id=local_message_id,
        provider="microsoft365",
        account_id="account-2",
        provider_message_id="provider-message",
        thread_id=None,
        sender="a@example.com",
        sender_name=None,
        subject="Attachment",
        received_at="2026-07-18T14:00:00+00:00",
    )
    runtime.store.replace_attachments(
        local_message_id,
        (AttachmentDescriptor("2", "attachment", "file.pdf", "application/pdf", 4, 0),),
    )
    monkeypatch.setattr(engine_api, "load_runtime", lambda _path: runtime)

    def reject_provider_interaction(*_args: object, **_kwargs: object) -> None:
        pytest.fail("An unavailable mailbox must be rejected before provider interaction")

    monkeypatch.setattr(engine_api, "load_configured_mailbox", reject_provider_interaction)
    monkeypatch.setattr(engine_api.connect, "discover_capabilities", reject_provider_interaction)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_summary_capability",
        reject_provider_interaction,
    )
    monkeypatch.setattr(
        engine_api.connect,
        "require_connect_entitlement",
        reject_provider_interaction,
    )
    payload = {
        key: local_message_id if value == "MESSAGE_ID" else value
        for key, value in operation_payload.items()
    }

    response = engine_api._response(request(config_path, operation, payload))

    assert response["error"]["code"] == "account_unavailable"


def test_attachment_export_fails_closed_before_gmail_or_file_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    destination = tmp_path / "exports"
    destination.mkdir()
    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.GmailGateway,
        "from_token",
        lambda *args: (_ for _ in ()).throw(AssertionError("Gmail must not be called")),
    )

    missing = engine_api._response(
        request(
            config_path,
            "attachment.export",
            {
                "message_id": "missing",
                "part_id": "",
                "destination_dir": str(destination),
            },
        )
    )
    relative = engine_api._response(
        request(
            config_path,
            "attachment.export",
            {"message_id": "missing", "part_id": "", "destination_dir": "relative"},
        )
    )

    assert missing["error"]["code"] == "not_found"
    assert relative["error"]["code"] == "invalid_request"
    assert list(destination.iterdir()) == []


def test_attachment_export_rejects_a_byte_count_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.store.add_message(
        message_id="m1",
        thread_id=None,
        sender="a@example.com",
        sender_name=None,
        subject="Attachment",
        received_at="2026-07-18T14:00:00+00:00",
    )
    runtime.store.replace_attachments(
        "m1",
        (AttachmentDescriptor("2", "attachment", "file.pdf", "application/pdf", 0, 0),),
    )
    destination = tmp_path / "exports"
    destination.mkdir()

    class TruncatedAttachmentGmail:
        def attachment_bytes(self, *args) -> bytes:
            return b"bad"

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.GmailGateway,
        "from_token",
        lambda *args: TruncatedAttachmentGmail(),
    )

    response = engine_api._response(
        request(
            config_path,
            "attachment.export",
            {"message_id": "m1", "part_id": "2", "destination_dir": str(destination)},
        )
    )

    assert response["error"]["code"] == "gmail_error"
    assert list(destination.iterdir()) == []


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"email": 42},
        {"email": "invalid"},
        {"email": "valid@example.com", "name": 42},
        {"email": "valid@example.com", "name": "Bad\nName"},
        {"email": "valid@example.com", "extra": True},
    ],
)
def test_watchlist_add_rejects_invalid_payloads(tmp_path: Path, payload: dict[str, object]) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path, include_senders=False)

    response = engine_api._response(request(config_path, "watchlist.add", payload))

    assert response["error"]["code"] == "invalid_request"


@pytest.mark.parametrize(
    "sender_config",
    [
        '[[senders]]\nemail = "invalid"\n',
        (
            '[[senders]]\nemail = "duplicate@example.com"\n'
            '[[senders]]\nemail = "DUPLICATE@example.com"\n'
        ),
    ],
)
def test_watchlist_mutation_preserves_existing_configuration_errors(
    tmp_path: Path, sender_config: str
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path, include_senders=False)
    config_path.write_text(
        f"{config_path.read_text(encoding='utf-8')}\n{sender_config}",
        encoding="utf-8",
    )

    added = engine_api._response(
        request(config_path, "watchlist.add", {"email": "valid@example.com"})
    )
    removed = engine_api._response(
        request(config_path, "watchlist.remove", {"email": "valid@example.com"})
    )

    assert added["error"]["code"] == "configuration_error"
    assert removed["error"]["code"] == "configuration_error"


def test_zero_sender_check_is_inactive_without_gmail_and_uses_operation_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path, include_senders=False)
    runtime = load_runtime(config_path)
    runtime.store.add_message(
        message_id="queued",
        thread_id=None,
        sender="former@example.com",
        sender_name="Former",
        subject="Queued before removal",
        received_at="2026-07-18T14:00:00+00:00",
    )
    runtime.store.mark_analyzed("queued", FakeModel().analyze().model_dump())
    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.GmailGateway,
        "from_token",
        lambda *args: (_ for _ in ()).throw(AssertionError("Gmail must not be called")),
    )
    lock_paths: list[Path] = []

    @contextmanager
    def acquired_lock(lock_path: Path, _busy_message: str):
        lock_paths.append(lock_path)
        yield

    monkeypatch.setattr(engine_api, "operation_lock_supported", lambda _path: True)
    monkeypatch.setattr(engine_api, "operation_lock", acquired_lock)

    response = engine_api._response(request(config_path, "watcher.check"))

    assert response["data"] == {
        "active": False,
        "discovered": 0,
        "fallback_notified": 0,
        "pending_notifications": 1,
        "purged": 0,
        "stale_cursor_recovered": False,
        "summarized": 0,
    }
    assert lock_paths == [engine_api._production_check_lock_path(runtime.config)]


def test_zero_sender_check_still_rejects_incompatible_host_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(
        config_path,
        include_senders=False,
        ntfy_topic="configured-private-topic",
    )
    runtime = load_runtime(config_path)
    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.GmailGateway,
        "from_token",
        lambda *args: (_ for _ in ()).throw(AssertionError("Gmail must not be called")),
    )

    response = engine_api._response(request(config_path, "watcher.check"))

    assert response["ok"] is False
    assert response["error"]["code"] == "unsupported_configuration"


class FakeGmail:
    def history_message_ids(self, cursor: str):
        return ["m1"], "200"

    def changes_since(self, cursor: str) -> MailboxChanges:
        message_ids, newest = self.history_message_ids(cursor)
        return MailboxChanges(tuple(message_ids), newest)

    def metadata(self, message_id: str) -> MessageMetadata:
        return MessageMetadata(
            message_id,
            None,
            "a@example.com",
            "Untrusted Header Name",
            "Action needed",
            "2026-07-18T14:00:00+00:00",
            frozenset({"INBOX"}),
        )

    def full_payload(self, message_id: str):
        return {"mimeType": "text/plain", "body": {"data": "SGVsbG8="}}

    def content(self, message_id: str, body_char_limit: int) -> MessageContent:
        body, attachment_names, attachments = extract_body(
            self.full_payload(message_id), body_char_limit
        )
        return MessageContent(body, attachment_names, attachments)


class FakeModel:
    def health(self) -> tuple[bool, str]:
        return True, "HTTP 200"

    def analyze(self, **kwargs) -> Analysis:
        return Analysis(
            category="customer_request",
            priority="high",
            summary="Please respond.",
            action_required=True,
            suggested_action="Reply.",
            deadline_text=None,
            deadline_iso=None,
            confidence=0.9,
        )


def test_check_defers_delivery_until_state_checked_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    loaded = load_runtime(config_path)
    loaded.store.set_state("100", datetime(2026, 7, 18, tzinfo=UTC))
    runtime = Runtime(config=loaded.config, store=loaded.store, model=FakeModel())
    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: FakeGmail())

    checked = engine_api._response(request(config_path, "watcher.check"))
    assert checked["ok"] is True
    assert checked["data"]["summarized"] == 1
    assert checked["data"]["pending_notifications"] == 1
    assert loaded.store.recent(1)[0]["status"] == "analyzed"

    pending = engine_api._response(request(config_path, "notifications.pending"))
    intent = pending["data"]["items"][0]
    assert intent["kind"] == "analysis"
    assert intent["body"] == "Please respond.\nNext: Reply."
    assert intent["title"] == "Trusted A: Action needed"

    stale = engine_api._response(
        request(
            config_path,
            "notifications.ack",
            {"message_id": "m1", "kind": "analysis", "analysis_at": "stale"},
        )
    )
    assert stale["error"]["code"] == "stale_notification"
    assert loaded.store.recent(1)[0]["status"] == "analyzed"

    acknowledged = engine_api._response(
        request(
            config_path,
            "notifications.ack",
            {
                "message_id": intent["message_id"],
                "kind": intent["kind"],
                "analysis_at": intent["analysis_at"],
            },
        )
    )
    assert acknowledged["data"]["status"] == "acknowledged"
    assert loaded.store.recent(1)[0]["status"] == "summarized"

    duplicate = engine_api._response(
        request(
            config_path,
            "notifications.ack",
            {
                "message_id": intent["message_id"],
                "kind": intent["kind"],
                "analysis_at": intent["analysis_at"],
            },
        )
    )
    assert duplicate["data"]["status"] == "already_acknowledged"


def test_check_rejects_ntfy_before_gmail_or_state_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path, ntfy_topic="configured-private-topic")
    loaded = load_runtime(config_path)
    loaded.store.set_state("100", datetime(2026, 7, 18, tzinfo=UTC))
    runtime = Runtime(config=loaded.config, store=loaded.store, model=FakeModel())
    loaded.store.add_message(
        message_id="queued",
        thread_id=None,
        sender="a@example.com",
        sender_name=None,
        subject="Queued notification",
        received_at="2026-07-18T14:00:00+00:00",
    )
    loaded.store.record_failure("queued", "local model unavailable", 0)
    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.GmailGateway,
        "from_token",
        lambda *args: (_ for _ in ()).throw(AssertionError("Gmail must not be called")),
    )

    health = engine_api._response(request(config_path, "health.get"))
    settings = engine_api._response(request(config_path, "settings.get"))
    assert health["data"]["notifications"] == {
        "delivery": "host",
        "enabled": True,
        "host_delivery_ready": False,
        "ntfy_configured": True,
    }
    assert settings["data"]["polling_supported"] is False

    checked = engine_api._response(request(config_path, "watcher.check"))
    pending = engine_api._response(request(config_path, "notifications.pending"))
    acknowledged = engine_api._response(
        request(
            config_path,
            "notifications.ack",
            {"message_id": "queued", "kind": "fallback", "analysis_at": None},
        )
    )

    assert checked["ok"] is False
    assert checked["error"]["code"] == "unsupported_configuration"
    assert pending["error"]["code"] == "unsupported_configuration"
    assert acknowledged["error"]["code"] == "unsupported_configuration"
    assert loaded.store.state()[0] == "100"
    assert loaded.store.notification_intents()[0].message_id == "queued"


def test_unsupported_platform_is_reported_before_production_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    loaded = load_runtime(config_path)
    loaded.store.set_state("100", datetime(2026, 7, 18, tzinfo=UTC))
    runtime = Runtime(config=loaded.config, store=loaded.store, model=FakeModel())
    gmail_calls = 0

    def gmail_from_token(*args):
        nonlocal gmail_calls
        gmail_calls += 1
        return FakeGmail()

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    checked_lock_paths: list[Path] = []

    def operation_lock_unsupported(lock_path: Path) -> bool:
        checked_lock_paths.append(lock_path)
        return False

    monkeypatch.setattr(engine_api, "operation_lock_supported", operation_lock_unsupported)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", gmail_from_token)

    health = engine_api._response(request(config_path, "health.get"))
    settings = engine_api._response(request(config_path, "settings.get"))
    checked = engine_api._response(request(config_path, "watcher.check"))

    assert health["data"]["production_check_supported"] is False
    assert health["data"]["notifications"]["host_delivery_ready"] is False
    assert settings["data"]["polling_supported"] is False
    assert checked["error"]["code"] == "unsupported_platform"
    expected_lock_path = loaded.config.database_file.with_name(
        f"{loaded.config.database_file.name}.check.lock"
    )
    assert checked_lock_paths == [
        expected_lock_path,
        expected_lock_path,
        expected_lock_path,
    ]
    assert gmail_calls == 0
    assert loaded.store.state()[0] == "100"


def test_disabled_notifications_hide_analysis_and_fallback_intents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path, notifications_enabled=False)
    runtime = load_runtime(config_path)
    runtime.store.add_message(
        message_id="m1",
        thread_id=None,
        sender="a@example.com",
        sender_name="Untrusted Header Name",
        subject="Action needed",
        received_at="2026-07-18T14:00:00+00:00",
    )
    runtime.store.record_failure("m1", "local model unavailable", 0)
    runtime.store.add_message(
        message_id="m2",
        thread_id=None,
        sender="a@example.com",
        sender_name="Untrusted Header Name",
        subject="Analyzed action",
        received_at="2026-07-18T15:00:00+00:00",
    )
    runtime.store.mark_analyzed(
        "m2",
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
    runtime.store.set_state("100", datetime(2026, 7, 18, tzinfo=UTC))
    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: FakeGmail())

    pending = engine_api._response(request(config_path, "notifications.pending"))
    checked = engine_api._response(request(config_path, "watcher.check", {"dry_run": True}))

    assert pending["data"]["items"] == []
    assert checked["data"]["pending_notifications"] == 0
    assert {row["status"] for row in runtime.store.recent(10)} == {
        "pending",
        "analyzed",
    }


def test_check_reports_complete_notification_backlog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    loaded = load_runtime(config_path)
    loaded.store.set_state("100", datetime(2026, 7, 18, tzinfo=UTC))
    runtime = Runtime(config=loaded.config, store=loaded.store, model=FakeModel())
    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: FakeGmail())
    monkeypatch.setattr(runtime.store, "notification_intent_count", lambda: 501)

    checked = engine_api._response(request(config_path, "watcher.check", {"dry_run": True}))

    assert checked["data"]["pending_notifications"] == 501


def test_protocol_rejects_unknown_top_level_fields_before_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    loaded = load_runtime(config_path)
    loaded.store.set_state("100", datetime(2026, 7, 18, tzinfo=UTC))
    runtime = Runtime(config=loaded.config, store=loaded.store, model=FakeModel())
    gmail_calls = 0

    def gmail_from_token(*args):
        nonlocal gmail_calls
        gmail_calls += 1
        return FakeGmail()

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", gmail_from_token)

    for field, value in (("dry_run", True), ("paylod", {"dry_run": True})):
        invalid = request(config_path, "watcher.check")
        invalid[field] = value
        response = engine_api._response(invalid)
        assert response["error"]["code"] == "invalid_request"
        assert field in response["error"]["message"]

    assert gmail_calls == 0
    assert loaded.store.state()[0] == "100"


def test_gmail_error_response_redacts_configured_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    loaded = load_runtime(config_path)
    loaded.store.set_state("100", datetime(2026, 7, 18, tzinfo=UTC))
    runtime = Runtime(config=loaded.config, store=loaded.store, model=FakeModel())
    sensitive_path = tmp_path / "private-token.json"

    def fail_from_token(*args):
        raise GmailError(f"Invalid OAuth token file: {sensitive_path}")

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", fail_from_token)

    with caplog.at_level(logging.WARNING, logger=engine_api.__name__):
        response = engine_api._response(request(config_path, "watcher.check"))

    assert response["error"] == {
        "code": "gmail_error",
        "message": "Gmail operation failed; see stderr for details",
    }
    assert str(sensitive_path) not in json.dumps(response)
    assert str(sensitive_path) in caplog.text


@pytest.mark.parametrize(
    ("extra_settings", "timezone", "message"),
    [
        (
            'retention_days = "seven"',
            "America/Chicago",
            "retention_days must be an integer",
        ),
        ("", "/tmp/foo", "Unknown timezone: /tmp/foo"),
    ],
)
def test_malformed_setting_returns_configuration_error(
    tmp_path: Path, extra_settings: str, timezone: str, message: str
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(
        config_path,
        extra_settings=extra_settings,
        timezone=timezone,
    )

    response = engine_api._response(request(config_path, "settings.get"))

    assert response["error"] == {
        "code": "configuration_error",
        "message": message,
    }


def test_protocol_rejects_unknown_payload_fields(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    response = engine_api._response(
        request(config_path, "watchlist.list", {"secret": "should-not-be-accepted"})
    )
    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


def test_protocol_rejects_boolean_version(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    invalid = request(config_path, "watchlist.list")
    invalid["protocol"] = True
    response = engine_api._response(invalid)
    assert response["ok"] is False
    assert response["error"]["code"] == "unsupported_protocol"


def test_main_emits_one_json_error_for_invalid_input(monkeypatch, capsys) -> None:
    monkeypatch.setattr(engine_api.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"{")))
    with pytest.raises(SystemExit) as exit_info:
        engine_api.main()
    assert exit_info.value.code == 2
    output = capsys.readouterr()
    assert output.err == ""
    assert json.loads(output.out) == {
        "error": {"code": "invalid_json", "message": "Request must be valid JSON"},
        "ok": False,
        "operation": None,
        "protocol": 1,
    }


@pytest.mark.parametrize("parse_error", [ValueError("integer too long"), RecursionError()])
def test_main_converts_bounded_json_parse_failures_to_one_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    parse_error: Exception,
) -> None:
    decode = json.loads

    def fail_parse(raw: bytes):
        raise parse_error

    monkeypatch.setattr(engine_api.json, "loads", fail_parse)
    monkeypatch.setattr(engine_api.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"{}")))

    with pytest.raises(SystemExit) as exit_info:
        engine_api.main()

    assert exit_info.value.code == 2
    output = capsys.readouterr()
    assert output.err == ""
    assert decode(output.out) == {
        "error": {"code": "invalid_json", "message": "Request must be valid JSON"},
        "ok": False,
        "operation": None,
        "protocol": 1,
    }
