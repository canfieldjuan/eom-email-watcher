import io
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from eom_email_watcher import engine_api
from eom_email_watcher.gmail import GmailError, MessageMetadata
from eom_email_watcher.model import Analysis
from eom_email_watcher.runtime import Runtime, load_runtime


def write_config(
    path: Path,
    *,
    ntfy_topic: str | None = None,
    notifications_enabled: bool = True,
    extra_settings: str = "",
    timezone: str = "America/Chicago",
) -> None:
    ntfy_setting = f'ntfy_topic = "{ntfy_topic}"\n' if ntfy_topic else ""
    notifications_setting = str(notifications_enabled).lower()
    path.write_text(
        f'''timezone = "{timezone}"
gmail_credentials_file = "{path.parent / "credentials.json"}"
gmail_token_file = "{path.parent / "token.json"}"
gmail_send_token_file = "{path.parent / "send-token.json"}"
database_file = "{path.parent / "watcher.sqlite3"}"
model_base_url = "http://127.0.0.1:1234/v1"
model_name = "local-model"
model_require_auth = false
notifications_enabled = {notifications_setting}
{ntfy_setting}
{extra_settings}

[[senders]]
email = "z@example.com"
name = "Zed"

[[senders]]
email = "A@Example.com"
name = "Trusted A"
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


class FakeGmail:
    def history_message_ids(self, cursor: str):
        return ["m1"], "200"

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
    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.GmailGateway,
        "from_token",
        lambda *args: (_ for _ in ()).throw(AssertionError("Gmail must not be called")),
    )

    health = engine_api._response(request(config_path, "health.get"))
    assert health["data"]["notifications"] == {
        "delivery": "host",
        "enabled": True,
        "host_delivery_ready": False,
        "ntfy_configured": True,
    }

    checked = engine_api._response(request(config_path, "watcher.check"))

    assert checked["ok"] is False
    assert checked["error"]["code"] == "unsupported_configuration"
    assert loaded.store.state()[0] == "100"
    assert loaded.store.recent(1) == []


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
    monkeypatch.setattr(engine_api, "operation_lock_supported", lambda: False)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", gmail_from_token)

    health = engine_api._response(request(config_path, "health.get"))
    checked = engine_api._response(request(config_path, "watcher.check"))

    assert health["data"]["production_check_supported"] is False
    assert health["data"]["notifications"]["host_delivery_ready"] is False
    assert checked["error"]["code"] == "unsupported_platform"
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
    checked = engine_api._response(
        request(config_path, "watcher.check", {"dry_run": True})
    )

    assert pending["data"]["items"] == []
    assert checked["data"]["pending_notifications"] == 0
    assert {row["status"] for row in runtime.store.recent(10)} == {"pending", "analyzed"}


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

    checked = engine_api._response(
        request(config_path, "watcher.check", {"dry_run": True})
    )

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
