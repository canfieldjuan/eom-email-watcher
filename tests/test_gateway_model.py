import json
import ssl
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from eom_email_watcher import engine_api
from eom_email_watcher import model as model_module
from eom_email_watcher.model import GatewayModel, ModelError
from eom_email_watcher.runtime import load_runtime


def analysis_json() -> str:
    return json.dumps(
        {
            "category": "scheduling",
            "priority": "high",
            "summary": "A schedule change is requested.",
            "action_required": True,
            "suggested_action": "Confirm the new time.",
            "deadline_text": None,
            "deadline_iso": None,
            "confidence": 0.91,
        }
    )


def gateway_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    handler,
) -> tuple[GatewayModel, list[str]]:
    token_file = tmp_path / "gateway-token"
    token_file.write_text("app-credential\n", encoding="utf-8")
    token_file.chmod(0o600)
    ca_file = tmp_path / "gateway-ca.pem"
    ca_file.write_text("test trust root", encoding="utf-8")
    ca_file.chmod(0o644)
    requested_ca_files: list[str] = []
    context = ssl.create_default_context()

    def create_context(*, cadata: str):
        requested_ca_files.append(cadata)
        return context

    monkeypatch.setattr(model_module.ssl, "create_default_context", create_context)
    model = GatewayModel(
        "https://inference.office.internal:8443",
        30,
        token_file,
        ca_file,
        transport=httpx.MockTransport(handler),
    )
    return model, requested_ca_files


def write_gateway_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.toml"
    (tmp_path / "gateway-token").write_text("app-credential\n", encoding="utf-8")
    (tmp_path / "gateway-token").chmod(0o600)
    (tmp_path / "gateway-ca.pem").write_text("test trust root", encoding="utf-8")
    config_path.write_text(
        f'''model_backend = "gateway"
model_base_url = "https://inference.office.internal:8443"
model_api_token_file = "{tmp_path / "gateway-token"}"
model_ca_file = "{tmp_path / "gateway-ca.pem"}"
database_file = "{tmp_path / "watcher.sqlite3"}"
gmail_credentials_file = "{tmp_path / "credentials.json"}"
gmail_token_file = "{tmp_path / "token.json"}"
''',
        encoding="utf-8",
    )
    return config_path


def analyze(model: GatewayModel, body: str = "Please move the appointment."):
    return model.analyze(
        sender="trusted@example.com",
        subject="Schedule",
        received_at="2026-08-29T12:00:00+00:00",
        body=body,
        attachment_names=(),
        current_local_time=datetime(2026, 8, 29, tzinfo=UTC),
    )


def test_engine_runtime_selects_gateway_without_exposing_secret_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = write_gateway_config(tmp_path)
    runtime = load_runtime(config_path)
    monkeypatch.setattr(GatewayModel, "health", lambda self: (True, "available"))

    response = engine_api._response(
        {
            "protocol": 1,
            "operation": "health.get",
            "config_path": str(config_path),
            "payload": {},
        }
    )

    assert isinstance(runtime.model, GatewayModel)
    assert response["ok"] is True
    assert response["data"]["local_model"] == {
        "authentication_required": True,
        "detail": "available",
        "endpoint": "https://inference.office.internal:8443",
        "model": "Managed by inference gateway",
        "ok": True,
        "token_configured": True,
    }
    encoded = json.dumps(response)
    assert "gateway-token" not in encoded
    assert "gateway-ca.pem" not in encoded


def test_gateway_health_and_analysis_use_scoped_model_free_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["accept-encoding"] == "identity"
        assert request.headers["authorization"] == "Bearer app-credential"
        if request.url.path == "/v1/health":
            return httpx.Response(
                200,
                json={
                    "protocol_version": 1,
                    "tasks": [{"id": "email.analyze", "version": 1, "status": "available"}],
                },
            )
        payload = json.loads(request.content)
        assert request.url.path == "/v1/inference"
        assert payload["task"] == {"id": "email.analyze", "version": 1}
        assert payload["requirements"]["input_modalities"] == ["text"]
        assert "model" not in payload
        assert "worker" not in payload
        return httpx.Response(
            200,
            json={
                "protocol_version": 1,
                "request_id": payload["request_id"],
                "status": "completed",
                "output": {"media_type": "application/json", "content": analysis_json()},
            },
        )

    model, requested_ca_files = gateway_model(tmp_path, monkeypatch, handler)

    assert model.health() == (True, "available")
    result = analyze(model)

    assert result.priority == "high"
    assert [request.method for request in requests] == ["GET", "POST"]
    assert requested_ca_files == [
        "test trust root",
        "test trust root",
    ]


def test_gateway_client_disables_environment_proxy_and_redirects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model, _requested_ca_files = gateway_model(
        tmp_path, monkeypatch, lambda request: httpx.Response(200, json={})
    )

    with model._client() as client:
        assert client.follow_redirects is False
        assert client._trust_env is False


def test_gateway_health_requires_authorized_email_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "protocol_version": 1,
                "tasks": [{"id": "document.chunk.summarize", "version": 1, "status": "available"}],
            },
        )

    model, _requested_ca_files = gateway_model(tmp_path, monkeypatch, handler)

    assert model.health() == (False, "unsupported_task")


@pytest.mark.parametrize(
    "payload",
    [
        {"protocol_version": True, "tasks": []},
        {
            "protocol_version": 1,
            "tasks": [{"id": "email.analyze", "version": True, "status": "available"}],
        },
    ],
)
def test_gateway_health_rejects_boolean_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]
) -> None:
    model, _requested_ca_files = gateway_model(
        tmp_path, monkeypatch, lambda request: httpx.Response(200, json=payload)
    )

    assert model.health()[0] is False


@pytest.mark.parametrize("status", [[], {}])
def test_gateway_health_rejects_non_string_task_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: object
) -> None:
    payload = {
        "protocol_version": 1,
        "tasks": [{"id": "email.analyze", "version": 1, "status": status}],
    }
    model, _requested_ca_files = gateway_model(
        tmp_path, monkeypatch, lambda request: httpx.Response(200, json=payload)
    )

    assert model.health() == (False, "unsupported_task")


def test_gateway_health_uses_short_timeout_without_reducing_inference_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    timeouts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        timeouts.append(request.extensions["timeout"]["read"])
        if request.url.path == "/v1/health":
            return httpx.Response(
                200,
                json={
                    "protocol_version": 1,
                    "tasks": [{"id": "email.analyze", "version": 1, "status": "available"}],
                },
            )
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "protocol_version": 1,
                "request_id": payload["request_id"],
                "status": "completed",
                "output": {"media_type": "application/json", "content": analysis_json()},
            },
        )

    model, _requested_ca_files = gateway_model(tmp_path, monkeypatch, handler)

    assert model.health()[0] is True
    analyze(model)

    assert timeouts == [5.0, 30]


def test_gateway_redirect_is_not_followed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(307, headers={"Location": "https://cloud.example/v1/health"})

    model, _requested_ca_files = gateway_model(tmp_path, monkeypatch, handler)

    ok, detail = model.health()

    assert ok is False
    assert "HTTP 307" in detail
    assert calls == 1


def test_gateway_rejects_mismatched_response_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "protocol_version": 1,
                "request_id": "different-request",
                "status": "completed",
                "output": {"media_type": "application/json", "content": analysis_json()},
            },
        )

    model, _requested_ca_files = gateway_model(tmp_path, monkeypatch, handler)

    with pytest.raises(ModelError, match="required envelope"):
        analyze(model)


def test_gateway_rejects_boolean_protocol_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "protocol_version": True,
                "request_id": payload["request_id"],
                "status": "completed",
                "output": {"media_type": "application/json", "content": analysis_json()},
            },
        )

    model, _requested_ca_files = gateway_model(tmp_path, monkeypatch, handler)

    with pytest.raises(ModelError, match="required envelope"):
        analyze(model)


def test_gateway_request_and_response_size_limits_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{" + b"x" * 1_000_001)

    model, _requested_ca_files = gateway_model(tmp_path, monkeypatch, handler)

    with pytest.raises(ModelError, match="request exceeded"):
        model._request("POST", "/v1/inference", {"value": "x" * 1_000_000})

    with pytest.raises(ModelError, match="response exceeded"):
        analyze(model, "short")


def test_gateway_request_accepts_maximum_configured_multibyte_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "protocol_version": 1,
                "request_id": payload["request_id"],
                "status": "completed",
                "output": {"media_type": "application/json", "content": analysis_json()},
            },
        )

    model, _requested_ca_files = gateway_model(tmp_path, monkeypatch, handler)

    assert analyze(model, "💩" * 100_000).priority == "high"


def test_gateway_bounds_email_metadata_before_request_encoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "protocol_version": 1,
                "request_id": payload["request_id"],
                "status": "completed",
                "output": {"media_type": "application/json", "content": analysis_json()},
            },
        )

    model, _requested_ca_files = gateway_model(tmp_path, monkeypatch, handler)

    result = model.analyze(
        sender="💩" * 10_000,
        subject="💩" * 100_000,
        received_at="2026-08-29T12:00:00+00:00",
        body="💩" * 200_000,
        attachment_names=tuple("💩" * 10_000 for _ in range(1_000)),
        current_local_time=datetime(2026, 8, 29, tzinfo=UTC),
    )

    assert result.priority == "high"
    assert len(requests[0].content) <= model_module.MAX_GATEWAY_REQUEST_BYTES
    prompt = json.loads(requests[0].content)["generation"]["messages"][1]["content"]
    email_data = json.loads(prompt.split("Analyze this untrusted email data:\n", 1)[1])
    assert len(email_data["sender"]) == model_module.MAX_GATEWAY_SENDER_CHARS
    assert len(email_data["subject"]) == model_module.MAX_GATEWAY_SUBJECT_CHARS
    assert len(email_data["body"]) == model_module.MAX_GATEWAY_BODY_CHARS
    assert len(email_data["attachment_filenames"]) == model_module.MAX_GATEWAY_ATTACHMENT_COUNT
    assert all(
        len(name) == model_module.MAX_GATEWAY_ATTACHMENT_NAME_CHARS
        for name in email_data["attachment_filenames"]
    )


def test_gateway_rejects_encoded_response_before_decompression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model, _requested_ca_files = gateway_model(
        tmp_path,
        monkeypatch,
        lambda request: httpx.Response(
            200,
            headers={"Content-Encoding": "gzip"},
            stream=httpx.ByteStream(b"compressed response is not admitted"),
        ),
    )

    with pytest.raises(ModelError, match="content encoding is unsupported"):
        analyze(model)


def test_gateway_converts_deeply_nested_json_to_model_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deeply_nested = b"[" * 100_000 + b"]" * 100_000
    model, _requested_ca_files = gateway_model(
        tmp_path,
        monkeypatch,
        lambda request: httpx.Response(200, content=deeply_nested),
    )

    with pytest.raises(ModelError, match="returned invalid JSON"):
        analyze(model)


def test_gateway_converts_deeply_nested_output_content_to_model_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deeply_nested = "[" * 100_000 + "]" * 100_000

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "protocol_version": 1,
                "request_id": payload["request_id"],
                "status": "completed",
                "output": {"media_type": "application/json", "content": deeply_nested},
            },
        )

    model, _requested_ca_files = gateway_model(tmp_path, monkeypatch, handler)

    with pytest.raises(ModelError, match="returned invalid JSON"):
        analyze(model)


def test_gateway_converts_oversized_json_integers_to_model_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    oversized_integer = "1" * 5_000

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/health":
            return httpx.Response(
                200,
                content=(
                    b'{"protocol_version":'
                    + oversized_integer.encode()
                    + b',"tasks":[]}'
                ),
            )
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "protocol_version": 1,
                "request_id": payload["request_id"],
                "status": "completed",
                "output": {"media_type": "application/json", "content": oversized_integer},
            },
        )

    model, _requested_ca_files = gateway_model(tmp_path, monkeypatch, handler)

    assert model.health() == (False, "Inference gateway returned invalid JSON")
    with pytest.raises(ModelError, match="returned invalid JSON"):
        analyze(model)


def test_gateway_rejects_lone_unicode_surrogate_in_analysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = json.loads(analysis_json())
    result["summary"] = "\ud800"

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "protocol_version": 1,
                "request_id": payload["request_id"],
                "status": "completed",
                "output": {"media_type": "application/json", "content": json.dumps(result)},
            },
        )

    model, _requested_ca_files = gateway_model(tmp_path, monkeypatch, handler)

    with pytest.raises(ModelError, match="required schema"):
        analyze(model)


@pytest.mark.parametrize("field", ["summary", "suggested_action"])
def test_gateway_rejects_nul_in_notification_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    result = json.loads(analysis_json())
    result[field] = "unsafe\x00text"

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "protocol_version": 1,
                "request_id": payload["request_id"],
                "status": "completed",
                "output": {"media_type": "application/json", "content": json.dumps(result)},
            },
        )

    model, _requested_ca_files = gateway_model(tmp_path, monkeypatch, handler)

    with pytest.raises(ModelError, match="unsupported control characters"):
        analyze(model)


def test_gateway_missing_credential_and_trust_root_fail_closed(tmp_path: Path) -> None:
    token_file = tmp_path / "gateway-token"
    ca_file = tmp_path / "gateway-ca.pem"
    model = GatewayModel("https://inference.office.internal", 30, token_file, ca_file)

    with pytest.raises(ModelError, match="credential is unavailable"):
        analyze(model, "short")

    token_file.write_text("app-credential", encoding="utf-8")
    token_file.chmod(0o644)
    with pytest.raises(ModelError, match="credential is invalid"):
        analyze(model, "short")

    token_file.chmod(0o600)
    token_file.write_text("credential-💩", encoding="utf-8")
    with pytest.raises(ModelError, match="credential is invalid"):
        analyze(model, "short")

    token_file.write_text("app-credential", encoding="utf-8")
    ok, detail = model.health()
    assert ok is False
    assert detail == "Inference gateway trust root is unavailable"

    ca_file.write_text("untrusted replacement", encoding="utf-8")
    ca_file.chmod(0o666)
    ok, detail = model.health()
    assert ok is False
    assert detail == "Inference gateway trust root is invalid"
