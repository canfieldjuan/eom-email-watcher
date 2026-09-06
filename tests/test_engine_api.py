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
    GmailProfile,
    MessageMetadata,
)
from eom_email_watcher.imap import (
    ImapCredentials,
    ImapError,
    imap_mailbox_identity,
    write_credentials,
)
from eom_email_watcher.mailbox import (
    DEFAULT_MAIL_ACCOUNT_ID,
    DEFAULT_MAIL_PROVIDER,
    MailboxChanges,
    MessageContent,
    scoped_message_id,
)
from eom_email_watcher.microsoft365 import Microsoft365Error, Microsoft365Profile
from eom_email_watcher.microsoft_calendar import MicrosoftPrincipal
from eom_email_watcher.mime import AttachmentDescriptor, extract_body
from eom_email_watcher.model import Analysis
from eom_email_watcher.runtime import (
    Runtime,
    load_runtime,
    mail_account_token_file,
    microsoft_calendar_read_token_file,
)

IMAP_CURSOR = f"eom-imap-v2:{'a' * 64}:44:7"
REPLACEMENT_IMAP_CURSOR = f"eom-imap-v2:{'b' * 64}:55:99"


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
    microsoft_credentials_file = (path.parent / "microsoft-oauth-client.json").as_posix()
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
microsoft_credentials_file = "{microsoft_credentials_file}"
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


def microsoft_principal(*, object_id: str = "object-1") -> MicrosoftPrincipal:
    return MicrosoftPrincipal(
        home_account_id=f"{object_id}.tenant-1",
        tenant_id="tenant-1",
        object_id=object_id,
        email_address="owner@example.com",
    )


def imap_credentials(
    *, host: str = "mail.example.com", password: str = "private-password"
) -> ImapCredentials:
    return ImapCredentials(
        email_address="owner@example.com",
        host=host,
        port=993,
        security="tls",
        username="owner@example.com",
        password=password,
    )


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


def test_legacy_gmail_health_reports_only_the_active_account_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.config.gmail_token_file.write_text("retained token", encoding="utf-8")
    disconnected = runtime.store.register_mail_account(
        "gmail",
        f"gmail-{'b' * 32}",
        display_name="Gmail",
        address="active@example.com",
        active=True,
    )
    monkeypatch.setattr(engine_api, "load_runtime", lambda _path: runtime)
    monkeypatch.setattr(
        "eom_email_watcher.model.LocalModel.health", lambda self: (True, "HTTP 200")
    )

    health = engine_api._response(request(config_path, "health.get"))

    assert runtime.store.active_mail_account() == disconnected
    assert health["data"]["gmail"]["connected"] is False
    assert [
        account["connected"]
        for account in health["data"]["mail"]["accounts"]
        if account["account_id"] == "gmail-default"
    ] == [True]


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
        def profile(self) -> GmailProfile:
            return GmailProfile("owner@example.com", "private-history-id")

    def authorize_with_status(
        credentials_file: Path,
        token_file: Path,
        *,
        force_reauthorize: bool,
    ) -> tuple[AuthorizedGmail, bool]:
        assert force_reauthorize is True
        token_file.write_text("readonly token", encoding="utf-8")
        return AuthorizedGmail(), True

    monkeypatch.setattr(engine_api.GmailGateway, "authorize_with_status", authorize_with_status)

    response = engine_api._response(request(config_path, "gmail.authorize"))

    assert response == {
        "data": {"baseline_initialized": True, "connected": True},
        "ok": True,
        "operation": "gmail.authorize",
        "protocol": 1,
    }
    assert "private-history-id" not in json.dumps(response)
    assert load_runtime(config_path).store.state()[0] == "private-history-id"


def test_gmail_authorize_compatibility_targets_the_active_generated_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.config.gmail_token_file.write_text("retained legacy token", encoding="utf-8")
    active = runtime.store.register_mail_account(
        "gmail",
        f"gmail-{'c' * 32}",
        display_name="Gmail",
        address="active@example.com",
        active=True,
    )

    class AuthorizedGmail:
        def profile(self) -> GmailProfile:
            return GmailProfile("active@example.com", "active-history-id")

    def authorize_with_status(
        credentials_file: Path,
        token_file: Path,
        *,
        force_reauthorize: bool,
    ) -> tuple[AuthorizedGmail, bool]:
        assert force_reauthorize is True
        token_file.write_text("active account token", encoding="utf-8")
        return AuthorizedGmail(), True

    monkeypatch.setattr(engine_api.GmailGateway, "authorize_with_status", authorize_with_status)

    response = engine_api._response(request(config_path, "gmail.authorize"))

    assert response["data"] == {"baseline_initialized": True, "connected": True}
    assert mail_account_token_file(runtime.config, active).read_text(encoding="utf-8") == (
        "active account token"
    )
    assert runtime.config.gmail_token_file.read_text(encoding="utf-8") == "retained legacy token"
    assert runtime.store.state(provider=active.provider, account_id=active.account_id)[0] == (
        "active-history-id"
    )


def test_gmail_authorize_preserves_existing_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.store.set_state("preserved-history-id", datetime(2026, 7, 18, tzinfo=UTC))
    runtime.config.gmail_token_file.write_text("existing token", encoding="utf-8")

    class ExistingGmail:
        def profile(self) -> GmailProfile:
            return GmailProfile("owner@example.com", "current-probe-history-id")

    monkeypatch.setattr(
        engine_api.GmailGateway,
        "from_token",
        lambda credentials_file, token_file: ExistingGmail(),
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
    runtime.store.update_mail_account_identity(
        "gmail",
        "gmail-default",
        display_name="Gmail",
        address="owner@example.com",
    )
    runtime.config.gmail_token_file.write_text("rejected token", encoding="utf-8")
    authorization_calls: list[bool] = []

    class RejectedGmail:
        def profile(self) -> GmailProfile:
            raise GmailAuthorizationRejected("rejected")

    class ReauthorizedGmail:
        def profile(self) -> GmailProfile:
            return GmailProfile("owner@example.com", "new-history-id")

    def authorize_with_status(credentials_file, token_file, *, force_reauthorize: bool = False):
        authorization_calls.append(force_reauthorize)
        token_file.write_text("replacement token", encoding="utf-8")
        return ReauthorizedGmail(), True

    monkeypatch.setattr(
        engine_api.GmailGateway,
        "from_token",
        lambda credentials_file, token_file: RejectedGmail(),
    )
    monkeypatch.setattr(
        engine_api.GmailGateway,
        "authorize_with_status",
        authorize_with_status,
    )

    response = engine_api._response(request(config_path, "gmail.authorize"))

    assert response["data"] == {"baseline_initialized": False, "connected": True}
    assert authorization_calls == [True]
    assert load_runtime(config_path).store.state()[0] == "old-history-id"


def test_gmail_authorize_preserves_token_on_transient_profile_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.store.update_mail_account_identity(
        "gmail",
        "gmail-default",
        display_name="Gmail",
        address="owner@example.com",
    )
    runtime.config.gmail_token_file.write_text("valid token", encoding="utf-8")

    class UnavailableGmail:
        def profile(self) -> GmailProfile:
            raise GmailError("Gmail profile request failed (HTTP 429)")

    authorization_calls = 0

    def authorize_with_status(*args, **kwargs):
        nonlocal authorization_calls
        authorization_calls += 1
        raise AssertionError("transient Gmail errors must not start browser authorization")

    monkeypatch.setattr(
        engine_api.GmailGateway,
        "from_token",
        lambda credentials_file, token_file: UnavailableGmail(),
    )
    monkeypatch.setattr(
        engine_api.GmailGateway,
        "authorize_with_status",
        authorize_with_status,
    )

    response = engine_api._response(request(config_path, "gmail.authorize"))

    assert response["error"] == {
        "code": "gmail_error",
        "message": "Gmail operation failed; see stderr for details",
    }
    assert authorization_calls == 0
    assert runtime.config.gmail_token_file.read_text(encoding="utf-8") == "valid token"


def test_gmail_authorize_initializes_missing_baseline_with_existing_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    token_file = tmp_path / "token.json"
    token_file.write_text("existing token", encoding="utf-8")

    class ExistingGmail:
        def profile(self) -> GmailProfile:
            return GmailProfile("owner@example.com", "current-history-id")

    monkeypatch.setattr(
        engine_api.GmailGateway,
        "from_token",
        lambda credentials_file, configured_token_file: ExistingGmail(),
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

    @contextmanager
    def account_operation_lock(path: Path, busy_message: str):
        nonlocal lock_held
        assert path.name == "watcher.sqlite3.check.lock"
        assert busy_message == "Another mailbox operation is already running"
        lock_held = True
        try:
            yield
        finally:
            lock_held = False

    class AuthorizedGmail:
        def profile(self) -> GmailProfile:
            assert lock_held
            return GmailProfile("owner@example.com", "serialized-history-id")

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

    monkeypatch.setattr(engine_api, "operation_lock_supported", lambda path: True)
    monkeypatch.setattr(engine_api, "operation_lock", account_operation_lock)
    monkeypatch.setattr(engine_api, "_runtime", lambda request: runtime)
    monkeypatch.setattr(runtime.store, "state", state)
    monkeypatch.setattr(runtime.store, "set_state", set_state)

    def authorize_with_status(
        credentials_file: Path,
        token_file: Path,
        *,
        force_reauthorize: bool,
    ) -> tuple[AuthorizedGmail, bool]:
        assert lock_held
        token_file.write_text("readonly token", encoding="utf-8")
        return AuthorizedGmail(), force_reauthorize

    monkeypatch.setattr(engine_api.GmailGateway, "authorize_with_status", authorize_with_status)

    response = engine_api._response(request(config_path, "gmail.authorize"))

    assert response["data"] == {"baseline_initialized": True, "connected": True}
    assert lock_held is False
    assert original_state()[0] == "serialized-history-id"


def test_mail_account_list_adopts_existing_gmail_token_without_exposing_paths(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    (tmp_path / "credentials.json").write_text("{}", encoding="utf-8")
    (tmp_path / "token.json").write_text("private readonly token", encoding="utf-8")

    response = engine_api._response(request(config_path, "mail.accounts.list"))

    assert response["ok"] is True
    assert response["data"] == {
        "accounts": [
            {
                "account_id": "gmail-default",
                "active": True,
                "address": None,
                "connected": True,
                "display_name": "Gmail",
                "last_check": None,
                "provider": "gmail",
            }
        ],
        "providers": [
            {
                "connection_available": True,
                "connection_method": "browser_oauth",
                "display_name": "Gmail",
                "multiple_accounts": True,
                "provider": "gmail",
            },
            {
                "connection_available": True,
                "connection_method": "server_credentials",
                "display_name": "Other mail server",
                "multiple_accounts": True,
                "provider": "imap",
            },
            {
                "connection_available": False,
                "connection_method": "browser_oauth",
                "display_name": "Microsoft 365",
                "multiple_accounts": True,
                "provider": "microsoft365",
            },
        ],
    }
    encoded = json.dumps(response)
    assert "private readonly token" not in encoded
    assert "token.json" not in encoded
    assert str(tmp_path) not in encoded


def test_mail_account_connect_installs_private_token_and_initializes_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)

    class AuthorizedGmail:
        def profile(self) -> GmailProfile:
            return GmailProfile("owner@example.com", "current-cursor")

    def authorize_with_status(
        credentials_file: Path,
        token_file: Path,
        *,
        force_reauthorize: bool,
    ) -> tuple[AuthorizedGmail, bool]:
        assert force_reauthorize is True
        token_file.write_text("private readonly token", encoding="utf-8")
        return AuthorizedGmail(), True

    monkeypatch.setattr(engine_api.GmailGateway, "authorize_with_status", authorize_with_status)

    response = engine_api._response(
        request(config_path, "mail.accounts.connect", {"provider": "GMAIL"})
    )

    assert response["ok"] is True
    assert response["data"]["baseline_initialized"] is True
    account_response = response["data"]["account"]
    assert isinstance(account_response["last_check"], str)
    assert {**account_response, "last_check": "<checked>"} == {
        "account_id": "gmail-default",
        "active": True,
        "address": "owner@example.com",
        "connected": True,
        "display_name": "Gmail",
        "last_check": "<checked>",
        "provider": "gmail",
    }
    runtime = load_runtime(config_path)
    assert runtime.config.gmail_token_file.read_text(encoding="utf-8") == "private readonly token"
    assert runtime.config.gmail_token_file.stat().st_mode & 0o777 == 0o600
    assert runtime.store.active_mail_account().address == "owner@example.com"
    assert runtime.store.state()[0] == "current-cursor"
    assert "private readonly token" not in json.dumps(response)


def test_mail_account_list_advertises_valid_microsoft_public_client_without_exposing_it(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    public_client = tmp_path / "microsoft-oauth-client.json"
    public_client.write_text(
        json.dumps(
            {
                "client_id": "11111111-2222-4333-8444-555555555555",
                "tenant": "organizations",
            }
        ),
        encoding="utf-8",
    )

    response = engine_api._response(request(config_path, "mail.accounts.list"))

    microsoft = next(
        item for item in response["data"]["providers"] if item["provider"] == "microsoft365"
    )
    assert microsoft == {
        "connection_available": True,
        "connection_method": "browser_oauth",
        "display_name": "Microsoft 365",
        "multiple_accounts": True,
        "provider": "microsoft365",
    }
    encoded = json.dumps(response)
    assert "11111111-2222-4333-8444-555555555555" not in encoded
    assert "microsoft-oauth-client.json" not in encoded
    assert str(tmp_path) not in encoded


def test_mail_account_connect_authorizes_microsoft_and_initializes_delta_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)

    class AuthorizedMicrosoft:
        def profile(self) -> Microsoft365Profile:
            return Microsoft365Profile("owner@example.com")

        def initial_cursor(self) -> str:
            return "https://graph.microsoft.com/private-delta-cursor"

    def authorize_with_status(
        credentials_file: Path,
        token_file: Path,
        *,
        force_reauthorize: bool,
    ) -> tuple[AuthorizedMicrosoft, bool]:
        assert force_reauthorize is True
        token_file.write_text("private Microsoft cache", encoding="utf-8")
        return AuthorizedMicrosoft(), True

    monkeypatch.setattr(
        engine_api.Microsoft365Gateway,
        "authorize_with_status",
        authorize_with_status,
    )

    response = engine_api._response(
        request(config_path, "mail.accounts.connect", {"provider": "microsoft365"})
    )

    assert response["ok"] is True
    assert response["data"]["baseline_initialized"] is True
    account_response = response["data"]["account"]
    assert account_response["provider"] == "microsoft365"
    assert account_response["address"] == "owner@example.com"
    assert account_response["connected"] is True
    assert account_response["active"] is True
    runtime = load_runtime(config_path)
    account = runtime.store.active_mail_account()
    assert account is not None
    assert account.provider == "microsoft365"
    assert account.account_id.startswith("microsoft365-")
    token_file = mail_account_token_file(runtime.config, account)
    assert token_file.read_text(encoding="utf-8") == "private Microsoft cache"
    assert token_file.stat().st_mode & 0o777 == 0o600
    assert runtime.store.state(provider=account.provider, account_id=account.account_id)[0] == (
        "https://graph.microsoft.com/private-delta-cursor"
    )
    encoded = json.dumps(response)
    assert "private Microsoft cache" not in encoded
    assert "private-delta-cursor" not in encoded


def test_calendar_read_connect_is_entitled_and_uses_separate_private_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    account = runtime.store.register_mail_account(
        "microsoft365",
        f"microsoft365-{'a' * 32}",
        display_name="Microsoft 365",
        address="owner@example.com",
        active=True,
    )
    mail_token = mail_account_token_file(runtime.config, account)
    mail_token.parent.mkdir(parents=True)
    mail_token.write_text("mail-read-cache", encoding="utf-8")
    monkeypatch.setattr(engine_api, "_calendar_entitlement_active", lambda: True)
    monkeypatch.setattr(
        engine_api, "microsoft_mailbox_principal", lambda *args: microsoft_principal()
    )

    class AuthorizedCalendar:
        principal = microsoft_principal()

    def authorize(credentials_file: Path, staged_token: Path):
        assert runtime.store.calendar_grant(account.account_id).state == "consent_pending"
        staged_token.write_text("calendar-read-cache", encoding="utf-8")
        return AuthorizedCalendar(), True

    monkeypatch.setattr(
        engine_api.MicrosoftCalendarReadAuthorization,
        "authorize_with_status",
        authorize,
    )
    monkeypatch.setattr(
        engine_api.MicrosoftCalendarReadAuthorization,
        "from_token",
        lambda *args: AuthorizedCalendar(),
    )
    payload = {"provider": account.provider, "account_id": account.account_id}

    response = engine_api._response(request(config_path, "calendar.read.connect", payload))

    assert response["ok"] is True
    assert response["data"] == {
        "account_id": account.account_id,
        "available": True,
        "entitlement_active": True,
        "profile": "read",
        "scope": "Calendars.Read",
        "state": "ready",
    }
    calendar_token = microsoft_calendar_read_token_file(runtime.config, account)
    assert calendar_token.read_text(encoding="utf-8") == "calendar-read-cache"
    assert calendar_token.stat().st_mode & 0o777 == 0o600
    assert mail_token.read_text(encoding="utf-8") == "mail-read-cache"
    grant = runtime.store.calendar_grant(account.account_id)
    assert grant is not None
    assert grant.principal_key == microsoft_principal().key
    encoded = json.dumps(response)
    assert "calendar-read-cache" not in encoded
    assert "home_account_id" not in encoded
    assert str(tmp_path) not in encoded

    monkeypatch.setattr(
        engine_api.MicrosoftCalendarReadAuthorization,
        "authorize_with_status",
        lambda *args: pytest.fail("Authorization started without a staging directory"),
    )

    def staging_unavailable(*args, **kwargs):
        raise OSError("calendar staging directory is unavailable")

    monkeypatch.setattr(engine_api.tempfile, "TemporaryDirectory", staging_unavailable)
    staging_failure = engine_api._response(request(config_path, "calendar.read.connect", payload))

    assert staging_failure["error"]["code"] == "calendar_error"
    assert runtime.store.calendar_grant(account.account_id) == grant
    assert calendar_token.read_text(encoding="utf-8") == "calendar-read-cache"


@pytest.mark.parametrize("existing_ready", [False, True])
def test_calendar_read_connect_recovers_exit_after_token_install(
    existing_ready: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    account = runtime.store.register_mail_account(
        "microsoft365",
        f"microsoft365-{'d' * 32}",
        display_name="Microsoft 365",
        address="owner@example.com",
        active=True,
    )
    mail_token = mail_account_token_file(runtime.config, account)
    mail_token.parent.mkdir(parents=True)
    mail_token.write_text("mail-read-cache", encoding="utf-8")
    calendar_token = microsoft_calendar_read_token_file(runtime.config, account)
    selected_principal = microsoft_principal()
    if existing_ready:
        runtime.store.set_calendar_grant(
            account.account_id,
            "read",
            "ready",
            principal_key=selected_principal.key,
            home_account_id=selected_principal.home_account_id,
            tenant_id=selected_principal.tenant_id,
            object_id=selected_principal.object_id,
            email_address=selected_principal.email_address,
        )
        calendar_token.write_text("old-calendar-cache", encoding="utf-8")
    monkeypatch.setattr(engine_api, "_calendar_entitlement_active", lambda: True)
    monkeypatch.setattr(engine_api, "microsoft_mailbox_principal", lambda *args: selected_principal)

    class AuthorizedCalendar:
        principal = selected_principal

    def authorize(credentials_file: Path, staged_token: Path):
        staged_token.write_text("installed-calendar-cache", encoding="utf-8")
        return AuthorizedCalendar(), True

    real_install = engine_api._install_private_token

    def install_then_exit(source: Path, destination: Path) -> None:
        real_install(source, destination)
        raise SystemExit

    monkeypatch.setattr(
        engine_api.MicrosoftCalendarReadAuthorization,
        "authorize_with_status",
        authorize,
    )
    monkeypatch.setattr(engine_api, "_install_private_token", install_then_exit)
    payload = {"provider": account.provider, "account_id": account.account_id}

    with pytest.raises(SystemExit):
        engine_api._response(request(config_path, "calendar.read.connect", payload))

    installed_grant = runtime.store.calendar_grant(account.account_id)
    assert installed_grant is not None
    assert installed_grant.state == "ready"
    assert installed_grant.principal_key == selected_principal.key
    assert calendar_token.read_text(encoding="utf-8") == "installed-calendar-cache"

    monkeypatch.setattr(
        engine_api.MicrosoftCalendarReadAuthorization,
        "from_token",
        lambda *args: AuthorizedCalendar(),
    )
    status = engine_api._response(request(config_path, "calendar.read.status", payload))

    assert status["data"]["state"] == "ready"
    assert status["data"]["available"] is True


def test_calendar_read_entitlement_and_principal_mismatch_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    account = runtime.store.register_mail_account(
        "microsoft365",
        f"microsoft365-{'b' * 32}",
        display_name="Microsoft 365",
        address="owner@example.com",
        active=True,
    )
    payload = {"provider": account.provider, "account_id": account.account_id}
    monkeypatch.setattr(engine_api, "_calendar_entitlement_active", lambda: False)
    monkeypatch.setattr(
        engine_api.MicrosoftCalendarReadAuthorization,
        "authorize_with_status",
        lambda *args: pytest.fail("Locked calendar setup reached Microsoft authorization"),
    )

    locked = engine_api._response(request(config_path, "calendar.read.status", payload))
    rejected = engine_api._response(request(config_path, "calendar.read.connect", payload))

    assert locked["data"]["state"] == "locked"
    assert locked["data"]["available"] is False
    assert rejected["error"]["code"] == "calendar_entitlement_required"

    original = microsoft_principal()
    runtime.store.set_calendar_grant(
        account.account_id,
        "read",
        "ready",
        principal_key=original.key,
        home_account_id=original.home_account_id,
        tenant_id=original.tenant_id,
        object_id=original.object_id,
        email_address=original.email_address,
    )
    calendar_token = microsoft_calendar_read_token_file(runtime.config, account)
    calendar_token.parent.mkdir(parents=True, exist_ok=True)
    calendar_token.write_text("preserved-calendar-cache", encoding="utf-8")
    monkeypatch.setattr(engine_api, "_calendar_entitlement_active", lambda: True)

    class CurrentCalendar:
        principal = original

    def rejected_calendar_cache(*args):
        raise engine_api.MicrosoftAuthorizationRejected("Calendar cache was revoked")

    monkeypatch.setattr(
        engine_api.MicrosoftCalendarReadAuthorization,
        "from_token",
        rejected_calendar_cache,
    )
    revoked_cache = engine_api._response(request(config_path, "calendar.read.status", payload))

    assert revoked_cache["data"]["state"] == "revoked"
    assert revoked_cache["data"]["available"] is False
    revoked_grant = runtime.store.calendar_grant(account.account_id)
    assert revoked_grant.state == "revoked"
    assert revoked_grant.principal_key == original.key

    runtime.store.set_calendar_grant(
        account.account_id,
        "read",
        "ready",
        principal_key=original.key,
        home_account_id=original.home_account_id,
        tenant_id=original.tenant_id,
        object_id=original.object_id,
        email_address=original.email_address,
    )

    monkeypatch.setattr(
        engine_api.MicrosoftCalendarReadAuthorization,
        "from_token",
        lambda *args: CurrentCalendar(),
    )

    def rejected_mailbox_cache(*args):
        raise engine_api.MicrosoftAuthorizationRejected("Mailbox cache was revoked")

    monkeypatch.setattr(engine_api, "microsoft_mailbox_principal", rejected_mailbox_cache)
    unavailable_mailbox = engine_api._response(
        request(config_path, "calendar.read.status", payload)
    )

    assert unavailable_mailbox["data"]["state"] == "ready"
    assert unavailable_mailbox["data"]["available"] is False

    monkeypatch.setattr(
        engine_api,
        "microsoft_mailbox_principal",
        lambda *args: microsoft_principal(object_id="replacement-object"),
    )

    stale_binding = engine_api._response(request(config_path, "calendar.read.status", payload))

    assert stale_binding["data"]["state"] == "ready"
    assert stale_binding["data"]["available"] is False

    monkeypatch.setattr(engine_api, "microsoft_mailbox_principal", lambda *args: original)

    def consent_pending(*args):
        raise engine_api.MicrosoftCalendarConsentPending("Administrator approval is pending")

    monkeypatch.setattr(
        engine_api.MicrosoftCalendarReadAuthorization,
        "authorize_with_status",
        consent_pending,
    )
    pending = engine_api._response(request(config_path, "calendar.read.connect", payload))

    assert pending["data"]["state"] == "consent_pending"
    assert pending["data"]["available"] is False
    assert runtime.store.calendar_grant(account.account_id).state == "consent_pending"
    assert calendar_token.read_text(encoding="utf-8") == "preserved-calendar-cache"

    runtime.store.set_calendar_grant(
        account.account_id,
        "read",
        "ready",
        principal_key=original.key,
        home_account_id=original.home_account_id,
        tenant_id=original.tenant_id,
        object_id=original.object_id,
        email_address=original.email_address,
    )

    def transient_calendar_error(*args):
        raise Microsoft365Error("Microsoft authorization service is temporarily unavailable")

    monkeypatch.setattr(
        engine_api.MicrosoftCalendarReadAuthorization,
        "authorize_with_status",
        transient_calendar_error,
    )
    transient = engine_api._response(request(config_path, "calendar.read.connect", payload))

    assert transient["error"]["code"] == "calendar_error"
    assert runtime.store.calendar_grant(account.account_id).state == "ready"
    assert runtime.store.calendar_grant(account.account_id).principal_key == original.key
    assert calendar_token.read_text(encoding="utf-8") == "preserved-calendar-cache"

    class AuthorizedCalendar:
        principal = original

    def authorize_current(credentials_file: Path, staged_token: Path):
        staged_token.write_text("replacement-calendar-cache", encoding="utf-8")
        return AuthorizedCalendar(), True

    def fail_token_install(source: Path, destination: Path) -> None:
        raise OSError("calendar cache destination is unavailable")

    monkeypatch.setattr(
        engine_api.MicrosoftCalendarReadAuthorization,
        "authorize_with_status",
        authorize_current,
    )
    monkeypatch.setattr(engine_api, "_install_private_token", fail_token_install)
    install_failure = engine_api._response(request(config_path, "calendar.read.connect", payload))

    assert install_failure["error"]["code"] == "calendar_error"
    assert runtime.store.calendar_grant(account.account_id).state == "ready"
    assert runtime.store.calendar_grant(account.account_id).principal_key == original.key
    assert calendar_token.read_text(encoding="utf-8") == "preserved-calendar-cache"

    runtime.store.disconnect_calendar_read(account.account_id)
    first_attempt_failure = engine_api._response(
        request(config_path, "calendar.read.connect", payload)
    )

    assert first_attempt_failure["error"]["code"] == "calendar_error"
    assert runtime.store.calendar_grant(account.account_id).state == "not_requested"

    runtime.store.set_calendar_grant(
        account.account_id,
        "read",
        "ready",
        principal_key=original.key,
        home_account_id=original.home_account_id,
        tenant_id=original.tenant_id,
        object_id=original.object_id,
        email_address=original.email_address,
    )

    class WrongCalendar:
        principal = microsoft_principal(object_id="object-2")

    def authorize_wrong(credentials_file: Path, staged_token: Path):
        staged_token.write_text("wrong-calendar-cache", encoding="utf-8")
        return WrongCalendar(), True

    monkeypatch.setattr(
        engine_api.MicrosoftCalendarReadAuthorization,
        "authorize_with_status",
        authorize_wrong,
    )

    mismatch = engine_api._response(request(config_path, "calendar.read.connect", payload))

    assert mismatch["error"]["code"] == "account_identity_mismatch"
    assert calendar_token.read_text(encoding="utf-8") == "preserved-calendar-cache"
    assert runtime.store.calendar_grant(account.account_id).principal_key == original.key
    assert runtime.store.calendar_grant(account.account_id).state == "ready"


def test_calendar_read_disconnect_preserves_mailbox_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    account = runtime.store.register_mail_account(
        "microsoft365",
        f"microsoft365-{'c' * 32}",
        display_name="Microsoft 365",
        address="owner@example.com",
        active=True,
    )
    selected_principal = microsoft_principal()
    runtime.store.set_calendar_grant(
        account.account_id,
        "read",
        "ready",
        principal_key=selected_principal.key,
        home_account_id=selected_principal.home_account_id,
        tenant_id=selected_principal.tenant_id,
        object_id=selected_principal.object_id,
        email_address=selected_principal.email_address,
    )
    mail_token = mail_account_token_file(runtime.config, account)
    mail_token.parent.mkdir(parents=True)
    mail_token.write_text("mail-read-cache", encoding="utf-8")
    calendar_token = microsoft_calendar_read_token_file(runtime.config, account)
    calendar_token.write_text("calendar-read-cache", encoding="utf-8")
    monkeypatch.setattr(engine_api, "_calendar_entitlement_active", lambda: True)

    response = engine_api._response(
        request(
            config_path,
            "calendar.read.disconnect",
            {"provider": account.provider, "account_id": account.account_id},
        )
    )

    assert response["data"]["state"] == "not_requested"
    assert not calendar_token.exists()
    assert mail_token.read_text(encoding="utf-8") == "mail-read-cache"
    assert runtime.store.calendar_grant(account.account_id).state == "not_requested"


def test_mail_account_connect_installs_private_imap_credentials_and_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    opened_credentials: list[Path] = []

    class AuthorizedImap:
        def initial_cursor(self) -> str:
            return IMAP_CURSOR

    def from_credentials_file(path: Path) -> AuthorizedImap:
        opened_credentials.append(path)
        assert "private-password" in path.read_text(encoding="utf-8")
        return AuthorizedImap()

    monkeypatch.setattr(engine_api.ImapGateway, "from_credentials_file", from_credentials_file)

    response = engine_api._response(
        request(
            config_path,
            "mail.accounts.connect",
            {
                "provider": "imap",
                "connection": {
                    "email_address": "OWNER@Example.com",
                    "host": "mail.example.com",
                    "port": 993,
                    "security": "tls",
                    "username": "owner@example.com",
                    "password": "private-password",
                },
            },
        )
    )

    assert response["ok"] is True
    account_response = response["data"]["account"]
    assert account_response["provider"] == "imap"
    assert account_response["address"] == "owner@example.com"
    assert account_response["active"] is True
    runtime = load_runtime(config_path)
    account = runtime.store.active_mail_account()
    assert account is not None
    assert account.account_id.startswith("imap-")
    credentials_file = mail_account_token_file(runtime.config, account)
    assert credentials_file.is_file()
    assert credentials_file.stat().st_mode & 0o777 == 0o600
    assert runtime.store.state(provider="imap", account_id=account.account_id)[0] == (IMAP_CURSOR)
    encoded = json.dumps(response)
    assert "private-password" not in encoded
    assert "mail.example.com" not in encoded
    assert str(tmp_path) not in encoded
    assert len(opened_credentials) == 1


def test_mail_account_reconnect_rejects_a_different_imap_identity_before_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    account = runtime.store.register_mail_account(
        "imap",
        f"imap-{'a' * 32}",
        display_name="Other mail server",
        address="owner@example.com",
        active=True,
    )
    destination = mail_account_token_file(runtime.config, account)
    monkeypatch.setattr(
        engine_api.ImapGateway,
        "from_credentials_file",
        lambda path: pytest.fail("identity mismatch must fail before opening IMAP"),
    )

    response = engine_api._response(
        request(
            config_path,
            "mail.accounts.reconnect",
            {
                "provider": "imap",
                "account_id": account.account_id,
                "connection": {
                    "email_address": "different@example.com",
                    "host": "mail.example.com",
                    "port": 993,
                    "security": "tls",
                    "username": "different@example.com",
                    "password": "private-password",
                },
            },
        )
    )

    assert response["error"]["code"] == "account_identity_mismatch"
    assert not destination.exists()


def test_mail_account_reconnect_verifies_imap_and_preserves_existing_cursor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    account = runtime.store.register_mail_account(
        "imap",
        f"imap-{'b' * 32}",
        display_name="Other mail server",
        address="owner@example.com",
        active=True,
    )
    runtime.store.set_state(
        IMAP_CURSOR,
        provider=account.provider,
        account_id=account.account_id,
    )
    destination = mail_account_token_file(runtime.config, account)
    destination.parent.mkdir(parents=True)
    write_credentials(destination, imap_credentials(password="old-password"))
    probes = 0

    class VerifiedImap:
        def initial_cursor(self) -> str:
            nonlocal probes
            probes += 1
            return REPLACEMENT_IMAP_CURSOR

    monkeypatch.setattr(
        engine_api.ImapGateway,
        "from_credentials_file",
        lambda _path: VerifiedImap(),
    )

    response = engine_api._response(
        request(
            config_path,
            "mail.accounts.reconnect",
            {
                "provider": "imap",
                "account_id": account.account_id,
                "connection": {
                    "email_address": "owner@example.com",
                    "host": "mail.example.com",
                    "port": 993,
                    "security": "tls",
                    "username": "owner@example.com",
                    "password": "new-password",
                },
            },
        )
    )

    assert response["ok"] is True
    assert response["data"]["baseline_initialized"] is False
    assert probes == 1
    assert "new-password" in destination.read_text(encoding="utf-8")
    assert runtime.store.state(provider=account.provider, account_id=account.account_id)[0] == (
        IMAP_CURSOR
    )


def test_mail_account_reconnect_resets_cursor_when_server_mailbox_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    account = runtime.store.register_mail_account(
        "imap",
        f"imap-{'d' * 32}",
        display_name="Other mail server",
        address="owner@example.com",
        active=True,
    )
    runtime.store.set_state(
        IMAP_CURSOR,
        provider=account.provider,
        account_id=account.account_id,
    )
    destination = mail_account_token_file(runtime.config, account)
    destination.parent.mkdir(parents=True)
    write_credentials(destination, imap_credentials(host="old.example.com"))

    class ReplacementImap:
        def initial_cursor(self) -> str:
            return REPLACEMENT_IMAP_CURSOR

    monkeypatch.setattr(
        engine_api.ImapGateway,
        "from_credentials_file",
        lambda _path: ReplacementImap(),
    )

    response = engine_api._response(
        request(
            config_path,
            "mail.accounts.reconnect",
            {
                "provider": "imap",
                "account_id": account.account_id,
                "connection": {
                    "email_address": "owner@example.com",
                    "host": "replacement.example.com",
                    "port": 993,
                    "security": "tls",
                    "username": "owner@example.com",
                    "password": "new-password",
                },
            },
        )
    )

    assert response["ok"] is True
    assert response["data"]["baseline_initialized"] is True
    assert runtime.store.state(provider=account.provider, account_id=account.account_id)[0] == (
        REPLACEMENT_IMAP_CURSOR
    )
    assert "replacement.example.com" in destination.read_text(encoding="utf-8")


def test_mail_account_reconnect_uses_retained_cursor_binding_after_disconnect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    account = runtime.store.register_mail_account(
        "imap",
        f"imap-{'e' * 32}",
        display_name="Other mail server",
        address="owner@example.com",
        active=True,
    )
    retained_cursor = f"eom-imap-v2:{imap_mailbox_identity(imap_credentials())}:44:7"
    runtime.store.set_state(
        retained_cursor,
        provider=account.provider,
        account_id=account.account_id,
    )

    class ReconnectedImap:
        def initial_cursor(self) -> str:
            return REPLACEMENT_IMAP_CURSOR

    monkeypatch.setattr(
        engine_api.ImapGateway,
        "from_credentials_file",
        lambda _path: ReconnectedImap(),
    )

    response = engine_api._response(
        request(
            config_path,
            "mail.accounts.reconnect",
            {
                "provider": "imap",
                "account_id": account.account_id,
                "connection": {
                    "email_address": "owner@example.com",
                    "host": "mail.example.com",
                    "port": 993,
                    "security": "tls",
                    "username": "owner@example.com",
                    "password": "new-password",
                },
            },
        )
    )

    assert response["ok"] is True
    assert response["data"]["baseline_initialized"] is False
    assert runtime.store.state(provider=account.provider, account_id=account.account_id)[0] == (
        retained_cursor
    )


def test_mail_account_reconnect_preserves_imap_credentials_when_probe_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    account = runtime.store.register_mail_account(
        "imap",
        f"imap-{'c' * 32}",
        display_name="Other mail server",
        address="owner@example.com",
        active=True,
    )
    runtime.store.set_state(
        IMAP_CURSOR,
        provider=account.provider,
        account_id=account.account_id,
    )
    destination = mail_account_token_file(runtime.config, account)
    destination.parent.mkdir(parents=True)
    write_credentials(destination, imap_credentials(password="preserved-password"))
    preserved = destination.read_text(encoding="utf-8")

    class RejectedImap:
        def initial_cursor(self) -> str:
            raise ImapError("imap_authentication_failed", "Mail server rejected the credentials")

    monkeypatch.setattr(
        engine_api.ImapGateway,
        "from_credentials_file",
        lambda _path: RejectedImap(),
    )

    response = engine_api._response(
        request(
            config_path,
            "mail.accounts.reconnect",
            {
                "provider": "imap",
                "account_id": account.account_id,
                "connection": {
                    "email_address": "owner@example.com",
                    "host": "mail.example.com",
                    "port": 993,
                    "security": "tls",
                    "username": "owner@example.com",
                    "password": "wrong-password",
                },
            },
        )
    )

    assert response["error"]["code"] == "imap_authentication_failed"
    assert destination.read_text(encoding="utf-8") == preserved
    assert runtime.store.state(provider=account.provider, account_id=account.account_id)[0] == (
        IMAP_CURSOR
    )


def test_mail_account_reconnect_rejects_different_microsoft_identity_before_cache_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    account = runtime.store.register_mail_account(
        "microsoft365",
        f"microsoft365-{'b' * 32}",
        display_name="Microsoft 365",
        address="owner@example.com",
        active=True,
    )
    token_file = mail_account_token_file(runtime.config, account)
    token_file.parent.mkdir(parents=True)
    token_file.write_text("preserved cache", encoding="utf-8")
    runtime.store.set_state(
        "preserved cursor",
        provider=account.provider,
        account_id=account.account_id,
    )

    class WrongMicrosoft:
        def profile(self) -> Microsoft365Profile:
            return Microsoft365Profile("other@example.com")

        def initial_cursor(self) -> str:
            pytest.fail("Identity mismatch must fail before baseline access")

    def authorize_with_status(
        credentials_file: Path,
        staged_token: Path,
        *,
        force_reauthorize: bool,
    ) -> tuple[WrongMicrosoft, bool]:
        staged_token.write_text("wrong cache", encoding="utf-8")
        return WrongMicrosoft(), force_reauthorize

    monkeypatch.setattr(
        engine_api.Microsoft365Gateway,
        "authorize_with_status",
        authorize_with_status,
    )

    response = engine_api._response(
        request(
            config_path,
            "mail.accounts.reconnect",
            {"provider": account.provider, "account_id": account.account_id},
        )
    )

    assert response["error"]["code"] == "account_identity_mismatch"
    assert token_file.read_text(encoding="utf-8") == "preserved cache"
    assert runtime.store.state(provider=account.provider, account_id=account.account_id)[0] == (
        "preserved cursor"
    )
    assert list(tmp_path.glob(".microsoft365-authorization-*")) == []


def test_mail_account_connect_reuses_and_activates_known_disconnected_microsoft_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    existing = runtime.store.register_mail_account(
        "microsoft365",
        f"microsoft365-{'c' * 32}",
        display_name="Microsoft 365",
        address="owner@example.com",
        active=False,
    )
    runtime.store.set_state(
        "preserved cursor",
        provider=existing.provider,
        account_id=existing.account_id,
    )

    class AuthorizedMicrosoft:
        def profile(self) -> Microsoft365Profile:
            return Microsoft365Profile("owner@example.com")

        def initial_cursor(self) -> str:
            pytest.fail("A reused account must preserve its existing baseline")

    def authorize_with_status(
        credentials_file: Path,
        token_file: Path,
        *,
        force_reauthorize: bool,
    ) -> tuple[AuthorizedMicrosoft, bool]:
        token_file.write_text("replacement cache", encoding="utf-8")
        return AuthorizedMicrosoft(), force_reauthorize

    monkeypatch.setattr(
        engine_api.Microsoft365Gateway,
        "authorize_with_status",
        authorize_with_status,
    )

    response = engine_api._response(
        request(config_path, "mail.accounts.connect", {"provider": "microsoft365"})
    )

    assert response["ok"] is True
    assert response["data"]["account"]["account_id"] == existing.account_id
    assert response["data"]["account"]["active"] is True
    assert response["data"]["baseline_initialized"] is False
    assert len(runtime.store.mail_accounts()) == 2
    assert runtime.store.state(provider=existing.provider, account_id=existing.account_id)[0] == (
        "preserved cursor"
    )


def test_microsoft_provider_error_uses_generic_secret_free_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)

    def reject(*args, **kwargs):
        raise Microsoft365Error("provider detail with private-token-value")

    monkeypatch.setattr(engine_api.Microsoft365Gateway, "authorize_with_status", reject)

    response = engine_api._response(
        request(config_path, "mail.accounts.connect", {"provider": "microsoft365"})
    )

    assert response["error"] == {
        "code": "mailbox_error",
        "message": "Email provider operation failed; see stderr for details",
    }
    assert "private-token-value" not in json.dumps(response)


def test_private_token_install_only_syncs_the_new_writable_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "staged-token.json"
    destination = tmp_path / "installed" / "readonly-token.json"
    source.write_bytes(b'{"token": "private"}')
    original_fsync = engine_api.os.fsync
    fsync_calls = 0

    def tracking_fsync(descriptor: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        original_fsync(descriptor)

    monkeypatch.setattr(engine_api.os, "fsync", tracking_fsync)

    engine_api._install_private_token(source, destination)

    assert fsync_calls == 1
    assert destination.read_bytes() == source.read_bytes()


def test_mail_account_reconnect_rejects_different_identity_before_replacing_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.store.update_mail_account_identity(
        "gmail",
        "gmail-default",
        display_name="Gmail",
        address="owner@example.com",
    )
    runtime.config.gmail_token_file.write_text("preserved token", encoding="utf-8")

    class ExistingGmail:
        def profile(self) -> GmailProfile:
            return GmailProfile("owner@example.com", "current-cursor")

    class WrongGmail:
        def profile(self) -> GmailProfile:
            return GmailProfile("other@example.com", "other-cursor")

    def authorize_with_status(
        credentials_file: Path,
        token_file: Path,
        *,
        force_reauthorize: bool,
    ) -> tuple[WrongGmail, bool]:
        token_file.write_text("wrong account token", encoding="utf-8")
        return WrongGmail(), force_reauthorize

    monkeypatch.setattr(
        engine_api.GmailGateway,
        "from_token",
        lambda credentials_file, token_file: ExistingGmail(),
    )
    monkeypatch.setattr(engine_api.GmailGateway, "authorize_with_status", authorize_with_status)

    response = engine_api._response(
        request(
            config_path,
            "mail.accounts.reconnect",
            {"provider": "gmail", "account_id": "gmail-default"},
        )
    )

    assert response["error"]["code"] == "account_identity_mismatch"
    assert runtime.config.gmail_token_file.read_text(encoding="utf-8") == "preserved token"
    assert runtime.store.active_mail_account().address == "owner@example.com"
    assert list(tmp_path.glob(".gmail-authorization-*")) == []


def test_mail_account_disconnect_preserves_history_and_send_authorization(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.config.gmail_token_file.write_text("readonly token", encoding="utf-8")
    runtime.config.gmail_send_token_file.write_text("send token", encoding="utf-8")
    runtime.store.set_state("preserved-cursor", datetime(2026, 9, 1, tzinfo=UTC))
    runtime.store.add_message(
        message_id="retained",
        thread_id=None,
        sender="owner@example.com",
        sender_name=None,
        subject="Retained",
        received_at="2026-09-01T12:00:00+00:00",
    )

    response = engine_api._response(
        request(
            config_path,
            "mail.accounts.disconnect",
            {"provider": "gmail", "account_id": "gmail-default"},
        )
    )

    assert response["data"]["account"]["connected"] is False
    assert not runtime.config.gmail_token_file.exists()
    assert runtime.config.gmail_send_token_file.read_text(encoding="utf-8") == "send token"
    assert runtime.store.state()[0] == "preserved-cursor"
    assert runtime.store.has_message("retained")
    checked = engine_api._response(request(config_path, "watcher.check"))
    retained = engine_api._response(request(config_path, "inbox.query", {"limit": 25}))
    assert checked["error"]["code"] == "account_unavailable"
    assert [item["message_id"] for item in retained["data"]["items"]] == ["retained"]


def test_mail_account_activate_rejects_disconnected_account(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)

    response = engine_api._response(
        request(
            config_path,
            "mail.accounts.activate",
            {"provider": "gmail", "account_id": "gmail-default"},
        )
    )

    assert response["error"]["code"] == "account_unavailable"


def test_mail_account_activate_switches_one_connected_account_and_preserves_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.config.gmail_token_file.write_text("first token", encoding="utf-8")
    runtime.store.set_state("first-cursor")
    second = runtime.store.register_mail_account(
        "gmail",
        f"gmail-{'a' * 32}",
        display_name="Gmail",
        address="second@example.com",
    )
    second_token = mail_account_token_file(runtime.config, second)
    second_token.parent.mkdir(parents=True)
    second_token.write_text("second token", encoding="utf-8")
    runtime.store.set_state(
        "second-cursor",
        provider=second.provider,
        account_id=second.account_id,
    )

    response = engine_api._response(
        request(
            config_path,
            "mail.accounts.activate",
            {"provider": second.provider, "account_id": second.account_id},
        )
    )
    monkeypatch.setattr("eom_email_watcher.model.LocalModel.health", lambda self: (True, "ready"))
    health = engine_api._response(request(config_path, "health.get"))

    assert response["data"]["account"]["active"] is True
    assert runtime.store.active_mail_account().account_id == second.account_id
    assert [account.active for account in runtime.store.mail_accounts()] == [True, False]
    assert runtime.store.state()[0] == "first-cursor"
    assert runtime.store.state(provider=second.provider, account_id=second.account_id)[0] == (
        "second-cursor"
    )
    assert health["data"]["gmail"]["connected"] is True
    assert [
        account["account_id"] for account in health["data"]["mail"]["accounts"] if account["active"]
    ] == [second.account_id]


@pytest.mark.parametrize(
    ("operation", "payload", "code"),
    [
        ("mail.accounts.list", {"unexpected": True}, "invalid_request"),
        ("mail.accounts.connect", {"provider": False}, "invalid_request"),
        ("mail.accounts.connect", {"provider": "pop3"}, "unsupported_provider"),
        ("mail.accounts.connect", {"provider": "imap"}, "imap_configuration_error"),
        (
            "mail.accounts.connect",
            {"provider": "gmail", "connection": {}},
            "invalid_request",
        ),
        (
            "mail.accounts.connect",
            {"provider": "gmail", "connection": None},
            "invalid_request",
        ),
        (
            "mail.accounts.reconnect",
            {"provider": "gmail", "account_id": ""},
            "invalid_request",
        ),
        (
            "mail.accounts.disconnect",
            {"provider": "gmail", "account_id": "x" * 129},
            "invalid_request",
        ),
        (
            "mail.accounts.activate",
            {"provider": "gmail", "account_id": "missing"},
            "not_found",
        ),
    ],
)
def test_mail_account_operations_reject_invalid_boundaries(
    tmp_path: Path,
    operation: str,
    payload: dict[str, object],
    code: str,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)

    response = engine_api._response(request(config_path, operation, payload))

    assert response["error"]["code"] == code


def test_mail_account_connect_reuses_matching_account_without_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.store.update_mail_account_identity(
        "gmail",
        "gmail-default",
        display_name="Gmail",
        address="owner@example.com",
    )
    runtime.config.gmail_token_file.write_text("existing token", encoding="utf-8")

    class AuthorizedGmail:
        def profile(self) -> GmailProfile:
            return GmailProfile("owner@example.com", "new-cursor")

    def authorize_with_status(
        credentials_file: Path,
        token_file: Path,
        *,
        force_reauthorize: bool,
    ) -> tuple[AuthorizedGmail, bool]:
        token_file.write_text("replacement token", encoding="utf-8")
        return AuthorizedGmail(), force_reauthorize

    monkeypatch.setattr(engine_api.GmailGateway, "authorize_with_status", authorize_with_status)

    response = engine_api._response(
        request(config_path, "mail.accounts.connect", {"provider": "gmail"})
    )

    assert response["data"]["account"]["account_id"] == "gmail-default"
    assert runtime.config.gmail_token_file.read_text(encoding="utf-8") == "replacement token"
    assert len(runtime.store.mail_accounts()) == 1


def test_mail_account_connect_reuses_verified_legacy_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.store.set_state("legacy-cursor", datetime(2026, 8, 1, tzinfo=UTC))
    runtime.config.gmail_token_file.write_text("existing token", encoding="utf-8")

    class ExistingGmail:
        def profile(self) -> GmailProfile:
            return GmailProfile("owner@example.com", "current-cursor")

    class AuthorizedGmail:
        def profile(self) -> GmailProfile:
            return GmailProfile("owner@example.com", "authorized-cursor")

    def authorize_with_status(
        credentials_file: Path,
        token_file: Path,
        *,
        force_reauthorize: bool,
    ) -> tuple[AuthorizedGmail, bool]:
        assert force_reauthorize is True
        token_file.write_text("replacement token", encoding="utf-8")
        return AuthorizedGmail(), True

    monkeypatch.setattr(
        engine_api.GmailGateway,
        "from_token",
        lambda credentials_file, token_file: ExistingGmail(),
    )
    monkeypatch.setattr(
        engine_api.GmailGateway,
        "authorize_with_status",
        authorize_with_status,
    )

    response = engine_api._response(
        request(config_path, "mail.accounts.connect", {"provider": "gmail"})
    )

    assert response["ok"] is True
    assert response["data"]["baseline_initialized"] is False
    assert response["data"]["account"]["account_id"] == "gmail-default"
    assert runtime.store.active_mail_account().address == "owner@example.com"
    assert runtime.store.state()[0] == "legacy-cursor"
    assert runtime.config.gmail_token_file.read_text(encoding="utf-8") == "replacement token"
    assert len(runtime.store.mail_accounts()) == 1


def test_mail_account_connect_keeps_unidentified_legacy_history_separate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.store.set_state("legacy-cursor", datetime(2026, 8, 1, tzinfo=UTC))
    runtime.store.add_message(
        message_id="legacy-message",
        thread_id=None,
        sender="legacy@example.com",
        sender_name=None,
        subject="Legacy history",
        received_at="2026-08-01T12:00:00+00:00",
    )

    class AuthorizedGmail:
        def profile(self) -> GmailProfile:
            return GmailProfile("new-owner@example.com", "new-cursor")

    def authorize_with_status(
        credentials_file: Path,
        token_file: Path,
        *,
        force_reauthorize: bool,
    ) -> tuple[AuthorizedGmail, bool]:
        token_file.write_text("new account token", encoding="utf-8")
        return AuthorizedGmail(), force_reauthorize

    monkeypatch.setattr(engine_api.GmailGateway, "authorize_with_status", authorize_with_status)

    response = engine_api._response(
        request(config_path, "mail.accounts.connect", {"provider": "gmail"})
    )

    assert response["ok"] is True
    new_account = runtime.store.active_mail_account()
    assert new_account is not None
    assert new_account.account_id.startswith("gmail-")
    assert new_account.address == "new-owner@example.com"
    assert (
        mail_account_token_file(runtime.config, new_account).read_text(encoding="utf-8")
        == "new account token"
    )
    legacy = runtime.store.mail_account("gmail", "gmail-default")
    assert legacy is not None
    assert legacy.active is False
    assert legacy.address is None
    assert runtime.store.state(provider="gmail", account_id="gmail-default")[0] == "legacy-cursor"
    assert (
        runtime.store.state(provider="gmail", account_id=new_account.account_id)[0] == "new-cursor"
    )
    assert runtime.store.has_message("legacy-message")


def test_mail_account_connect_activates_replacement_for_rejected_legacy_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.store.set_state("legacy-cursor", datetime(2026, 8, 1, tzinfo=UTC))
    runtime.config.gmail_token_file.write_text("rejected legacy token", encoding="utf-8")

    class RejectedGmail:
        def profile(self) -> GmailProfile:
            raise GmailAuthorizationRejected("rejected")

    class AuthorizedGmail:
        def profile(self) -> GmailProfile:
            return GmailProfile("new-owner@example.com", "new-cursor")

    def authorize_with_status(
        credentials_file: Path,
        token_file: Path,
        *,
        force_reauthorize: bool,
    ) -> tuple[AuthorizedGmail, bool]:
        token_file.write_text("new account token", encoding="utf-8")
        return AuthorizedGmail(), force_reauthorize

    monkeypatch.setattr(
        engine_api.GmailGateway,
        "from_token",
        lambda credentials_file, token_file: RejectedGmail(),
    )
    monkeypatch.setattr(engine_api.GmailGateway, "authorize_with_status", authorize_with_status)

    response = engine_api._response(
        request(config_path, "mail.accounts.connect", {"provider": "gmail"})
    )

    assert response["ok"] is True
    active = runtime.store.active_mail_account()
    assert active is not None
    assert active.account_id.startswith("gmail-")
    assert active.address == "new-owner@example.com"
    assert response["data"]["account"]["active"] is True
    legacy = runtime.store.mail_account("gmail", "gmail-default")
    assert legacy is not None
    assert legacy.active is False
    assert runtime.config.gmail_token_file.read_text(encoding="utf-8") == ("rejected legacy token")


def test_mail_account_reconnect_refuses_unverifiable_legacy_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.store.set_state("legacy-cursor")
    monkeypatch.setattr(
        engine_api.GmailGateway,
        "authorize_with_status",
        lambda *args, **kwargs: pytest.fail("OAuth must not rebind unidentified history"),
    )

    response = engine_api._response(
        request(
            config_path,
            "mail.accounts.reconnect",
            {"provider": "gmail", "account_id": "gmail-default"},
        )
    )

    assert response["error"]["code"] == "account_identity_unverified"


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
    stale_runtime.config.gmail_token_file.write_text("connected token", encoding="utf-8")
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


def test_attachment_export_uses_inactive_source_account_and_safe_private_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.config.gmail_token_file.write_text("source token", encoding="utf-8")
    active_account = runtime.store.register_mail_account(
        "gmail",
        f"gmail-{'a' * 32}",
        display_name="Gmail",
        address="active@example.com",
        active=True,
    )
    active_token = mail_account_token_file(runtime.config, active_account)
    active_token.parent.mkdir(parents=True)
    active_token.write_text("active token", encoding="utf-8")
    local_message_id = scoped_message_id("gmail", "gmail-default", "provider-message")
    runtime.store.add_message(
        message_id=local_message_id,
        provider="gmail",
        account_id="gmail-default",
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

    opened_tokens: list[Path] = []

    monkeypatch.setattr(
        engine_api.GmailGateway,
        "from_token",
        lambda _credentials, token: opened_tokens.append(token) or FakeAttachmentGmail(),
    )
    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)

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
    assert runtime.store.active_mail_account().account_id == active_account.account_id
    assert opened_tokens == [runtime.config.gmail_token_file]
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
        "load_mailbox_account",
        lambda *_args: pytest.fail("An unavailable account must not be opened"),
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

    monkeypatch.setattr(engine_api, "load_mailbox_account", reject_provider_interaction)
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


def test_attachment_export_reports_provider_neutral_byte_count_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.config.gmail_token_file.write_text("connected token", encoding="utf-8")
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

    assert response["error"] == {
        "code": "mailbox_error",
        "message": "Email provider operation failed; see stderr for details",
    }
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
    loaded.config.gmail_token_file.write_text("connected token", encoding="utf-8")
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
    runtime.config.gmail_token_file.write_text("connected token", encoding="utf-8")
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
    loaded.config.gmail_token_file.write_text("connected token", encoding="utf-8")
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
    loaded.config.gmail_token_file.write_text("connected token", encoding="utf-8")
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
