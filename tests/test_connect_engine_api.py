import hashlib
import json
from pathlib import Path

import pytest

from eom_email_watcher import connect, engine_api
from eom_email_watcher.mime import AttachmentDescriptor
from eom_email_watcher.runtime import load_runtime

INSTANCE = "11111111-1111-4111-8111-111111111111"


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
        lambda **kwargs: connect.CapabilityDiscovery(provider()),
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
        lambda **kwargs: connect.CapabilityDiscovery(provider()),
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
            "job_id": jobs[0]["job_id"],
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


def test_provider_crash_after_acceptance_remains_active_for_reconciliation(
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
    assert active is not None
    assert active.status == "accepted"
    persisted = runtime.store.recent(1)[0]["attachments"][0]["capability_results"][0]
    assert persisted["status"] == "accepted"
    assert "error" not in persisted


def test_lost_submit_ack_reuses_identity_and_resubmits_only_after_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    pdf = b"%PDF-1.4\nreal attachment\nEOF"
    submitted: list[connect.PreparedSummaryJob] = []
    queried: list[str] = []

    class FakeGmail:
        def attachment_bytes(self, *args) -> bytes:
            return pdf

    class RecoveringClient:
        def __init__(self, selected):
            pass

        def submit(self, job, content):
            assert content == pdf
            submitted.append(job)
            if len(submitted) == 1:
                raise connect.ConnectError(
                    "PROVIDER_UNAVAILABLE",
                    "The provider response was lost.",
                    retryable=True,
                )
            return update(job, "completed", result=summary(job))

        def get(self, job):
            queried.append(job.job_id)
            raise connect.ConnectError(
                "JOB_NOT_FOUND",
                "The provider did not accept this job.",
            )

        def wait_for_terminal(self, job, initial, on_update):
            return initial

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_summary_capability",
        lambda **kwargs: connect.CapabilityDiscovery(provider()),
    )
    monkeypatch.setattr(engine_api.connect, "ConnectClient", RecoveringClient)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: FakeGmail())

    first = engine_api._response(
        api_request(
            config_path,
            "connect.attachment.summarize",
            {"message_id": "message-1", "part_id": "2"},
        )
    )
    active = runtime.store.active_connect_job(
        message_id="message-1",
        part_id="2",
        capability_id="document.summarize",
        capability_version="1.0",
    )

    assert first["error"]["code"] == "provider_unavailable"
    assert active is not None
    assert active.status == "requested"

    second = engine_api._response(
        api_request(
            config_path,
            "connect.attachment.summarize",
            {"message_id": "message-1", "part_id": "2"},
        )
    )

    assert second["ok"] is True
    assert queried == [active.job_id]
    assert [job.job_id for job in submitted] == [active.job_id, active.job_id]
    assert submitted[0].artifact == submitted[1].artifact
    assert submitted[0].request == submitted[1].request


def test_active_job_is_not_handed_to_a_different_provider_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    original = provider()
    runtime.store.create_connect_job(
        job_id="33333333-3333-4333-8333-333333333333",
        message_id="message-1",
        part_id="2",
        capability_id="document.summarize",
        capability_version="1.0",
        provider_app_id=original.app_id,
        provider_instance_id=original.instance_id,
        input_artifact_id="22222222-2222-4222-8222-222222222222",
        input_media_type="application/pdf",
        input_byte_size=28,
        input_sha256="a" * 64,
    )
    replacement = connect.ProviderCapability(
        base_url=original.base_url,
        token=original.token,
        app_id=original.app_id,
        instance_id="99999999-9999-4999-8999-999999999999",
        max_input_bytes=original.max_input_bytes,
    )

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_summary_capability",
        lambda **kwargs: connect.CapabilityDiscovery(replacement),
    )
    monkeypatch.setattr(
        engine_api.connect,
        "ConnectClient",
        lambda provider: (_ for _ in ()).throw(
            AssertionError("replacement provider must not receive the active job")
        ),
    )

    response = engine_api._response(
        api_request(
            config_path,
            "connect.attachment.summarize",
            {"message_id": "message-1", "part_id": "2"},
        )
    )

    assert response["error"] == {
        "code": "provider_unavailable",
        "message": "The provider for the active local capability job is unavailable.",
    }
    assert runtime.store.connect_job("33333333-3333-4333-8333-333333333333").status == (
        "requested"
    )


def test_poll_not_found_reloads_processing_state_before_same_identity_resubmission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected = provider()
    job_id = "33333333-3333-4333-8333-333333333333"
    runtime.store.create_connect_job(
        job_id=job_id,
        message_id="message-1",
        part_id="2",
        capability_id="document.summarize",
        capability_version="1.0",
        provider_app_id=selected.app_id,
        provider_instance_id=selected.instance_id,
        input_artifact_id="22222222-2222-4222-8222-222222222222",
        input_media_type="application/pdf",
        input_byte_size=28,
        input_sha256=hashlib.sha256(b"%PDF-1.4\nreal attachment\nEOF").hexdigest(),
    )
    discovered_instances: list[str | None] = []
    submitted: list[str] = []

    class FakeGmail:
        def attachment_bytes(self, *args) -> bytes:
            return b"%PDF-1.4\nreal attachment\nEOF"

    class RecoveringClient:
        def __init__(self, provider_value):
            assert provider_value == selected

        def get(self, job):
            return update(job, "accepted")

        def submit(self, job, content):
            assert runtime.store.connect_job(job_id).status == "requested"
            submitted.append(job.job_id)
            return update(job, "accepted")

        def wait_for_terminal(self, job, initial, on_update):
            assert initial.status == "accepted"
            if not submitted:
                on_update(update(job, "processing"))
                raise connect.ConnectError(
                    "JOB_NOT_FOUND", "The provider lost the accepted job."
                )
            completed = update(job, "completed", result=summary(job))
            on_update(completed)
            return completed

    def discover(**kwargs):
        discovered_instances.append(kwargs.get("provider_instance_id"))
        return connect.CapabilityDiscovery(selected)

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(engine_api.connect, "discover_summary_capability", discover)
    monkeypatch.setattr(engine_api.connect, "ConnectClient", RecoveringClient)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: FakeGmail())

    response = engine_api._response(
        api_request(
            config_path,
            "connect.attachment.summarize",
            {"message_id": "message-1", "part_id": "2"},
        )
    )

    assert response["ok"] is True
    assert response["data"]["job_id"] == job_id
    assert discovered_instances == [selected.instance_id]
    assert submitted == [job_id]
    assert runtime.store.connect_job(job_id).status == "completed"


def test_terminal_job_not_found_failure_is_not_treated_as_resubmission_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected = provider()
    job_id = "33333333-3333-4333-8333-333333333333"
    runtime.store.create_connect_job(
        job_id=job_id,
        message_id="message-1",
        part_id="2",
        capability_id="document.summarize",
        capability_version="1.0",
        provider_app_id=selected.app_id,
        provider_instance_id=selected.instance_id,
        input_artifact_id="22222222-2222-4222-8222-222222222222",
        input_media_type="application/pdf",
        input_byte_size=28,
        input_sha256="a" * 64,
    )

    class TerminalClient:
        def __init__(self, provider_value):
            assert provider_value == selected

        def get(self, job):
            return update(
                job,
                "failed",
                error=connect.ConnectError("JOB_NOT_FOUND", "Document was rejected."),
            )

        def wait_for_terminal(self, job, initial, on_update):
            return initial

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_summary_capability",
        lambda **kwargs: connect.CapabilityDiscovery(selected),
    )
    monkeypatch.setattr(engine_api.connect, "ConnectClient", TerminalClient)
    monkeypatch.setattr(
        engine_api.GmailGateway,
        "from_token",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("terminal failure must not refetch Gmail")
        ),
    )

    response = engine_api._response(
        api_request(
            config_path,
            "connect.attachment.summarize",
            {"message_id": "message-1", "part_id": "2"},
        )
    )

    assert response["error"] == {
        "code": "job_not_found",
        "message": "Document was rejected.",
    }
    assert runtime.store.connect_job(job_id).status == "failed"


def test_active_job_is_queried_before_a_lower_current_input_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected = connect.ProviderCapability(
        base_url=provider().base_url,
        token=provider().token,
        app_id=provider().app_id,
        instance_id=provider().instance_id,
        max_input_bytes=1,
    )
    job_id = "33333333-3333-4333-8333-333333333333"
    artifact_id = "22222222-2222-4222-8222-222222222222"
    runtime.store.create_connect_job(
        job_id=job_id,
        message_id="message-1",
        part_id="2",
        capability_id="document.summarize",
        capability_version="1.0",
        provider_app_id=selected.app_id,
        provider_instance_id=selected.instance_id,
        input_artifact_id=artifact_id,
        input_media_type="application/pdf",
        input_byte_size=28,
        input_sha256="a" * 64,
    )

    class CompletedClient:
        def __init__(self, provider_value):
            assert provider_value == selected

        def get(self, job):
            return update(job, "completed", result=summary(job))

        def wait_for_terminal(self, job, initial, on_update):
            return initial

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_summary_capability",
        lambda **kwargs: connect.CapabilityDiscovery(selected),
    )
    monkeypatch.setattr(engine_api.connect, "ConnectClient", CompletedClient)
    monkeypatch.setattr(
        engine_api.GmailGateway,
        "from_token",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("accepted job must be queried before any new upload")
        ),
    )

    response = engine_api._response(
        api_request(
            config_path,
            "connect.attachment.summarize",
            {"message_id": "message-1", "part_id": "2"},
        )
    )

    assert response["ok"] is True
    assert response["data"]["job_id"] == job_id


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
