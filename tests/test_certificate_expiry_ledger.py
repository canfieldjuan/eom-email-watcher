from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from connect_automate import connect
from test_connect_v2_engine_api import (
    INPUT_ARTIFACT_ID,
    INSTANCE_A,
    OUTPUT_ID,
    PDF,
    TEST_MAILBOX_IDENTITY_KEY,
    capability,
    contract_rule_definition,
    seeded_runtime,
)

from eom_email_watcher import db as db_module
from eom_email_watcher import engine_api
from eom_email_watcher.db import CertificateResultInvalid, Store, validate_certificate_result_json

CERTIFICATE_MEDIA_TYPE = "application/vnd.local-connect.certificate+json"
JOB_ID = "66666666-6666-4666-8666-666666666666"


def _provenance(text: str, span_id: str) -> dict[str, object]:
    return {
        "span_id": span_id,
        "page": 1,
        "bbox": [0.0, 0.0, 100.0, 10.0],
        "exact_text": text,
        "token_start": 0,
        "token_end": 1,
    }


def _text(text: str, span_id: str) -> dict[str, object]:
    return {"text": text, "whole_span": True, "provenance": _provenance(text, span_id)}


def _date(iso: str, span_id: str) -> dict[str, object]:
    return {
        "iso": iso,
        "ambiguous": False,
        "candidates": [iso],
        "provenance": _provenance(iso, span_id),
    }


def _policy(
    ordinal: int,
    expiration: dict[str, object] | None,
    *,
    reasons: list[str] | None = None,
) -> dict[str, object]:
    return {
        "coverage": _text(f"Coverage {ordinal}", f"coverage-{ordinal}"),
        "insurer": _text("Acme Mutual", f"insurer-{ordinal}"),
        "policy_number": _text(f"POL-{ordinal}", f"policy-{ordinal}"),
        "effective_date": _date("2026-01-01", f"effective-{ordinal}"),
        "expiration_date": expiration,
        "review_reasons": reasons or [],
    }


def _record(*, policies: list[dict[str, object]] | None = None) -> dict[str, object]:
    selected = policies
    if selected is None:
        selected = [
            _policy(0, _date("2026-09-19", "expiration-0")),
            _policy(1, _date("2026-09-20", "expiration-1")),
            _policy(2, _date("2026-09-21", "expiration-2")),
            _policy(
                3,
                {
                    "iso": None,
                    "ambiguous": True,
                    "candidates": ["2026-09-22", "2026-10-09"],
                    "provenance": _provenance("09/10/2026", "expiration-3"),
                },
                reasons=["EXPIRATION_DATE_AMBIGUOUS"],
            ),
        ]
    top_reasons = ["NO_POLICY_ROWS"] if not selected else []
    if any(policy["review_reasons"] for policy in selected):
        top_reasons.append("POLICY_DATE_REVIEW")
    return {
        "record_version": "1.0",
        "source": {
            "sha256": hashlib.sha256(PDF).hexdigest(),
            "byte_size": len(PDF),
            "page_count": 1,
            "display_name": "invoice.pdf",
            "parser": {"id": "pdfplumber", "version": "1", "settings": "native"},
        },
        "insured": _text("Northstar Services LLC", "insured"),
        "certificate_holder": _text("City of Effingham", "holder"),
        "producer": _text("Local Insurance", "producer"),
        "policies": selected,
        "withheld": [],
        "review": {"required": bool(top_reasons), "reasons": top_reasons},
        "extracted_at": "2026-09-20T12:00:00Z",
    }


def _use_integer_bbox_spellings(record: dict[str, object]) -> None:
    values: list[object] = [
        record["insured"],
        record["certificate_holder"],
        record["producer"],
    ]
    for policy in record["policies"]:
        assert isinstance(policy, dict)
        values.extend(policy.values())
    for value in values:
        if isinstance(value, dict) and isinstance(value.get("provenance"), dict):
            bbox = value["provenance"]["bbox"]
            assert isinstance(bbox, list)
            value["provenance"]["bbox"] = [int(coordinate) for coordinate in bbox]


def _capability_result(record: dict[str, object]) -> connect.CapabilityResult:
    payload = json.dumps(record, separators=(",", ":"), sort_keys=True).encode()
    output = connect.CapabilityOutput(
        artifact_id=OUTPUT_ID,
        media_type=CERTIFICATE_MEDIA_TYPE,
        display_name="certificate.json",
        byte_size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        payload=payload,
    )
    return connect.CapabilityResult((output,))


def _result(record: dict[str, object]) -> dict[str, object]:
    return _capability_result(record).store_dict()


def _certificate_job(store: Store, job_id: str = JOB_ID) -> None:
    request = {
        "protocol_version": 2,
        "job_id": job_id,
        "capability": {"id": "certificate.extract", "version": "1.0"},
        "inputs": [
            {
                "artifact_id": INPUT_ARTIFACT_ID,
                "media_type": "application/pdf",
                "byte_size": len(PDF),
                "sha256": hashlib.sha256(PDF).hexdigest(),
                "display_name": "invoice.pdf",
                "source_app_id": "eom-email-watcher",
            }
        ],
        "parameters": {},
    }
    store.create_connect_job(
        job_id=job_id,
        message_id="message-1",
        part_id="2",
        protocol_version=2,
        capability_id="certificate.extract",
        capability_version="1.0",
        provider_app_id="invoice-processor",
        provider_app_version="0.1.0",
        provider_instance_id=INSTANCE_A,
        input_artifact_id=INPUT_ARTIFACT_ID,
        input_media_type="application/pdf",
        input_byte_size=len(PDF),
        input_sha256=hashlib.sha256(PDF).hexdigest(),
        input_display_name="invoice.pdf",
        source_app_id="eom-email-watcher",
        request_json=json.dumps(request, separators=(",", ":")).encode(),
        capability_produces=(CERTIFICATE_MEDIA_TYPE,),
    )


def _certificate_pending_fires(store: Store, *, count: int = 1) -> list[object]:
    selected = capability(
        app_id="invoice-processor",
        app_version="0.1.0",
        capability_id="certificate.extract",
        produces=(CERTIFICATE_MEDIA_TYPE,),
        parameters=(),
    )
    for index in range(count):
        name = "Certificate tracker" if index == 0 else f"Certificate tracker {index + 1}"
        rule = contract_rule_definition(selected, name=name)
        action = rule["action"]
        assert isinstance(action, dict)
        action["parameters"] = {}
        store.put_automation_rule(rule)
    store.mark_analyzed(
        "message-1",
        {
            "category": "informational",
            "priority": "normal",
            "summary": "A certificate arrived.",
            "action_required": True,
            "suggested_action": "Review certificate expiry.",
            "deadline_text": None,
            "deadline_iso": None,
            "confidence": 0.9,
        },
        mailbox_identity_key=TEST_MAILBOX_IDENTITY_KEY,
    )
    fires = store.automation_fires_for_message("message-1")
    assert len(fires) == count
    return list(fires)


def _certificate_fire_jobs(
    store: Store,
    *,
    count: int = 1,
    joined_job_id: str | None = None,
) -> tuple[list[object], str]:
    fires = _certificate_pending_fires(store, count=count)
    attempt = store.automation_fire_attempts(fires[0].fire_id)[0]
    job_id = joined_job_id or attempt.dispatch_request_id
    _certificate_job(store, job_id)
    submitted = [
        store.transition_automation_fire(
            fire_id=fire.fire_id,
            expected_state=fire.state,
            expected_version=fire.state_version,
            next_state="submitted",
            reason="connect_admitted",
            job_id=job_id,
        )
        for fire in fires
    ]
    return submitted, job_id


def _certificate_fire_job(
    store: Store,
    *,
    joined_job_id: str | None = None,
) -> tuple[object, str]:
    fires, job_id = _certificate_fire_jobs(store, joined_job_id=joined_job_id)
    return fires[0], job_id


def test_certificate_expiry_ledger_empty_list(tmp_path: Path) -> None:
    store = Store(tmp_path / "watcher.sqlite3")
    store.initialize()

    assert store.list_certificate_expiry_ledger(today="2026-09-20", limit=100) == []


def test_completed_certificate_projects_once_and_lists_all_expiry_states(tmp_path: Path) -> None:
    _, runtime = seeded_runtime(tmp_path)
    fire, job_id = _certificate_fire_job(runtime.store)

    completed = runtime.store.transition_connect_job(
        job_id=job_id,
        expected_state="requested",
        next_state="completed",
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
        result=_result(_record()),
    )

    assert completed.status == "completed"
    settled_fire = runtime.store.automation_fire(fire.fire_id)
    assert settled_fire is not None and settled_fire.state == "completed"
    rows = runtime.store.list_certificate_expiry_ledger(today="2026-09-20", limit=100)
    assert [row["expiry_status"] for row in rows] == [
        "expired",
        "expires_today",
        "upcoming",
        "review",
    ]
    assert [row["policy_ordinal"] for row in rows] == [0, 1, 2, 3]
    assert rows[3]["expiration_date_candidates"] == ["2026-09-22", "2026-10-09"]
    assert rows[3]["review_state"] == "needs_review"
    assert [row["review_state"] for row in rows] == [
        "extracted",
        "extracted",
        "extracted",
        "needs_review",
    ]
    with runtime.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM certificate_records").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM certificate_policy_rows").fetchone()[0] == 4


def test_valid_certificate_completion_settles_entitlement_paused_fire(tmp_path: Path) -> None:
    _, runtime = seeded_runtime(tmp_path)
    fire, job_id = _certificate_fire_job(runtime.store)
    paused = runtime.store.transition_automation_fire(
        fire_id=fire.fire_id,
        expected_state=fire.state,
        expected_version=fire.state_version,
        next_state="entitlement_paused",
        reason="entitlement_inactive",
        job_id=job_id,
    )

    runtime.store.transition_connect_job(
        job_id=job_id,
        expected_state="requested",
        next_state="completed",
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
        result=_result(_record()),
    )

    settled = runtime.store.automation_fire(paused.fire_id)
    assert settled is not None
    assert settled.state == "completed"
    assert settled.reason == "connect_completed"

    with pytest.raises(RuntimeError, match="expected-state race"):
        runtime.store.transition_connect_job(
            job_id=job_id,
            expected_state="requested",
            next_state="completed",
            provider_app_id="invoice-processor",
            provider_instance_id=INSTANCE_A,
            result=_result(_record()),
        )
    with runtime.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM certificate_records").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM certificate_policy_rows").fetchone()[0] == 4


def test_completed_certificate_projects_for_joined_job_with_distinct_dispatch_id(
    tmp_path: Path,
) -> None:
    _, runtime = seeded_runtime(tmp_path)
    joined_job_id = "20000000-0000-4000-8000-000000000099"
    fire, job_id = _certificate_fire_job(runtime.store, joined_job_id=joined_job_id)
    attempt = runtime.store.automation_fire_attempts(fire.fire_id)[0]
    original_job_id = attempt.dispatch_request_id
    assert job_id == joined_job_id
    assert original_job_id != joined_job_id

    runtime.store.transition_connect_job(
        job_id=joined_job_id,
        expected_state="requested",
        next_state="completed",
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
        result=_result(_record()),
    )

    settled = runtime.store.automation_fire(fire.fire_id)
    assert settled is not None and settled.state == "completed"
    attempt = runtime.store.automation_fire_attempts(fire.fire_id)[0]
    assert attempt.dispatch_request_id == original_job_id
    assert attempt.job_id == joined_job_id
    assert len(runtime.store.list_certificate_expiry_ledger(today="2026-09-20")) == 4


def test_completed_certificate_projects_once_for_multiple_joined_fires(tmp_path: Path) -> None:
    _, runtime = seeded_runtime(tmp_path)
    fires, job_id = _certificate_fire_jobs(runtime.store, count=2)

    runtime.store.transition_connect_job(
        job_id=job_id,
        expected_state="requested",
        next_state="completed",
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
        result=_result(_record()),
    )

    assert [runtime.store.automation_fire(fire.fire_id).state for fire in fires] == [
        "completed",
        "completed",
    ]
    with runtime.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM certificate_records").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM certificate_policy_rows").fetchone()[0] == 4


def test_invalid_completed_certificate_fails_a_later_joined_fire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, runtime = seeded_runtime(tmp_path)
    _certificate_job(runtime.store, JOB_ID)
    invalid = _record()
    invalid["unexpected"] = True
    runtime.store.transition_connect_job(
        job_id=JOB_ID,
        expected_state="requested",
        next_state="completed",
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
        result=_result(invalid),
    )
    fire = _certificate_pending_fires(runtime.store)[0]
    submitted = runtime.store.transition_automation_fire(
        fire_id=fire.fire_id,
        expected_state=fire.state,
        expected_version=fire.state_version,
        next_state="submitted",
        reason="connect_admitted",
        job_id=JOB_ID,
    )
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)

    engine_api._settle_submitted_automation_fires(runtime, limit=1)

    settled = runtime.store.automation_fire(submitted.fire_id)
    assert settled is not None
    assert settled.state == "failed"
    assert settled.reason == "CERTIFICATE_RESULT_INVALID"
    assert runtime.store.list_certificate_expiry_ledger(today="2026-09-20") == []


def test_source_cleanup_cannot_complete_an_unreconciled_certificate_join(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, runtime = seeded_runtime(tmp_path)
    _certificate_job(runtime.store, JOB_ID)
    invalid = _record()
    invalid["unexpected"] = True
    runtime.store.transition_connect_job(
        job_id=JOB_ID,
        expected_state="requested",
        next_state="completed",
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
        result=_result(invalid),
    )
    fire = _certificate_pending_fires(runtime.store)[0]
    submitted = runtime.store.transition_automation_fire(
        fire_id=fire.fire_id,
        expected_state=fire.state,
        expected_version=fire.state_version,
        next_state="submitted",
        reason="connect_admitted",
        job_id=JOB_ID,
    )

    assert runtime.store.delete_message("message-1") is True
    retained = runtime.store.automation_fire(submitted.fire_id)
    assert retained is not None and retained.state == "submitted"
    assert runtime.store.connect_job(JOB_ID) is not None

    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    engine_api._settle_submitted_automation_fires(runtime, limit=1)

    settled = runtime.store.automation_fire(submitted.fire_id)
    assert settled is not None and settled.state == "failed"
    assert settled.reason == "CERTIFICATE_RESULT_INVALID"
    assert runtime.store.list_certificate_expiry_ledger(today="2026-09-20") == []


def test_valid_completed_certificate_completes_a_later_joined_fire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, runtime = seeded_runtime(tmp_path)
    _certificate_job(runtime.store, JOB_ID)
    runtime.store.transition_connect_job(
        job_id=JOB_ID,
        expected_state="requested",
        next_state="completed",
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
        result=_result(_record()),
    )
    fire = _certificate_pending_fires(runtime.store)[0]
    submitted = runtime.store.transition_automation_fire(
        fire_id=fire.fire_id,
        expected_state=fire.state,
        expected_version=fire.state_version,
        next_state="submitted",
        reason="connect_admitted",
        job_id=JOB_ID,
    )
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)

    engine_api._settle_submitted_automation_fires(runtime, limit=1)

    settled = runtime.store.automation_fire(submitted.fire_id)
    assert settled is not None
    assert settled.state == "completed"
    with runtime.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM certificate_records").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM certificate_policy_rows").fetchone()[0] == 4


def test_incomplete_existing_projection_keeps_a_later_join_retryable(tmp_path: Path) -> None:
    _, runtime = seeded_runtime(tmp_path)
    fires = _certificate_pending_fires(runtime.store, count=2)
    first_attempt = runtime.store.automation_fire_attempts(fires[0].fire_id)[0]
    job_id = first_attempt.dispatch_request_id
    _certificate_job(runtime.store, job_id)
    runtime.store.transition_automation_fire(
        fire_id=fires[0].fire_id,
        expected_state=fires[0].state,
        expected_version=fires[0].state_version,
        next_state="submitted",
        reason="connect_admitted",
        job_id=job_id,
    )
    runtime.store.transition_connect_job(
        job_id=job_id,
        expected_state="requested",
        next_state="completed",
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
        result=_result(_record()),
    )
    with runtime.store.connection() as db:
        db.execute(
            """DELETE FROM certificate_policy_rows
            WHERE certificate_id = (
                SELECT certificate_id FROM certificate_records WHERE connect_job_id = ?
            ) AND ordinal = 0""",
            (job_id,),
        )
    submitted = runtime.store.transition_automation_fire(
        fire_id=fires[1].fire_id,
        expected_state=fires[1].state,
        expected_version=fires[1].state_version,
        next_state="submitted",
        reason="connect_admitted",
        job_id=job_id,
    )

    with pytest.raises(RuntimeError, match="projection is incomplete"):
        runtime.store.reconcile_certificate_completed_join(job_id=job_id)

    retryable = runtime.store.automation_fire(submitted.fire_id)
    assert retryable is not None
    assert retryable.state == "submitted"
    assert retryable.reason == "connect_admitted"


def test_conflicting_terminal_replay_fails_fire_without_replacing_evidence(tmp_path: Path) -> None:
    _, runtime = seeded_runtime(tmp_path)
    fire, job_id = _certificate_fire_job(runtime.store)
    original = _record()
    runtime.store.transition_connect_job(
        job_id=job_id,
        expected_state="requested",
        next_state="completed",
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
        result=_result(original),
    )
    conflicting = _record()
    conflicting["insured"] = _text("Different Insured LLC", "different-insured")

    replayed = engine_api._apply_connect_update(
        runtime.store,
        connect.CapabilityJobUpdate(
            job_id=job_id,
            status="completed",
            provider_app_id="invoice-processor",
            provider_instance_id=INSTANCE_A,
            result=_capability_result(conflicting),
            error=None,
        ),
    )

    assert replayed.status == "completed"
    settled = runtime.store.automation_fire(fire.fire_id)
    assert settled is not None
    assert settled.state == "failed"
    assert settled.reason == "CERTIFICATE_RESULT_CONFLICT"
    rows = runtime.store.list_certificate_expiry_ledger(today="2026-09-20", limit=100)
    assert {row["insured"] for row in rows} == {"Northstar Services LLC"}


def test_conflicting_terminal_replay_fails_completed_and_later_submitted_fires(
    tmp_path: Path,
) -> None:
    _, runtime = seeded_runtime(tmp_path)
    fires = _certificate_pending_fires(runtime.store, count=2)
    first_attempt = runtime.store.automation_fire_attempts(fires[0].fire_id)[0]
    job_id = first_attempt.dispatch_request_id
    _certificate_job(runtime.store, job_id)
    runtime.store.transition_automation_fire(
        fire_id=fires[0].fire_id,
        expected_state=fires[0].state,
        expected_version=fires[0].state_version,
        next_state="submitted",
        reason="connect_admitted",
        job_id=job_id,
    )
    original = _record()
    runtime.store.transition_connect_job(
        job_id=job_id,
        expected_state="requested",
        next_state="completed",
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
        result=_result(original),
    )
    runtime.store.transition_automation_fire(
        fire_id=fires[1].fire_id,
        expected_state=fires[1].state,
        expected_version=fires[1].state_version,
        next_state="submitted",
        reason="connect_admitted",
        job_id=job_id,
    )
    conflicting = _record()
    conflicting["insured"] = _text("Different Insured LLC", "different-insured")

    engine_api._apply_connect_update(
        runtime.store,
        connect.CapabilityJobUpdate(
            job_id=job_id,
            status="completed",
            provider_app_id="invoice-processor",
            provider_instance_id=INSTANCE_A,
            result=_capability_result(conflicting),
            error=None,
        ),
    )

    settled = [runtime.store.automation_fire(fire.fire_id) for fire in fires]
    assert [fire.state for fire in settled] == ["failed", "failed"]
    assert [fire.reason for fire in settled] == [
        "CERTIFICATE_RESULT_CONFLICT",
        "CERTIFICATE_RESULT_CONFLICT",
    ]


def test_conflicting_terminal_replay_fences_a_later_completed_join(tmp_path: Path) -> None:
    _, runtime = seeded_runtime(tmp_path)
    fires = _certificate_pending_fires(runtime.store, count=2)
    first_attempt = runtime.store.automation_fire_attempts(fires[0].fire_id)[0]
    job_id = first_attempt.dispatch_request_id
    _certificate_job(runtime.store, job_id)
    runtime.store.transition_automation_fire(
        fire_id=fires[0].fire_id,
        expected_state=fires[0].state,
        expected_version=fires[0].state_version,
        next_state="submitted",
        reason="connect_admitted",
        job_id=job_id,
    )
    original = _record()
    runtime.store.transition_connect_job(
        job_id=job_id,
        expected_state="requested",
        next_state="completed",
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
        result=_result(original),
    )
    conflicting = _record()
    conflicting["insured"] = _text("Different Insured LLC", "different-insured")
    engine_api._apply_connect_update(
        runtime.store,
        connect.CapabilityJobUpdate(
            job_id=job_id,
            status="completed",
            provider_app_id="invoice-processor",
            provider_instance_id=INSTANCE_A,
            result=_capability_result(conflicting),
            error=None,
        ),
    )

    runtime.store.transition_automation_fire(
        fire_id=fires[1].fire_id,
        expected_state=fires[1].state,
        expected_version=fires[1].state_version,
        next_state="submitted",
        reason="connect_admitted",
        job_id=job_id,
    )
    runtime.store.reconcile_certificate_completed_join(job_id=job_id)

    later = runtime.store.automation_fire(fires[1].fire_id)
    assert later is not None
    assert later.state == "failed"
    assert later.reason == "CERTIFICATE_RESULT_CONFLICT"


def test_invalid_terminal_replay_fences_a_later_completed_join(tmp_path: Path) -> None:
    _, runtime = seeded_runtime(tmp_path)
    fires = _certificate_pending_fires(runtime.store, count=2)
    first_attempt = runtime.store.automation_fire_attempts(fires[0].fire_id)[0]
    job_id = first_attempt.dispatch_request_id
    _certificate_job(runtime.store, job_id)
    runtime.store.transition_automation_fire(
        fire_id=fires[0].fire_id,
        expected_state=fires[0].state,
        expected_version=fires[0].state_version,
        next_state="submitted",
        reason="connect_admitted",
        job_id=job_id,
    )
    runtime.store.transition_connect_job(
        job_id=job_id,
        expected_state="requested",
        next_state="completed",
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
        result=_result(_record()),
    )
    invalid = _record()
    invalid["unexpected"] = True
    engine_api._apply_connect_update(
        runtime.store,
        connect.CapabilityJobUpdate(
            job_id=job_id,
            status="completed",
            provider_app_id="invoice-processor",
            provider_instance_id=INSTANCE_A,
            result=_capability_result(invalid),
            error=None,
        ),
    )

    runtime.store.transition_automation_fire(
        fire_id=fires[1].fire_id,
        expected_state=fires[1].state,
        expected_version=fires[1].state_version,
        next_state="submitted",
        reason="connect_admitted",
        job_id=job_id,
    )
    runtime.store.reconcile_certificate_completed_join(job_id=job_id)

    later = runtime.store.automation_fire(fires[1].fire_id)
    assert later is not None
    assert later.state == "failed"
    assert later.reason == "CERTIFICATE_RESULT_INVALID"


def test_matching_terminal_replay_preserves_completed_fire(tmp_path: Path) -> None:
    _, runtime = seeded_runtime(tmp_path)
    fire, job_id = _certificate_fire_job(runtime.store)
    record = _record()
    runtime.store.transition_connect_job(
        job_id=job_id,
        expected_state="requested",
        next_state="completed",
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
        result=_result(record),
    )

    engine_api._apply_connect_update(
        runtime.store,
        connect.CapabilityJobUpdate(
            job_id=job_id,
            status="completed",
            provider_app_id="invoice-processor",
            provider_instance_id=INSTANCE_A,
            result=_capability_result(record),
            error=None,
        ),
    )

    settled = runtime.store.automation_fire(fire.fire_id)
    assert settled is not None
    assert settled.state == "completed"
    assert settled.reason == "connect_completed"


def test_numeric_json_spellings_do_not_create_a_terminal_replay_conflict(tmp_path: Path) -> None:
    _, runtime = seeded_runtime(tmp_path)
    fire, job_id = _certificate_fire_job(runtime.store)
    record = _record()
    runtime.store.transition_connect_job(
        job_id=job_id,
        expected_state="requested",
        next_state="completed",
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
        result=_result(record),
    )
    equivalent = json.loads(json.dumps(record))
    _use_integer_bbox_spellings(equivalent)

    engine_api._apply_connect_update(
        runtime.store,
        connect.CapabilityJobUpdate(
            job_id=job_id,
            status="completed",
            provider_app_id="invoice-processor",
            provider_instance_id=INSTANCE_A,
            result=_capability_result(equivalent),
            error=None,
        ),
    )

    settled = runtime.store.automation_fire(fire.fire_id)
    assert settled is not None and settled.state == "completed"
    assert settled.reason == "connect_completed"


def test_invalid_certificate_result_completes_provider_job_without_partial_ledger(
    tmp_path: Path,
) -> None:
    _, runtime = seeded_runtime(tmp_path)
    fire, job_id = _certificate_fire_job(runtime.store)
    invalid = _record()
    invalid["unexpected"] = True

    completed = runtime.store.transition_connect_job(
        job_id=job_id,
        expected_state="requested",
        next_state="completed",
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
        result=_result(invalid),
    )

    assert completed.status == "completed"
    settled_fire = runtime.store.automation_fire(fire.fire_id)
    assert settled_fire is not None
    assert settled_fire.state == "failed"
    assert settled_fire.reason == "CERTIFICATE_RESULT_INVALID"
    with runtime.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM certificate_records").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM certificate_policy_rows").fetchone()[0] == 0


def test_invalid_completion_replay_without_linked_fires_is_idempotent(tmp_path: Path) -> None:
    _, runtime = seeded_runtime(tmp_path)
    _certificate_job(runtime.store, JOB_ID)
    invalid = _record()
    invalid["unexpected"] = True
    result = _result(invalid)
    runtime.store.transition_connect_job(
        job_id=JOB_ID,
        expected_state="requested",
        next_state="completed",
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
        result=result,
    )

    replayed = runtime.store.reconcile_certificate_completed_replay(
        job_id=JOB_ID,
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
        result=result,
    )

    assert replayed.status == "completed"
    assert runtime.store.list_certificate_expiry_ledger(today="2026-09-20") == []


def test_different_replay_of_invalid_completion_fences_later_joins(tmp_path: Path) -> None:
    _, runtime = seeded_runtime(tmp_path)
    fires = _certificate_pending_fires(runtime.store, count=2)
    first_attempt = runtime.store.automation_fire_attempts(fires[0].fire_id)[0]
    job_id = first_attempt.dispatch_request_id
    _certificate_job(runtime.store, job_id)
    runtime.store.transition_automation_fire(
        fire_id=fires[0].fire_id,
        expected_state=fires[0].state,
        expected_version=fires[0].state_version,
        next_state="submitted",
        reason="connect_admitted",
        job_id=job_id,
    )
    invalid = _record()
    invalid["unexpected"] = True
    runtime.store.transition_connect_job(
        job_id=job_id,
        expected_state="requested",
        next_state="completed",
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
        result=_result(invalid),
    )
    different = _record()
    different["different"] = True

    runtime.store.reconcile_certificate_completed_replay(
        job_id=job_id,
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
        result=_result(different),
    )

    first = runtime.store.automation_fire(fires[0].fire_id)
    assert first is not None
    assert first.reason == "CERTIFICATE_RESULT_CONFLICT"
    runtime.store.transition_automation_fire(
        fire_id=fires[1].fire_id,
        expected_state=fires[1].state,
        expected_version=fires[1].state_version,
        next_state="submitted",
        reason="connect_admitted",
        job_id=job_id,
    )
    runtime.store.reconcile_certificate_completed_join(job_id=job_id)
    later = runtime.store.automation_fire(fires[1].fire_id)
    assert later is not None
    assert later.reason == "CERTIFICATE_RESULT_CONFLICT"


def test_matching_invalid_replay_settles_a_newly_joined_fire(tmp_path: Path) -> None:
    _, runtime = seeded_runtime(tmp_path)
    fires = _certificate_pending_fires(runtime.store, count=2)
    first_attempt = runtime.store.automation_fire_attempts(fires[0].fire_id)[0]
    job_id = first_attempt.dispatch_request_id
    _certificate_job(runtime.store, job_id)
    runtime.store.transition_automation_fire(
        fire_id=fires[0].fire_id,
        expected_state=fires[0].state,
        expected_version=fires[0].state_version,
        next_state="submitted",
        reason="connect_admitted",
        job_id=job_id,
    )
    invalid = _record()
    invalid["unexpected"] = True
    result = _result(invalid)
    runtime.store.transition_connect_job(
        job_id=job_id,
        expected_state="requested",
        next_state="completed",
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
        result=result,
    )
    runtime.store.transition_automation_fire(
        fire_id=fires[1].fire_id,
        expected_state=fires[1].state,
        expected_version=fires[1].state_version,
        next_state="submitted",
        reason="connect_admitted",
        job_id=job_id,
    )

    runtime.store.reconcile_certificate_completed_replay(
        job_id=job_id,
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
        result=result,
    )

    settled = [runtime.store.automation_fire(fire.fire_id) for fire in fires]
    assert [fire.state for fire in settled] == ["failed", "failed"]
    assert [fire.reason for fire in settled] == [
        "CERTIFICATE_RESULT_INVALID",
        "CERTIFICATE_RESULT_INVALID",
    ]


def test_terminal_replay_conflict_fence_is_monotonic(tmp_path: Path) -> None:
    _, runtime = seeded_runtime(tmp_path)
    fires = _certificate_pending_fires(runtime.store, count=2)
    first_attempt = runtime.store.automation_fire_attempts(fires[0].fire_id)[0]
    job_id = first_attempt.dispatch_request_id
    _certificate_job(runtime.store, job_id)
    runtime.store.transition_automation_fire(
        fire_id=fires[0].fire_id,
        expected_state=fires[0].state,
        expected_version=fires[0].state_version,
        next_state="submitted",
        reason="connect_admitted",
        job_id=job_id,
    )
    runtime.store.transition_connect_job(
        job_id=job_id,
        expected_state="requested",
        next_state="completed",
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
        result=_result(_record()),
    )
    conflicting = _record()
    conflicting["insured"] = _text("Different Insured LLC", "different-insured")
    runtime.store.reconcile_certificate_completed_replay(
        job_id=job_id,
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
        result=_result(conflicting),
    )
    malformed = _record()
    malformed["unexpected"] = True
    runtime.store.reconcile_certificate_completed_replay(
        job_id=job_id,
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
        result=_result(malformed),
    )
    runtime.store.transition_automation_fire(
        fire_id=fires[1].fire_id,
        expected_state=fires[1].state,
        expected_version=fires[1].state_version,
        next_state="submitted",
        reason="connect_admitted",
        job_id=job_id,
    )

    runtime.store.reconcile_certificate_completed_join(job_id=job_id)

    later = runtime.store.automation_fire(fires[1].fire_id)
    assert later is not None
    assert later.reason == "CERTIFICATE_RESULT_CONFLICT"


def test_terminal_replay_conflict_rewrites_prior_invalid_fire(tmp_path: Path) -> None:
    _, runtime = seeded_runtime(tmp_path)
    fire, job_id = _certificate_fire_job(runtime.store)
    runtime.store.transition_connect_job(
        job_id=job_id,
        expected_state="requested",
        next_state="completed",
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
        result=_result(_record()),
    )
    malformed = _record()
    malformed["unexpected"] = True
    runtime.store.reconcile_certificate_completed_replay(
        job_id=job_id,
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
        result=_result(malformed),
    )
    invalid = runtime.store.automation_fire(fire.fire_id)
    assert invalid is not None
    assert invalid.reason == "CERTIFICATE_RESULT_INVALID"

    conflicting = _record()
    conflicting["insured"] = _text("Different Insured LLC", "different-insured")
    runtime.store.reconcile_certificate_completed_replay(
        job_id=job_id,
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
        result=_result(conflicting),
    )

    escalated = runtime.store.automation_fire(fire.fire_id)
    assert escalated is not None
    assert escalated.reason == "CERTIFICATE_RESULT_CONFLICT"


def test_provider_owned_certificate_survives_source_deletion(tmp_path: Path) -> None:
    _, runtime = seeded_runtime(tmp_path)
    fire, job_id = _certificate_fire_job(runtime.store)
    runtime.store.transition_connect_job(
        job_id=job_id,
        expected_state="requested",
        next_state="accepted",
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
    )
    with runtime.store.connection() as db:
        identity_count = db.execute(
            "SELECT COUNT(*) FROM automation_fire_source_identities WHERE fire_id = ?",
            (fire.fire_id,),
        ).fetchone()[0]
    assert identity_count == 1

    assert runtime.store.delete_message("message-1") is True
    runtime.store.transition_connect_job(
        job_id=job_id,
        expected_state="accepted",
        next_state="completed",
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
        result=_result(_record()),
    )

    rows = runtime.store.list_certificate_expiry_ledger(today="2026-09-20", limit=100)
    assert len(rows) == 4
    assert {row["source_available"] for row in rows} == {False}
    assert runtime.store.connect_job(job_id) is None
    assert runtime.store.connect_dispatch(job_id) is None


def test_invalid_source_less_certificate_discards_its_terminal_job(tmp_path: Path) -> None:
    _, runtime = seeded_runtime(tmp_path)
    fire, job_id = _certificate_fire_job(runtime.store)
    runtime.store.transition_connect_job(
        job_id=job_id,
        expected_state="requested",
        next_state="accepted",
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
    )
    assert runtime.store.delete_message("message-1") is True
    invalid = _record()
    invalid["unexpected"] = True

    runtime.store.transition_connect_job(
        job_id=job_id,
        expected_state="accepted",
        next_state="completed",
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
        result=_result(invalid),
    )

    assert runtime.store.connect_job(job_id) is None
    assert runtime.store.connect_dispatch(job_id) is None
    settled = runtime.store.automation_fire(fire.fire_id)
    assert settled is not None
    assert settled.state == "failed"
    assert settled.reason == "CERTIFICATE_RESULT_INVALID"
    assert settled.job_id == job_id
    assert runtime.store.list_certificate_expiry_ledger(today="2026-09-20", limit=100) == []


def test_completed_join_discards_source_less_certificate_job_after_settlement(
    tmp_path: Path,
) -> None:
    _, runtime = seeded_runtime(tmp_path)
    fires = _certificate_pending_fires(runtime.store, count=2)
    first_attempt = runtime.store.automation_fire_attempts(fires[0].fire_id)[0]
    job_id = first_attempt.dispatch_request_id
    _certificate_job(runtime.store, job_id)
    runtime.store.transition_automation_fire(
        fire_id=fires[0].fire_id,
        expected_state=fires[0].state,
        expected_version=fires[0].state_version,
        next_state="submitted",
        reason="connect_admitted",
        job_id=job_id,
    )
    runtime.store.transition_connect_job(
        job_id=job_id,
        expected_state="requested",
        next_state="completed",
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
        result=_result(_record()),
    )
    runtime.store.transition_automation_fire(
        fire_id=fires[1].fire_id,
        expected_state=fires[1].state,
        expected_version=fires[1].state_version,
        next_state="submitted",
        reason="connect_admitted",
        job_id=job_id,
    )

    assert runtime.store.delete_message("message-1") is True
    assert runtime.store.connect_job(job_id) is not None
    runtime.store.reconcile_certificate_completed_join(job_id=job_id)

    assert runtime.store.connect_job(job_id) is None
    assert runtime.store.connect_dispatch(job_id) is None
    rows = runtime.store.list_certificate_expiry_ledger(today="2026-09-20", limit=100)
    assert len(rows) == 4
    assert {row["source_available"] for row in rows} == {False}
    settled = runtime.store.automation_fire(fires[1].fire_id)
    assert settled is not None
    assert settled.state == "completed"
    assert settled.reason == "connect_completed"


def test_failed_terminal_replay_discards_source_less_certificate_job(tmp_path: Path) -> None:
    _, runtime = seeded_runtime(tmp_path)
    fires = _certificate_pending_fires(runtime.store, count=2)
    first_attempt = runtime.store.automation_fire_attempts(fires[0].fire_id)[0]
    job_id = first_attempt.dispatch_request_id
    _certificate_job(runtime.store, job_id)
    runtime.store.transition_automation_fire(
        fire_id=fires[0].fire_id,
        expected_state=fires[0].state,
        expected_version=fires[0].state_version,
        next_state="submitted",
        reason="connect_admitted",
        job_id=job_id,
    )
    original = _record()
    runtime.store.transition_connect_job(
        job_id=job_id,
        expected_state="requested",
        next_state="completed",
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
        result=_result(original),
    )
    runtime.store.transition_automation_fire(
        fire_id=fires[1].fire_id,
        expected_state=fires[1].state,
        expected_version=fires[1].state_version,
        next_state="submitted",
        reason="connect_admitted",
        job_id=job_id,
    )
    assert runtime.store.delete_message("message-1") is True
    conflicting = _record()
    conflicting["insured"] = _text("Different Insured LLC", "different-insured")

    runtime.store.reconcile_certificate_completed_replay(
        job_id=job_id,
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
        result=_result(conflicting),
    )

    assert runtime.store.connect_job(job_id) is None
    assert runtime.store.connect_dispatch(job_id) is None
    assert len(runtime.store.list_certificate_expiry_ledger(today="2026-09-20")) == 4
    settled = [runtime.store.automation_fire(fire.fire_id) for fire in fires]
    assert [fire.reason for fire in settled] == [
        "CERTIFICATE_RESULT_CONFLICT",
        "CERTIFICATE_RESULT_CONFLICT",
    ]


def test_bulk_completed_join_skips_siblings_settled_by_the_first_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, runtime = seeded_runtime(tmp_path)
    _certificate_job(runtime.store, JOB_ID)
    runtime.store.transition_connect_job(
        job_id=JOB_ID,
        expected_state="requested",
        next_state="completed",
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
        result=_result(_record()),
    )
    fires = _certificate_pending_fires(runtime.store, count=2)
    for fire in fires:
        runtime.store.transition_automation_fire(
            fire_id=fire.fire_id,
            expected_state=fire.state,
            expected_version=fire.state_version,
            next_state="submitted",
            reason="connect_admitted",
            job_id=JOB_ID,
        )
    assert runtime.store.delete_message("message-1") is True
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)

    engine_api._settle_submitted_automation_fires(runtime, limit=2)

    settled = [runtime.store.automation_fire(fire.fire_id) for fire in fires]
    assert [fire.state for fire in settled] == ["completed", "completed"]
    assert runtime.store.connect_job(JOB_ID) is None


def test_zero_policy_certificate_is_visible_as_review_placeholder(tmp_path: Path) -> None:
    _, runtime = seeded_runtime(tmp_path)
    _fire, job_id = _certificate_fire_job(runtime.store)
    runtime.store.transition_connect_job(
        job_id=job_id,
        expected_state="requested",
        next_state="completed",
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
        result=_result(_record(policies=[])),
    )

    rows = runtime.store.list_certificate_expiry_ledger(today="2026-09-20", limit=100)
    assert len(rows) == 1
    certificate_id = rows[0]["certificate_id"]
    assert rows == [
        {
            "certificate_id": certificate_id,
            "certificate_holder": "City of Effingham",
            "insured": "Northstar Services LLC",
            "producer": "Local Insurance",
            "policy_id": None,
            "policy_ordinal": None,
            "coverage": None,
            "insurer": None,
            "policy_number": None,
            "effective_date_iso": None,
            "effective_date_ambiguous": None,
            "effective_date_candidates": [],
            "expiration_date_iso": None,
            "expiration_date_ambiguous": None,
            "expiration_date_candidates": [],
            "expiry_status": "review",
            "review_state": "needs_review",
            "review_reasons": ["NO_POLICY_ROWS"],
            "source_message_id": "message-1",
            "source_part_id": "2",
            "connect_job_id": job_id,
            "source_available": True,
        }
    ]


def test_policy_association_review_remains_scoped_to_its_policy_row(tmp_path: Path) -> None:
    _, runtime = seeded_runtime(tmp_path)
    _fire, job_id = _certificate_fire_job(runtime.store)
    policies = [
        _policy(
            0,
            _date("2027-01-01", "expiration-0"),
            reasons=["ASSOCIATION_UNCLEAR"],
        ),
        _policy(1, _date("2027-02-01", "expiration-1")),
    ]
    record = _record(policies=policies)
    record["withheld"] = [
        {
            "field": "policies[0].insurer",
            "reason": "POLICY_ASSOCIATION_UNCLEAR",
            "detail": "wrong policy block",
        }
    ]
    record["review"] = {
        "required": True,
        "reasons": ["POLICY_ASSOCIATION_UNCLEAR"],
    }
    runtime.store.transition_connect_job(
        job_id=job_id,
        expected_state="requested",
        next_state="completed",
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
        result=_result(record),
    )

    rows = runtime.store.list_certificate_expiry_ledger(today="2026-09-20", limit=100)

    assert [row["review_state"] for row in rows] == ["needs_review", "extracted"]
    assert rows[0]["review_reasons"] == ["ASSOCIATION_UNCLEAR"]
    assert rows[1]["review_reasons"] == []


def test_withheld_only_association_review_maps_to_the_affected_policy(tmp_path: Path) -> None:
    _, runtime = seeded_runtime(tmp_path)
    _fire, job_id = _certificate_fire_job(runtime.store)
    record = _record(
        policies=[
            _policy(0, _date("2027-01-01", "expiration-0")),
            _policy(1, _date("2027-02-01", "expiration-1")),
        ]
    )
    record["withheld"] = [
        {
            "field": "policies[0].insurer",
            "reason": "POLICY_ASSOCIATION_UNCLEAR",
            "detail": "wrong policy field",
        }
    ]
    record["review"] = {
        "required": True,
        "reasons": ["POLICY_ASSOCIATION_UNCLEAR"],
    }
    runtime.store.transition_connect_job(
        job_id=job_id,
        expected_state="requested",
        next_state="completed",
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
        result=_result(record),
    )

    rows = runtime.store.list_certificate_expiry_ledger(today="2026-09-20", limit=100)

    assert [row["review_state"] for row in rows] == ["needs_review", "extracted"]
    assert rows[0]["review_reasons"] == ["ASSOCIATION_UNCLEAR"]
    assert rows[1]["review_reasons"] == []


def test_unmapped_association_review_remains_visible_on_ledger_rows(tmp_path: Path) -> None:
    _, runtime = seeded_runtime(tmp_path)
    _fire, job_id = _certificate_fire_job(runtime.store)
    record = _record(
        policies=[_policy(0, _date("2027-01-01", "expiration-0"))]
    )
    record["withheld"] = [
        {
            "field": "policies[99].insurer",
            "reason": "POLICY_ASSOCIATION_UNCLEAR",
            "detail": "dropped policy evidence",
        }
    ]
    record["review"] = {
        "required": True,
        "reasons": ["POLICY_ASSOCIATION_UNCLEAR"],
    }
    runtime.store.transition_connect_job(
        job_id=job_id,
        expected_state="requested",
        next_state="completed",
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
        result=_result(record),
    )

    rows = runtime.store.list_certificate_expiry_ledger(today="2026-09-20", limit=100)

    assert rows[0]["review_state"] == "needs_review"
    assert rows[0]["review_reasons"] == ["POLICY_ASSOCIATION_UNCLEAR"]


def test_top_level_association_review_remains_visible_on_ledger_rows(tmp_path: Path) -> None:
    _, runtime = seeded_runtime(tmp_path)
    _fire, job_id = _certificate_fire_job(runtime.store)
    record = _record(policies=[_policy(0, _date("2027-01-01", "expiration-0"))])
    record["withheld"] = [
        {
            "field": "insured",
            "reason": "POLICY_ASSOCIATION_UNCLEAR",
            "detail": "unmapped party evidence",
        }
    ]
    record["review"] = {
        "required": True,
        "reasons": ["POLICY_ASSOCIATION_UNCLEAR"],
    }
    runtime.store.transition_connect_job(
        job_id=job_id,
        expected_state="requested",
        next_state="completed",
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
        result=_result(record),
    )

    rows = runtime.store.list_certificate_expiry_ledger(today="2026-09-20", limit=100)

    assert rows[0]["review_state"] == "needs_review"
    assert rows[0]["review_reasons"] == ["POLICY_ASSOCIATION_UNCLEAR"]


def test_certificate_ledger_checks_canonical_budget_before_loading_parent_blob(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, runtime = seeded_runtime(tmp_path)
    _fire, job_id = _certificate_fire_job(runtime.store)
    runtime.store.transition_connect_job(
        job_id=job_id,
        expected_state="requested",
        next_state="completed",
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
        result=_result(_record()),
    )
    with runtime.store.connection() as db:
        canonical_bytes = int(
            db.execute(
                "SELECT length(canonical_result_json) FROM certificate_records"
            ).fetchone()[0]
        )
    monkeypatch.setattr(
        db_module,
        "MAX_CERTIFICATE_LEDGER_EVIDENCE_BYTES",
        canonical_bytes,
    )
    assert len(runtime.store.list_certificate_expiry_ledger(today="2026-09-20")) == 4
    monkeypatch.setattr(
        db_module,
        "MAX_CERTIFICATE_LEDGER_EVIDENCE_BYTES",
        canonical_bytes - 1,
    )
    statements: list[str] = []
    real_connect = sqlite3.connect

    def traced_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        connection = real_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced_connect)

    with pytest.raises(RuntimeError, match="evidence size"):
        runtime.store.list_certificate_expiry_ledger(today="2026-09-20", limit=100)

    assert not any(
        "SELECT * FROM certificate_records WHERE certificate_id" in statement
        for statement in statements
    )


def test_certificate_ledger_refuses_an_oversized_serialized_response(tmp_path: Path) -> None:
    _, runtime = seeded_runtime(tmp_path)
    _fire, job_id = _certificate_fire_job(runtime.store)
    record = _record()
    record["insured"] = _text("N" * 600_000, "insured-large")
    runtime.store.transition_connect_job(
        job_id=job_id,
        expected_state="requested",
        next_state="completed",
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
        result=_result(record),
    )

    with pytest.raises(RuntimeError, match="response size"):
        runtime.store.list_certificate_expiry_ledger(today="2026-09-20", limit=100)


def test_ledger_query_loads_parent_blob_once_per_certificate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, runtime = seeded_runtime(tmp_path)
    _fire, job_id = _certificate_fire_job(runtime.store)
    runtime.store.transition_connect_job(
        job_id=job_id,
        expected_state="requested",
        next_state="completed",
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
        result=_result(_record()),
    )
    statements: list[str] = []
    real_connect = sqlite3.connect

    def traced_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        connection = real_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced_connect)

    assert len(runtime.store.list_certificate_expiry_ledger(today="2026-09-20")) == 4
    selection = [
        statement
        for statement in statements
        if "FROM certificate_records AS certificate" in statement
    ]
    assert len(selection) == 1
    assert "certificate.*" not in selection[0]
    parent_reads = [
        statement
        for statement in statements
        if "SELECT * FROM certificate_records WHERE certificate_id" in statement
    ]
    assert len(parent_reads) == 1


@pytest.mark.parametrize("limit", [False, 0, -1, 501])
def test_certificate_ledger_rejects_invalid_limits(tmp_path: Path, limit: object) -> None:
    store = Store(tmp_path / "watcher.sqlite3")
    store.initialize()

    with pytest.raises(ValueError, match="limit"):
        store.list_certificate_expiry_ledger(today="2026-09-20", limit=limit)  # type: ignore[arg-type]


@pytest.mark.parametrize("today", ["", "2026-9-20", "2026-02-30"])
def test_certificate_ledger_rejects_invalid_calendar_dates(tmp_path: Path, today: str) -> None:
    store = Store(tmp_path / "watcher.sqlite3")
    store.initialize()

    with pytest.raises(ValueError, match="today"):
        store.list_certificate_expiry_ledger(today=today)


def test_schema_24_migrates_certificate_ledger_tables(tmp_path: Path) -> None:
    store = Store(tmp_path / "watcher.sqlite3")
    store.initialize()
    with store.connection() as db:
        db.execute("DROP TABLE certificate_policy_rows")
        db.execute("DROP TABLE certificate_records")
        db.execute("DROP TABLE automation_fire_source_identities")
        db.execute("PRAGMA user_version = 24")

    store.initialize()

    with store.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 25
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert {
        "automation_fire_source_identities",
        "certificate_records",
        "certificate_policy_rows",
    } <= tables


def test_certificate_validator_rejects_duplicate_keys_and_policy_overflow() -> None:
    valid = json.dumps(_record(), separators=(",", ":"), sort_keys=True).encode()
    validate_certificate_result_json(valid)

    duplicated = valid.replace(
        b'{"certificate_holder"',
        b'{"certificate_holder":null,"certificate_holder"',
        1,
    )
    with pytest.raises(CertificateResultInvalid, match="duplicate JSON keys"):
        validate_certificate_result_json(duplicated)

    boundary = _record(
        policies=[_policy(i, _date("2027-01-01", f"e-{i}")) for i in range(100)]
    )
    validate_certificate_result_json(
        json.dumps(boundary, separators=(",", ":"), sort_keys=True).encode()
    )
    overflow = _record(
        policies=[_policy(i, _date("2027-01-01", f"e-{i}")) for i in range(101)]
    )
    with pytest.raises(CertificateResultInvalid, match="policy rows"):
        validate_certificate_result_json(
            json.dumps(overflow, separators=(",", ":"), sort_keys=True).encode()
        )


def test_certificate_validator_classifies_deep_json_as_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def recurse(*args, **kwargs):
        raise RecursionError

    monkeypatch.setattr(db_module.json, "loads", recurse)

    with pytest.raises(CertificateResultInvalid, match="JSON is invalid"):
        validate_certificate_result_json(b"{}")


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("policies[0].unknown", "SPAN_UNKNOWN"),
        ("policies[100].expiration_date", "DATE_UNPARSEABLE"),
        ("insured", "UNKNOWN_REASON"),
    ],
)
def test_certificate_validator_rejects_unknown_withheld_values(field: str, reason: str) -> None:
    record = _record()
    record["withheld"] = [{"field": field, "reason": reason, "detail": "unresolved"}]

    with pytest.raises(CertificateResultInvalid, match="withheld"):
        validate_certificate_result_json(
            json.dumps(record, separators=(",", ":"), sort_keys=True).encode()
        )


def test_certificate_validator_accepts_closed_withheld_values_and_max_policy_index() -> None:
    record = _record(policies=[_policy(0, _date("2027-01-01", "expiration"))])
    record["insured"] = None
    record["withheld"] = [
        {"field": "insured", "reason": "VALUE_NOT_VERBATIM", "detail": "not exact"},
        {
            "field": "policies[99].expiration_date",
            "reason": "DATE_UNPARSEABLE",
            "detail": "not a complete date",
        },
    ]
    record["review"] = {"required": True, "reasons": ["INSURED_MISSING"]}

    validate_certificate_result_json(
        json.dumps(record, separators=(",", ":"), sort_keys=True).encode()
    )


def test_certificate_validator_checks_bbox_order_before_float_normalization() -> None:
    valid = _record()
    valid_insured = valid["insured"]
    assert isinstance(valid_insured, dict)
    valid_provenance = valid_insured["provenance"]
    assert isinstance(valid_provenance, dict)
    valid_provenance["bbox"] = [9007199254740991, 0, 9007199254740992, 10]
    validate_certificate_result_json(
        json.dumps(valid, separators=(",", ":"), sort_keys=True).encode()
    )

    reversed_record = _record()
    reversed_insured = reversed_record["insured"]
    assert isinstance(reversed_insured, dict)
    reversed_provenance = reversed_insured["provenance"]
    assert isinstance(reversed_provenance, dict)
    reversed_provenance["bbox"] = [9007199254740992, 0, 9007199254740991, 10]
    with pytest.raises(CertificateResultInvalid, match="coordinates"):
        validate_certificate_result_json(
            json.dumps(reversed_record, separators=(",", ":"), sort_keys=True).encode()
        )

    inexact_record = _record()
    inexact_insured = inexact_record["insured"]
    assert isinstance(inexact_insured, dict)
    inexact_provenance = inexact_insured["provenance"]
    assert isinstance(inexact_provenance, dict)
    inexact_provenance["bbox"] = [0, 0, 9007199254740993, 10]
    with pytest.raises(CertificateResultInvalid, match="coordinates"):
        validate_certificate_result_json(
            json.dumps(inexact_record, separators=(",", ":"), sort_keys=True).encode()
        )


def test_certificate_validator_uses_full_canonical_policy_for_duplicate_identity() -> None:
    exact_duplicate = _record(policies=[_policy(0, _date("2027-01-01", "expiration"))])
    exact_duplicate["policies"].append(exact_duplicate["policies"][0])
    with pytest.raises(CertificateResultInvalid, match="duplicate row"):
        validate_certificate_result_json(
            json.dumps(exact_duplicate, separators=(",", ":"), sort_keys=True).encode()
        )

    first = _policy(0, _date("2027-01-01", "expiration"))
    second = _policy(1, _date("2027-02-01", "expiration"))
    for name in ("coverage", "insurer", "policy_number", "effective_date"):
        second_value = second[name]
        first_value = first[name]
        assert isinstance(second_value, dict) and isinstance(first_value, dict)
        second_value["provenance"]["span_id"] = first_value["provenance"]["span_id"]
    distinct = _record(policies=[first, second])
    validate_certificate_result_json(
        json.dumps(distinct, separators=(",", ":"), sort_keys=True).encode()
    )

    numeric_duplicate = _policy(0, _date("2027-01-01", "expiration"))
    same_policy = json.loads(json.dumps(numeric_duplicate))
    numeric_record = _record(policies=[numeric_duplicate, same_policy])
    _use_integer_bbox_spellings({**numeric_record, "policies": [same_policy]})
    with pytest.raises(CertificateResultInvalid, match="duplicate row"):
        validate_certificate_result_json(
            json.dumps(numeric_record, separators=(",", ":"), sort_keys=True).encode()
        )


@pytest.mark.parametrize(
    ("page_count", "page", "error"),
    [(0, 1, "dimensions"), (1, 0, "page"), (1, 2, "page")],
)
def test_certificate_validator_rejects_provenance_outside_source_pages(
    page_count: int,
    page: int,
    error: str,
) -> None:
    record = _record()
    source = record["source"]
    assert isinstance(source, dict)
    source["page_count"] = page_count
    insured = record["insured"]
    assert isinstance(insured, dict)
    provenance = insured["provenance"]
    assert isinstance(provenance, dict)
    provenance["page"] = page

    with pytest.raises(CertificateResultInvalid, match=error):
        validate_certificate_result_json(
            json.dumps(record, separators=(",", ":"), sort_keys=True).encode()
        )


def test_certificate_validator_retains_reversed_range_only_with_review_reason() -> None:
    reversed_policy = _policy(
        0,
        _date("2025-12-31", "reversed-expiration"),
        reasons=["DATE_RANGE_INVALID"],
    )
    reviewed = _record(policies=[reversed_policy])
    reviewed["review"] = {"required": True, "reasons": ["POLICY_DATE_REVIEW"]}
    validate_certificate_result_json(
        json.dumps(reviewed, separators=(",", ":"), sort_keys=True).encode()
    )

    reversed_policy["review_reasons"] = []
    contradicted = _record(policies=[reversed_policy])
    with pytest.raises(CertificateResultInvalid, match="contradict"):
        validate_certificate_result_json(
            json.dumps(contradicted, separators=(",", ":"), sort_keys=True).encode()
        )
