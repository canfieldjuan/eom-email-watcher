import hashlib
from pathlib import Path

import pytest

from eom_email_watcher import connect, engine_api
from eom_email_watcher.mime import AttachmentDescriptor
from eom_email_watcher.runtime import load_runtime

INSTANCE_A = "11111111-1111-4111-8111-111111111111"
INSTANCE_B = "22222222-2222-4222-8222-222222222222"
OUTPUT_ID = "33333333-3333-4333-8333-333333333333"
RACE_JOB_ID = "44444444-4444-4444-8444-444444444444"
RACE_ARTIFACT_ID = "55555555-5555-4555-8555-555555555555"
REQUEST_ID = "66666666-6666-4666-8666-666666666666"
SECOND_REQUEST_ID = "77777777-7777-4777-8777-777777777777"
TOKEN = "A" * 43
PDF = b"%PDF-1.4\nreal attachment\nEOF"


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


def capability(
    *,
    app_id: str = "generic-provider",
    app_version: str = "1.2.3",
    instance_id: str = INSTANCE_A,
    capability_id: str = "document.translate",
    media_type: str = "application/pdf",
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
        produces=("text/plain",),
        parameters=parameters,
        external_effects=external_effects,
        confirmation_required=confirmation_required,
    )


def invocation_payload(
    selected: connect.DiscoveredCapability,
    *,
    parameters: dict[str, object] | None = None,
    confirmed: bool = False,
    request_id: str = REQUEST_ID,
) -> dict[str, object]:
    return {
        "request_id": request_id,
        "message_id": "message-1",
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

    def discover(**kwargs):
        discovered_instances.append(kwargs.get("provider_instance_id"))
        return connect.CapabilityCatalog((first, selected))

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(engine_api.connect, "discover_capabilities", discover)
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
            persisted = runtime.store.connect_job(job.job_id)
            assert persisted is not None
            assert persisted.status == "requested"
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
    assert discovered_instances == [INSTANCE_B] * 5
    assert [job.job_id for job in submitted] == [REQUEST_ID, SECOND_REQUEST_ID]
    assert [dict(job.parameters) for job in submitted] == [
        {"target-language": "es"},
        {"target-language": "es"},
    ]
    assert gmail_reads == 2


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
    second = engine_api._response(request)

    assert first["error"]["code"] == "provider_unavailable"
    assert durable is not None
    assert durable.status == "requested"
    assert second["ok"] is True
    assert queries == [durable.job_id]
    assert submissions == [
        (durable.job_id, durable.request_json),
        (durable.job_id, durable.request_json),
    ]
    assert gmail_reads == 2
    with runtime.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM connect_attachment_jobs").fetchone()[0] == 1


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
    second = engine_api._response(request)

    assert first["error"]["code"] == "provider_unavailable"
    assert second["ok"] is True
    assert second["data"]["job_id"] == REQUEST_ID
    assert submissions == 1
    assert queries == 1
    assert gmail_reads == 1
    assert raced is True


def test_concurrent_identical_click_reuses_the_persisted_winner(
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
        winner = connect.restore_capability_job(
            selected,
            job_id=RACE_JOB_ID,
            artifact_id=RACE_ARTIFACT_ID,
            media_type="application/pdf",
            byte_size=len(PDF),
            sha256=hashlib.sha256(PDF).hexdigest(),
            filename="invoice.pdf",
        )
        real_create(
            **{
                **values,
                "job_id": winner.job_id,
                "input_artifact_id": winner.artifact.artifact_id,
                "request_json": winner.request_json,
            }
        )
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
    assert response["data"]["job_id"] == RACE_JOB_ID
    assert queried == [RACE_JOB_ID]
    with runtime.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM connect_attachment_jobs").fetchone()[0] == 1
