import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eom_email_watcher import connect, engine_api
from eom_email_watcher.db import ConnectQueueFull, MessageSource
from eom_email_watcher.mailbox import MailboxError, MailboxMessageUnavailable
from eom_email_watcher.mime import AttachmentDescriptor
from eom_email_watcher.runtime import Runtime, load_runtime

INSTANCE_A = "11111111-1111-4111-8111-111111111111"
INSTANCE_B = "22222222-2222-4222-8222-222222222222"
OUTPUT_ID = "33333333-3333-4333-8333-333333333333"
INPUT_ARTIFACT_ID = "55555555-5555-4555-8555-555555555555"
REQUEST_ID = "66666666-6666-4666-8666-666666666666"
SECOND_REQUEST_ID = "77777777-7777-4777-8777-777777777777"
TOKEN = "A" * 43
PDF = b"%PDF-1.4\nreal attachment\nEOF"
LOCK_HOLDER = """
import sys
from pathlib import Path

from eom_email_watcher.locking import connect_operation_lock

with connect_operation_lock(Path(sys.argv[1]), "busy"):
    print("locked", flush=True)
    sys.stdin.read(1)
"""
DELETE_MESSAGE_PROBE = """
import sys
from pathlib import Path

from eom_email_watcher.db import Store

deleted = Store(Path(sys.argv[1])).delete_message(sys.argv[2])
print("deleted" if deleted else "missing", flush=True)
"""


@pytest.fixture(autouse=True)
def active_connect_entitlement(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        connect.entitlement,
        "connect_entitlement_decision",
        lambda: connect.entitlement.EntitlementDecision.ACTIVE,
    )


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
notifications_enabled = false
''',
        encoding="utf-8",
    )


def api_request(
    config_path: Path,
    operation: str,
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "protocol": 1,
        "operation": operation,
        "config_path": str(config_path),
        "payload": payload or {},
    }


def make_connect_job_due(runtime: Runtime, job_id: str) -> None:
    with runtime.store.connection() as db:
        db.execute(
            "UPDATE connect_job_dispatch SET next_attempt_at = ? WHERE job_id = ?",
            ("2000-01-01T00:00:00+00:00", job_id),
        )


def assert_active_response(
    response: dict[str, object],
    *,
    status: str,
    dispatch_state: str,
    job_id: str = REQUEST_ID,
    queue_ahead: int = 0,
) -> None:
    assert response["ok"] is True
    data = response["data"]
    assert isinstance(data, dict)
    assert data["job_id"] == job_id
    assert data["status"] == status
    assert data["dispatch_state"] == dispatch_state
    assert data["queue_ahead"] == queue_ahead
    assert data["outputs"] == []


@pytest.mark.parametrize(
    ("failure_count", "delay"),
    [(0, 2), (1, 4), (2, 8), (3, 16), (4, 30), (25, 30)],
)
def test_connect_retry_delay_is_bounded(failure_count: int, delay: int) -> None:
    assert engine_api._connect_retry_delay(failure_count) == delay


def test_connect_retry_delay_rejects_negative_counts() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        engine_api._connect_retry_delay(-1)


@pytest.mark.parametrize(
    ("received_at", "discovered_at", "expected"),
    [
        ("2026-08-10T11:59:59+00:00", "2026-09-01T12:00:00+00:00", False),
        ("2026-08-10T12:00:00+00:00", "2026-09-01T12:00:00+00:00", True),
        ("2026-09-10T12:00:01+00:00", "2026-09-01T12:00:00+00:00", True),
        ("2026-09-10T12:00:01+00:00", "2026-08-10T11:59:59+00:00", False),
        ("2026-09-10T12:00:01+00:00", "2026-09-10T12:00:00+00:00", False),
        ("2026-09-01T12:00:00", "2026-09-01T12:00:00+00:00", False),
        ("not-a-time", "2026-09-01T12:00:00+00:00", False),
    ],
)
def test_connect_source_retention_boundary(
    received_at: str,
    discovered_at: str,
    expected: bool,
) -> None:
    source = MessageSource(
        "message-1",
        "gmail",
        "gmail-default",
        "gmail-1",
        received_at,
        discovered_at,
    )

    assert (
        engine_api._connect_source_is_retained(
            source,
            30,
            observed_at=datetime(2026, 9, 9, 12, tzinfo=UTC),
        )
        is expected
    )


@pytest.mark.parametrize("limit", [False, 0, 26])
def test_connect_queue_pump_rejects_invalid_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit: object,
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)

    response = engine_api._response(
        api_request(config_path, "connect.queue.pump", {"limit": limit})
    )

    assert response["error"]["code"] == "invalid_request"


@pytest.mark.parametrize("limit", [1, 25])
def test_connect_queue_pump_accepts_boundary_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit: int,
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)

    response = engine_api._response(
        api_request(config_path, "connect.queue.pump", {"limit": limit})
    )

    assert response["data"] == {"items": [], "next_wake_unix_ms": None}


@pytest.mark.parametrize(
    ("blocked_outcome", "blocked_retry_seconds", "other_lane_seconds"),
    [
        ("lock_contended", 2, 1),
        ("lock_unavailable", 30, 10),
    ],
)
def test_connect_queue_wakeup_does_not_delay_another_provider_lane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    blocked_outcome: str,
    blocked_retry_seconds: int,
    other_lane_seconds: int,
) -> None:
    _, runtime = seeded_runtime(tmp_path)
    observed_at = datetime(2026, 9, 9, 12, tzinfo=UTC)
    other_lane_wakeup = observed_at + timedelta(seconds=other_lane_seconds)
    monkeypatch.setattr(
        runtime.store,
        "connect_queue_wakeups",
        lambda *, now: [
            (REQUEST_ID, observed_at),
            (SECOND_REQUEST_ID, other_lane_wakeup),
        ],
    )

    next_wakeup = engine_api._next_connect_queue_wakeup(
        runtime,
        [{"job_id": REQUEST_ID, "outcome": blocked_outcome}],
        observed_at,
    )

    assert blocked_retry_seconds > other_lane_seconds
    assert next_wakeup == other_lane_wakeup


def test_connect_queue_pump_reports_admission_deadline_expiry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected = capability()
    job = connect.prepare_capability_job(
        selected,
        PDF,
        "application/pdf",
        "invoice.pdf",
        job_id=REQUEST_ID,
    )
    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    runtime.store.create_connect_job(
        job_id=job.job_id,
        message_id="message-1",
        part_id="2",
        protocol_version=connect.GENERIC_PROTOCOL_VERSION,
        capability_id=job.capability_id,
        capability_version=job.capability_version,
        provider_app_id=job.provider_app_id,
        provider_app_version=job.provider_app_version,
        provider_instance_id=job.provider_instance_id,
        input_artifact_id=job.artifact.artifact_id,
        input_media_type=job.artifact.media_type,
        input_byte_size=job.artifact.byte_size,
        input_sha256=job.artifact.sha256,
        input_display_name=job.display_name,
        source_app_id=connect.SOURCE_APP_ID,
        request_json=job.request_json,
        now=datetime(2000, 1, 1, tzinfo=UTC),
    )
    with runtime.store.connection() as db:
        db.execute(
            """UPDATE connect_job_dispatch
            SET state = 'waiting', submission_possible = 0,
                next_attempt_at = admission_deadline
            WHERE job_id = ?""",
            (REQUEST_ID,),
        )

    response = engine_api._response(api_request(config_path, "connect.queue.pump"))

    assert response["data"]["items"] == [
        {
            "job_id": REQUEST_ID,
            "job_status": "failed",
            "dispatch_state": "terminal",
            "outcome": "expired",
        }
    ]
    assert response["data"]["next_wake_unix_ms"] is None


def seeded_runtime(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.config.gmail_token_file.write_text("connected token", encoding="utf-8")
    runtime.store.add_message(
        message_id="message-1",
        thread_id=None,
        sender="private@example.com",
        sender_name="Private Sender",
        subject="Private subject",
        received_at="2026-08-30T12:00:00+00:00",
    )
    runtime.store.replace_attachments(
        "message-1",
        (
            AttachmentDescriptor(
                "2",
                "gmail-attachment",
                "invoice.pdf",
                "application/pdf",
                len(PDF),
                0,
            ),
        ),
    )
    return config_path, runtime


def seed_second_attachment(runtime: Runtime) -> None:
    runtime.store.add_message(
        message_id="message-2",
        thread_id=None,
        sender="second@example.com",
        sender_name="Second Sender",
        subject="Second private subject",
        received_at="2026-08-30T12:01:00+00:00",
    )
    runtime.store.replace_attachments(
        "message-2",
        (
            AttachmentDescriptor(
                "2",
                "gmail-attachment-2",
                "invoice-2.pdf",
                "application/pdf",
                len(PDF),
                0,
            ),
        ),
    )


def capability(
    *,
    app_id: str = "generic-provider",
    app_version: str = "1.2.3",
    instance_id: str = INSTANCE_A,
    capability_id: str = "document.translate",
    media_type: str = "application/pdf",
    produces: tuple[str, ...] = ("text/plain",),
    parameters: tuple[connect.CapabilityParameter, ...] = (),
    external_effects: bool = False,
    confirmation_required: bool = False,
) -> connect.DiscoveredCapability:
    return connect.DiscoveredCapability(
        protocol_version=2,
        base_url="http://127.0.0.1:32123/",
        token=TOKEN,
        app_id=app_id,
        app_name=f"{app_id} name",
        app_version=app_version,
        instance_id=instance_id,
        capability_id=capability_id,
        capability_version="1.0",
        action_label="Translate",
        action_description="Translate this attachment locally.",
        accepts=(connect.AcceptedArtifactType(media_type, 1024),),
        produces=produces,
        parameters=parameters,
        external_effects=external_effects,
        confirmation_required=confirmation_required,
    )


def invocation_payload(
    selected: connect.DiscoveredCapability,
    *,
    message_id: str = "message-1",
    parameters: dict[str, object] | None = None,
    confirmed: bool = False,
    request_id: str = REQUEST_ID,
) -> dict[str, object]:
    return {
        "request_id": request_id,
        "message_id": message_id,
        "part_id": "2",
        "provider": {
            "app_id": selected.app_id,
            "version": selected.app_version,
            "instance_id": selected.instance_id,
        },
        "capability": {
            "id": selected.capability_id,
            "version": selected.capability_version,
        },
        "parameters": parameters or {},
        "confirmed": confirmed,
    }


def update(
    job: connect.PreparedCapabilityJob,
    status: str,
    *,
    payload: bytes | None = None,
    error: connect.ConnectError | None = None,
) -> connect.CapabilityJobUpdate:
    result = None
    if payload is not None:
        result = connect.CapabilityResult(
            (
                connect.CapabilityOutput(
                    artifact_id=OUTPUT_ID,
                    media_type="text/plain",
                    display_name="translation.txt",
                    byte_size=len(payload),
                    sha256=hashlib.sha256(payload).hexdigest(),
                    payload=payload,
                ),
            )
        )
    return connect.CapabilityJobUpdate(
        job_id=job.job_id,
        status=status,
        provider_app_id=job.provider_app_id,
        provider_instance_id=job.provider_instance_id,
        result=result,
        error=error,
    )


def persist_completed_outputs(
    runtime: Runtime,
    job: connect.PreparedCapabilityJob,
    outputs: tuple[connect.CapabilityOutput, ...],
) -> None:
    runtime.store.create_connect_job(
        job_id=job.job_id,
        message_id="message-1",
        part_id="2",
        protocol_version=2,
        capability_id=job.capability_id,
        capability_version=job.capability_version,
        provider_app_id=job.provider_app_id,
        provider_app_version=job.provider_app_version,
        provider_instance_id=job.provider_instance_id,
        input_artifact_id=job.artifact.artifact_id,
        input_media_type=job.artifact.media_type,
        input_byte_size=job.artifact.byte_size,
        input_sha256=job.artifact.sha256,
        input_display_name=job.display_name,
        source_app_id=connect.SOURCE_APP_ID,
        request_json=job.request_json,
    )
    runtime.store.transition_connect_job(
        job_id=job.job_id,
        expected_state="requested",
        next_state="completed",
        provider_app_id=job.provider_app_id,
        provider_instance_id=job.provider_instance_id,
        result=connect.CapabilityResult(outputs).store_dict(),
    )


def test_attachment_capabilities_are_contextual_and_do_not_expose_transport_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    accepted = capability()
    incompatible = capability(
        capability_id="text.classify",
        instance_id=INSTANCE_B,
        media_type="text/plain",
    )
    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities",
        lambda **kwargs: connect.CapabilityCatalog((accepted, incompatible)),
    )

    response = engine_api._response(
        api_request(
            config_path,
            "connect.attachment.capabilities",
            {"message_id": "message-1", "part_id": "2"},
        )
    )

    assert response["ok"] is True
    assert len(response["data"]["items"]) == 1
    assert response["data"]["items"][0]["capability"]["id"] == "document.translate"
    assert TOKEN not in str(response)
    assert "127.0.0.1" not in str(response)


def test_invoke_rejects_stale_capability_version_before_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected = capability()
    payload = invocation_payload(selected)
    payload["capability"] = {"id": selected.capability_id, "version": "9.0"}

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities",
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(
        engine_api.GmailGateway,
        "from_token",
        lambda *args: (_ for _ in ()).throw(AssertionError("stale selection reached Gmail")),
    )
    monkeypatch.setattr(
        engine_api.connect,
        "ConnectV2Client",
        lambda *args: (_ for _ in ()).throw(AssertionError("stale selection reached handoff")),
    )

    response = engine_api._response(api_request(config_path, "connect.attachment.invoke", payload))

    assert response["error"]["code"] == "capability_unavailable"
    assert runtime.store.connect_job(REQUEST_ID) is None


def test_unentitled_invoke_stops_before_discovery_gmail_or_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected = capability()
    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        connect.entitlement,
        "connect_entitlement_decision",
        lambda: connect.entitlement.EntitlementDecision.EXPIRED,
    )
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("unentitled invocation reached provider discovery")
        ),
    )
    monkeypatch.setattr(
        engine_api.GmailGateway,
        "from_token",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("unentitled invocation reached Gmail")
        ),
    )

    response = engine_api._response(
        api_request(
            config_path,
            "connect.attachment.invoke",
            invocation_payload(selected),
        )
    )

    assert response["error"]["code"] == "connect_entitlement_required"
    assert runtime.store.connect_job(REQUEST_ID) is None


def test_completed_connect_result_remains_readable_after_entitlement_expires(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected = capability()
    job = connect.prepare_capability_job(
        selected,
        PDF,
        "application/pdf",
        "invoice.pdf",
        job_id=REQUEST_ID,
    )
    output = connect.CapabilityOutput(
        artifact_id=OUTPUT_ID,
        media_type="text/plain",
        display_name="translation.txt",
        byte_size=5,
        sha256=hashlib.sha256(b"saved").hexdigest(),
        payload=b"saved",
    )
    persist_completed_outputs(runtime, job, (output,))
    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        connect.entitlement,
        "connect_entitlement_decision",
        lambda: connect.entitlement.EntitlementDecision.EXPIRED,
    )
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("completed result attempted provider discovery")
        ),
    )
    monkeypatch.setattr(
        engine_api.GmailGateway,
        "from_token",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("completed result attempted Gmail access")
        ),
    )

    response = engine_api._response(
        api_request(
            config_path,
            "connect.attachment.invoke",
            invocation_payload(selected),
        )
    )

    assert response["ok"] is True
    assert response["data"]["status"] == "completed"
    assert response["data"]["outputs"][0]["byte_size"] == 5


def test_late_terminal_after_source_cleanup_is_returned_without_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config_path, runtime = seeded_runtime(tmp_path)
    selected = capability()
    job = connect.prepare_capability_job(
        selected,
        PDF,
        "application/pdf",
        "invoice.pdf",
        job_id=REQUEST_ID,
    )
    runtime.store.create_connect_job(
        job_id=job.job_id,
        message_id="message-1",
        part_id="2",
        protocol_version=2,
        capability_id=job.capability_id,
        capability_version=job.capability_version,
        provider_app_id=job.provider_app_id,
        provider_app_version=job.provider_app_version,
        provider_instance_id=job.provider_instance_id,
        input_artifact_id=job.artifact.artifact_id,
        input_media_type=job.artifact.media_type,
        input_byte_size=job.artifact.byte_size,
        input_sha256=job.artifact.sha256,
        input_display_name=job.display_name,
        source_app_id=connect.SOURCE_APP_ID,
        request_json=job.request_json,
    )
    runtime.store.transition_connect_job(
        job_id=job.job_id,
        expected_state="requested",
        next_state="accepted",
        provider_app_id=job.provider_app_id,
        provider_instance_id=job.provider_instance_id,
    )
    assert runtime.store.delete_message("message-1") is True

    class CompletingClient:
        def __init__(self, capability_value):
            assert capability_value == selected

        def get(self, requested_job):
            return update(requested_job, "accepted")

        def wait_for_terminal(self, requested_job, initial, on_update):
            assert initial.status == "accepted"
            completed = update(requested_job, "completed", payload=b"Ephemeral result")
            on_update(completed)
            return completed

    monkeypatch.setattr(engine_api.connect, "ConnectV2Client", CompletingClient)

    result = engine_api._run_generic_connect_job(runtime, selected, job, None)

    assert result["job_id"] == REQUEST_ID
    assert runtime.store.connect_job(REQUEST_ID) is None
    assert runtime.store.connect_dispatch(REQUEST_ID) is None


def test_completed_outputs_use_trusted_presentations_and_safe_binary_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected = capability(
        capability_id="document.summarize",
        produces=(connect.OUTPUT_MEDIA_TYPE, "text/plain", "application/x-msdownload"),
    )
    job = connect.restore_capability_job(
        selected,
        job_id=REQUEST_ID,
        artifact_id=INPUT_ARTIFACT_ID,
        media_type="application/pdf",
        byte_size=len(PDF),
        sha256=hashlib.sha256(PDF).hexdigest(),
        filename="invoice.pdf",
    )
    summary_payload = json.dumps(
        {
            "summary_version": "1.0",
            "text": "Invoice due Friday.",
            "warnings": [],
            "input_artifact": job.artifact.public_dict(),
        },
        separators=(",", ":"),
    ).encode()
    text_payload = b"Factura vence el viernes."
    invalid_text_payload = b"\xff"
    opaque_payload = b"dangerous-content-must-not-reach-the-dom"
    summary_output = connect.CapabilityOutput(
        artifact_id=OUTPUT_ID,
        media_type=connect.OUTPUT_MEDIA_TYPE,
        display_name="summary.json",
        byte_size=len(summary_payload),
        sha256=hashlib.sha256(summary_payload).hexdigest(),
        payload=summary_payload,
    )
    text_output = connect.CapabilityOutput(
        artifact_id="88888888-8888-4888-8888-888888888888",
        media_type="text/plain",
        display_name="translation.txt",
        byte_size=len(text_payload),
        sha256=hashlib.sha256(text_payload).hexdigest(),
        payload=text_payload,
    )
    opaque_output = connect.CapabilityOutput(
        artifact_id="99999999-9999-4999-8999-999999999999",
        media_type="application/x-msdownload",
        display_name="../../run-me.exe",
        byte_size=len(opaque_payload),
        sha256=hashlib.sha256(opaque_payload).hexdigest(),
        payload=opaque_payload,
    )
    invalid_text_output = connect.CapabilityOutput(
        artifact_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        media_type="text/plain",
        display_name="invalid.txt",
        byte_size=len(invalid_text_payload),
        sha256=hashlib.sha256(invalid_text_payload).hexdigest(),
        payload=invalid_text_payload,
    )
    persist_completed_outputs(
        runtime,
        job,
        (summary_output, text_output, opaque_output, invalid_text_output),
    )
    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)

    def present(artifact_id: str) -> dict[str, object]:
        return engine_api._response(
            api_request(
                config_path,
                "connect.output.present",
                {
                    "message_id": "message-1",
                    "part_id": "2",
                    "job_id": job.job_id,
                    "artifact_id": artifact_id,
                },
            )
        )

    summary = present(summary_output.artifact_id)
    text = present(text_output.artifact_id)
    opaque = present(opaque_output.artifact_id)
    invalid_text = present(invalid_text_output.artifact_id)

    assert summary["data"]["presentation"] == {
        "kind": "document_summary",
        "summary": {
            "summary_version": "1.0",
            "text": "Invoice due Friday.",
            "warnings": [],
        },
    }
    assert text["data"]["presentation"] == {
        "kind": "text",
        "text": "Factura vence el viernes.",
    }
    assert opaque["data"]["presentation"] == {"kind": "opaque"}
    assert "dangerous-content-must-not-reach-the-dom" not in str(opaque)
    assert invalid_text["error"]["code"] == "output_invalid"
    assert present("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")["error"]["code"] == "not_found"

    destination = tmp_path / "exports"
    destination.mkdir()
    exported = engine_api._response(
        api_request(
            config_path,
            "connect.output.export",
            {
                "message_id": "message-1",
                "part_id": "2",
                "job_id": job.job_id,
                "artifact_id": opaque_output.artifact_id,
                "destination_dir": str(destination),
            },
        )
    )
    exported_path = Path(exported["data"]["path"])
    assert exported_path.parent == destination.resolve()
    assert exported_path.name.startswith("email-watcher-output-")
    assert exported_path.suffix == ".bin"
    assert exported_path.read_bytes() == opaque_payload
    assert exported_path.stat().st_mode & 0o777 == 0o600
    assert "run-me.exe" not in exported_path.name
    assert "dangerous-content-must-not-reach-the-dom" not in str(exported)

    missing = engine_api._response(
        api_request(
            config_path,
            "connect.output.present",
            {
                "message_id": "another-message",
                "part_id": "2",
                "job_id": job.job_id,
                "artifact_id": opaque_output.artifact_id,
            },
        )
    )
    assert missing["error"]["code"] == "not_found"

    with pytest.raises(connect.ConnectError, match="input artifact"):
        connect.decode_document_summary_output(
            summary_output,
            connect.ArtifactIdentity(
                artifact_id=job.artifact.artifact_id,
                media_type=job.artifact.media_type,
                byte_size=job.artifact.byte_size,
                sha256="0" * 64,
            ),
        )


def test_generic_invoke_requires_explicit_provider_and_confirmation_then_persists_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    parameter = connect.CapabilityParameter(
        name="target-language",
        value_type="string",
        required=True,
        label="Target language",
        description="Language to produce.",
    )
    first = capability(
        app_id="first-provider",
        instance_id=INSTANCE_A,
        parameters=(parameter,),
        external_effects=True,
        confirmation_required=True,
    )
    selected = capability(
        app_id="second-provider",
        instance_id=INSTANCE_B,
        parameters=(parameter,),
        external_effects=True,
        confirmation_required=True,
    )
    discovered_instances: list[str | None] = []
    gmail_reads = 0
    lane_lock_held = False

    def discover(**kwargs):
        discovered_instances.append(kwargs.get("provider_instance_id"))
        return connect.CapabilityCatalog((first, selected))

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(engine_api.connect, "discover_capabilities", discover)

    class LaneLockProbe:
        def __enter__(self):
            nonlocal lane_lock_held
            lane_lock_held = True

        def __exit__(self, *_args):
            nonlocal lane_lock_held
            lane_lock_held = False

    real_claim = runtime.store.claim_connect_lane_head

    def claim_under_lock(**values):
        assert lane_lock_held is True
        return real_claim(**values)

    monkeypatch.setattr(engine_api, "connect_operation_lock", lambda *_args: LaneLockProbe())
    monkeypatch.setattr(runtime.store, "claim_connect_lane_head", claim_under_lock)
    monkeypatch.setattr(
        engine_api.GmailGateway,
        "from_token",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("Gmail must not be read before explicit confirmation")
        ),
    )

    missing_provider = invocation_payload(selected, parameters={"target-language": "es"})
    missing_provider.pop("provider")
    malformed = engine_api._response(
        api_request(config_path, "connect.attachment.invoke", missing_provider)
    )
    missing_request_id = invocation_payload(
        selected, parameters={"target-language": "es"}
    )
    missing_request_id.pop("request_id")
    unidentified = engine_api._response(
        api_request(config_path, "connect.attachment.invoke", missing_request_id)
    )
    unconfirmed = engine_api._response(
        api_request(
            config_path,
            "connect.attachment.invoke",
            invocation_payload(selected, parameters={"target-language": "es"}),
        )
    )

    assert malformed["error"]["code"] == "invalid_request"
    assert unidentified["error"]["code"] == "job_request_invalid"
    assert unconfirmed["error"]["code"] == "confirmation_required"

    submitted: list[connect.PreparedCapabilityJob] = []

    class FakeGmail:
        def attachment_bytes(self, *args) -> bytes:
            nonlocal gmail_reads
            gmail_reads += 1
            return PDF

    class CompletingClient:
        def __init__(self, capability_value):
            assert capability_value == selected

        def submit(self, job, content):
            assert content == PDF
            assert lane_lock_held is True
            persisted = runtime.store.connect_job(job.job_id)
            dispatch = runtime.store.connect_dispatch(job.job_id)
            assert persisted is not None
            assert persisted.status == "requested"
            assert dispatch is not None
            assert dispatch.state == "dispatching"
            assert dispatch.submission_possible is True
            assert dispatch.attempt_count == 1
            submitted.append(job)
            return update(job, "accepted")

        def wait_for_terminal(self, job, initial, on_update):
            completed = update(job, "completed", payload=b"Factura vence el viernes.")
            on_update(completed)
            return completed

    monkeypatch.setattr(engine_api.connect, "ConnectV2Client", CompletingClient)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: FakeGmail())

    request = api_request(
        config_path,
        "connect.attachment.invoke",
        invocation_payload(
            selected,
            parameters={"target-language": "es"},
            confirmed=True,
        ),
    )
    response = engine_api._response(request)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("terminal request identity must resolve without provider discovery")
        ),
    )
    repeated = engine_api._response(request)
    conflict = engine_api._response(
        api_request(
            config_path,
            "connect.attachment.invoke",
            invocation_payload(
                selected,
                parameters={"target-language": "fr"},
                confirmed=True,
            ),
        )
    )
    monkeypatch.setattr(engine_api.connect, "discover_capabilities", discover)
    second_effect = engine_api._response(
        api_request(
            config_path,
            "connect.attachment.invoke",
            invocation_payload(
                selected,
                parameters={"target-language": "es"},
                confirmed=True,
                request_id=SECOND_REQUEST_ID,
            ),
        )
    )

    assert response["ok"] is True
    assert lane_lock_held is False
    assert repeated == response
    assert conflict["error"]["code"] == "request_id_conflict"
    assert second_effect["ok"] is True
    assert second_effect["data"]["job_id"] == SECOND_REQUEST_ID
    assert response["data"]["provider"] == {
        "app_id": "second-provider",
        "version": "1.2.3",
        "instance_id": INSTANCE_B,
    }
    assert response["data"]["capability"] == {"id": "document.translate", "version": "1.0"}
    assert response["data"]["outputs"] == [
        {
            "artifact_id": OUTPUT_ID,
            "media_type": "text/plain",
            "display_name": "translation.txt",
            "byte_size": len(b"Factura vence el viernes."),
            "sha256": hashlib.sha256(b"Factura vence el viernes.").hexdigest(),
        }
    ]
    assert "payload" not in str(response["data"])
    assert discovered_instances == [INSTANCE_B] * 3
    assert [job.job_id for job in submitted] == [REQUEST_ID, SECOND_REQUEST_ID]
    assert [dict(job.parameters) for job in submitted] == [
        {"target-language": "es"},
        {"target-language": "es"},
    ]
    assert gmail_reads == 4


def test_lost_acknowledgement_reconciles_and_resubmits_the_same_durable_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected = capability()
    submissions: list[tuple[str, bytes]] = []
    queries: list[str] = []
    gmail_reads = 0

    class FakeGmail:
        def attachment_bytes(self, *args) -> bytes:
            nonlocal gmail_reads
            gmail_reads += 1
            return PDF

    class RecoveringClient:
        def __init__(self, capability_value):
            assert capability_value == selected

        def submit(self, job, content):
            assert content == PDF
            submissions.append((job.job_id, job.request_json))
            if len(submissions) == 1:
                raise connect.ConnectError(
                    "PROVIDER_UNAVAILABLE",
                    "The provider response was lost.",
                    retryable=True,
                )
            return update(job, "completed", payload=b"Recovered")

        def get(self, job):
            queries.append(job.job_id)
            raise connect.ConnectError(
                "JOB_NOT_FOUND",
                "The provider did not accept this job.",
            )

        def wait_for_terminal(self, job, initial, on_update):
            return initial

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities",
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api.connect, "ConnectV2Client", RecoveringClient)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: FakeGmail())
    request = api_request(
        config_path,
        "connect.attachment.invoke",
        invocation_payload(selected),
    )

    first = engine_api._response(request)
    durable = runtime.store.connect_job(submissions[0][0])
    early = engine_api._response(request)
    make_connect_job_due(runtime, REQUEST_ID)
    second = engine_api._response(request)

    assert_active_response(first, status="requested", dispatch_state="reconciling")
    assert durable is not None
    assert durable.status == "requested"
    assert_active_response(early, status="requested", dispatch_state="reconciling")
    assert second["ok"] is True
    assert queries == [durable.job_id]
    assert submissions == [
        (durable.job_id, durable.request_json),
        (durable.job_id, durable.request_json),
    ]
    assert gmail_reads == 3
    with runtime.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM connect_attachment_jobs").fetchone()[0] == 1


def test_queue_pump_retries_provider_busy_with_same_job_after_durable_due_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected = capability()
    submissions: list[str] = []

    class FakeGmail:
        def attachment_bytes(self, *args) -> bytes:
            return PDF

    class BusyThenCompleteClient:
        def __init__(self, capability_value):
            assert capability_value == selected

        def submit(self, job, content):
            assert content == PDF
            submissions.append(job.job_id)
            if len(submissions) == 1:
                raise connect.ConnectError(
                    "PROVIDER_BUSY",
                    "The provider is already processing another job.",
                    retryable=True,
                )
            return update(job, "completed", payload=b"Completed after retry")

        def wait_for_terminal(self, job, initial, on_update):
            return initial

    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities",
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api.connect, "ConnectV2Client", BusyThenCompleteClient)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: FakeGmail())

    invoked = engine_api._response(
        api_request(
            config_path,
            "connect.attachment.invoke",
            invocation_payload(selected),
        )
    )
    dispatch = runtime.store.connect_dispatch(REQUEST_ID)
    early = engine_api._response(api_request(config_path, "connect.queue.pump"))

    assert_active_response(invoked, status="requested", dispatch_state="waiting")
    assert dispatch is not None
    assert dispatch.state == "waiting"
    assert dispatch.attempt_count == 1
    assert dispatch.last_error_code == "PROVIDER_BUSY"
    assert early["data"]["items"] == []
    assert submissions == [REQUEST_ID]

    make_connect_job_due(runtime, REQUEST_ID)
    pumped = engine_api._response(api_request(config_path, "connect.queue.pump"))

    assert pumped["ok"] is True
    assert pumped["data"]["items"] == [
        {
            "job_id": REQUEST_ID,
            "job_status": "completed",
            "dispatch_state": "terminal",
            "outcome": "completed",
        }
    ]
    assert submissions == [REQUEST_ID, REQUEST_ID]
    assert runtime.store.connect_job(REQUEST_ID).status == "completed"  # type: ignore[union-attr]


def test_post_submit_poll_timeout_schedules_durable_reconciliation_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected = capability()

    class FakeGmail:
        def attachment_bytes(self, *args) -> bytes:
            return PDF

    class AcceptedThenTimeoutClient:
        def __init__(self, capability_value):
            assert capability_value == selected

        def submit(self, job, content):
            return update(job, "accepted")

        def wait_for_terminal(self, job, initial, on_update):
            raise connect.ConnectError(
                "JOB_TIMEOUT",
                "The accepted provider job is still running.",
                retryable=True,
            )

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities",
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(
        engine_api.connect,
        "ConnectV2Client",
        AcceptedThenTimeoutClient,
    )
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: FakeGmail())

    response = engine_api._response(
        api_request(config_path, "connect.attachment.invoke", invocation_payload(selected))
    )
    dispatch = runtime.store.connect_dispatch(REQUEST_ID)

    assert_active_response(response, status="accepted", dispatch_state="provider_owned")
    assert dispatch is not None
    assert dispatch.state == "provider_owned"
    assert dispatch.next_attempt_at is not None
    assert dispatch.reconciliation_failure_count == 1
    assert dispatch.last_error_code == "JOB_TIMEOUT"


def test_queue_pump_respects_cross_process_lane_owner_then_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected = capability()
    submissions: list[str] = []

    class FakeGmail:
        def attachment_bytes(self, *args) -> bytes:
            return PDF

    class BusyThenCompleteClient:
        def __init__(self, capability_value):
            assert capability_value == selected

        def submit(self, job, content):
            submissions.append(job.job_id)
            if len(submissions) == 1:
                raise connect.ConnectError(
                    "PROVIDER_BUSY",
                    "The provider is busy.",
                    retryable=True,
                )
            return update(job, "completed", payload=b"Completed after contention")

        def wait_for_terminal(self, job, initial, on_update):
            return initial

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities",
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api.connect, "ConnectV2Client", BusyThenCompleteClient)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: FakeGmail())

    engine_api._response(
        api_request(config_path, "connect.attachment.invoke", invocation_payload(selected))
    )
    make_connect_job_due(runtime, REQUEST_ID)
    lock_path = engine_api.connect_lane_lock_path(
        runtime.store.path,
        protocol_version=connect.GENERIC_PROTOCOL_VERSION,
        provider_app_id=selected.app_id,
        provider_instance_id=selected.instance_id,
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", LOCK_HOLDER, str(lock_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        contended = engine_api._response(api_request(config_path, "connect.queue.pump"))
        assert contended["data"]["items"][0]["outcome"] == "lock_contended"
        assert submissions == [REQUEST_ID]
    finally:
        if holder.stdin is not None:
            holder.stdin.write("\n")
            holder.stdin.flush()
        holder.wait(timeout=10)

    recovered = engine_api._response(api_request(config_path, "connect.queue.pump"))

    assert recovered["data"]["items"][0]["outcome"] == "completed"
    assert submissions == [REQUEST_ID, REQUEST_ID]


def test_queue_pump_completes_provider_owned_head_then_drains_waiting_invoice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    seed_second_attachment(runtime)
    selected = capability()
    submissions: list[str] = []
    queries: list[str] = []

    class FakeGmail:
        def attachment_bytes(self, provider_message_id, *args) -> bytes:
            assert provider_message_id in {"message-1", "message-2"}
            return PDF

    class SerializedClient:
        def __init__(self, capability_value):
            assert capability_value == selected

        def submit(self, job, content):
            assert content == PDF
            submissions.append(job.job_id)
            if job.job_id == REQUEST_ID:
                return update(job, "accepted")
            return update(job, "completed", payload=b"Second invoice completed")

        def get(self, job):
            queries.append(job.job_id)
            return update(job, "completed", payload=b"First invoice completed")

        def wait_for_terminal(self, job, initial, on_update):
            if job.job_id == REQUEST_ID:
                raise connect.ConnectError(
                    "JOB_TIMEOUT",
                    "The accepted provider job is still running.",
                    retryable=True,
                )
            return initial

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities",
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities_for_reconciliation",
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api.connect, "ConnectV2Client", SerializedClient)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: FakeGmail())

    first = engine_api._response(
        api_request(config_path, "connect.attachment.invoke", invocation_payload(selected))
    )
    second = engine_api._response(
        api_request(
            config_path,
            "connect.attachment.invoke",
            invocation_payload(
                selected,
                message_id="message-2",
                request_id=SECOND_REQUEST_ID,
            ),
        )
    )

    assert_active_response(first, status="accepted", dispatch_state="provider_owned")
    assert_active_response(
        second,
        status="requested",
        dispatch_state="waiting",
        job_id=SECOND_REQUEST_ID,
        queue_ahead=1,
    )
    assert submissions == [REQUEST_ID]
    assert runtime.store.connect_dispatch(REQUEST_ID).state == "provider_owned"  # type: ignore[union-attr]
    assert runtime.store.connect_dispatch(SECOND_REQUEST_ID).state == "waiting"  # type: ignore[union-attr]

    make_connect_job_due(runtime, REQUEST_ID)
    pumped = engine_api._response(api_request(config_path, "connect.queue.pump"))

    assert [item["job_id"] for item in pumped["data"]["items"]] == [
        REQUEST_ID,
        SECOND_REQUEST_ID,
    ]
    assert submissions == [REQUEST_ID, SECOND_REQUEST_ID]
    assert queries == [REQUEST_ID]
    assert runtime.store.connect_job(REQUEST_ID).status == "completed"  # type: ignore[union-attr]
    assert runtime.store.connect_job(SECOND_REQUEST_ID).status == "completed"  # type: ignore[union-attr]


def test_queue_pump_reconciles_after_entitlement_revocation_without_resubmitting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected = capability()
    submissions = 0
    queries = 0

    class FakeGmail:
        def attachment_bytes(self, *args) -> bytes:
            return PDF

    class LostAckThenCompleteClient:
        def __init__(self, capability_value):
            assert capability_value == selected

        def submit(self, job, content):
            nonlocal submissions
            submissions += 1
            raise connect.ConnectError(
                "PROVIDER_UNAVAILABLE",
                "The provider response was lost.",
                retryable=True,
            )

        def get(self, job):
            nonlocal queries
            queries += 1
            return update(job, "completed", payload=b"Recovered without entitlement")

        def wait_for_terminal(self, job, initial, on_update):
            return initial

    discovery_modes: list[str] = []

    def discover(**kwargs):
        discovery_modes.append("entitlement-gated")
        return connect.CapabilityCatalog((selected,))

    def reconcile_discovery(**kwargs):
        discovery_modes.append("reconciliation")
        return connect.CapabilityCatalog((selected,))

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(engine_api.connect, "discover_capabilities", discover)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities_for_reconciliation",
        reconcile_discovery,
    )
    monkeypatch.setattr(engine_api.connect, "ConnectV2Client", LostAckThenCompleteClient)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: FakeGmail())

    invoked = engine_api._response(
        api_request(config_path, "connect.attachment.invoke", invocation_payload(selected))
    )
    monkeypatch.setattr(
        engine_api.connect.entitlement,
        "connect_entitlement_decision",
        lambda: connect.entitlement.EntitlementDecision.EXPIRED,
    )
    make_connect_job_due(runtime, REQUEST_ID)
    pumped = engine_api._response(api_request(config_path, "connect.queue.pump"))

    assert_active_response(invoked, status="requested", dispatch_state="reconciling")
    assert pumped["data"]["items"][0]["job_status"] == "completed"
    assert submissions == 1
    assert queries == 1
    assert discovery_modes == ["entitlement-gated", "reconciliation"]


def test_queue_pump_never_resubmits_job_not_found_after_authoritative_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected = capability()
    submissions = 0
    queries = 0

    class FakeGmail:
        def attachment_bytes(self, *args) -> bytes:
            return PDF

    class AcceptedThenMissingClient:
        def __init__(self, capability_value):
            assert capability_value == selected

        def submit(self, job, content):
            nonlocal submissions
            submissions += 1
            return update(job, "accepted")

        def get(self, job):
            nonlocal queries
            queries += 1
            raise connect.ConnectError(
                "JOB_NOT_FOUND",
                "The accepted job was not found.",
            )

        def wait_for_terminal(self, job, initial, on_update):
            raise connect.ConnectError(
                "JOB_TIMEOUT",
                "The accepted provider job is still running.",
                retryable=True,
            )

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities",
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities_for_reconciliation",
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api.connect, "ConnectV2Client", AcceptedThenMissingClient)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: FakeGmail())

    invoked = engine_api._response(
        api_request(config_path, "connect.attachment.invoke", invocation_payload(selected))
    )
    make_connect_job_due(runtime, REQUEST_ID)
    pumped = engine_api._response(api_request(config_path, "connect.queue.pump"))
    dispatch = runtime.store.connect_dispatch(REQUEST_ID)

    assert_active_response(invoked, status="accepted", dispatch_state="provider_owned")
    assert pumped["data"]["items"][0]["outcome"] == "deferred_or_failed"
    assert submissions == 1
    assert queries == 1
    assert dispatch is not None
    assert dispatch.state == "provider_owned"
    assert dispatch.highest_provider_state == "accepted"


def test_queue_pump_blocks_a_new_post_after_entitlement_revocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected = capability()
    submissions = 0

    class FakeGmail:
        def attachment_bytes(self, *args) -> bytes:
            return PDF

    class BusyClient:
        def __init__(self, capability_value):
            assert capability_value == selected

        def submit(self, job, content):
            nonlocal submissions
            submissions += 1
            raise connect.ConnectError(
                "PROVIDER_BUSY",
                "The provider is busy.",
                retryable=True,
            )

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities",
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api.connect, "ConnectV2Client", BusyClient)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: FakeGmail())

    invoked = engine_api._response(
        api_request(config_path, "connect.attachment.invoke", invocation_payload(selected))
    )
    monkeypatch.setattr(
        engine_api.connect.entitlement,
        "connect_entitlement_decision",
        lambda: connect.entitlement.EntitlementDecision.EXPIRED,
    )
    make_connect_job_due(runtime, REQUEST_ID)
    pumped = engine_api._response(api_request(config_path, "connect.queue.pump"))
    job = runtime.store.connect_job(REQUEST_ID)

    assert_active_response(invoked, status="requested", dispatch_state="waiting")
    assert pumped["data"]["items"][0]["outcome"] == "failed"
    assert job is not None
    assert job.status == "failed"
    assert job.error_code == "CONNECT_ENTITLEMENT_REQUIRED"
    assert submissions == 1


def test_nonretryable_provider_refusal_remains_immediately_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected = capability()

    class FakeGmail:
        def attachment_bytes(self, *args) -> bytes:
            return PDF

    class RejectingClient:
        def __init__(self, capability_value):
            assert capability_value == selected

        def submit(self, job, content):
            raise connect.ConnectError(
                "INPUT_REJECTED",
                "The provider rejected this input.",
                retryable=False,
            )

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities",
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api.connect, "ConnectV2Client", RejectingClient)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: FakeGmail())

    response = engine_api._response(
        api_request(config_path, "connect.attachment.invoke", invocation_payload(selected))
    )
    job = runtime.store.connect_job(REQUEST_ID)

    assert response["error"]["code"] == "input_rejected"
    assert job is not None
    assert job.status == "failed"
    assert runtime.store.connect_dispatch(REQUEST_ID).state == "terminal"  # type: ignore[union-attr]
    assert runtime.store.due_connect_lane_heads() == ()


def test_queue_pump_rejects_changed_source_before_a_retry_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected = capability()
    source_bytes = [PDF]
    submissions = 0

    class FakeGmail:
        def attachment_bytes(self, *args) -> bytes:
            return source_bytes[0]

    class BusyClient:
        def __init__(self, capability_value):
            assert capability_value == selected

        def submit(self, job, content):
            nonlocal submissions
            submissions += 1
            raise connect.ConnectError(
                "PROVIDER_BUSY",
                "The provider is busy.",
                retryable=True,
            )

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities",
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api.connect, "ConnectV2Client", BusyClient)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: FakeGmail())

    engine_api._response(
        api_request(config_path, "connect.attachment.invoke", invocation_payload(selected))
    )
    source_bytes[0] = PDF.replace(b"real", b"fake")
    make_connect_job_due(runtime, REQUEST_ID)
    pumped = engine_api._response(api_request(config_path, "connect.queue.pump"))
    job = runtime.store.connect_job(REQUEST_ID)

    assert pumped["data"]["items"][0]["outcome"] == "deferred_or_failed"
    assert job is not None
    assert job.status == "failed"
    assert job.error_code == "CONNECT_SOURCE_UNAVAILABLE"
    assert submissions == 1


def test_queue_pump_rejects_retention_expired_source_before_a_retry_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected = capability()
    submissions = 0

    class FakeGmail:
        def attachment_bytes(self, *args) -> bytes:
            return PDF

    class BusyClient:
        def __init__(self, capability_value):
            assert capability_value == selected

        def submit(self, job, content):
            nonlocal submissions
            submissions += 1
            raise connect.ConnectError(
                "PROVIDER_BUSY",
                "The provider is busy.",
                retryable=True,
            )

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities",
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api.connect, "ConnectV2Client", BusyClient)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: FakeGmail())

    engine_api._response(
        api_request(config_path, "connect.attachment.invoke", invocation_payload(selected))
    )
    expired_at = datetime.now(UTC) - timedelta(days=runtime.config.retention_days + 1)
    with runtime.store.connection() as db:
        db.execute(
            "UPDATE messages SET received_at = ? WHERE message_id = ?",
            (expired_at.isoformat(), "message-1"),
        )
    make_connect_job_due(runtime, REQUEST_ID)

    pumped = engine_api._response(api_request(config_path, "connect.queue.pump"))
    job = runtime.store.connect_job(REQUEST_ID)

    assert pumped["data"]["items"][0]["outcome"] == "deferred_or_failed"
    assert job is not None
    assert job.status == "failed"
    assert job.error_code == "CONNECT_SOURCE_UNAVAILABLE"
    assert submissions == 1


def test_queue_pump_retries_transient_source_fetch_under_original_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected = capability()
    reads = 0
    submissions = 0

    class FlakyGmail:
        def attachment_bytes(self, *args) -> bytes:
            nonlocal reads
            reads += 1
            if reads == 3:
                raise MailboxError("Temporary mailbox outage")
            return PDF

    class BusyThenCompleteClient:
        def __init__(self, capability_value):
            assert capability_value == selected

        def submit(self, job, content):
            nonlocal submissions
            submissions += 1
            if submissions == 1:
                raise connect.ConnectError(
                    "PROVIDER_BUSY",
                    "The provider is busy.",
                    retryable=True,
                )
            return update(job, "completed", payload=b"Recovered output")

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities",
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(
        engine_api.connect,
        "ConnectV2Client",
        BusyThenCompleteClient,
    )
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: FlakyGmail())

    engine_api._response(
        api_request(config_path, "connect.attachment.invoke", invocation_payload(selected))
    )
    original_deadline = runtime.store.connect_dispatch(REQUEST_ID).admission_deadline  # type: ignore[union-attr]
    make_connect_job_due(runtime, REQUEST_ID)
    deferred = engine_api._response(api_request(config_path, "connect.queue.pump"))
    dispatch = runtime.store.connect_dispatch(REQUEST_ID)

    assert deferred["data"]["items"][0]["outcome"] == "deferred_or_failed"
    assert dispatch is not None
    assert dispatch.state == "waiting"
    assert dispatch.last_error_code == "CONNECT_SOURCE_TEMPORARILY_UNAVAILABLE"
    assert dispatch.admission_deadline == original_deadline
    assert submissions == 1

    make_connect_job_due(runtime, REQUEST_ID)
    completed = engine_api._response(api_request(config_path, "connect.queue.pump"))

    assert completed["data"]["items"][0]["outcome"] == "completed"
    assert submissions == 2


def test_queue_pump_treats_missing_mailbox_source_as_definitive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected = capability()
    reads = 0
    submissions = 0

    class DisappearingGmail:
        def attachment_bytes(self, *args) -> bytes:
            nonlocal reads
            reads += 1
            if reads > 2:
                raise MailboxMessageUnavailable("The message was removed")
            return PDF

    class BusyClient:
        def __init__(self, capability_value):
            assert capability_value == selected

        def submit(self, job, content):
            nonlocal submissions
            submissions += 1
            raise connect.ConnectError(
                "PROVIDER_BUSY",
                "The provider is busy.",
                retryable=True,
            )

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities",
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api.connect, "ConnectV2Client", BusyClient)
    monkeypatch.setattr(
        engine_api.GmailGateway,
        "from_token",
        lambda *args: DisappearingGmail(),
    )

    engine_api._response(
        api_request(config_path, "connect.attachment.invoke", invocation_payload(selected))
    )
    make_connect_job_due(runtime, REQUEST_ID)
    pumped = engine_api._response(api_request(config_path, "connect.queue.pump"))
    job = runtime.store.connect_job(REQUEST_ID)

    assert pumped["data"]["items"][0]["outcome"] == "deferred_or_failed"
    assert job is not None
    assert job.status == "failed"
    assert job.error_code == "CONNECT_SOURCE_UNAVAILABLE"
    assert submissions == 1


def test_source_cleanup_wins_before_retry_and_prevents_another_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected = capability()
    submissions = 0

    class FakeGmail:
        def attachment_bytes(self, *args) -> bytes:
            return PDF

    class BusyClient:
        def __init__(self, capability_value):
            assert capability_value == selected

        def submit(self, job, content):
            nonlocal submissions
            submissions += 1
            raise connect.ConnectError(
                "PROVIDER_BUSY",
                "The provider is busy.",
                retryable=True,
            )

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities",
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api.connect, "ConnectV2Client", BusyClient)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: FakeGmail())

    engine_api._response(
        api_request(config_path, "connect.attachment.invoke", invocation_payload(selected))
    )
    assert runtime.store.delete_message("message-1") is True

    pumped = engine_api._response(api_request(config_path, "connect.queue.pump"))

    assert pumped["data"]["items"] == []
    assert submissions == 1
    assert runtime.store.connect_job(REQUEST_ID) is None
    assert runtime.store.connect_dispatch(REQUEST_ID) is None


def test_handoff_releases_source_lock_after_durable_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected = capability()

    class FakeGmail:
        def attachment_bytes(self, *args) -> bytes:
            return PDF

    class AcceptedClient:
        def __init__(self, capability_value):
            assert capability_value == selected

        def submit(self, job, content):
            assert content == PDF
            return update(job, "accepted")

        def wait_for_terminal(self, job, initial, on_update):
            assert initial.status == "accepted"
            cleanup = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    DELETE_MESSAGE_PROBE,
                    str(runtime.store.path),
                    "message-1",
                ],
                stdout=subprocess.PIPE,
                text=True,
            )
            try:
                output, _ = cleanup.communicate(timeout=10)
            except Exception:
                cleanup.kill()
                cleanup.wait(timeout=10)
                raise
            assert cleanup.returncode == 0
            assert output.strip() == "deleted"
            completed = update(job, "completed", payload=b"Late terminal output")
            on_update(completed)
            return completed

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities",
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api.connect, "ConnectV2Client", AcceptedClient)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: FakeGmail())

    response = engine_api._response(
        api_request(config_path, "connect.attachment.invoke", invocation_payload(selected))
    )

    assert response["ok"] is True
    assert runtime.store.connect_job(REQUEST_ID) is None
    assert runtime.store.connect_dispatch(REQUEST_ID) is None


def test_generic_invoke_maps_provider_lane_capacity_without_submitting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected = capability()

    class FakeGmail:
        def attachment_bytes(self, *args) -> bytes:
            return PDF

    def reject_full_lane(**values):
        raise ConnectQueueFull("Connect provider queue is full")

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities",
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: FakeGmail())
    monkeypatch.setattr(runtime.store, "create_connect_job", reject_full_lane)
    monkeypatch.setattr(
        engine_api.connect,
        "ConnectV2Client",
        lambda *_args: pytest.fail("A full provider lane must not reach transport"),
    )

    response = engine_api._response(
        api_request(
            config_path,
            "connect.attachment.invoke",
            invocation_payload(selected),
        )
    )

    assert response["error"] == {
        "code": "connect_queue_full",
        "message": "The selected provider already has the maximum number of queued jobs.",
    }


def test_generic_invoke_rejects_unsupported_lock_before_enqueue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected = capability()

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities",
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api, "operation_lock_supported", lambda _path: False)
    monkeypatch.setattr(
        engine_api.GmailGateway,
        "from_token",
        lambda *_args: pytest.fail("Unsupported locking must fail before mailbox access"),
    )
    monkeypatch.setattr(
        engine_api.connect,
        "ConnectV2Client",
        lambda *_args: pytest.fail("Unsupported locking must fail before provider access"),
    )

    response = engine_api._response(
        api_request(
            config_path,
            "connect.attachment.invoke",
            invocation_payload(selected),
        )
    )

    assert response["error"]["code"] == "connect_queue_unavailable"
    assert runtime.store.connect_job(REQUEST_ID) is None
    assert runtime.store.connect_dispatch(REQUEST_ID) is None


def test_generic_invoke_rejects_retention_expired_source_before_enqueue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected = capability()
    expired_at = datetime.now(UTC) - timedelta(days=runtime.config.retention_days + 1)
    with runtime.store.connection() as db:
        db.execute(
            "UPDATE messages SET received_at = ? WHERE message_id = ?",
            (expired_at.isoformat(), "message-1"),
        )

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities",
        lambda **kwargs: pytest.fail("Expired source must fail before provider discovery"),
    )
    monkeypatch.setattr(
        engine_api.GmailGateway,
        "from_token",
        lambda *_args: pytest.fail("Expired source must fail before mailbox access"),
    )
    monkeypatch.setattr(
        engine_api.connect,
        "ConnectV2Client",
        lambda *_args: pytest.fail("Expired source must fail before provider access"),
    )

    response = engine_api._response(
        api_request(
            config_path,
            "connect.attachment.invoke",
            invocation_payload(selected),
        )
    )

    assert response["error"]["code"] == "connect_source_unavailable"
    assert runtime.store.connect_job(REQUEST_ID) is None


def test_nonterminal_get_error_preserves_reconciliation_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected = capability()
    submissions = 0
    queries = 0

    class FakeGmail:
        def attachment_bytes(self, *args) -> bytes:
            return PDF

    class InconclusiveClient:
        def __init__(self, capability_value):
            assert capability_value == selected

        def submit(self, job, content):
            nonlocal submissions
            submissions += 1
            raise connect.ConnectError(
                "PROVIDER_UNAVAILABLE",
                "The provider response was lost.",
                retryable=True,
            )

        def get(self, job):
            nonlocal queries
            queries += 1
            raise connect.ConnectError(
                "PROVIDER_AUTHENTICATION_FAILED",
                "The provider could not authenticate this reconciliation.",
                retryable=False,
            )

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities",
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api.connect, "ConnectV2Client", InconclusiveClient)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: FakeGmail())
    request = api_request(
        config_path,
        "connect.attachment.invoke",
        invocation_payload(selected),
    )

    first = engine_api._response(request)
    make_connect_job_due(runtime, REQUEST_ID)
    second = engine_api._response(request)

    assert_active_response(first, status="requested", dispatch_state="reconciling")
    assert_active_response(second, status="requested", dispatch_state="reconciling")
    assert submissions == 1
    assert queries == 1
    assert runtime.store.connect_job(REQUEST_ID).status == "requested"  # type: ignore[union-attr]
    dispatch = runtime.store.connect_dispatch(REQUEST_ID)
    assert dispatch is not None
    assert dispatch.state == "reconciling"
    assert dispatch.submission_possible is True


def test_distinct_request_ids_reuse_the_same_active_logical_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected = capability(external_effects=True, confirmation_required=True)
    submissions: list[str] = []
    queries: list[str] = []

    class FakeGmail:
        def attachment_bytes(self, *args) -> bytes:
            return PDF

    class AmbiguousClient:
        def __init__(self, capability_value):
            assert capability_value == selected

        def submit(self, job, content):
            assert content == PDF
            submissions.append(job.job_id)
            raise connect.ConnectError(
                "PROVIDER_UNAVAILABLE",
                "The provider response was lost.",
                retryable=True,
            )

        def get(self, job):
            queries.append(job.job_id)
            return update(job, "completed", payload=b"Recovered")

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities",
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api.connect, "ConnectV2Client", AmbiguousClient)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: FakeGmail())

    first = engine_api._response(
        api_request(
            config_path,
            "connect.attachment.invoke",
            invocation_payload(selected, confirmed=True, request_id=REQUEST_ID),
        )
    )
    make_connect_job_due(runtime, REQUEST_ID)
    second = engine_api._response(
        api_request(
            config_path,
            "connect.attachment.invoke",
            invocation_payload(selected, confirmed=True, request_id=SECOND_REQUEST_ID),
        )
    )

    assert_active_response(first, status="requested", dispatch_state="reconciling")
    assert second["ok"] is True
    assert second["data"]["job_id"] == REQUEST_ID
    assert submissions == [REQUEST_ID]
    assert queries == [REQUEST_ID]
    with runtime.store.connection() as db:
        rows = db.execute(
            "SELECT job_id, status FROM connect_attachment_jobs ORDER BY job_id"
        ).fetchall()
    assert [tuple(row) for row in rows] == [(REQUEST_ID, "completed")]


def test_active_request_reconciles_without_gmail_and_tolerates_transition_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected = capability()
    gmail_reads = 0
    submissions = 0
    queries = 0
    raced = False
    real_transition = runtime.store.transition_connect_job

    class FakeGmail:
        def attachment_bytes(self, *args) -> bytes:
            nonlocal gmail_reads
            gmail_reads += 1
            return PDF

    class ReconcilingClient:
        def __init__(self, capability_value):
            assert capability_value == selected

        def submit(self, job, content):
            nonlocal submissions
            submissions += 1
            raise connect.ConnectError(
                "PROVIDER_UNAVAILABLE",
                "The provider response was lost.",
                retryable=True,
            )

        def get(self, job):
            nonlocal queries
            queries += 1
            return update(job, "completed", payload=b"Already completed")

        def wait_for_terminal(self, job, initial, on_update):
            return initial

    def racing_transition(**values):
        nonlocal raced
        if not raced and values["next_state"] == "completed":
            raced = True
            real_transition(**values)
            raise RuntimeError("simulated competing poller won the transition")
        return real_transition(**values)

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities",
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api.connect, "ConnectV2Client", ReconcilingClient)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: FakeGmail())
    monkeypatch.setattr(runtime.store, "transition_connect_job", racing_transition)
    request = api_request(
        config_path,
        "connect.attachment.invoke",
        invocation_payload(selected),
    )

    first = engine_api._response(request)
    make_connect_job_due(runtime, REQUEST_ID)
    second = engine_api._response(request)

    assert_active_response(first, status="requested", dispatch_state="reconciling")
    assert second["ok"] is True
    assert second["data"]["job_id"] == REQUEST_ID
    assert submissions == 1
    assert queries == 1
    assert gmail_reads == 2
    assert raced is True


def test_reconciliation_returns_a_terminal_row_won_by_another_poller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected = capability()
    submitted: list[connect.PreparedCapabilityJob] = []
    wait_calls = 0

    class FakeGmail:
        def attachment_bytes(self, *args) -> bytes:
            return PDF

    class RacingClient:
        def __init__(self, capability_value):
            assert capability_value == selected

        def submit(self, job, content):
            assert content == PDF
            submitted.append(job)
            raise connect.ConnectError(
                "PROVIDER_UNAVAILABLE",
                "The provider response was lost.",
                retryable=True,
            )

        def get(self, job):
            return update(job, "accepted")

        def wait_for_terminal(self, job, initial, on_update):
            nonlocal wait_calls
            wait_calls += 1
            raise AssertionError("a durable terminal row must stop provider polling")

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities",
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api.connect, "ConnectV2Client", RacingClient)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: FakeGmail())
    request = api_request(
        config_path,
        "connect.attachment.invoke",
        invocation_payload(selected),
    )
    first = engine_api._response(request)
    real_transition = runtime.store.transition_connect_job
    raced = False

    def racing_transition(**values):
        nonlocal raced
        if not raced and values["next_state"] == "accepted":
            raced = True
            terminal = update(submitted[0], "completed", payload=b"Already complete")
            assert terminal.result is not None
            real_transition(
                job_id=submitted[0].job_id,
                expected_state="requested",
                next_state="completed",
                provider_app_id=selected.app_id,
                provider_instance_id=selected.instance_id,
                result=terminal.result.store_dict(),
            )
            raise RuntimeError("simulated competing poller completed the job")
        return real_transition(**values)

    monkeypatch.setattr(runtime.store, "transition_connect_job", racing_transition)
    make_connect_job_due(runtime, REQUEST_ID)
    second = engine_api._response(request)

    assert_active_response(first, status="requested", dispatch_state="reconciling")
    assert second["ok"] is True
    assert second["data"]["job_id"] == REQUEST_ID
    assert raced is True
    assert wait_calls == 0


def test_concurrent_same_request_id_reuses_the_persisted_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected = capability()
    real_create = runtime.store.create_connect_job
    queried: list[str] = []

    class FakeGmail:
        def attachment_bytes(self, *args) -> bytes:
            return PDF

    class WinnerClient:
        def __init__(self, capability_value):
            assert capability_value == selected

        def get(self, job):
            queried.append(job.job_id)
            return update(job, "completed", payload=b"Winner")

        def submit(self, job, content):
            raise AssertionError("the losing click must not submit its candidate identity")

        def wait_for_terminal(self, job, initial, on_update):
            return initial

    def race_create(**values):
        real_create(**values)
        real_create(**values)

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities",
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api.connect, "ConnectV2Client", WinnerClient)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: FakeGmail())
    monkeypatch.setattr(runtime.store, "create_connect_job", race_create)

    response = engine_api._response(
        api_request(
            config_path,
            "connect.attachment.invoke",
            invocation_payload(selected),
        )
    )

    assert response["ok"] is True
    assert response["data"]["job_id"] == REQUEST_ID
    assert queried == [REQUEST_ID]
    with runtime.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM connect_attachment_jobs").fetchone()[0] == 1
