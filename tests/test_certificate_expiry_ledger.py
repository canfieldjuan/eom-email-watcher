from __future__ import annotations

import hashlib
import json
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


def _result(record: dict[str, object]) -> dict[str, object]:
    payload = json.dumps(record, separators=(",", ":"), sort_keys=True).encode()
    output = connect.CapabilityOutput(
        artifact_id=OUTPUT_ID,
        media_type=CERTIFICATE_MEDIA_TYPE,
        display_name="certificate.json",
        byte_size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        payload=payload,
    )
    return connect.CapabilityResult((output,)).store_dict()


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


def _certificate_fire_job(store: Store) -> tuple[object, str]:
    selected = capability(
        app_id="invoice-processor",
        app_version="0.1.0",
        capability_id="certificate.extract",
        produces=(CERTIFICATE_MEDIA_TYPE,),
        parameters=(),
    )
    rule = contract_rule_definition(selected, name="Certificate tracker")
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
    fire = store.automation_fires_for_message("message-1")[0]
    attempt = store.automation_fire_attempts(fire.fire_id)[0]
    job_id = attempt.dispatch_request_id
    _certificate_job(store, job_id)
    submitted = store.transition_automation_fire(
        fire_id=fire.fire_id,
        expected_state=fire.state,
        expected_version=fire.state_version,
        next_state="submitted",
        reason="connect_admitted",
        job_id=job_id,
    )
    return submitted, job_id


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
    with runtime.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM certificate_records").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM certificate_policy_rows").fetchone()[0] == 4

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
