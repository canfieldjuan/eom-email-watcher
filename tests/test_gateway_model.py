import json
import os
import ssl
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from eom_email_watcher import engine_api
from eom_email_watcher import model as model_module
from eom_email_watcher.model import (
    GatewayModel,
    GatewayModelError,
    GatewayOutputRejected,
    ModelError,
)
from eom_email_watcher.runtime import load_runtime
from eom_email_watcher.scheduling import SchedulingSource, SchedulingViolation


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


def scheduling_source() -> SchedulingSource:
    return SchedulingSource(
        sender="trusted@example.com",
        subject="Meeting request",
        received_at="2026-09-07T12:00:00+00:00",
        body="Meet jane@example.com on September 8, 2026 from 10:00 to 10:30 AM.",
        attachment_names=(),
        organizer_address="owner@example.com",
        configured_timezone="America/Chicago",
        context_at=datetime(2026, 9, 7, 9, 0, tzinfo=UTC),
    )


def scheduling_json() -> str:
    return json.dumps(
        {
            "intent": "new_meeting",
            "intent_evidence": {
                "source": "body",
                "quote": "Meet jane@example.com",
            },
            "proposed_times": [
                {
                    "start": "2026-09-08T10:00:00-05:00",
                    "end": "2026-09-08T10:30:00-05:00",
                    "timezone": "America/Chicago",
                    "evidence": [
                        {
                            "source": "body",
                            "quote": "September 8, 2026 from 10:00 to 10:30 AM",
                        }
                    ],
                }
            ],
            "attendees": [
                {
                    "email": "jane@example.com",
                    "evidence": {"source": "body", "quote": "jane@example.com"},
                }
            ],
            "referenced_event": None,
            "confidence": 0.95,
            "ambiguity_reasons": [],
        }
    )


def gateway_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    handler,
    *,
    clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
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
        clock=clock,
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


def analyze(
    model: GatewayModel,
    body: str = "Please move the appointment.",
    *,
    request_id: str | None = None,
    current_local_time: datetime | None = None,
):
    return model.analyze(
        sender="trusted@example.com",
        subject="Schedule",
        received_at="2026-08-29T12:00:00+00:00",
        body=body,
        attachment_names=(),
        current_local_time=current_local_time or datetime(2026, 8, 29, tzinfo=UTC),
        request_id=request_id,
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
        assert payload["request_expires_at"] == "2026-08-29T00:10:00Z"
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


def test_gateway_retry_reuses_request_identity_and_immutable_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
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
    request_id = "11111111-1111-4111-8111-111111111111"
    reserved_at = datetime(2026, 9, 11, 10, 0, 0, 999999, tzinfo=UTC)

    analyze(model, request_id=request_id, current_local_time=reserved_at)
    analyze(model, request_id=request_id, current_local_time=reserved_at)

    assert [request["request_id"] for request in requests] == [request_id, request_id]
    assert [request["request_expires_at"] for request in requests] == [
        "2026-09-11T10:10:00Z",
        "2026-09-11T10:10:00Z",
    ]


def test_gateway_expiry_boundary_blocks_transport_and_requires_requeue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[httpx.Request] = []
    observed_at = [datetime(2026, 9, 11, 10, 9, 59, 999999, tzinfo=UTC)]

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

    model, _requested_ca_files = gateway_model(
        tmp_path,
        monkeypatch,
        handler,
        clock=lambda: observed_at[0],
    )
    reserved_at = datetime(2026, 9, 11, 10, 0, tzinfo=UTC)

    analyze(model, current_local_time=reserved_at)
    observed_at[0] = datetime(2026, 9, 11, 10, 10, tzinfo=UTC)
    with pytest.raises(GatewayModelError) as captured:
        analyze(model, current_local_time=reserved_at)

    assert captured.value.code == "request_expired"
    assert captured.value.retryable is False
    assert len(requests) == 1


def test_gateway_expired_scheduling_request_never_reaches_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model, _requested_ca_files = gateway_model(
        tmp_path,
        monkeypatch,
        lambda request: pytest.fail("expired scheduling request reached transport"),
        clock=lambda: datetime(2026, 9, 7, 10, 10, tzinfo=UTC),
    )

    with pytest.raises(GatewayModelError) as captured:
        model.extract_scheduling(
            source=scheduling_source(),
            feedback=(),
            request_id="11111111-1111-4111-8111-111111111111",
            request_started_at=datetime(2026, 9, 7, 10, 0, tzinfo=UTC),
        )

    assert captured.value.code == "request_expired"
    assert captured.value.retryable is False


def test_gateway_request_expiry_rejects_naive_reservation_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model, _requested_ca_files = gateway_model(
        tmp_path,
        monkeypatch,
        lambda request: pytest.fail("naive reservation reached transport"),
    )

    with pytest.raises(ModelError, match="must include a time zone"):
        analyze(model, current_local_time=datetime(2026, 9, 11, 10, 0))


@pytest.mark.parametrize(
    "request_id",
    [
        "../health/live",
        "33333333-3333-4333-8333-333333333333/extra",
        "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
        "11111111-1111-1111-8111-111111111111",
    ],
)
def test_gateway_rejects_noncanonical_request_identity_before_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request_id: str,
) -> None:
    model, _requested_ca_files = gateway_model(
        tmp_path,
        monkeypatch,
        lambda request: pytest.fail("invalid request identity reached transport"),
    )

    with pytest.raises(ModelError, match="request identity is invalid"):
        model.acknowledge(request_id, "persisted")


def test_gateway_acknowledges_only_the_matching_result_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append((request.url.path, payload))
        return httpx.Response(
            200,
            json={
                "protocol_version": 1,
                "request_id": payload["request_id"],
                "status": "acknowledged",
                "disposition": payload["disposition"],
            },
        )

    model, _requested_ca_files = gateway_model(tmp_path, monkeypatch, handler)
    request_id = "33333333-3333-4333-8333-333333333333"

    model.acknowledge(request_id, "persisted")

    assert requests == [
        (
            f"/v1/inference/{request_id}/ack",
            {
                "protocol_version": 1,
                "request_id": request_id,
                "disposition": "persisted",
            },
        )
    ]


def test_gateway_rejects_mismatched_acknowledgement_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_id = "44444444-4444-4444-8444-444444444444"
    model, _requested_ca_files = gateway_model(
        tmp_path,
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={
                "protocol_version": 1,
                "request_id": request_id,
                "status": "acknowledged",
                "disposition": "application_rejected",
            },
        ),
    )

    with pytest.raises(GatewayModelError) as captured:
        model.acknowledge(request_id, "persisted")

    assert captured.value.code == "invalid_acknowledgement_envelope"
    assert captured.value.retryable is False


def test_gateway_scheduling_extraction_reuses_versioned_email_task_and_request_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(
            200,
            json={
                "protocol_version": 1,
                "request_id": payload["request_id"],
                "status": "completed",
                "output": {
                    "media_type": "application/json",
                    "content": scheduling_json(),
                },
            },
        )

    model, _requested_ca_files = gateway_model(tmp_path, monkeypatch, handler)

    result = model.extract_scheduling(
        source=scheduling_source(),
        feedback=(SchedulingViolation("time_naive", "proposed_times.0"),),
        request_id="22222222-2222-4222-8222-222222222222",
        request_started_at=datetime(2026, 9, 7, 10, 0, 0, 999999, tzinfo=UTC),
    )

    assert result.accepted is True
    assert requests[0]["request_id"] == "22222222-2222-4222-8222-222222222222"
    assert requests[0]["request_expires_at"] == "2026-09-07T10:10:00Z"
    assert requests[0]["task"] == {"id": "email.analyze", "version": 1}
    assert requests[0]["requirements"]["max_output_tokens"] == 1_500
    assert "time_naive" in requests[0]["generation"]["messages"][1]["content"]
    assert "model" not in requests[0]


def test_gateway_client_disables_environment_proxy_and_redirects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model, _requested_ca_files = gateway_model(
        tmp_path, monkeypatch, lambda request: httpx.Response(200, json={})
    )

    with model._client() as client:
        assert client.follow_redirects is False
        assert client._trust_env is False


def test_gateway_client_disables_inherited_tls_key_logging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "gateway-token"
    token_file.write_text("app-credential", encoding="utf-8")
    token_file.chmod(0o600)
    ca_file = tmp_path / "gateway-ca.pem"
    ca_file.write_text("test trust root", encoding="ascii")
    ca_file.chmod(0o644)
    context = ssl.create_default_context()
    context.keylog_filename = str(tmp_path / "tls-keys.log")
    monkeypatch.setattr(
        model_module.ssl,
        "create_default_context",
        lambda *, cadata: context,
    )
    model = GatewayModel(
        "https://inference.office.internal:8443",
        30,
        token_file,
        ca_file,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
    )

    with model._client():
        pass

    assert context.keylog_filename is None


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

    with pytest.raises(GatewayModelError) as captured:
        analyze(model)

    assert captured.value.code == "invalid_success_envelope"
    assert captured.value.retryable is False


@pytest.mark.parametrize("http_status", [200, 429])
def test_gateway_exposes_validated_retry_directives_for_any_http_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, http_status: int
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(
            http_status,
            json={
                "protocol_version": 1,
                "request_id": payload["request_id"],
                "status": "failed",
                "error": {
                    "code": "capacity_limited",
                    "retryable": True,
                    "retry_after_seconds": 90,
                },
            },
        )

    model, _requested_ca_files = gateway_model(tmp_path, monkeypatch, handler)

    with pytest.raises(GatewayModelError) as captured:
        analyze(model)

    assert str(captured.value) == "Inference gateway error: capacity_limited"
    assert captured.value.code == "capacity_limited"
    assert captured.value.retryable is True
    assert captured.value.retry_after_seconds == 90


@pytest.mark.parametrize(
    "error",
    [
        {"code": "forbidden", "retryable": False, "retry_after_seconds": 30},
        {"code": "capacity_limited", "retryable": 1, "retry_after_seconds": 30},
        {"code": "capacity_limited", "retryable": True, "retry_after_seconds": 0},
    ],
)
def test_gateway_rejects_invalid_error_directives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: dict[str, object]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(
            400,
            json={
                "protocol_version": 1,
                "request_id": payload["request_id"],
                "status": "failed",
                "error": error,
            },
        )

    model, _requested_ca_files = gateway_model(tmp_path, monkeypatch, handler)

    with pytest.raises(GatewayModelError) as captured:
        analyze(model)

    assert captured.value.code == "invalid_error_envelope"
    assert captured.value.retryable is False
    assert captured.value.retry_after_seconds is None


def test_gateway_transport_failure_is_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    model, _requested_ca_files = gateway_model(tmp_path, monkeypatch, handler)

    with pytest.raises(GatewayModelError) as captured:
        analyze(model)

    assert captured.value.code == "transport_error"
    assert captured.value.retryable is True


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

    with pytest.raises(GatewayModelError) as captured:
        analyze(model)

    assert captured.value.code == "invalid_success_envelope"
    assert captured.value.retryable is False


def test_gateway_request_and_response_size_limits_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{" + b"x" * 1_000_001)

    model, _requested_ca_files = gateway_model(tmp_path, monkeypatch, handler)

    with pytest.raises(ModelError, match="request exceeded"):
        model._request("POST", "/v1/inference", {"value": "x" * 1_000_000})

    with pytest.raises(GatewayModelError) as captured:
        analyze(model, "short")

    assert captured.value.code == "invalid_success_envelope"
    assert captured.value.retryable is False


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
    untrusted_block = prompt.split("BEGIN UNTRUSTED EMAIL DATA\n", 1)[1]
    email_data = json.loads(untrusted_block.split("\nEND UNTRUSTED EMAIL DATA", 1)[0])
    assert len(email_data["sender"]) == model_module.MAX_GATEWAY_SENDER_CHARS
    assert len(email_data["subject"]) == model_module.MAX_GATEWAY_SUBJECT_CHARS
    assert len(email_data["current_message_text"]) == model_module.MAX_GATEWAY_BODY_CHARS
    assert email_data["quoted_history"] is None
    assert len(email_data["attachment_filenames"]) == model_module.MAX_GATEWAY_ATTACHMENT_COUNT
    assert all(
        len(name) == model_module.MAX_GATEWAY_ATTACHMENT_NAME_CHARS
        for name in email_data["attachment_filenames"]
    )


def test_gateway_replaces_unencodable_surrogates_in_email_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        prompts.append(payload["generation"]["messages"][1]["content"])
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
        sender="sender\ud800@example.com",
        subject="subject\ud800",
        received_at="2026-08-29T12:00:00+00:00",
        body="body\ud800",
        attachment_names=("attachment\ud800.pdf",),
        current_local_time=datetime(2026, 8, 29, tzinfo=UTC),
    )

    assert result.priority == "high"
    assert "\ud800" not in prompts[0]
    assert prompts[0].count("?") == 4


def test_gateway_translates_request_encoding_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model, _requested_ca_files = gateway_model(
        tmp_path, monkeypatch, lambda request: httpx.Response(200, json={})
    )

    with pytest.raises(ModelError, match="request could not be encoded"):
        model._request("POST", "/v1/inference", {"value": "\ud800"})


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

    with pytest.raises(GatewayModelError) as captured:
        analyze(model)

    assert captured.value.code == "invalid_success_envelope"
    assert captured.value.retryable is False


def test_gateway_converts_deeply_nested_json_to_model_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deeply_nested = b"[" * 100_000 + b"]" * 100_000
    model, _requested_ca_files = gateway_model(
        tmp_path,
        monkeypatch,
        lambda request: httpx.Response(200, content=deeply_nested),
    )

    with pytest.raises(GatewayModelError) as captured:
        analyze(model)

    assert captured.value.code == "invalid_success_envelope"
    assert captured.value.retryable is False


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

    with pytest.raises(GatewayOutputRejected) as captured:
        analyze(model)
    assert captured.value.code == "application_output_rejected"


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
    with pytest.raises(GatewayOutputRejected) as captured:
        analyze(model)
    assert captured.value.code == "application_output_rejected"


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

    with pytest.raises(GatewayOutputRejected) as captured:
        analyze(model)
    assert captured.value.code == "application_output_rejected"


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

    with pytest.raises(GatewayOutputRejected) as captured:
        analyze(model)
    assert captured.value.code == "application_output_rejected"


def test_gateway_missing_credential_and_trust_root_fail_closed(tmp_path: Path) -> None:
    token_file = tmp_path / "gateway-token"
    ca_file = tmp_path / "gateway-ca.pem"
    model = GatewayModel(
        "https://inference.office.internal",
        30,
        token_file,
        ca_file,
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
    )

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


@pytest.mark.skipif(os.name != "posix", reason="FIFO probe is POSIX-specific")
def test_gateway_rejects_fifo_credential_without_blocking(tmp_path: Path) -> None:
    token_file = tmp_path / "gateway-token"
    os.mkfifo(token_file, mode=0o600)
    model = GatewayModel(
        "https://inference.office.internal",
        30,
        token_file,
        tmp_path / "gateway-ca.pem",
    )

    with pytest.raises(ModelError, match="credential is invalid"):
        model._headers()
