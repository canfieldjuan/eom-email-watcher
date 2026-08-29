import hashlib
import json
from pathlib import Path

import pytest

from eom_email_watcher import connect, engine_api
from eom_email_watcher.mime import AttachmentDescriptor
from eom_email_watcher.runtime import load_runtime

INSTANCE = "11111111-1111-4111-8111-111111111111"


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


def api_request(config_path: Path, operation: str, payload: dict[str, object] | None = None):
    return {
        "protocol": 1,
        "operation": operation,
        "config_path": str(config_path),
        "payload": payload or {},
    }


def seeded_runtime(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.store.add_message(
        message_id="message-1",
        thread_id=None,
        sender="private@example.com",
        sender_name="Private Sender",
        subject="Private subject",
        received_at="2026-08-29T12:00:00+00:00",
    )
    runtime.store.replace_attachments(
        "message-1",
        (
            AttachmentDescriptor(
                "2", "gmail-attachment", "invoice.pdf", "application/pdf", 28, 0
            ),
        ),
    )
    return config_path, runtime


def provider() -> connect.ProviderCapability:
    return connect.ProviderCapability(
        base_url="http://127.0.0.1:32123/",
        token="A" * 43,
        app_id="alternate-provider",
        instance_id=INSTANCE,
        max_input_bytes=1024,
    )


def update(
    job: connect.PreparedSummaryJob,
    status: str,
    *,
    result: connect.SummaryResult | None = None,
    error: connect.ConnectError | None = None,
) -> connect.JobUpdate:
    return connect.JobUpdate(
        job_id=job.job_id,
        status=status,
        provider_app_id="alternate-provider",
        provider_instance_id=INSTANCE,
        result=result,
        error=error,
    )


def summary(job: connect.PreparedSummaryJob) -> connect.SummaryResult:
    warnings = ({"code": "REVIEW", "message": "Human review required."},)
    content = {
        "summary_version": "1.0",
        "text": "Revenue was $1,247,392.17. José Álvarez shall NOT be liable.",
        "warnings": [dict(warning) for warning in warnings],
        "input_artifact": job.artifact.public_dict(),
    }
    encoded = json.dumps(content, separators=(",", ":"), ensure_ascii=False).encode()
    return connect.SummaryResult(
        artifact_id="22222222-2222-4222-8222-222222222222",
        media_type="application/vnd.local-connect.document-summary+json",
        byte_size=len(encoded),
        sha256=hashlib.sha256(encoded).hexdigest(),
        summary_version="1.0",
        text="Revenue was $1,247,392.17. José Álvarez shall NOT be liable.",
        warnings=warnings,
    )


def test_attachment_summary_persists_terminal_result_and_reuses_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    pdf = b"%PDF-1.4\nreal attachment\nEOF"
    assert len(pdf) == 28
    captured: dict[str, object] = {}

    class FakeGmail:
        def attachment_bytes(self, message_id: str, part_id: str, attachment_id: str) -> bytes:
            assert (message_id, part_id, attachment_id) == (
                "message-1",
                "2",
                "gmail-attachment",
            )
            return pdf

    class FakeClient:
        def __init__(self, selected: connect.ProviderCapability):
            assert selected.app_id == "alternate-provider"

        def submit(
            self, job: connect.PreparedSummaryJob, content: bytes
        ) -> connect.JobUpdate:
            captured["request"] = job.request
            captured["content"] = content
            return update(job, "accepted")

        def wait_for_terminal(self, job, initial, on_update):
            assert initial.status == "accepted"
            on_update(update(job, "processing"))
            completed = update(job, "completed", result=summary(job))
            on_update(completed)
            return completed

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_summary_capability",
        lambda: connect.CapabilityDiscovery(provider()),
    )
    monkeypatch.setattr(engine_api.connect, "ConnectClient", FakeClient)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: FakeGmail())

    response = engine_api._response(
        api_request(
            config_path,
            "connect.attachment.summarize",
            {"message_id": "message-1", "part_id": "2"},
        )
    )

    assert response["ok"] is True
    assert response["data"]["status"] == "completed"
    assert response["data"]["summary"] == {
        "summary_version": "1.0",
        "text": "Revenue was $1,247,392.17. José Álvarez shall NOT be liable.",
        "warnings": [{"code": "REVIEW", "message": "Human review required."}],
    }
    assert captured["content"] == pdf
    request_input = captured["request"]["inputs"][0]  # type: ignore[index]
    assert set(request_input) == {
        "artifact_id",
        "media_type",
        "byte_size",
        "sha256",
        "display_name",
        "source_app_id",
    }
    stored = runtime.store.completed_connect_job(
        message_id="message-1",
        part_id="2",
        capability_id="document.summarize",
        capability_version="1.0",
    )
    assert stored is not None
    assert stored.summary_text == response["data"]["summary"]["text"]

    monkeypatch.setattr(
        engine_api.connect,
        "discover_summary_capability",
        lambda: (_ for _ in ()).throw(AssertionError("completed result must be reused")),
    )
    monkeypatch.setattr(
        engine_api.GmailGateway,
        "from_token",
        lambda *args: (_ for _ in ()).throw(AssertionError("Gmail must not be fetched twice")),
    )
    reused = engine_api._response(
        api_request(
            config_path,
            "connect.attachment.summarize",
            {"message_id": "message-1", "part_id": "2"},
        )
    )
    assert reused == response


def test_provider_failure_is_durable_and_never_masquerades_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    pdf = b"%PDF-1.4\nreal attachment\nEOF"

    class FakeGmail:
        def attachment_bytes(self, *args) -> bytes:
            return pdf

    class FailingClient:
        def __init__(self, selected):
            pass

        def submit(self, job, content):
            raise connect.ConnectError("PDF_MALFORMED", "The PDF could not be parsed.")

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_summary_capability",
        lambda: connect.CapabilityDiscovery(provider()),
    )
    monkeypatch.setattr(engine_api.connect, "ConnectClient", FailingClient)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: FakeGmail())

    response = engine_api._response(
        api_request(
            config_path,
            "connect.attachment.summarize",
            {"message_id": "message-1", "part_id": "2"},
        )
    )

    assert response["ok"] is False
    assert response["error"] == {
        "code": "pdf_malformed",
        "message": "The PDF could not be parsed.",
    }
    jobs = runtime.store.recent(1)[0]["attachments"][0]["capability_results"]
    assert jobs == [
        {
            "capability_id": "document.summarize",
            "capability_version": "1.0",
            "status": "failed",
            "updated_at": jobs[0]["updated_at"],
            "error": {
                "code": "PDF_MALFORMED",
                "message": "The PDF could not be parsed.",
                "retryable": False,
            },
        }
    ]
    assert runtime.store.completed_connect_job(
        message_id="message-1",
        part_id="2",
        capability_id="document.summarize",
        capability_version="1.0",
    ) is None


def test_provider_crash_after_acceptance_is_persisted_as_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    pdf = b"%PDF-1.4\nreal attachment\nEOF"

    class FakeGmail:
        def attachment_bytes(self, *args) -> bytes:
            return pdf

    class CrashingClient:
        def __init__(self, selected):
            pass

        def submit(self, job, content):
            return update(job, "accepted")

        def wait_for_terminal(self, job, initial, on_update):
            raise connect.ConnectError(
                "PROVIDER_UNAVAILABLE",
                "The local capability provider became unavailable.",
                retryable=True,
            )

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_summary_capability",
        lambda: connect.CapabilityDiscovery(provider()),
    )
    monkeypatch.setattr(engine_api.connect, "ConnectClient", CrashingClient)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: FakeGmail())

    response = engine_api._response(
        api_request(
            config_path,
            "connect.attachment.summarize",
            {"message_id": "message-1", "part_id": "2"},
        )
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "provider_unavailable"
    active = runtime.store.active_connect_job(
        message_id="message-1",
        part_id="2",
        capability_id="document.summarize",
        capability_version="1.0",
    )
    assert active is None
    persisted = runtime.store.recent(1)[0]["attachments"][0]["capability_results"][0]
    assert persisted["status"] == "failed"
    assert persisted["error"]["retryable"] is True


def test_simultaneous_submit_returns_structured_in_progress_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    pdf = b"%PDF-1.4\nreal attachment\nEOF"
    real_create = runtime.store.create_connect_job

    class FakeGmail:
        def attachment_bytes(self, *args) -> bytes:
            return pdf

    def race_create(**values) -> None:
        concurrent = dict(values)
        concurrent.update(
            {
                "job_id": "99999999-9999-4999-8999-999999999999",
                "input_artifact_id": "88888888-8888-4888-8888-888888888888",
            }
        )
        real_create(**concurrent)
        real_create(**values)

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_summary_capability",
        lambda: connect.CapabilityDiscovery(provider()),
    )
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: FakeGmail())
    monkeypatch.setattr(runtime.store, "create_connect_job", race_create)

    response = engine_api._response(
        api_request(
            config_path,
            "connect.attachment.summarize",
            {"message_id": "message-1", "part_id": "2"},
        )
    )

    assert response == {
        "protocol": 1,
        "ok": False,
        "operation": "connect.attachment.summarize",
        "error": {
            "code": "connect_job_in_progress",
            "message": "A summary job is already in progress for this attachment.",
        },
    }
    active = runtime.store.active_connect_job(
        message_id="message-1",
        part_id="2",
        capability_id="document.summarize",
        capability_version="1.0",
    )
    assert active is not None
    assert active.status == "requested"


def test_no_provider_keeps_inbox_available_and_returns_no_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_summary_capability",
        lambda: connect.CapabilityDiscovery(None, "provider_unavailable"),
    )

    capabilities = engine_api._response(api_request(config_path, "connect.capabilities"))
    inbox = engine_api._response(api_request(config_path, "inbox.recent"))

    assert capabilities["data"] == {"items": [], "diagnostic": None}
    assert inbox["ok"] is True
    assert inbox["data"]["items"][0]["attachments"][0]["filename"] == "invoice.pdf"
