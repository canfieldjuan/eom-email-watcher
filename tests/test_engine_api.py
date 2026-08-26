import io
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from eom_email_watcher import engine_api
from eom_email_watcher.gmail import MessageMetadata
from eom_email_watcher.model import Analysis
from eom_email_watcher.runtime import Runtime, load_runtime


def write_config(path: Path) -> None:
    path.write_text(
        f'''timezone = "America/Chicago"
gmail_credentials_file = "{path.parent / "credentials.json"}"
gmail_token_file = "{path.parent / "token.json"}"
gmail_send_token_file = "{path.parent / "send-token.json"}"
database_file = "{path.parent / "watcher.sqlite3"}"
model_base_url = "http://127.0.0.1:1234/v1"
model_name = "local-model"
model_require_auth = false
notifications_enabled = true

[[senders]]
email = "z@example.com"
name = "Zed"

[[senders]]
email = "A@Example.com"
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
                {"email": "a@example.com", "name": None},
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
    assert health["data"]["notifications"] == {"delivery": "host", "enabled": True}

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
            None,
            "Action needed",
            "2026-07-18T14:00:00+00:00",
            frozenset({"INBOX"}),
        )

    def full_payload(self, message_id: str):
        return {"mimeType": "text/plain", "body": {"data": "SGVsbG8="}}


class FakeModel:
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
