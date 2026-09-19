import hashlib
import json
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from connect_automate import connect

from eom_email_watcher import engine_api
from eom_email_watcher.db import ConnectQueueFull, MessageSource
from eom_email_watcher.imap import MAX_MESSAGE_BYTES as MAX_IMAP_MESSAGE_BYTES
from eom_email_watcher.mailbox import (
    DEFAULT_MAIL_ACCOUNT_ID,
    DEFAULT_MAIL_PROVIDER,
    MailboxError,
    MailboxMessageInvalid,
    MailboxMessageUnavailable,
    MailboxSession,
)
from eom_email_watcher.mime import AttachmentDescriptor
from eom_email_watcher.runtime import Runtime, load_runtime, mail_account_token_file

INSTANCE_A = "11111111-1111-4111-8111-111111111111"
INSTANCE_B = "22222222-2222-4222-8222-222222222222"
OUTPUT_ID = "33333333-3333-4333-8333-333333333333"
INPUT_ARTIFACT_ID = "55555555-5555-4555-8555-555555555555"
REQUEST_ID = "66666666-6666-4666-8666-666666666666"
SECOND_REQUEST_ID = "77777777-7777-4777-8777-777777777777"
TOKEN = "A" * 43
TEST_MAILBOX_IDENTITY_KEY = "a" * 64
PDF = b"%PDF-1.4\nreal attachment\nEOF"
LOCK_HOLDER = """
import sys
from pathlib import Path

from connect_automate.locking import connect_operation_lock

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


class SeededMailboxGateway:
    def mailbox_identity_key(self) -> str:
        return TEST_MAILBOX_IDENTITY_KEY


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


def force_unaccepted_admission_deadline(runtime: Runtime, job_id: str) -> None:
    with runtime.store.connection() as db:
        db.execute(
            """UPDATE connect_job_dispatch
            SET state = 'waiting', submission_possible = 0,
                admission_deadline = ?, next_attempt_at = ?
            WHERE job_id = ?""",
            (
                "2000-01-01T00:00:00+00:00",
                "2000-01-01T00:00:00+00:00",
                job_id,
            ),
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


def test_connect_queue_pump_materializes_matching_automation_fire_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    mode = connect.CapabilityParameter(
        name="mode",
        value_type="string",
        required=False,
        label="Summary mode",
        description="Choose general, story, or contract. Defaults to general.",
    )
    selected = capability(
        app_id="document-summarizer",
        app_version="0.1.0",
        capability_id="document.summarize",
        parameters=(mode,),
    )
    runtime.store.put_automation_rule(
        {
            "name": "Contract watch",
            "scope": {},
            "trigger": {"source_kind": "mail.message"},
            "conditions": [
                {
                    "field": "attachment.media_type",
                    "op": "equals",
                    "value": "application/pdf",
                }
            ],
            "action": {
                "kind": "connect.invoke",
                "capability": {"id": "document.summarize", "version": "1.0"},
                "provider": {
                    "app_id": selected.app_id,
                    "version": selected.app_version,
                    "instance_id": selected.instance_id,
                },
                "parameters": {"mode": "contract"},
            },
            "confirm_each": False,
        }
    )
    runtime.store.mark_analyzed(
        "message-1",
        {
            "category": "informational",
            "priority": "normal",
            "summary": "A contract arrived.",
            "action_required": True,
            "suggested_action": "Review the contract.",
            "deadline_text": None,
            "deadline_iso": None,
            "confidence": 0.9,
        },
        mailbox_identity_key=TEST_MAILBOX_IDENTITY_KEY,
    )
    fire = runtime.store.automation_fires_for_message("message-1")[0]
    attempt = runtime.store.automation_fire_attempts(fire.fire_id)[0]

    class FakeGmail(SeededMailboxGateway):
        def set_operation_timeout(self, timeout_seconds: float) -> None:
            pass

        def attachment_bytes(self, *args: object) -> bytes:
            return PDF

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities",
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: FakeGmail())
    monkeypatch.setattr(
        engine_api,
        "_pump_generic_connect_lane",
        lambda active_runtime, head: engine_api._connect_queue_item(
            active_runtime, head.job_id, "queued"
        ),
    )

    response = engine_api._response(api_request(config_path, "connect.queue.pump"))

    assert response["ok"] is True
    job = runtime.store.connect_job(attempt.dispatch_request_id)
    assert job is not None
    assert job.message_id == "message-1"
    assert job.part_id == "2"
    assert job.capability_id == "document.summarize"
    assert job.provider_instance_id == selected.instance_id
    assert runtime.store.connect_job_parameters(job) == {"mode": "contract"}
    settled = runtime.store.automation_fires_for_message("message-1")[0]
    assert settled.state == "submitted"
    assert settled.job_id == job.job_id

    engine_api._response(api_request(config_path, "connect.queue.pump"))
    repeated_attempts = runtime.store.automation_fire_attempts(fire.fire_id)
    assert len(repeated_attempts) == 1
    assert repeated_attempts[0].dispatch_request_id == attempt.dispatch_request_id
    assert repeated_attempts[0].job_id == job.job_id
    with runtime.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM connect_attachment_jobs").fetchone()[0] == 1


def test_source_delete_recovers_provider_owned_unbound_automation_fire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, runtime = seeded_runtime(tmp_path)
    selected, fire, attempt = seed_contract_fire(runtime)
    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    _candidate, created, _collision, _content = engine_api._prepare_or_create_generic_connect_job(
        runtime,
        request_id=attempt.dispatch_request_id,
        message_id=fire.message_id,
        part_id=fire.part_id,
        capability=selected,
        parameters={"mode": "contract"},
        confirmed=False,
        artifact_id=engine_api._automation_artifact_id(attempt.dispatch_request_id),
    )
    assert created is not None
    runtime.store.transition_connect_job(
        job_id=created.job_id,
        expected_state="requested",
        next_state="accepted",
        provider_app_id=selected.app_id,
        provider_instance_id=selected.instance_id,
    )
    with runtime.store.connection() as db:
        db.execute(
            """UPDATE connect_job_dispatch
            SET state = 'provider_owned', submission_possible = 1
            WHERE job_id = ?""",
            (created.job_id,),
        )
    assert runtime.store.automation_fire(fire.fire_id).job_id is None  # type: ignore[union-attr]

    assert runtime.store.delete_message(fire.message_id) is True

    retained = runtime.store.automation_fire(fire.fire_id)
    retained_attempt = runtime.store.automation_fire_attempts(fire.fire_id)
    assert retained is not None
    assert retained.state == "submitted"
    assert retained.job_id == created.job_id
    assert len(retained_attempt) == 1
    assert retained_attempt[0].job_id == created.job_id
    assert runtime.store.connect_job(created.job_id) is not None
    output = connect.CapabilityOutput(
        artifact_id=OUTPUT_ID,
        media_type="application/vnd.local-connect.cited-summary+json",
        display_name="contract-summary.json",
        byte_size=2,
        sha256=hashlib.sha256(b"{}").hexdigest(),
        payload=b"{}",
    )

    runtime.store.transition_connect_job(
        job_id=created.job_id,
        expected_state="accepted",
        next_state="completed",
        provider_app_id=selected.app_id,
        provider_instance_id=selected.instance_id,
        result=connect.CapabilityResult((output,)).store_dict(),
    )

    completed = runtime.store.automation_fire(fire.fire_id)
    assert completed is not None
    assert completed.state == "completed"
    assert runtime.store.connect_job(REQUEST_ID) is None
    assert completed.reason == "connect_completed"
    assert runtime.store.connect_job(created.job_id) is None


@pytest.mark.parametrize(
    ("terminal_status", "expected_reason"),
    (("completed", "connect_completed"), ("failed", "PDF_MALFORMED")),
)
def test_source_delete_preserves_unbound_terminal_automation_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal_status: str,
    expected_reason: str,
) -> None:
    _, runtime = seeded_runtime(tmp_path)
    selected, fire, attempt = seed_contract_fire(runtime)
    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    _candidate, created, _collision, _content = engine_api._prepare_or_create_generic_connect_job(
        runtime,
        request_id=attempt.dispatch_request_id,
        message_id=fire.message_id,
        part_id=fire.part_id,
        capability=selected,
        parameters={"mode": "contract"},
        confirmed=False,
        artifact_id=engine_api._automation_artifact_id(attempt.dispatch_request_id),
    )
    assert created is not None
    if terminal_status == "completed":
        output = connect.CapabilityOutput(
            artifact_id=OUTPUT_ID,
            media_type="application/vnd.local-connect.cited-summary+json",
            display_name="contract-summary.json",
            byte_size=2,
            sha256=hashlib.sha256(b"{}").hexdigest(),
            payload=b"{}",
        )
        runtime.store.transition_connect_job(
            job_id=created.job_id,
            expected_state="requested",
            next_state="completed",
            provider_app_id=selected.app_id,
            provider_instance_id=selected.instance_id,
            result=connect.CapabilityResult((output,)).store_dict(),
        )
    else:
        runtime.store.transition_connect_job(
            job_id=created.job_id,
            expected_state="requested",
            next_state="failed",
            provider_app_id=selected.app_id,
            provider_instance_id=selected.instance_id,
            error={"code": "PDF_MALFORMED", "message": "Invalid PDF", "retryable": False},
        )
    assert runtime.store.automation_fire(fire.fire_id).job_id is None  # type: ignore[union-attr]

    assert runtime.store.delete_message(fire.message_id) is True

    retained = runtime.store.automation_fire(fire.fire_id)
    retained_attempts = runtime.store.automation_fire_attempts(fire.fire_id)
    assert retained is not None
    assert retained.state == terminal_status
    assert retained.reason == expected_reason
    assert retained.job_id == created.job_id
    assert len(retained_attempts) == 1
    assert retained_attempts[0].job_id == created.job_id
    assert runtime.store.connect_job(created.job_id) is None


def test_automation_join_rejects_persisted_effectful_authority_after_manifest_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, runtime = seeded_runtime(tmp_path)
    selected = capability(external_effects=True)
    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    _candidate, created, _collision, _content = engine_api._prepare_or_create_generic_connect_job(
        runtime,
        request_id=REQUEST_ID,
        message_id="message-1",
        part_id="2",
        capability=selected,
        parameters={},
        confirmed=True,
        artifact_id=INPUT_ARTIFACT_ID,
    )
    assert created is not None
    drifted = capability(external_effects=False)

    with pytest.raises(engine_api.ApiError) as rejected:
        engine_api._prepare_or_create_generic_connect_job(
            runtime,
            request_id=SECOND_REQUEST_ID,
            message_id="message-1",
            part_id="2",
            capability=drifted,
            parameters={},
            confirmed=False,
            artifact_id=OUTPUT_ID,
            join_effectful=False,
        )

    assert rejected.value.code == "capability_authority_changed"
    failed = runtime.store.connect_job(created.job_id)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.error_code == "capability_authority_changed"


def test_automation_join_rejects_live_effect_escalation_after_manifest_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, runtime = seeded_runtime(tmp_path)
    admitted = capability(external_effects=False)
    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((admitted,)),
    )
    _candidate, created, _collision, _content = engine_api._prepare_or_create_generic_connect_job(
        runtime,
        request_id=REQUEST_ID,
        message_id="message-1",
        part_id="2",
        capability=admitted,
        parameters={},
        confirmed=False,
        artifact_id=INPUT_ARTIFACT_ID,
    )
    assert created is not None
    escalated = capability(external_effects=True)

    with pytest.raises(engine_api.ApiError) as rejected:
        engine_api._prepare_or_create_generic_connect_job(
            runtime,
            request_id=SECOND_REQUEST_ID,
            message_id="message-1",
            part_id="2",
            capability=escalated,
            parameters={},
            confirmed=True,
            artifact_id=OUTPUT_ID,
            join_effectful=False,
        )

    assert rejected.value.code == "capability_authority_changed"
    failed = runtime.store.connect_job(created.job_id)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.error_code == "capability_authority_changed"


def test_automation_join_rejects_live_output_contract_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, runtime = seeded_runtime(tmp_path)
    admitted = capability(produces=("text/plain",))
    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((admitted,)),
    )
    _candidate, created, _collision, _content = engine_api._prepare_or_create_generic_connect_job(
        runtime,
        request_id=REQUEST_ID,
        message_id="message-1",
        part_id="2",
        capability=admitted,
        parameters={},
        confirmed=False,
        artifact_id=INPUT_ARTIFACT_ID,
    )
    assert created is not None
    drifted = capability(produces=("application/json",))

    with pytest.raises(engine_api.ApiError) as rejected:
        engine_api._prepare_or_create_generic_connect_job(
            runtime,
            request_id=SECOND_REQUEST_ID,
            message_id="message-1",
            part_id="2",
            capability=drifted,
            parameters={},
            confirmed=False,
            artifact_id=OUTPUT_ID,
            join_effectful=False,
        )

    assert rejected.value.code == "capability_authority_changed"
    failed = runtime.store.connect_job(created.job_id)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.error_code == "capability_authority_changed"


def test_unknown_persisted_capability_authority_fails_before_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, runtime = seeded_runtime(tmp_path)
    selected = capability()
    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    _candidate, created, _collision, _content = engine_api._prepare_or_create_generic_connect_job(
        runtime,
        request_id=REQUEST_ID,
        message_id="message-1",
        part_id="2",
        capability=selected,
        parameters={},
        confirmed=False,
        artifact_id=INPUT_ARTIFACT_ID,
    )
    assert created is not None
    with runtime.store.connection() as db:
        db.execute(
            "UPDATE connect_job_dispatch SET capability_authority_known = 0 WHERE job_id = ?",
            (created.job_id,),
        )
    dispatch = runtime.store.connect_dispatch(created.job_id)
    assert dispatch is not None

    with pytest.raises(engine_api.ApiError) as rejected:
        engine_api._require_persisted_capability_authority(
            runtime,
            created,
            dispatch,
            selected,
        )

    assert rejected.value.code == "capability_authority_unknown"
    failed = runtime.store.connect_job(created.job_id)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.error_code == "capability_authority_unknown"


def test_automation_entitlement_is_rechecked_after_source_fetch_before_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, runtime = seeded_runtime(tmp_path)
    selected, fire, attempt = seed_contract_fire(runtime)
    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    entitlement = {"active": True}

    class ExpiringGmail(SeededMailboxGateway):
        def attachment_bytes(self, *args: object) -> bytes:
            entitlement["active"] = False
            return PDF

    monkeypatch.setattr(
        engine_api,
        "_automation_entitlement_active",
        lambda: entitlement["active"],
    )
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: ExpiringGmail())

    engine_api._dispatch_automation_fire(runtime, fire.fire_id)

    paused = runtime.store.automation_fire(fire.fire_id)
    assert paused is not None
    assert paused.state == "entitlement_paused"
    assert runtime.store.connect_job(attempt.dispatch_request_id) is None


def test_due_lane_selection_skips_paused_waiting_automation_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, runtime = seeded_runtime(tmp_path)
    seed_second_attachment(runtime)
    selected = capability()
    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    _first_candidate, first, _collision, _content = (
        engine_api._prepare_or_create_generic_connect_job(
            runtime,
            request_id=REQUEST_ID,
            message_id="message-1",
            part_id="2",
            capability=selected,
            parameters={},
            confirmed=False,
            artifact_id=INPUT_ARTIFACT_ID,
        )
    )
    _second_candidate, second, _collision, _content = (
        engine_api._prepare_or_create_generic_connect_job(
            runtime,
            request_id=SECOND_REQUEST_ID,
            message_id="message-2",
            part_id="2",
            capability=selected,
            parameters={},
            confirmed=False,
            artifact_id=OUTPUT_ID,
        )
    )
    assert first is not None
    assert second is not None
    with runtime.store.connection() as db:
        db.execute(
            """UPDATE connect_job_dispatch
            SET automation_paused_at = ? WHERE job_id = ?""",
            (datetime.now(UTC).isoformat(), first.job_id),
        )

    due = runtime.store.due_connect_lane_heads(now=datetime.now(UTC))
    wakeups = runtime.store.connect_queue_wakeups(now=datetime.now(UTC))

    assert [job.job_id for job in due] == [second.job_id]
    wakeup_job_ids = [job_id for job_id, _wakeup in wakeups]
    assert first.job_id not in wakeup_job_ids
    assert second.job_id in wakeup_job_ids


def test_automation_entitlement_pause_prevents_discovery_then_resumes_same_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected, fire, attempt = seed_contract_fire(runtime)
    discovered = 0

    def discover(**kwargs: object) -> connect.CapabilityCatalog:
        nonlocal discovered
        discovered += 1
        return connect.CapabilityCatalog((selected,))

    install_automation_dispatch_fakes(monkeypatch, runtime, discover)
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: False)

    paused_response = engine_api._response(api_request(config_path, "connect.queue.pump"))

    assert paused_response["ok"] is True
    paused = runtime.store.automation_fire(fire.fire_id)
    assert paused is not None
    assert paused.state == "entitlement_paused"
    assert paused.pending_since is None
    assert discovered == 0
    assert runtime.store.connect_job(attempt.dispatch_request_id) is None

    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    resumed_response = engine_api._response(api_request(config_path, "connect.queue.pump"))

    assert resumed_response["ok"] is True
    resumed = runtime.store.automation_fire(fire.fire_id)
    assert resumed is not None
    assert resumed.state == "submitted"
    assert resumed.current_attempt_no == 1
    assert resumed.job_id == attempt.dispatch_request_id
    assert discovered == 1


def test_automation_entitlement_is_rechecked_before_proven_new_provider_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, runtime = seeded_runtime(tmp_path)
    selected, fire, attempt = seed_contract_fire(runtime)

    class FakeGmail(SeededMailboxGateway):
        def attachment_bytes(self, *args: object) -> bytes:
            return PDF

    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities",
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: FakeGmail())
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    engine_api._dispatch_automation_fire(runtime, fire.fire_id)
    job = runtime.store.connect_job(attempt.dispatch_request_id)
    assert job is not None
    make_connect_job_due(runtime, job.job_id)
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: False)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("inactive automation must not discover or submit")
        ),
    )

    outcome = engine_api._pump_generic_connect_lane(runtime, job)

    assert outcome["outcome"] == "entitlement_paused"
    paused = runtime.store.automation_fire(fire.fire_id)
    dispatch = runtime.store.connect_dispatch(job.job_id)
    assert paused is not None
    assert paused.state == "entitlement_paused"
    assert dispatch is not None
    assert dispatch.state == "waiting"
    assert dispatch.attempt_count == 1


def test_automation_entitlement_is_rechecked_after_source_fetch_before_provider_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, runtime = seeded_runtime(tmp_path)
    selected, fire, attempt = seed_contract_fire(runtime)
    automation_authorized = True
    submissions = 0

    class ExpiringGmail(SeededMailboxGateway):
        def attachment_bytes(self, *args: object) -> bytes:
            nonlocal automation_authorized
            automation_authorized = False
            return PDF

    class CompletingClient:
        def __init__(self, capability_value: connect.DiscoveredCapability) -> None:
            assert capability_value == selected

        def submit(
            self, job_value: connect.PreparedCapabilityJob, content: bytes
        ) -> connect.CapabilityJobUpdate:
            nonlocal submissions
            submissions += 1
            return update(job_value, "completed", payload=b"must not submit")

    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
        stub_lane=False,
    )
    monkeypatch.setattr(
        engine_api,
        "_automation_entitlement_active",
        lambda: automation_authorized,
    )
    monkeypatch.setattr(engine_api.connect, "ConnectV2Client", CompletingClient)
    engine_api._dispatch_automation_fire(runtime, fire.fire_id)
    job = runtime.store.connect_job(attempt.dispatch_request_id)
    assert job is not None
    make_connect_job_due(runtime, job.job_id)
    monkeypatch.setattr(
        engine_api.GmailGateway,
        "from_token",
        lambda *args: ExpiringGmail(),
    )

    outcome = engine_api._pump_generic_connect_lane(runtime, job)

    paused = runtime.store.automation_fire(fire.fire_id)
    dispatch = runtime.store.connect_dispatch(job.job_id)
    assert outcome["outcome"] == "entitlement_paused"
    assert submissions == 0
    assert paused is not None
    assert paused.state == "entitlement_paused"
    assert dispatch is not None
    assert dispatch.state == "waiting"
    assert dispatch.automation_paused_at is not None


def test_automation_submission_requires_connect_entitlement_before_provider_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, runtime = seeded_runtime(tmp_path)
    selected, fire, attempt = seed_contract_fire(runtime)
    submissions = 0

    class CompletingClient:
        def __init__(self, capability_value: connect.DiscoveredCapability) -> None:
            assert capability_value == selected

        def submit(
            self, job_value: connect.PreparedCapabilityJob, content: bytes
        ) -> connect.CapabilityJobUpdate:
            nonlocal submissions
            submissions += 1
            return update(job_value, "completed", payload=b"must not submit")

    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
        stub_lane=False,
    )
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    monkeypatch.setattr(engine_api.connect, "ConnectV2Client", CompletingClient)
    engine_api._dispatch_automation_fire(runtime, fire.fire_id)
    job = runtime.store.connect_job(attempt.dispatch_request_id)
    assert job is not None
    make_connect_job_due(runtime, job.job_id)
    monkeypatch.setattr(
        engine_api.connect,
        "require_connect_entitlement",
        lambda: (_ for _ in ()).throw(
            connect.ConnectError(
                "CONNECT_ENTITLEMENT_REQUIRED",
                "Connect requires an active license.",
                retryable=False,
            )
        ),
    )

    outcome = engine_api._pump_generic_connect_lane(runtime, job)

    paused_job = runtime.store.connect_job(job.job_id)
    paused_fire = runtime.store.automation_fire(fire.fire_id)
    dispatch = runtime.store.connect_dispatch(job.job_id)
    assert outcome["outcome"] == "entitlement_paused"
    assert submissions == 0
    assert paused_job is not None
    assert paused_job.status == "requested"
    assert paused_fire is not None
    assert paused_fire.state == "entitlement_paused"
    assert dispatch is not None
    assert dispatch.state == "waiting"


def test_connect_entitlement_is_rechecked_after_source_fetch_before_provider_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, runtime = seeded_runtime(tmp_path)
    selected, fire, attempt = seed_contract_fire(runtime)
    connect_authorized = True
    submissions = 0

    class ExpiringGmail(SeededMailboxGateway):
        def attachment_bytes(self, *args: object) -> bytes:
            nonlocal connect_authorized
            connect_authorized = False
            return PDF

    class CompletingClient:
        def __init__(self, capability_value: connect.DiscoveredCapability) -> None:
            assert capability_value == selected

        def submit(
            self, job_value: connect.PreparedCapabilityJob, content: bytes
        ) -> connect.CapabilityJobUpdate:
            nonlocal submissions
            submissions += 1
            return update(job_value, "completed", payload=b"must not submit")

    def require_connect() -> None:
        if not connect_authorized:
            raise connect.ConnectError(
                "CONNECT_ENTITLEMENT_REQUIRED",
                "Connect requires an active license.",
                retryable=False,
            )

    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
        stub_lane=False,
    )
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    monkeypatch.setattr(engine_api.connect, "ConnectV2Client", CompletingClient)
    engine_api._dispatch_automation_fire(runtime, fire.fire_id)
    job = runtime.store.connect_job(attempt.dispatch_request_id)
    assert job is not None
    make_connect_job_due(runtime, job.job_id)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: ExpiringGmail())
    monkeypatch.setattr(engine_api.connect, "require_connect_entitlement", require_connect)

    outcome = engine_api._pump_generic_connect_lane(runtime, job)

    paused_job = runtime.store.connect_job(job.job_id)
    paused_fire = runtime.store.automation_fire(fire.fire_id)
    dispatch = runtime.store.connect_dispatch(job.job_id)
    assert outcome["outcome"] == "entitlement_paused"
    assert submissions == 0
    assert paused_job is not None
    assert paused_job.status == "requested"
    assert paused_fire is not None
    assert paused_fire.state == "entitlement_paused"
    assert dispatch is not None
    assert dispatch.state == "waiting"


def test_submission_authority_is_rechecked_after_lane_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, runtime = seeded_runtime(tmp_path)
    selected, fire, attempt = seed_contract_fire(runtime)
    submissions = 0

    class CompletingClient:
        def __init__(self, capability_value: connect.DiscoveredCapability) -> None:
            assert capability_value == selected

        def submit(
            self, job_value: connect.PreparedCapabilityJob, content: bytes
        ) -> connect.CapabilityJobUpdate:
            nonlocal submissions
            submissions += 1
            assert content == PDF
            return update(job_value, "completed", payload=b"done")

    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
        stub_lane=False,
    )
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    monkeypatch.setattr(engine_api.connect, "ConnectV2Client", CompletingClient)
    engine_api._dispatch_automation_fire(runtime, fire.fire_id)
    job = runtime.store.connect_job(attempt.dispatch_request_id)
    assert job is not None
    make_connect_job_due(runtime, job.job_id)
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: False)
    monkeypatch.setattr(engine_api.connect, "require_connect_entitlement", lambda: None)
    real_claim = runtime.store.claim_connect_lane_head

    def authorize_then_claim(**values: object):
        runtime.store.authorize_connect_job_interactively(job.job_id)
        return real_claim(**values)

    monkeypatch.setattr(runtime.store, "claim_connect_lane_head", authorize_then_claim)

    outcome = engine_api._pump_generic_connect_lane(runtime, job)

    completed = runtime.store.connect_job(job.job_id)
    linked_fire = runtime.store.automation_fire(fire.fire_id)
    assert outcome["outcome"] == "completed"
    assert submissions == 1
    assert completed is not None
    assert completed.status == "completed"
    assert linked_fire is not None
    assert linked_fire.state == "entitlement_paused"


def test_connect_entitlement_expiry_during_automation_admission_pauses_fire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, runtime = seeded_runtime(tmp_path)
    selected, fire, attempt = seed_contract_fire(runtime)
    authority_checks = 0

    def require_connect() -> None:
        nonlocal authority_checks
        authority_checks += 1
        if authority_checks == 2:
            raise connect.ConnectError(
                "CONNECT_ENTITLEMENT_REQUIRED",
                "Connect requires an active license.",
                retryable=False,
            )

    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    monkeypatch.setattr(engine_api.connect, "require_connect_entitlement", require_connect)

    engine_api._dispatch_automation_fire(runtime, fire.fire_id)

    paused = runtime.store.automation_fire(fire.fire_id)
    assert authority_checks == 2
    assert paused is not None
    assert paused.state == "entitlement_paused"
    assert paused.reason == "entitlement_inactive"
    assert runtime.store.connect_job(attempt.dispatch_request_id) is None


def test_automation_entitlement_pause_rolls_back_dispatch_when_fire_pause_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, runtime = seeded_runtime(tmp_path)
    selected, fire, attempt = seed_contract_fire(runtime)
    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
        stub_lane=False,
    )
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    engine_api._dispatch_automation_fire(runtime, fire.fire_id)
    job = runtime.store.connect_job(attempt.dispatch_request_id)
    assert job is not None
    make_connect_job_due(runtime, job.job_id)
    claimed = runtime.store.claim_connect_lane_head(
        provider_app_id=selected.app_id,
        provider_instance_id=selected.instance_id,
        expected_job_id=job.job_id,
    )
    assert claimed is not None
    assert claimed[1].state == "dispatching"
    with runtime.store.connection() as db:
        db.execute(
            """CREATE TRIGGER reject_automation_entitlement_pause
            BEFORE UPDATE OF state ON automation_fires
            WHEN NEW.state = 'entitlement_paused'
            BEGIN
                SELECT RAISE(ABORT, 'injected automation pause failure');
            END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected automation pause failure"):
        engine_api._defer_inactive_automation_job(
            runtime,
            job.job_id,
            expected_dispatch_state="dispatching",
            next_dispatch_state="waiting",
        )

    unchanged_fire = runtime.store.automation_fire(fire.fire_id)
    unchanged_dispatch = runtime.store.connect_dispatch(job.job_id)
    assert unchanged_fire is not None
    assert unchanged_fire.state == "submitted"
    assert unchanged_dispatch is not None
    assert unchanged_dispatch.state == "dispatching"
    assert unchanged_dispatch.automation_paused_at is None


def test_inactive_automation_reconciles_provider_owned_job_without_resubmission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, runtime = seeded_runtime(tmp_path)
    selected, fire, attempt = seed_contract_fire(runtime)
    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
        stub_lane=False,
    )
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    engine_api._dispatch_automation_fire(runtime, fire.fire_id)
    job = runtime.store.connect_job(attempt.dispatch_request_id)
    assert job is not None
    runtime.store.transition_connect_job(
        job_id=job.job_id,
        expected_state="requested",
        next_state="accepted",
        provider_app_id=selected.app_id,
        provider_instance_id=selected.instance_id,
    )
    make_connect_job_due(runtime, job.job_id)
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: False)

    def forbidden_manifest_discovery(**kwargs: object) -> connect.CapabilityCatalog:
        raise AssertionError("provider-owned reconciliation must not fetch the manifest")

    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities_for_reconciliation",
        forbidden_manifest_discovery,
    )
    def reconcile_registered(**kwargs: object):
        assert kwargs["produces"] == selected.produces
        return registered_capability(selected), None

    monkeypatch.setattr(
        engine_api.connect,
        "registered_capability_for_reconciliation",
        reconcile_registered,
        raising=False,
    )
    submissions = 0
    queries = 0

    class CompletedClient:
        def __init__(
            self,
            capability_value: connect.DiscoveredCapability | connect.RegisteredCapability,
        ) -> None:
            assert capability_value == registered_capability(selected)

        def submit(self, job_value: object, content: bytes) -> object:
            nonlocal submissions
            submissions += 1
            raise AssertionError("reconciliation must not submit")

        def get(self, job_value: connect.PreparedCapabilityJob) -> connect.CapabilityJobUpdate:
            nonlocal queries
            queries += 1
            return update(job_value, "completed", payload=b"Completed while paused")

        def wait_for_terminal(
            self,
            job_value: object,
            initial: object,
            on_update: object,
        ) -> object:
            return initial

    monkeypatch.setattr(engine_api.connect, "ConnectV2Client", CompletedClient)

    outcome = engine_api._pump_generic_connect_lane(runtime, job)
    engine_api._settle_submitted_automation_fires(runtime, limit=25)

    settled = runtime.store.automation_fire(fire.fire_id)
    assert outcome["job_status"] == "completed"
    assert submissions == 0
    assert queries == 1
    assert settled is not None
    assert settled.state == "completed"


def test_crash_admitted_job_is_bound_before_automation_pause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, runtime = seeded_runtime(tmp_path)
    selected, fire, attempt = seed_contract_fire(runtime)
    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
        stub_lane=False,
    )
    _candidate, created, _collision, _content = engine_api._prepare_or_create_generic_connect_job(
        runtime,
        request_id=attempt.dispatch_request_id,
        message_id=fire.message_id,
        part_id=fire.part_id,
        capability=selected,
        parameters={"mode": "contract"},
        confirmed=False,
        artifact_id=engine_api._automation_artifact_id(attempt.dispatch_request_id),
    )
    assert created is not None
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: False)

    engine_api._dispatch_automation_fire(runtime, fire.fire_id)

    recovered = runtime.store.automation_fire(fire.fire_id)
    assert recovered is not None
    assert recovered.state == "entitlement_paused"
    assert recovered.job_id == created.job_id
    assert runtime.store.automation_fires_linked_to_job(created.job_id) == (recovered,)
    assert engine_api._pump_generic_connect_lane(runtime, created)["outcome"] == "not_due"


def test_unbound_automation_job_keeps_automation_authority_before_fire_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, runtime = seeded_runtime(tmp_path)
    selected, fire, attempt = seed_contract_fire(runtime)
    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
        stub_lane=False,
    )
    _candidate, created, _collision, _content = engine_api._prepare_or_create_generic_connect_job(
        runtime,
        request_id=attempt.dispatch_request_id,
        message_id=fire.message_id,
        part_id=fire.part_id,
        capability=selected,
        parameters={"mode": "contract"},
        confirmed=False,
        artifact_id=engine_api._automation_artifact_id(attempt.dispatch_request_id),
    )
    assert created is not None
    assert runtime.store.automation_fires_linked_to_job(created.job_id) == ()
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: False)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("inactive unbound automation must not discover or submit")
        ),
    )

    outcome = engine_api._pump_generic_connect_lane(runtime, created)

    dispatch = runtime.store.connect_dispatch(created.job_id)
    assert outcome["outcome"] == "entitlement_paused"
    assert runtime.store.connect_job(created.job_id).status == "requested"  # type: ignore[union-attr]
    assert dispatch is not None
    assert dispatch.state == "waiting"
    assert dispatch.automation_paused_at is not None
    assert runtime.store.automation_fire(fire.fire_id).state == "pending_dispatch"  # type: ignore[union-attr]


def test_authorized_unbound_automation_recovery_clears_dispatch_pause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, runtime = seeded_runtime(tmp_path)
    selected, fire, attempt = seed_contract_fire(runtime)
    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
        stub_lane=False,
    )
    _candidate, created, _collision, _content = engine_api._prepare_or_create_generic_connect_job(
        runtime,
        request_id=attempt.dispatch_request_id,
        message_id=fire.message_id,
        part_id=fire.part_id,
        capability=selected,
        parameters={"mode": "contract"},
        confirmed=False,
        artifact_id=engine_api._automation_artifact_id(attempt.dispatch_request_id),
    )
    assert created is not None
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: False)
    assert (
        engine_api._pump_generic_connect_lane(runtime, created)["outcome"]
        == "entitlement_paused"
    )
    paused = runtime.store.connect_dispatch(created.job_id)
    assert paused is not None
    assert paused.automation_paused_at is not None
    paused_deadline = paused.admission_deadline

    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    engine_api._dispatch_automation_fire(runtime, fire.fire_id)

    recovered = runtime.store.automation_fire(fire.fire_id)
    resumed = runtime.store.connect_dispatch(created.job_id)
    assert recovered is not None
    assert recovered.state == "submitted"
    assert recovered.job_id == created.job_id
    assert resumed is not None
    assert resumed.automation_paused_at is None
    assert datetime.fromisoformat(resumed.admission_deadline) >= datetime.fromisoformat(
        paused_deadline
    )


def test_automation_entitlement_is_rechecked_after_missing_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, runtime = seeded_runtime(tmp_path)
    selected, fire, attempt = seed_contract_fire(runtime)
    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
        stub_lane=False,
    )
    monkeypatch.setattr(
        engine_api.connect,
        "registered_capability_for_reconciliation",
        lambda **kwargs: (registered_capability(selected), None),
    )
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    engine_api._dispatch_automation_fire(runtime, fire.fire_id)
    job = runtime.store.connect_job(attempt.dispatch_request_id)
    assert job is not None
    submissions = 0

    class MissingAfterLostAckClient:
        def __init__(
            self,
            capability_value: connect.DiscoveredCapability | connect.RegisteredCapability,
        ) -> None:
            assert capability_value in {selected, registered_capability(selected)}

        def submit(self, job_value: connect.PreparedCapabilityJob, content: bytes) -> object:
            nonlocal submissions
            submissions += 1
            raise connect.ConnectError(
                "PROVIDER_UNAVAILABLE",
                "The provider response was lost.",
                retryable=True,
            )

        def get(self, job_value: connect.PreparedCapabilityJob) -> object:
            raise connect.ConnectError("JOB_NOT_FOUND", "The job is absent.")

        def wait_for_terminal(self, job_value, initial, on_update):
            return initial

    monkeypatch.setattr(engine_api.connect, "ConnectV2Client", MissingAfterLostAckClient)
    make_connect_job_due(runtime, job.job_id)
    first = engine_api._pump_generic_connect_lane(runtime, job)
    assert first["outcome"] == "deferred_or_failed"
    assert submissions == 1
    make_connect_job_due(runtime, job.job_id)
    decisions = iter((True, False))
    monkeypatch.setattr(
        engine_api,
        "_automation_entitlement_active",
        lambda: next(decisions),
    )

    engine_api._pump_generic_connect_lane(runtime, runtime.store.connect_job(job.job_id))  # type: ignore[arg-type]

    current = runtime.store.connect_job(job.job_id)
    dispatch = runtime.store.connect_dispatch(job.job_id)
    current_fire = runtime.store.automation_fire(fire.fire_id)
    assert submissions == 1
    assert current is not None
    assert current.status == "requested"
    assert dispatch is not None
    assert dispatch.state == "reconciling"
    assert current_fire is not None
    assert current_fire.state == "entitlement_paused"


def test_capability_effect_authority_drift_fails_before_provider_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, runtime = seeded_runtime(tmp_path)
    selected, fire, attempt = seed_contract_fire(runtime)
    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
        stub_lane=False,
    )
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    engine_api._dispatch_automation_fire(runtime, fire.fire_id)
    job = runtime.store.connect_job(attempt.dispatch_request_id)
    assert job is not None
    make_connect_job_due(runtime, job.job_id)
    changed = capability(
        app_id=selected.app_id,
        app_version=selected.app_version,
        capability_id=selected.capability_id,
        parameters=selected.parameters,
        external_effects=True,
    )
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities",
        lambda **kwargs: connect.CapabilityCatalog((changed,)),
    )

    outcome = engine_api._pump_generic_connect_lane(runtime, job)
    engine_api._settle_submitted_automation_fires(runtime, limit=25)

    failed_job = runtime.store.connect_job(job.job_id)
    failed_fire = runtime.store.automation_fire(fire.fire_id)
    assert outcome["outcome"] == "deferred_or_failed"
    assert failed_job is not None
    assert failed_job.status == "failed"
    assert failed_job.error_code == "capability_authority_changed"
    assert failed_fire is not None
    assert failed_fire.state == "failed"


def test_shared_job_deadline_is_extended_once_for_one_entitlement_pause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, runtime = seeded_runtime(tmp_path)
    mode = connect.CapabilityParameter(
        name="mode",
        value_type="string",
        required=False,
        label="Summary mode",
        description="Choose general, story, or contract. Defaults to general.",
    )
    selected = capability(
        app_id="document-summarizer",
        app_version="0.1.0",
        capability_id="document.summarize",
        parameters=(mode,),
    )
    for index in range(2):
        runtime.store.put_automation_rule(
            contract_rule_definition(selected, name=f"Contract watch {index}")
        )
    runtime.store.mark_analyzed(
        "message-1",
        {
            "category": "informational",
            "priority": "normal",
            "summary": "A contract arrived.",
            "action_required": True,
            "suggested_action": "Review the contract.",
            "deadline_text": None,
            "deadline_iso": None,
            "confidence": 0.9,
        },
        mailbox_identity_key=TEST_MAILBOX_IDENTITY_KEY,
    )
    fires = runtime.store.automation_fires_for_message("message-1")
    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    for fire in fires:
        engine_api._dispatch_automation_fire(runtime, fire.fire_id)
    linked = [runtime.store.automation_fire(fire.fire_id) for fire in fires]
    assert all(item is not None and item.state == "submitted" for item in linked)
    job_ids = {item.job_id for item in linked if item is not None}
    assert len(job_ids) == 1
    job_id = next(iter(job_ids))
    assert job_id is not None
    dispatch = runtime.store.connect_dispatch(job_id)
    assert dispatch is not None
    original_deadline = datetime.fromisoformat(dispatch.admission_deadline)
    paused_at = datetime.now(UTC)
    paused = [
        runtime.store.transition_automation_fire(
            fire_id=item.fire_id,
            expected_state=item.state,
            expected_version=item.state_version,
            next_state="entitlement_paused",
            reason="entitlement_inactive",
            now=paused_at,
        )
        for item in linked
        if item is not None
    ]
    runtime.store.resume_automation_fire_job(
        fire_id=paused[0].fire_id,
        expected_version=paused[0].state_version,
        now=paused_at + timedelta(seconds=60),
    )
    runtime.store.resume_automation_fire_job(
        fire_id=paused[1].fire_id,
        expected_version=paused[1].state_version,
        now=paused_at + timedelta(seconds=120),
    )

    resumed_dispatch = runtime.store.connect_dispatch(job_id)
    assert resumed_dispatch is not None
    assert datetime.fromisoformat(resumed_dispatch.admission_deadline) == (
        original_deadline + timedelta(seconds=60)
    )
    assert resumed_dispatch.automation_paused_at is None


def test_unchanged_fire_prefix_rotates_so_later_work_is_selected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, runtime = seeded_runtime(tmp_path)
    mode = connect.CapabilityParameter(
        name="mode",
        value_type="string",
        required=False,
        label="Summary mode",
        description="Choose general, story, or contract. Defaults to general.",
    )
    selected = capability(
        app_id="document-summarizer",
        app_version="0.1.0",
        capability_id="document.summarize",
        parameters=(mode,),
    )
    for index in range(26):
        runtime.store.put_automation_rule(
            contract_rule_definition(selected, name=f"Contract watch {index}")
        )
    runtime.store.mark_analyzed(
        "message-1",
        {
            "category": "informational",
            "priority": "normal",
            "summary": "A contract arrived.",
            "action_required": True,
            "suggested_action": "Review the contract.",
            "deadline_text": None,
            "deadline_iso": None,
            "confidence": 0.9,
        },
        mailbox_identity_key=TEST_MAILBOX_IDENTITY_KEY,
    )
    all_fire_ids = {
        fire.fire_id for fire in runtime.store.automation_fires_for_message("message-1")
    }
    attempted: list[str] = []
    monkeypatch.setattr(
        engine_api,
        "_dispatch_automation_fire",
        lambda active_runtime, fire_id, **kwargs: attempted.append(fire_id),
    )

    engine_api._dispatch_automation_fires(runtime, limit=25)
    first_prefix = set(attempted)
    attempted.clear()
    engine_api._dispatch_automation_fires(runtime, limit=25)

    assert len(all_fire_ids) == 26
    assert len(first_prefix) == 25
    assert all_fire_ids - first_prefix
    assert (all_fire_ids - first_prefix).issubset(set(attempted))


def test_active_entitlement_wakes_paused_fire_beyond_pump_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, runtime = seeded_runtime(tmp_path)
    selected = capability()
    for index in range(26):
        runtime.store.put_automation_rule(
            contract_rule_definition(selected, name=f"Paused watch {index}")
        )
    runtime.store.mark_analyzed(
        "message-1",
        {
            "category": "informational",
            "priority": "normal",
            "summary": "A contract arrived.",
            "action_required": True,
            "suggested_action": "Review the contract.",
            "deadline_text": None,
            "deadline_iso": None,
            "confidence": 0.9,
        },
        mailbox_identity_key=TEST_MAILBOX_IDENTITY_KEY,
    )
    fires = runtime.store.automation_fires_for_message("message-1")
    for fire in fires:
        runtime.store.transition_automation_fire(
            fire_id=fire.fire_id,
            expected_state=fire.state,
            expected_version=fire.state_version,
            next_state="entitlement_paused",
            reason="entitlement_inactive",
        )

    def move_to_manual_review(
        active_runtime: Runtime, fire_id: str, **kwargs: object
    ) -> None:
        fire = active_runtime.store.automation_fire(fire_id)
        assert fire is not None
        active_runtime.store.transition_automation_fire(
            fire_id=fire.fire_id,
            expected_state=fire.state,
            expected_version=fire.state_version,
            next_state="manual_review",
            reason="fixture_terminal",
        )

    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    monkeypatch.setattr(engine_api, "_dispatch_automation_fire", move_to_manual_review)

    outcome = engine_api.pump_connect_runtime(runtime, 25)
    paused = runtime.store.automation_fires_in_states(("entitlement_paused",), limit=25)

    assert len(fires) == 26
    assert len(paused) == 1
    assert outcome["next_wake_unix_ms"] is not None


def test_inactive_entitlement_does_not_spin_on_paused_fire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, runtime = seeded_runtime(tmp_path)
    _selected, fire, _attempt = seed_contract_fire(runtime)
    runtime.store.transition_automation_fire(
        fire_id=fire.fire_id,
        expected_state=fire.state,
        expected_version=fire.state_version,
        next_state="entitlement_paused",
        reason="entitlement_inactive",
    )
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: False)

    next_wakeup = engine_api._next_connect_queue_wakeup(
        runtime, [], datetime(2026, 9, 9, 12, tzinfo=UTC)
    )

    assert next_wakeup is None


def test_maximum_valid_rule_parameters_fit_prepared_confirmation_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, runtime = seeded_runtime(tmp_path)
    parameters = tuple(
        connect.CapabilityParameter(
            name=f"parameter-{index}",
            value_type="string",
            required=False,
            label=f"Parameter {index}",
            description=f"Parameter {index} for the local capability.",
        )
        for index in range(10)
    )
    values = {parameter.name: "x" * 900 for parameter in parameters}
    selected = capability(
        app_id="document-summarizer",
        app_version="0.1.0",
        capability_id="document.summarize",
        parameters=parameters,
    )
    runtime.store.put_automation_rule(
        contract_rule_definition(
            selected,
            confirm_each=True,
            parameters=values,
        )
    )
    runtime.store.mark_analyzed(
        "message-1",
        {
            "category": "informational",
            "priority": "normal",
            "summary": "A contract arrived.",
            "action_required": True,
            "suggested_action": "Review the contract.",
            "deadline_text": None,
            "deadline_iso": None,
            "confidence": 0.9,
        },
        mailbox_identity_key=TEST_MAILBOX_IDENTITY_KEY,
    )
    fire = runtime.store.automation_fires_for_message("message-1")[0]
    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)

    engine_api._dispatch_automation_fire(runtime, fire.fire_id)

    prepared = runtime.store.automation_fire(fire.fire_id)
    assert prepared is not None
    assert prepared.state == "awaiting_confirmation"
    assert prepared.prepared_identity_json is not None
    assert len(prepared.prepared_identity_json) > 8 * 1024


def test_automation_confirmation_binds_stable_preparation_and_admits_after_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected, fire, attempt = seed_contract_fire(runtime, confirm_each=True)
    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)

    prepared_response = engine_api._response(api_request(config_path, "connect.queue.pump"))

    assert prepared_response["ok"] is True
    prepared = runtime.store.automation_fire(fire.fire_id)
    assert prepared is not None
    assert prepared.state == "awaiting_confirmation"
    assert prepared.job_id is None
    assert prepared.prepared_identity_sha256 is not None
    assert prepared.prepared_identity_json is not None
    projected = runtime.store.recent(1)[0]["attachments"][0]["automation_fires"][0]
    assert projected["fire_id"] == fire.fire_id
    assert projected["state"] == "awaiting_confirmation"
    assert projected["state_version"] == prepared.state_version
    assert projected["prepared_identity_sha256"] == prepared.prepared_identity_sha256
    identity = json.loads(prepared.prepared_identity_json)
    expected_artifact_id = engine_api._automation_artifact_id(attempt.dispatch_request_id)
    assert identity["input"]["artifact_id"] == expected_artifact_id

    decision_response = engine_api._response(
        api_request(
            config_path,
            "automation.fire.decide",
            {
                "fire_id": fire.fire_id,
                "expected_version": prepared.state_version,
                "prepared_identity_sha256": prepared.prepared_identity_sha256,
                "decision": "confirmed",
            },
        )
    )
    assert decision_response["data"] == {
        "fire_id": fire.fire_id,
        "state": "pending_dispatch",
        "state_version": prepared.state_version + 1,
    }

    admitted_response = engine_api._response(api_request(config_path, "connect.queue.pump"))

    assert admitted_response["ok"] is True
    admitted = runtime.store.automation_fire(fire.fire_id)
    job = runtime.store.connect_job(attempt.dispatch_request_id)
    assert admitted is not None
    assert admitted.state == "submitted"
    assert job is not None
    assert job.input_artifact_id == expected_artifact_id
    assert runtime.store.automation_confirmation_matches(admitted)


def test_automation_confirmation_rejects_live_effect_drift_without_creating_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected, fire, attempt = seed_contract_fire(runtime, confirm_each=True)
    current = selected
    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((current,)),
    )
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    engine_api._response(api_request(config_path, "connect.queue.pump"))
    prepared = runtime.store.automation_fire(fire.fire_id)
    assert prepared is not None
    assert prepared.prepared_identity_sha256 is not None
    engine_api._response(
        api_request(
            config_path,
            "automation.fire.decide",
            {
                "fire_id": fire.fire_id,
                "expected_version": prepared.state_version,
                "prepared_identity_sha256": prepared.prepared_identity_sha256,
                "decision": "confirmed",
            },
        )
    )
    current = capability(
        app_id=selected.app_id,
        app_version=selected.app_version,
        capability_id=selected.capability_id,
        parameters=selected.parameters,
        external_effects=True,
    )

    drift_response = engine_api._response(api_request(config_path, "connect.queue.pump"))

    assert drift_response["ok"] is True
    halted = runtime.store.automation_fire(fire.fire_id)
    assert halted is not None
    assert halted.state == "manual_review"
    assert halted.reason == "automation_confirmation_stale"
    assert runtime.store.connect_job(attempt.dispatch_request_id) is None


def test_automation_confirmation_rejects_effect_requirement_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected, fire, attempt = seed_contract_fire(runtime, external_effects=True)
    current = selected
    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((current,)),
    )
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    engine_api._response(api_request(config_path, "connect.queue.pump"))
    prepared = runtime.store.automation_fire(fire.fire_id)
    assert prepared is not None
    assert prepared.prepared_identity_sha256 is not None
    engine_api._response(
        api_request(
            config_path,
            "automation.fire.decide",
            {
                "fire_id": fire.fire_id,
                "expected_version": prepared.state_version,
                "prepared_identity_sha256": prepared.prepared_identity_sha256,
                "decision": "confirmed",
            },
        )
    )
    current = capability(
        app_id=selected.app_id,
        app_version=selected.app_version,
        capability_id=selected.capability_id,
        parameters=selected.parameters,
    )

    engine_api._response(api_request(config_path, "connect.queue.pump"))

    halted = runtime.store.automation_fire(fire.fire_id)
    assert halted is not None
    assert halted.state == "manual_review"
    assert halted.reason == "automation_confirmation_stale"
    assert runtime.store.connect_job(attempt.dispatch_request_id) is None


@pytest.mark.parametrize(
    "override",
    [
        {"expected_version": False},
        {"expected_version": 0},
        {"expected_version": 2**63},
        {"prepared_identity_sha256": "A" * 64},
        {"decision": "approve"},
    ],
)
def test_automation_confirmation_decision_rejects_invalid_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    override: dict[str, object],
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected, fire, _attempt = seed_contract_fire(runtime, confirm_each=True)
    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    engine_api._response(api_request(config_path, "connect.queue.pump"))
    awaiting = runtime.store.automation_fire(fire.fire_id)
    assert awaiting is not None
    assert awaiting.prepared_identity_sha256 is not None
    payload = {
        "fire_id": fire.fire_id,
        "expected_version": awaiting.state_version,
        "prepared_identity_sha256": awaiting.prepared_identity_sha256,
        "decision": "confirmed",
        **override,
    }

    rejected = engine_api._response(api_request(config_path, "automation.fire.decide", payload))

    assert rejected["error"]["code"] == "invalid_request"
    unchanged = runtime.store.automation_fire(fire.fire_id)
    assert unchanged is not None
    assert unchanged.state == "awaiting_confirmation"
    assert unchanged.state_version == awaiting.state_version


def test_automation_confirmation_decline_is_exact_and_cannot_be_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected, fire, _attempt = seed_contract_fire(runtime, confirm_each=True)
    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    engine_api._response(api_request(config_path, "connect.queue.pump"))
    awaiting = runtime.store.automation_fire(fire.fire_id)
    assert awaiting is not None
    assert awaiting.prepared_identity_sha256 is not None
    decision = {
        "fire_id": fire.fire_id,
        "expected_version": awaiting.state_version,
        "prepared_identity_sha256": awaiting.prepared_identity_sha256,
        "decision": "declined",
    }
    wrong_hash = engine_api._response(
        api_request(
            config_path,
            "automation.fire.decide",
            {**decision, "prepared_identity_sha256": "0" * 64},
        )
    )
    assert wrong_hash["error"]["code"] == "stale_automation_fire"

    declined = engine_api._response(api_request(config_path, "automation.fire.decide", decision))
    replayed = engine_api._response(api_request(config_path, "automation.fire.decide", decision))

    assert declined["data"]["state"] == "declined"
    assert replayed["error"]["code"] == "stale_automation_fire"
    settled = runtime.store.automation_fire(fire.fire_id)
    assert settled is not None
    assert settled.state == "declined"
    assert settled.reason == "declined_by_user"


@pytest.mark.parametrize(
    ("failure", "expected_state"),
    [
        (MailboxError("temporary"), "pending_dispatch"),
        (MailboxMessageUnavailable("gone"), "source_unavailable"),
        (
            MailboxMessageInvalid("imap_bodystructure_invalid", "malformed MIME"),
            "source_unavailable",
        ),
    ],
)
def test_automation_source_failure_distinguishes_transient_from_definitive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    expected_state: str,
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected, fire, attempt = seed_contract_fire(runtime)

    class FailingGmail(SeededMailboxGateway):
        def set_operation_timeout(self, timeout_seconds: float) -> None:
            pass

        def attachment_bytes(self, *args: object) -> bytes:
            raise failure

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities",
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: FailingGmail())

    response = engine_api._response(api_request(config_path, "connect.queue.pump"))

    assert response["ok"] is True
    settled = runtime.store.automation_fire(fire.fire_id)
    assert settled is not None
    assert settled.state == expected_state
    assert runtime.store.connect_job(attempt.dispatch_request_id) is None
    if expected_state == "pending_dispatch":
        assert response["data"]["next_wake_unix_ms"] is not None
    else:
        assert response["data"]["next_wake_unix_ms"] is None


@pytest.mark.parametrize("identity_case", ["missing", "replaced"])
def test_automation_source_rejects_unbound_or_replaced_mailbox_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identity_case: str
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected, fire, attempt = seed_contract_fire(runtime)
    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    if identity_case == "replaced":
        runtime.store.reconcile_mailbox_identity(
            DEFAULT_MAIL_PROVIDER,
            DEFAULT_MAIL_ACCOUNT_ID,
            "b" * 64,
            legacy_status="replacement",
        )
    else:
        with runtime.store.connection() as db:
            db.execute(
                "UPDATE messages SET mailbox_identity_key = NULL WHERE message_id = ?",
                (fire.message_id,),
            )

    response = engine_api._response(api_request(config_path, "connect.queue.pump"))

    assert response["ok"] is True
    settled = runtime.store.automation_fire(fire.fire_id)
    assert settled is not None
    assert settled.state == "source_unavailable"
    assert runtime.store.connect_job(attempt.dispatch_request_id) is None


def test_mailbox_gateway_rejects_live_identity_changed_after_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, runtime = seeded_runtime(tmp_path)
    source = runtime.store.message_source("message-1")

    class ReplacementGateway:
        def mailbox_identity_key(self) -> str:
            return "b" * 64

        def attachment_bytes(self, *args: object) -> bytes:
            raise AssertionError("replacement identity reached the attachment read")

    monkeypatch.setattr(
        engine_api,
        "load_mailbox_account",
        lambda *args, **kwargs: MailboxSession(
            source.provider,
            source.account_id,
            ReplacementGateway(),  # type: ignore[arg-type]
        ),
    )

    with pytest.raises(engine_api.ApiError) as rejected:
        engine_api._verified_mailbox_attachment_bytes(runtime, source, "2", "attachment-1")

    assert rejected.value.code == "connect_source_unavailable"


def test_automation_queue_capacity_failure_remains_pending_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected, fire, attempt = seed_contract_fire(runtime)
    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    monkeypatch.setattr(
        runtime.store,
        "create_connect_job",
        lambda **kwargs: (_ for _ in ()).throw(ConnectQueueFull("full")),
    )

    response = engine_api._response(api_request(config_path, "connect.queue.pump"))

    assert response["ok"] is True
    pending = runtime.store.automation_fire(fire.fire_id)
    assert pending is not None
    assert pending.state == "pending_dispatch"
    assert runtime.store.connect_job(attempt.dispatch_request_id) is None


def test_concurrent_interactive_and_automation_admission_join_one_active_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_pump = engine_api._pump_generic_connect_lane
    config_path, runtime = seeded_runtime(tmp_path)
    selected, fire, attempt = seed_contract_fire(runtime)
    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)

    interactive = engine_api._response(
        api_request(
            config_path,
            "connect.attachment.invoke",
            invocation_payload(selected, parameters={"mode": "contract"}),
        )
    )
    automated = engine_api._response(api_request(config_path, "connect.queue.pump"))

    assert interactive["data"]["job_id"] == REQUEST_ID
    assert automated["ok"] is True
    joined = runtime.store.automation_fire(fire.fire_id)
    bound_attempt = runtime.store.automation_fire_attempts(fire.fire_id)[0]
    assert joined is not None
    assert joined.state == "submitted"
    assert joined.job_id == REQUEST_ID
    assert bound_attempt.dispatch_request_id == attempt.dispatch_request_id
    assert bound_attempt.job_id == REQUEST_ID
    with runtime.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM connect_attachment_jobs").fetchone()[0] == 1

    with runtime.store.connection() as db:
        db.execute(
            """UPDATE connect_job_dispatch
            SET state = 'waiting', submission_possible = 0,
                next_attempt_at = '2000-01-01T00:00:00+00:00'
            WHERE job_id = ?""",
            (REQUEST_ID,),
        )
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: False)
    connect_authority_checks = 0
    run_calls = 0

    def require_connect() -> None:
        nonlocal connect_authority_checks
        connect_authority_checks += 1

    def run_joined_job(*args: object, **kwargs: object) -> None:
        nonlocal run_calls
        run_calls += 1

    monkeypatch.setattr(engine_api.connect, "require_connect_entitlement", require_connect)
    monkeypatch.setattr(
        engine_api,
        "_discover_persisted_generic_capability",
        lambda job, dispatch, *, require_entitlement: (selected, None),
    )
    monkeypatch.setattr(engine_api, "_run_claimed_generic_connect_job", run_joined_job)
    active = runtime.store.connect_job(REQUEST_ID)
    assert active is not None

    outcome = real_pump(runtime, active)

    assert outcome["outcome"] == "entitlement_paused"
    assert connect_authority_checks == 1
    assert run_calls == 1
    paused_join = runtime.store.automation_fire(fire.fire_id)
    dispatch = runtime.store.connect_dispatch(REQUEST_ID)
    assert paused_join is not None
    assert paused_join.state == "entitlement_paused"
    assert dispatch is not None
    assert dispatch.automation_paused_at is None

    output = connect.CapabilityOutput(
        artifact_id=OUTPUT_ID,
        media_type="text/plain",
        display_name="contract-summary.txt",
        byte_size=4,
        sha256=hashlib.sha256(b"done").hexdigest(),
        payload=b"done",
    )
    runtime.store.transition_connect_job(
        job_id=REQUEST_ID,
        expected_state="requested",
        next_state="completed",
        provider_app_id=selected.app_id,
        provider_instance_id=selected.instance_id,
        result=connect.CapabilityResult((output,)).store_dict(),
    )
    engine_api._settle_submitted_automation_fires(runtime, limit=25)
    still_paused = runtime.store.automation_fire(fire.fire_id)
    assert still_paused is not None
    assert still_paused.state == "entitlement_paused"
    assert runtime.store.automation_fire_settlement_due() is False

    assert runtime.store.delete_message(fire.message_id) is True
    retained = runtime.store.automation_fire(fire.fire_id)
    assert retained is not None
    assert retained.state == "entitlement_paused"
    assert retained.reason == "entitlement_inactive"
    assert runtime.store.connect_job(REQUEST_ID) is not None

    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    engine_api._dispatch_automation_fire(runtime, fire.fire_id)
    engine_api._settle_submitted_automation_fires(runtime, limit=25)
    completed = runtime.store.automation_fire(fire.fire_id)
    assert completed is not None
    assert completed.state == "completed"


@pytest.mark.parametrize("terminal_status", ["completed", "failed"])
def test_submitted_interactive_join_pauses_before_terminal_settlement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, terminal_status: str
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected, fire, _attempt = seed_contract_fire(runtime)
    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    interactive = engine_api._response(
        api_request(
            config_path,
            "connect.attachment.invoke",
            invocation_payload(selected, parameters={"mode": "contract"}),
        )
    )
    engine_api._response(api_request(config_path, "connect.queue.pump"))
    submitted = runtime.store.automation_fire(fire.fire_id)
    assert submitted is not None
    assert submitted.state == "submitted"
    assert submitted.job_id == interactive["data"]["job_id"]
    assert runtime.store.connect_job_requires_automation_entitlement(submitted.job_id) is False

    output = connect.CapabilityOutput(
        artifact_id=OUTPUT_ID,
        media_type="application/vnd.local-connect.cited-summary+json",
        display_name="contract-summary.json",
        byte_size=2,
        sha256=hashlib.sha256(b"{}").hexdigest(),
        payload=b"{}",
    )
    if terminal_status == "completed":
        runtime.store.transition_connect_job(
            job_id=submitted.job_id,
            expected_state="requested",
            next_state="completed",
            provider_app_id=selected.app_id,
            provider_instance_id=selected.instance_id,
            result=connect.CapabilityResult((output,)).store_dict(),
        )
    else:
        runtime.store.transition_connect_job(
            job_id=submitted.job_id,
            expected_state="requested",
            next_state="failed",
            provider_app_id=selected.app_id,
            provider_instance_id=selected.instance_id,
            error={"code": "provider_failed", "message": "failed", "retryable": False},
        )
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: False)

    engine_api._settle_submitted_automation_fires(runtime, limit=25)

    paused = runtime.store.automation_fire(fire.fire_id)
    dispatch = runtime.store.connect_dispatch(submitted.job_id)
    assert paused is not None
    assert paused.state == "entitlement_paused"
    assert paused.reason == "entitlement_inactive"
    assert dispatch is not None
    assert dispatch.automation_paused_at is None


def test_automation_dispatch_bounds_discovery_and_source_fetch_to_phase_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, runtime = seeded_runtime(tmp_path)
    selected, fire, _attempt = seed_contract_fire(runtime)
    discovery_budgets: list[float] = []
    construction_budgets: list[float] = []
    operation_budgets: list[float] = []

    def discover(**kwargs: object) -> connect.CapabilityCatalog:
        remaining_timeout = kwargs.get("remaining_timeout")
        assert callable(remaining_timeout)
        budget = remaining_timeout()
        assert isinstance(budget, float)
        discovery_budgets.append(budget)
        return connect.CapabilityCatalog((selected,))

    class BudgetedGmail(SeededMailboxGateway):
        def set_operation_timeout(self, timeout_seconds: float) -> None:
            operation_budgets.append(timeout_seconds)

        def attachment_bytes(self, *args: object) -> bytes:
            return PDF

    def load_budgeted_gmail(*args: object) -> BudgetedGmail:
        assert len(args) == 3
        remaining_timeout = args[2]
        assert callable(remaining_timeout)
        budget = remaining_timeout()
        assert isinstance(budget, float)
        construction_budgets.append(budget)
        return BudgetedGmail()

    install_automation_dispatch_fakes(monkeypatch, runtime, discover)
    monkeypatch.setattr(
        engine_api.GmailGateway,
        "from_token",
        load_budgeted_gmail,
    )
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)

    engine_api._dispatch_automation_fires(runtime, limit=25)

    submitted = runtime.store.automation_fire(fire.fire_id)
    assert submitted is not None
    assert submitted.state == "submitted"
    assert len(discovery_budgets) == 1
    assert len(construction_budgets) == 1
    assert len(operation_budgets) == 1
    assert 0 < operation_budgets[0] <= construction_budgets[0] <= discovery_budgets[0] <= 5


def test_interactive_join_authorizes_automation_origin_job_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_pump = engine_api._pump_generic_connect_lane
    config_path, runtime = seeded_runtime(tmp_path)
    selected, fire, attempt = seed_contract_fire(runtime)
    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)

    automated = engine_api._response(api_request(config_path, "connect.queue.pump"))
    assert (
        runtime.store.connect_job_requires_automation_entitlement(
            attempt.dispatch_request_id
        )
        is True
    )
    claimed = runtime.store.claim_connect_lane_head(
        provider_app_id=selected.app_id,
        provider_instance_id=selected.instance_id,
        expected_job_id=attempt.dispatch_request_id,
    )
    assert claimed is not None
    engine_api._defer_inactive_automation_job(
        runtime,
        attempt.dispatch_request_id,
        expected_dispatch_state="dispatching",
        next_dispatch_state="waiting",
    )
    with runtime.store.connection() as db:
        db.execute(
            """UPDATE connect_job_dispatch SET automation_paused_at = ?
            WHERE job_id = ?""",
            (
                (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
                attempt.dispatch_request_id,
            ),
        )
    paused_before_click = runtime.store.automation_fire(fire.fire_id)
    dispatch_before_click = runtime.store.connect_dispatch(attempt.dispatch_request_id)
    assert paused_before_click is not None
    assert paused_before_click.state == "entitlement_paused"
    assert dispatch_before_click is not None
    assert dispatch_before_click.automation_paused_at is not None
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: False)
    interactive = engine_api._response(
        api_request(
            config_path,
            "connect.attachment.invoke",
            invocation_payload(selected, parameters={"mode": "contract"}),
        )
    )

    assert automated["ok"] is True
    assert interactive["data"]["job_id"] == attempt.dispatch_request_id
    assert (
        runtime.store.connect_job_requires_automation_entitlement(
            attempt.dispatch_request_id
        )
        is False
    )
    authorized_dispatch = runtime.store.connect_dispatch(attempt.dispatch_request_id)
    still_paused = runtime.store.automation_fire(fire.fire_id)
    assert authorized_dispatch is not None
    assert authorized_dispatch.automation_paused_at is None
    assert datetime.fromisoformat(authorized_dispatch.admission_deadline) > datetime.fromisoformat(
        dispatch_before_click.admission_deadline
    )
    assert still_paused is not None
    assert still_paused.state == "entitlement_paused"
    with runtime.store.connection() as db:
        db.execute(
            """UPDATE connect_job_dispatch
            SET state = 'waiting', submission_possible = 0,
                next_attempt_at = '2000-01-01T00:00:00+00:00'
            WHERE job_id = ?""",
            (attempt.dispatch_request_id,),
        )
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: False)
    connect_authority_checks = 0
    run_calls = 0

    def require_connect() -> None:
        nonlocal connect_authority_checks
        connect_authority_checks += 1

    def run_joined_job(*args: object, **kwargs: object) -> None:
        nonlocal run_calls
        run_calls += 1

    monkeypatch.setattr(engine_api.connect, "require_connect_entitlement", require_connect)
    monkeypatch.setattr(
        engine_api,
        "_discover_persisted_generic_capability",
        lambda job, dispatch, *, require_entitlement: (selected, None),
    )
    monkeypatch.setattr(engine_api, "_run_claimed_generic_connect_job", run_joined_job)
    active = runtime.store.connect_job(attempt.dispatch_request_id)
    assert active is not None

    outcome = real_pump(runtime, active)

    assert outcome["outcome"] == "entitlement_paused"
    assert connect_authority_checks == 1
    assert run_calls == 1
    paused = runtime.store.automation_fire(fire.fire_id)
    dispatch = runtime.store.connect_dispatch(attempt.dispatch_request_id)
    assert paused is not None
    assert paused.state == "entitlement_paused"
    assert dispatch is not None
    assert dispatch.automation_paused_at is None

    output = connect.CapabilityOutput(
        artifact_id=OUTPUT_ID,
        media_type="application/vnd.local-connect.cited-summary+json",
        display_name="contract-summary.json",
        byte_size=2,
        sha256=hashlib.sha256(b"{}").hexdigest(),
        payload=b"{}",
    )
    runtime.store.transition_connect_job(
        job_id=attempt.dispatch_request_id,
        expected_state="requested",
        next_state="completed",
        provider_app_id=selected.app_id,
        provider_instance_id=selected.instance_id,
        result=connect.CapabilityResult((output,)).store_dict(),
    )
    engine_api._settle_submitted_automation_fires(runtime, limit=25)
    still_paused = runtime.store.automation_fire(fire.fire_id)
    assert still_paused is not None
    assert still_paused.state == "entitlement_paused"
    assert runtime.store.automation_fire_settlement_due() is False

    assert runtime.store.delete_message(fire.message_id) is True
    retained = runtime.store.automation_fire(fire.fire_id)
    assert retained is not None
    assert retained.state == "entitlement_paused"
    assert retained.reason == "entitlement_inactive"
    assert runtime.store.connect_job(attempt.dispatch_request_id) is not None

    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    engine_api._dispatch_automation_fire(runtime, fire.fire_id)
    engine_api._settle_submitted_automation_fires(runtime, limit=25)
    completed = runtime.store.automation_fire(fire.fire_id)
    assert completed is not None
    assert completed.state == "completed"


@pytest.mark.parametrize("terminal_status", ["completed", "failed"])
def test_interactive_join_returns_terminal_job_when_authority_race_is_lost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal_status: str,
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected, _fire, attempt = seed_contract_fire(runtime)
    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    engine_api._response(api_request(config_path, "connect.queue.pump"))
    original_authorize = runtime.store.authorize_connect_job_interactively
    race_won = False

    def complete_then_authorize(job_id: str, *, now: datetime | None = None) -> object:
        nonlocal race_won
        if not race_won:
            if terminal_status == "completed":
                output = connect.CapabilityOutput(
                    artifact_id=OUTPUT_ID,
                    media_type="application/vnd.local-connect.cited-summary+json",
                    display_name="contract-summary.json",
                    byte_size=2,
                    sha256=hashlib.sha256(b"{}").hexdigest(),
                    payload=b"{}",
                )
                runtime.store.transition_connect_job(
                    job_id=job_id,
                    expected_state="requested",
                    next_state="completed",
                    provider_app_id=selected.app_id,
                    provider_instance_id=selected.instance_id,
                    result=connect.CapabilityResult((output,)).store_dict(),
                )
            else:
                runtime.store.transition_connect_job(
                    job_id=job_id,
                    expected_state="requested",
                    next_state="failed",
                    provider_app_id=selected.app_id,
                    provider_instance_id=selected.instance_id,
                    error={
                        "code": "PDF_MALFORMED",
                        "message": "Invalid PDF",
                        "retryable": False,
                    },
                )
            race_won = True
        return original_authorize(job_id, now=now)

    monkeypatch.setattr(
        runtime.store,
        "authorize_connect_job_interactively",
        complete_then_authorize,
    )

    response = engine_api._response(
        api_request(
            config_path,
            "connect.attachment.invoke",
            invocation_payload(selected, parameters={"mode": "contract"}),
        )
    )

    assert race_won is True
    if terminal_status == "completed":
        assert response["ok"] is True
        assert response["data"]["job_id"] == attempt.dispatch_request_id
        assert response["data"]["status"] == "completed"
    else:
        assert response["ok"] is False
        assert response["error"]["code"] == "pdf_malformed"


def test_inbox_keeps_completed_automation_result_when_newer_retry_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected, fire, attempt = seed_contract_fire(runtime)
    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    engine_api._response(api_request(config_path, "connect.queue.pump"))
    output = connect.CapabilityOutput(
        artifact_id=OUTPUT_ID,
        media_type="application/vnd.local-connect.cited-summary+json",
        display_name="contract-summary.json",
        byte_size=2,
        sha256=hashlib.sha256(b"{}").hexdigest(),
        payload=b"{}",
    )
    runtime.store.transition_connect_job(
        job_id=attempt.dispatch_request_id,
        expected_state="requested",
        next_state="completed",
        provider_app_id=selected.app_id,
        provider_instance_id=selected.instance_id,
        result=connect.CapabilityResult((output,)).store_dict(),
    )
    engine_api._settle_submitted_automation_fires(runtime, limit=25)
    completed_fire = runtime.store.automation_fire(fire.fire_id)
    assert completed_fire is not None
    assert completed_fire.state == "completed"

    _candidate, retry, collision, _content = (
        engine_api._prepare_or_create_generic_connect_job(
            runtime,
            request_id=SECOND_REQUEST_ID,
            message_id=fire.message_id,
            part_id=fire.part_id,
            capability=selected,
            parameters={"mode": "contract"},
            confirmed=False,
        )
    )
    assert retry is not None
    assert retry.job_id == SECOND_REQUEST_ID
    assert collision is False
    runtime.store.transition_connect_job(
        job_id=retry.job_id,
        expected_state="requested",
        next_state="failed",
        provider_app_id=selected.app_id,
        provider_instance_id=selected.instance_id,
        error={"code": "MODEL_UNAVAILABLE", "message": "Try again", "retryable": True},
    )

    results = runtime.store.recent(1)[0]["attachments"][0]["capability_results"]
    by_job_id = {result["job_id"]: result for result in results}
    assert set(by_job_id) == {attempt.dispatch_request_id, SECOND_REQUEST_ID}
    assert by_job_id[attempt.dispatch_request_id]["status"] == "completed"
    assert by_job_id[attempt.dispatch_request_id]["outputs"][0]["sha256"] == output.sha256
    assert by_job_id[SECOND_REQUEST_ID]["status"] == "failed"


def test_effectful_automation_collision_fails_closed_before_active_job_join(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected, fire, _attempt = seed_contract_fire(runtime, external_effects=True)
    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    interactive = engine_api._response(
        api_request(
            config_path,
            "connect.attachment.invoke",
            invocation_payload(
                selected,
                parameters={"mode": "contract"},
                confirmed=True,
            ),
        )
    )
    assert interactive["data"]["job_id"] == REQUEST_ID

    engine_api._response(api_request(config_path, "connect.queue.pump"))
    awaiting = runtime.store.automation_fire(fire.fire_id)
    assert awaiting is not None
    assert awaiting.state == "awaiting_confirmation"
    assert awaiting.prepared_identity_sha256 is not None
    engine_api._response(
        api_request(
            config_path,
            "automation.fire.decide",
            {
                "fire_id": fire.fire_id,
                "expected_version": awaiting.state_version,
                "prepared_identity_sha256": awaiting.prepared_identity_sha256,
                "decision": "confirmed",
            },
        )
    )

    engine_api._response(api_request(config_path, "connect.queue.pump"))

    collision = runtime.store.automation_fire(fire.fire_id)
    assert collision is not None
    assert collision.state == "manual_review"
    assert collision.reason == "effectful_job_active"
    assert collision.job_id is None
    with runtime.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM connect_attachment_jobs").fetchone()[0] == 1


def test_effectful_exact_automation_attempt_recovers_after_concurrent_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, runtime = seeded_runtime(tmp_path)
    selected, fire, attempt = seed_contract_fire(runtime, external_effects=True)
    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    common = {
        "request_id": attempt.dispatch_request_id,
        "message_id": fire.message_id,
        "part_id": fire.part_id,
        "capability": selected,
        "parameters": {"mode": "contract"},
        "confirmed": True,
        "artifact_id": engine_api._automation_artifact_id(attempt.dispatch_request_id),
        "join_effectful": False,
    }
    _candidate, created, collision, _content = engine_api._prepare_or_create_generic_connect_job(
        runtime, **common
    )
    assert created is not None
    assert collision is False

    _candidate, recovered, collision, _content = engine_api._prepare_or_create_generic_connect_job(
        runtime, **common
    )

    assert recovered is not None
    assert recovered.job_id == attempt.dispatch_request_id
    assert collision is True
    dispatch = runtime.store.connect_dispatch(recovered.job_id)
    assert dispatch is not None
    assert dispatch.capability_external_effects is True


def test_confirmed_pending_automation_is_deleted_with_its_source_and_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected, fire, _attempt = seed_contract_fire(runtime, confirm_each=True)
    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    engine_api._response(api_request(config_path, "connect.queue.pump"))
    awaiting = runtime.store.automation_fire(fire.fire_id)
    assert awaiting is not None
    assert awaiting.prepared_identity_sha256 is not None
    engine_api._response(
        api_request(
            config_path,
            "automation.fire.decide",
            {
                "fire_id": fire.fire_id,
                "expected_version": awaiting.state_version,
                "prepared_identity_sha256": awaiting.prepared_identity_sha256,
                "decision": "confirmed",
            },
        )
    )

    assert runtime.store.delete_message("message-1") is True

    assert runtime.store.automation_fire(fire.fire_id) is None
    assert runtime.store.automation_fire_attempts(fire.fire_id) == []
    with runtime.store.connection() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM automation_fire_confirmations WHERE fire_id = ?",
                (fire.fire_id,),
            ).fetchone()[0]
            == 0
        )


def test_provider_owned_paused_fire_survives_source_delete_and_records_terminal_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config_path, runtime = seeded_runtime(tmp_path)
    selected, fire, attempt = seed_contract_fire(runtime)
    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    engine_api._dispatch_automation_fire(runtime, fire.fire_id)
    runtime.store.transition_connect_job(
        job_id=attempt.dispatch_request_id,
        expected_state="requested",
        next_state="accepted",
        provider_app_id=selected.app_id,
        provider_instance_id=selected.instance_id,
    )
    submitted = runtime.store.automation_fire(fire.fire_id)
    assert submitted is not None
    runtime.store.transition_automation_fire(
        fire_id=fire.fire_id,
        expected_state=submitted.state,
        expected_version=submitted.state_version,
        next_state="entitlement_paused",
        reason="entitlement_inactive",
    )
    with runtime.store.connection() as db:
        db.executescript(
            """DROP TRIGGER messages_delete_pending_automation_fires;
            CREATE TRIGGER messages_delete_pending_automation_fires
            BEFORE DELETE ON messages
            BEGIN
                DELETE FROM automation_fires
                WHERE message_id = OLD.message_id AND state = 'entitlement_paused';
            END;"""
        )
    runtime.store.initialize()

    assert runtime.store.delete_message("message-1") is True

    retained = runtime.store.automation_fire(fire.fire_id)
    assert retained is not None
    assert retained.state == "entitlement_paused"
    output = connect.CapabilityOutput(
        artifact_id=OUTPUT_ID,
        media_type="application/vnd.local-connect.cited-summary+json",
        display_name="contract-summary.json",
        byte_size=2,
        sha256=hashlib.sha256(b"{}").hexdigest(),
        payload=b"{}",
    )
    runtime.store.transition_connect_job(
        job_id=attempt.dispatch_request_id,
        expected_state="accepted",
        next_state="completed",
        provider_app_id=selected.app_id,
        provider_instance_id=selected.instance_id,
        result=connect.CapabilityResult((output,)).store_dict(),
    )

    completed = runtime.store.automation_fire(fire.fire_id)
    assert completed is not None
    assert completed.state == "completed"
    assert completed.reason == "connect_completed"
    assert runtime.store.connect_job(attempt.dispatch_request_id) is None


@pytest.mark.parametrize(
    ("terminal_status", "expected_reason"),
    (("completed", "connect_completed"), ("failed", "PDF_MALFORMED")),
)
def test_source_delete_preserves_unsettled_terminal_automation_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal_status: str,
    expected_reason: str,
) -> None:
    _config_path, runtime = seeded_runtime(tmp_path)
    selected, fire, attempt = seed_contract_fire(runtime)
    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    engine_api._dispatch_automation_fire(runtime, fire.fire_id)
    if terminal_status == "completed":
        output = connect.CapabilityOutput(
            artifact_id=OUTPUT_ID,
            media_type="application/vnd.local-connect.cited-summary+json",
            display_name="contract-summary.json",
            byte_size=2,
            sha256=hashlib.sha256(b"{}").hexdigest(),
            payload=b"{}",
        )
        runtime.store.transition_connect_job(
            job_id=attempt.dispatch_request_id,
            expected_state="requested",
            next_state="completed",
            provider_app_id=selected.app_id,
            provider_instance_id=selected.instance_id,
            result=connect.CapabilityResult((output,)).store_dict(),
        )
    else:
        runtime.store.transition_connect_job(
            job_id=attempt.dispatch_request_id,
            expected_state="requested",
            next_state="failed",
            provider_app_id=selected.app_id,
            provider_instance_id=selected.instance_id,
            error={"code": "PDF_MALFORMED", "message": "Invalid PDF", "retryable": False},
        )

    assert runtime.store.delete_message("message-1") is True

    settled = runtime.store.automation_fire(fire.fire_id)
    assert settled is not None
    assert settled.state == terminal_status
    assert settled.reason == expected_reason
    assert runtime.store.connect_job(attempt.dispatch_request_id) is None


def test_active_pending_dispatch_ceiling_stops_before_provider_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    _selected, fire, attempt = seed_contract_fire(runtime)
    with runtime.store.connection() as db:
        db.execute(
            """UPDATE automation_fires
            SET pending_since = ?, authorized_pending_seconds = 7200
            WHERE fire_id = ?""",
            (datetime.now(UTC).isoformat(), fire.fire_id),
        )
    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("stalled automation must not discover a provider")
        ),
    )

    response = engine_api._response(api_request(config_path, "connect.queue.pump"))

    assert response["ok"] is True
    halted = runtime.store.automation_fire(fire.fire_id)
    assert halted is not None
    assert halted.state == "manual_review"
    assert halted.reason == "dispatch_stalled"
    assert runtime.store.connect_job(attempt.dispatch_request_id) is None


def test_entitlement_pause_preserves_only_observed_authorized_pending_time(
    tmp_path: Path,
) -> None:
    _config_path, runtime = seeded_runtime(tmp_path)
    _selected, fire, _attempt = seed_contract_fire(runtime)
    observed_at = datetime(2026, 9, 17, 12, tzinfo=UTC)
    with runtime.store.connection() as db:
        db.execute(
            """UPDATE automation_fires
            SET pending_since = ?, authorized_pending_seconds = 90
            WHERE fire_id = ?""",
            ((observed_at - timedelta(hours=3)).isoformat(), fire.fire_id),
        )
    current = runtime.store.automation_fire(fire.fire_id)
    assert current is not None

    paused = runtime.store.transition_automation_fire(
        fire_id=current.fire_id,
        expected_state=current.state,
        expected_version=current.state_version,
        next_state="entitlement_paused",
        reason="entitlement_inactive",
        now=observed_at,
    )

    assert paused.authorized_pending_seconds == 90
    resumed = runtime.store.transition_automation_fire(
        fire_id=paused.fire_id,
        expected_state=paused.state,
        expected_version=paused.state_version,
        next_state="pending_dispatch",
        now=observed_at + timedelta(minutes=5),
    )
    assert runtime.store.automation_pending_seconds(
        resumed,
        now=observed_at + timedelta(minutes=5),
    ) == 90


def test_submitted_automation_settles_from_real_completed_connect_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected, fire, attempt = seed_contract_fire(runtime)
    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    engine_api._response(api_request(config_path, "connect.queue.pump"))
    output = connect.CapabilityOutput(
        artifact_id=OUTPUT_ID,
        media_type="application/vnd.local-connect.cited-summary+json",
        display_name="contract-summary.json",
        byte_size=2,
        sha256=hashlib.sha256(b"{}").hexdigest(),
        payload=b"{}",
    )
    runtime.store.transition_connect_job(
        job_id=attempt.dispatch_request_id,
        expected_state="requested",
        next_state="completed",
        provider_app_id=selected.app_id,
        provider_instance_id=selected.instance_id,
        result=connect.CapabilityResult((output,)).store_dict(),
    )
    assert runtime.store.automation_fire_settlement_due() is True

    response = engine_api._response(api_request(config_path, "connect.queue.pump"))

    assert response["ok"] is True
    completed = runtime.store.automation_fire(fire.fire_id)
    job = runtime.store.connect_job(attempt.dispatch_request_id)
    assert completed is not None
    assert completed.state == "completed"
    assert completed.reason == "connect_completed"
    assert job is not None
    assert job.status == "completed"
    assert runtime.store.completed_connect_outputs(job)[0].sha256 == output.sha256


def test_non_deadline_provider_failure_settles_without_another_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected, fire, attempt = seed_contract_fire(runtime)
    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    engine_api._response(api_request(config_path, "connect.queue.pump"))
    runtime.store.transition_connect_job(
        job_id=attempt.dispatch_request_id,
        expected_state="requested",
        next_state="failed",
        provider_app_id=selected.app_id,
        provider_instance_id=selected.instance_id,
        error={
            "code": "MODEL_UNAVAILABLE",
            "message": "The local model is unavailable.",
            "retryable": True,
        },
    )

    response = engine_api._response(api_request(config_path, "connect.queue.pump"))

    assert response["ok"] is True
    failed = runtime.store.automation_fire(fire.fire_id)
    assert failed is not None
    assert failed.state == "failed"
    assert failed.reason == "MODEL_UNAVAILABLE"
    assert len(runtime.store.automation_fire_attempts(fire.fire_id)) == 1


def test_deleting_linked_connect_job_settles_automation_source_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config_path, runtime = seeded_runtime(tmp_path)
    selected, fire, attempt = seed_contract_fire(runtime)
    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    engine_api._dispatch_automation_fire(runtime, fire.fire_id)

    with runtime.store.connection() as db:
        db.execute(
            "DELETE FROM connect_attachment_jobs WHERE job_id = ?",
            (attempt.dispatch_request_id,),
        )

    settled = runtime.store.automation_fire(fire.fire_id)
    assert settled is not None
    assert settled.state == "source_unavailable"
    assert settled.reason == "job_removed"
    assert settled.job_id is None


def test_automation_allows_one_proven_unaccepted_retry_then_requires_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected, fire, first_attempt = seed_contract_fire(runtime, confirm_each=True)
    install_automation_dispatch_fakes(
        monkeypatch,
        runtime,
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)

    engine_api._response(api_request(config_path, "connect.queue.pump"))
    awaiting = runtime.store.automation_fire(fire.fire_id)
    assert awaiting is not None
    assert awaiting.prepared_identity_sha256 is not None
    engine_api._response(
        api_request(
            config_path,
            "automation.fire.decide",
            {
                "fire_id": fire.fire_id,
                "expected_version": awaiting.state_version,
                "prepared_identity_sha256": awaiting.prepared_identity_sha256,
                "decision": "confirmed",
            },
        )
    )
    engine_api._response(api_request(config_path, "connect.queue.pump"))
    assert runtime.store.connect_job(first_attempt.dispatch_request_id) is not None

    force_unaccepted_admission_deadline(runtime, first_attempt.dispatch_request_id)
    engine_api._response(api_request(config_path, "connect.queue.pump"))

    retrying = runtime.store.automation_fire(fire.fire_id)
    attempts = runtime.store.automation_fire_attempts(fire.fire_id)
    assert retrying is not None
    assert retrying.state == "pending_dispatch"
    assert retrying.current_attempt_no == 2
    assert retrying.prepared_identity_sha256 is None
    assert retrying.prepared_identity_json is None
    assert retrying.confirmed is False
    assert len(attempts) == 2
    assert attempts[0].dispatch_request_id == first_attempt.dispatch_request_id
    assert attempts[1].job_id is None
    with runtime.store.connection() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM automation_fire_confirmations WHERE fire_id = ?",
                (fire.fire_id,),
            ).fetchone()[0]
            == 0
        )

    engine_api._response(api_request(config_path, "connect.queue.pump"))
    second_awaiting = runtime.store.automation_fire(fire.fire_id)
    assert second_awaiting is not None
    assert second_awaiting.state == "awaiting_confirmation"
    assert second_awaiting.prepared_identity_sha256 is not None
    engine_api._response(
        api_request(
            config_path,
            "automation.fire.decide",
            {
                "fire_id": fire.fire_id,
                "expected_version": second_awaiting.state_version,
                "prepared_identity_sha256": second_awaiting.prepared_identity_sha256,
                "decision": "confirmed",
            },
        )
    )
    engine_api._response(api_request(config_path, "connect.queue.pump"))
    second_attempt = runtime.store.automation_fire_attempts(fire.fire_id)[1]
    assert second_attempt.job_id == second_attempt.dispatch_request_id
    force_unaccepted_admission_deadline(runtime, second_attempt.dispatch_request_id)

    engine_api._response(api_request(config_path, "connect.queue.pump"))

    halted = runtime.store.automation_fire(fire.fire_id)
    assert halted is not None
    assert halted.state == "manual_review"
    assert halted.reason == "second_admission_deadline"
    assert len(runtime.store.automation_fire_attempts(fire.fire_id)) == 2


@pytest.mark.parametrize(
    ("blocked_outcome", "blocked_retry_seconds", "other_lane_seconds"),
    [
        ("lock_contended", 2, 1),
        ("lock_unavailable", 30, 10),
        ("deferred_or_failed", 30, 10),
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


@pytest.mark.parametrize(
    ("outcome", "retry_seconds"),
    [
        ("lock_contended", 2),
        ("lock_unavailable", 30),
        ("deferred_or_failed", 30),
    ],
)
def test_connect_queue_wakeup_backs_off_an_attempted_head_still_due(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    retry_seconds: int,
) -> None:
    _, runtime = seeded_runtime(tmp_path)
    observed_at = datetime(2026, 9, 9, 12, tzinfo=UTC)
    monkeypatch.setattr(
        runtime.store,
        "connect_queue_wakeups",
        lambda *, now: [(REQUEST_ID, observed_at)],
    )

    next_wakeup = engine_api._next_connect_queue_wakeup(
        runtime,
        [{"job_id": REQUEST_ID, "outcome": outcome}],
        observed_at,
    )

    assert next_wakeup == observed_at + timedelta(seconds=retry_seconds)


def test_queue_pump_continues_terminal_fire_settlement_past_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, runtime = seeded_runtime(tmp_path)

    class FakeGmail(SeededMailboxGateway):
        def attachment_bytes(self, *args: object) -> bytes:
            return PDF

    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: FakeGmail())
    mode = connect.CapabilityParameter(
        name="mode",
        value_type="string",
        required=False,
        label="Summary mode",
        description="Choose general, story, or contract. Defaults to general.",
    )
    selected = capability(
        app_id="document-summarizer",
        app_version="0.1.0",
        capability_id="document.summarize",
        parameters=(mode,),
    )
    for index in range(26):
        runtime.store.put_automation_rule(
            contract_rule_definition(selected, name=f"Contract watch {index}")
        )
    runtime.store.mark_analyzed(
        "message-1",
        {
            "category": "informational",
            "priority": "normal",
            "summary": "A contract arrived.",
            "action_required": True,
            "suggested_action": "Review the contract.",
            "deadline_text": None,
            "deadline_iso": None,
            "confidence": 0.9,
        },
        mailbox_identity_key=TEST_MAILBOX_IDENTITY_KEY,
    )
    fires = runtime.store.automation_fires_for_message("message-1")
    first_attempt = runtime.store.automation_fire_attempts(fires[0].fire_id)[0]
    _candidate, job, _collision, _content = engine_api._prepare_or_create_generic_connect_job(
        runtime,
        request_id=first_attempt.dispatch_request_id,
        message_id="message-1",
        part_id="2",
        capability=selected,
        parameters={"mode": "contract"},
        confirmed=False,
        artifact_id=engine_api._automation_artifact_id(first_attempt.dispatch_request_id),
    )
    assert job is not None
    output = connect.CapabilityOutput(
        artifact_id=OUTPUT_ID,
        media_type="application/vnd.local-connect.cited-summary+json",
        display_name="contract-summary.json",
        byte_size=2,
        sha256=hashlib.sha256(b"{}").hexdigest(),
        payload=b"{}",
    )
    runtime.store.transition_connect_job(
        job_id=job.job_id,
        expected_state="requested",
        next_state="completed",
        provider_app_id=selected.app_id,
        provider_instance_id=selected.instance_id,
        result=connect.CapabilityResult((output,)).store_dict(),
    )
    for fire in fires:
        runtime.store.transition_automation_fire(
            fire_id=fire.fire_id,
            expected_state=fire.state,
            expected_version=fire.state_version,
            next_state="submitted",
            reason="connect_admitted",
            job_id=job.job_id,
        )
    monkeypatch.setattr(
        engine_api,
        "_dispatch_automation_fire",
        lambda active_runtime, fire_id: None,
    )

    outcome = engine_api.pump_connect_runtime(runtime, 25)

    unsettled = runtime.store.automation_fires_in_states(("submitted",), limit=25)
    assert len(fires) == 26
    assert len(unsettled) == 1
    assert outcome["next_wake_unix_ms"] is not None


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
        capability_produces=selected.produces,
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


def test_active_result_replays_terminal_job_won_during_dispatch_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, runtime = seeded_runtime(tmp_path)
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
        capability_produces=selected.produces,
    )
    active = runtime.store.connect_job(REQUEST_ID)
    assert active is not None
    real_dispatch = runtime.store.connect_dispatch
    raced = False

    def racing_dispatch(job_id: str):
        nonlocal raced
        if not raced:
            raced = True
            output = connect.CapabilityOutput(
                artifact_id=OUTPUT_ID,
                media_type="text/plain",
                display_name="translation.txt",
                byte_size=4,
                sha256=hashlib.sha256(b"done").hexdigest(),
                payload=b"done",
            )
            runtime.store.transition_connect_job(
                job_id=job_id,
                expected_state="requested",
                next_state="completed",
                provider_app_id=selected.app_id,
                provider_instance_id=selected.instance_id,
                result=connect.CapabilityResult((output,)).store_dict(),
            )
        return real_dispatch(job_id)

    monkeypatch.setattr(runtime.store, "connect_dispatch", racing_dispatch)

    result = engine_api._generic_connect_active_result(runtime, active)

    assert raced is True
    assert result["status"] == "completed"
    assert result["outputs"][0]["artifact_id"] == OUTPUT_ID


def seeded_runtime(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.config.gmail_token_file.write_text("connected token", encoding="utf-8")
    runtime.store.reconcile_mailbox_identity(
        DEFAULT_MAIL_PROVIDER,
        DEFAULT_MAIL_ACCOUNT_ID,
        TEST_MAILBOX_IDENTITY_KEY,
        legacy_status="replacement",
    )
    runtime.store.add_message(
        message_id="message-1",
        thread_id=None,
        sender="private@example.com",
        sender_name="Private Sender",
        subject="Private subject",
        received_at="2026-08-30T12:00:00+00:00",
        mailbox_identity_key=TEST_MAILBOX_IDENTITY_KEY,
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


def seeded_imap_runtime(tmp_path: Path, *, descriptor_size: int):
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
    runtime.store.reconcile_mailbox_identity(
        account.provider,
        account.account_id,
        TEST_MAILBOX_IDENTITY_KEY,
        legacy_status="replacement",
    )
    credentials_file = mail_account_token_file(runtime.config, account)
    credentials_file.parent.mkdir(parents=True)
    credentials_file.write_text("private credentials", encoding="utf-8")
    runtime.store.add_message(
        message_id="message-1",
        provider="imap",
        account_id=account.account_id,
        provider_message_id="provider-message",
        thread_id=None,
        sender="private@example.com",
        sender_name="Private Sender",
        subject="Private subject",
        received_at="2026-09-12T12:00:00+00:00",
        mailbox_identity_key=TEST_MAILBOX_IDENTITY_KEY,
    )
    runtime.store.replace_attachments(
        "message-1",
        (
            AttachmentDescriptor(
                "mime-0",
                None,
                "invoice.pdf",
                "application/pdf",
                descriptor_size,
                0,
            ),
        ),
    )
    return config_path, runtime, credentials_file


def seed_second_attachment(runtime: Runtime) -> None:
    runtime.store.add_message(
        message_id="message-2",
        thread_id=None,
        sender="second@example.com",
        sender_name="Second Sender",
        subject="Second private subject",
        received_at="2026-08-30T12:01:00+00:00",
        mailbox_identity_key=TEST_MAILBOX_IDENTITY_KEY,
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


def registered_capability(
    selected: connect.DiscoveredCapability,
) -> connect.RegisteredCapability:
    return connect.RegisteredCapability(
        protocol_version=selected.protocol_version,
        base_url=selected.base_url,
        token=selected.token,
        app_id=selected.app_id,
        app_version=selected.app_version,
        instance_id=selected.instance_id,
        capability_id=selected.capability_id,
        capability_version=selected.capability_version,
        produces=selected.produces,
        external_effects=selected.external_effects,
        confirmation_required=selected.confirmation_required,
    )


def contract_rule_definition(
    selected: connect.DiscoveredCapability,
    *,
    name: str = "Contract watch",
    confirm_each: bool = False,
    parameters: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "name": name,
        "scope": {},
        "trigger": {"source_kind": "mail.message"},
        "conditions": [
            {
                "field": "attachment.media_type",
                "op": "equals",
                "value": "application/pdf",
            }
        ],
        "action": {
            "kind": "connect.invoke",
            "capability": {
                "id": selected.capability_id,
                "version": selected.capability_version,
            },
            "provider": {
                "app_id": selected.app_id,
                "version": selected.app_version,
                "instance_id": selected.instance_id,
            },
            "parameters": parameters or {"mode": "contract"},
        },
        "confirm_each": confirm_each,
    }


def seed_contract_fire(
    runtime: Runtime,
    *,
    confirm_each: bool = False,
    external_effects: bool = False,
) -> tuple[connect.DiscoveredCapability, object, object]:
    mode = connect.CapabilityParameter(
        name="mode",
        value_type="string",
        required=False,
        label="Summary mode",
        description="Choose general, story, or contract. Defaults to general.",
    )
    selected = capability(
        app_id="document-summarizer",
        app_version="0.1.0",
        capability_id="document.summarize",
        parameters=(mode,),
        external_effects=external_effects,
    )
    runtime.store.put_automation_rule(
        contract_rule_definition(selected, confirm_each=confirm_each)
    )
    runtime.store.mark_analyzed(
        "message-1",
        {
            "category": "informational",
            "priority": "normal",
            "summary": "A contract arrived.",
            "action_required": True,
            "suggested_action": "Review the contract.",
            "deadline_text": None,
            "deadline_iso": None,
            "confidence": 0.9,
        },
        mailbox_identity_key=TEST_MAILBOX_IDENTITY_KEY,
    )
    fire = runtime.store.automation_fires_for_message("message-1")[0]
    attempt = runtime.store.automation_fire_attempts(fire.fire_id)[0]
    return selected, fire, attempt


def install_automation_dispatch_fakes(
    monkeypatch: pytest.MonkeyPatch,
    runtime: Runtime,
    discover: object,
    *,
    stub_lane: bool = True,
) -> None:
    class FakeGmail(SeededMailboxGateway):
        def set_operation_timeout(self, _timeout_seconds: float) -> None:
            return

        def attachment_bytes(self, *args: object) -> bytes:
            return PDF

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(engine_api.connect, "discover_capabilities", discover)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: FakeGmail())
    if stub_lane:
        monkeypatch.setattr(
            engine_api,
            "_pump_generic_connect_lane",
            lambda active_runtime, head: engine_api._connect_queue_item(
                active_runtime, head.job_id, "queued"
            ),
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
        capability_produces=("text/plain",),
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


@pytest.mark.parametrize(
    ("descriptor_size", "expected_items"),
    [(MAX_IMAP_MESSAGE_BYTES, 1), (MAX_IMAP_MESSAGE_BYTES + 1, 0)],
)
def test_imap_capability_discovery_respects_the_local_fetch_ceiling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    descriptor_size: int,
    expected_items: int,
) -> None:
    config_path, runtime, _credentials_file = seeded_imap_runtime(
        tmp_path, descriptor_size=descriptor_size
    )
    accepted = capability()
    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities",
        lambda **kwargs: connect.CapabilityCatalog((accepted,)),
    )

    response = engine_api._response(
        api_request(
            config_path,
            "connect.attachment.capabilities",
            {"message_id": "message-1", "part_id": "mime-0"},
        )
    )

    assert response["ok"] is True
    assert len(response["data"]["items"]) == expected_items


@pytest.mark.parametrize("descriptor_size", [4096, MAX_IMAP_MESSAGE_BYTES])
def test_imap_generic_invoke_uses_actual_download_size_for_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, descriptor_size: int
) -> None:
    config_path, runtime, credentials_file = seeded_imap_runtime(
        tmp_path, descriptor_size=descriptor_size
    )
    selected = capability()
    payload = invocation_payload(selected)
    payload["part_id"] = "mime-0"

    class FakeImap(SeededMailboxGateway):
        def __init__(self) -> None:
            self.session_active = False

        @contextmanager
        def polling_session(self):
            assert self.session_active is False
            self.session_active = True
            try:
                yield
            finally:
                self.session_active = False

        def mailbox_identity_key(self) -> str:
            assert self.session_active, "identity read outside IMAP session"
            return TEST_MAILBOX_IDENTITY_KEY

        def attachment_bytes(self, *args: object) -> bytes:
            assert self.session_active, "attachment read outside IMAP session"
            assert args == ("provider-message", "mime-0", None)
            return PDF

    class CompletingClient:
        def __init__(self, capability_value: connect.DiscoveredCapability):
            assert capability_value == selected

        def submit(
            self, job: connect.PreparedCapabilityJob, content: bytes
        ) -> connect.CapabilityJobUpdate:
            assert job.artifact.byte_size == len(PDF)
            assert content == PDF
            return update(job, "accepted")

        def wait_for_terminal(
            self,
            job: connect.PreparedCapabilityJob,
            initial: connect.CapabilityJobUpdate,
            on_update,
        ) -> connect.CapabilityJobUpdate:
            assert initial.status == "accepted"
            completed = update(job, "completed", payload=b"done")
            on_update(completed)
            return completed

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities",
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(
        engine_api.ImapGateway,
        "from_credentials_file",
        lambda path: FakeImap() if path == credentials_file else pytest.fail(path),
    )
    monkeypatch.setattr(engine_api.connect, "ConnectV2Client", CompletingClient)

    response = engine_api._response(api_request(config_path, "connect.attachment.invoke", payload))

    assert response["ok"] is True
    job = runtime.store.connect_job(REQUEST_ID)
    assert job is not None
    assert job.input_byte_size == len(PDF)


def test_imap_generic_invoke_rejects_descriptor_over_local_fetch_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime, credentials_file = seeded_imap_runtime(
        tmp_path, descriptor_size=MAX_IMAP_MESSAGE_BYTES + 1
    )
    selected = capability()
    payload = invocation_payload(selected)
    payload["part_id"] = "mime-0"

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities",
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(
        engine_api.ImapGateway,
        "from_credentials_file",
        lambda path: pytest.fail(f"oversized descriptor opened {path}"),
    )
    monkeypatch.setattr(
        engine_api.connect,
        "ConnectV2Client",
        lambda *_args: pytest.fail("oversized descriptor reached the provider"),
    )

    response = engine_api._response(api_request(config_path, "connect.attachment.invoke", payload))

    assert response["error"]["code"] == "unsupported_attachment"
    assert runtime.store.connect_job(REQUEST_ID) is None
    assert credentials_file.exists()


def test_imap_generic_invoke_rechecks_local_fetch_ceiling_under_source_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime, credentials_file = seeded_imap_runtime(tmp_path, descriptor_size=1)
    selected = capability()
    payload = invocation_payload(selected)
    payload["part_id"] = "mime-0"

    class MutatingSourceLock:
        def __enter__(self) -> None:
            runtime.store.replace_attachments(
                "message-1",
                (
                    AttachmentDescriptor(
                        "mime-0",
                        None,
                        "invoice.pdf",
                        "application/pdf",
                        MAX_IMAP_MESSAGE_BYTES + 1,
                        0,
                    ),
                ),
            )

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities",
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(
        engine_api,
        "connect_operation_lock",
        lambda *_args: MutatingSourceLock(),
    )
    monkeypatch.setattr(
        engine_api.ImapGateway,
        "from_credentials_file",
        lambda path: pytest.fail(f"changed descriptor opened {path}"),
    )
    monkeypatch.setattr(
        engine_api.connect,
        "ConnectV2Client",
        lambda *_args: pytest.fail("changed descriptor reached the provider"),
    )

    response = engine_api._response(api_request(config_path, "connect.attachment.invoke", payload))

    assert response["error"]["code"] == "connect_source_unavailable"
    assert runtime.store.connect_job(REQUEST_ID) is None
    assert credentials_file.exists()


def test_imap_generic_invoke_rejects_actual_bytes_over_capability_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime, credentials_file = seeded_imap_runtime(tmp_path, descriptor_size=1)
    selected = capability()
    payload = invocation_payload(selected)
    payload["part_id"] = "mime-0"

    class OversizedImap(SeededMailboxGateway):
        def attachment_bytes(self, *args: object) -> bytes:
            return b"X" * 1025

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities",
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(
        engine_api.ImapGateway,
        "from_credentials_file",
        lambda path: OversizedImap() if path == credentials_file else pytest.fail(path),
    )
    monkeypatch.setattr(
        engine_api.connect,
        "ConnectV2Client",
        lambda *_args: pytest.fail("oversized IMAP content reached the provider"),
    )

    response = engine_api._response(api_request(config_path, "connect.attachment.invoke", payload))

    assert response["error"]["code"] == "connect_source_unavailable"
    assert runtime.store.connect_job(REQUEST_ID) is None


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
        lambda *args: (_ for _ in ()).throw(AssertionError("unentitled invocation reached Gmail")),
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
        capability_produces=selected.produces,
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
    missing_request_id = invocation_payload(selected, parameters={"target-language": "es"})
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

    class FakeGmail(SeededMailboxGateway):
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

    class FakeGmail(SeededMailboxGateway):
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

    class FakeGmail(SeededMailboxGateway):
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

    class FakeGmail(SeededMailboxGateway):
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

    class FakeGmail(SeededMailboxGateway):
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


def test_queue_pump_advances_other_provider_lane_without_waiting_for_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    first_capability = capability(instance_id=INSTANCE_A)
    second_capability = capability(instance_id=INSTANCE_B)
    first_job = connect.prepare_capability_job(
        first_capability,
        PDF,
        "application/pdf",
        "invoice.pdf",
        job_id=REQUEST_ID,
    )
    second_job = connect.prepare_capability_job(
        second_capability,
        PDF,
        "application/pdf",
        "invoice.pdf",
        job_id=SECOND_REQUEST_ID,
    )
    for job in (first_job, second_job):
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
            capability_produces=("text/plain",),
        )
    submissions: list[str] = []
    waits = 0

    class FakeGmail(SeededMailboxGateway):
        def attachment_bytes(self, *args) -> bytes:
            return PDF

    class OneRoundClient:
        def __init__(self, selected):
            self.selected = selected

        def submit(self, job, content):
            assert content == PDF
            submissions.append(job.job_id)
            if self.selected.instance_id == INSTANCE_A:
                return update(job, "accepted")
            return update(job, "completed", payload=b"done")

        def wait_for_terminal(self, job, initial, on_update):
            nonlocal waits
            waits += 1
            raise AssertionError("a scheduled queue pass must not wait for terminal state")

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities_for_reconciliation",
        lambda *, provider_instance_id: connect.CapabilityCatalog(
            tuple(
                candidate
                for candidate in (first_capability, second_capability)
                if candidate.instance_id == provider_instance_id
            )
        ),
    )
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities",
        lambda *, provider_instance_id: connect.CapabilityCatalog(
            tuple(
                candidate
                for candidate in (first_capability, second_capability)
                if candidate.instance_id == provider_instance_id
            )
        ),
    )
    monkeypatch.setattr(engine_api.connect, "ConnectV2Client", OneRoundClient)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: FakeGmail())

    response = engine_api._response(api_request(config_path, "connect.queue.pump"))

    assert response["ok"] is True
    assert submissions == [REQUEST_ID, SECOND_REQUEST_ID]
    assert waits == 0
    assert runtime.store.connect_job(REQUEST_ID).status == "accepted"  # type: ignore[union-attr]
    assert runtime.store.connect_job(SECOND_REQUEST_ID).status == "completed"  # type: ignore[union-attr]


def test_queue_pump_completes_provider_owned_head_then_drains_waiting_invoice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    seed_second_attachment(runtime)
    selected = capability()
    submissions: list[str] = []
    queries: list[str] = []

    class FakeGmail(SeededMailboxGateway):
        def attachment_bytes(self, provider_message_id, *args) -> bytes:
            assert provider_message_id in {"message-1", "message-2"}
            return PDF

    class SerializedClient:
        def __init__(self, capability_value):
            assert capability_value in {selected, registered_capability(selected)}

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
        "registered_capability_for_reconciliation",
        lambda **kwargs: (registered_capability(selected), None),
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

    class FakeGmail(SeededMailboxGateway):
        def attachment_bytes(self, *args) -> bytes:
            return PDF

    class LostAckThenCompleteClient:
        def __init__(self, capability_value):
            assert capability_value in {selected, registered_capability(selected)}

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

    def reconcile_registration(**kwargs):
        discovery_modes.append("reconciliation")
        return registered_capability(selected), None

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(engine_api.connect, "discover_capabilities", discover)
    monkeypatch.setattr(
        engine_api.connect,
        "registered_capability_for_reconciliation",
        reconcile_registration,
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

    class FakeGmail(SeededMailboxGateway):
        def attachment_bytes(self, *args) -> bytes:
            return PDF

    class AcceptedThenMissingClient:
        def __init__(self, capability_value):
            assert capability_value in {selected, registered_capability(selected)}

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
        "registered_capability_for_reconciliation",
        lambda **kwargs: (registered_capability(selected), None),
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

    class FakeGmail(SeededMailboxGateway):
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

    class FakeGmail(SeededMailboxGateway):
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

    class FakeGmail(SeededMailboxGateway):
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

    class FakeGmail(SeededMailboxGateway):
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

    class FlakyGmail(SeededMailboxGateway):
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


@pytest.mark.parametrize(
    "source_error",
    [
        MailboxMessageUnavailable("The message was removed"),
        MailboxMessageInvalid("imap_bodystructure_invalid", "malformed MIME"),
    ],
)
def test_queue_pump_treats_invalid_or_missing_mailbox_source_as_definitive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_error: MailboxError,
) -> None:
    config_path, runtime = seeded_runtime(tmp_path)
    selected = capability()
    reads = 0
    submissions = 0

    class DisappearingGmail(SeededMailboxGateway):
        def attachment_bytes(self, *args) -> bytes:
            nonlocal reads
            reads += 1
            if reads > 2:
                raise source_error
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

    class FakeGmail(SeededMailboxGateway):
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

    class FakeGmail(SeededMailboxGateway):
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

    class FakeGmail(SeededMailboxGateway):
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

    class FakeGmail(SeededMailboxGateway):
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

    class FakeGmail(SeededMailboxGateway):
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

    class FakeGmail(SeededMailboxGateway):
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

    class FakeGmail(SeededMailboxGateway):
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

    class FakeGmail(SeededMailboxGateway):
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
