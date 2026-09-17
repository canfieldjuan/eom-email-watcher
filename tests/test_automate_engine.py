from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from eom_email_watcher import entitlement
from eom_email_watcher.automate import (
    AmbiguousDecision,
    AutomateHost,
    AutomateLicenseError,
    Workflow,
    WorkflowEngine,
    WorkflowMismatch,
)
from eom_email_watcher.automate.store import (
    OperationConflict,
    StaleRecord,
    UnknownRecord,
    WorkflowStore,
)

NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _encoded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _keyring(key: Ed25519PrivateKey, key_id: str = "test-key") -> bytes:
    return json.dumps(
        {
            "keys": [
                {
                    "key_id": key_id,
                    "algorithm": "Ed25519",
                    "public_key_base64url": _encoded(key.public_key().public_bytes_raw()),
                }
            ]
        },
        separators=(",", ":"),
    ).encode()


def _signed_license(key: Ed25519PrivateKey, features: list[str]) -> bytes:
    payload = json.dumps(
        {
            "format_version": 1,
            "entitlement_id": "11111111-1111-4111-8111-111111111111",
            "subject": "test-customer",
            "features": features,
            "issued_at": "2026-01-01T00:00:00Z",
            "not_before": "2026-01-01T00:00:00Z",
            "expires_at": "2027-01-01T00:00:00Z",
        },
        separators=(",", ":"),
    ).encode()
    return json.dumps(
        {
            "format_version": 1,
            "key_id": "test-key",
            "payload_base64url": _encoded(payload),
            "signature_base64url": _encoded(key.sign(payload)),
        },
        separators=(",", ":"),
    ).encode()


def _host(tmp_path: Path, features: list[str] | None) -> AutomateHost:
    key = Ed25519PrivateKey.generate()
    directory = tmp_path / "local-connect"
    directory.mkdir(parents=True, mode=0o700)
    path = directory / entitlement.ENTITLEMENT_FILE_NAME
    if features is not None:
        path.write_bytes(_signed_license(key, features))
        path.chmod(0o600)
    gate = entitlement.EntitlementGate.for_test(path=path, keyring_document=_keyring(key), now=NOW)
    return AutomateHost(entitlement=gate)


def _licensed_host(tmp_path: Path) -> AutomateHost:
    return _host(tmp_path, [entitlement.CONNECT_FEATURE_ID, entitlement.AUTOMATIONS_FEATURE_ID])


def _workflow() -> Workflow:
    return Workflow.model_validate(
        {
            "name": "lead-funnel",
            "stages": ["captured", "reviewing", "converted"],
            "initial_stage": "captured",
            "definitions": [
                {
                    "name": "start-review",
                    "trigger": {"source_kind": "operator.decision", "decision": "start_review"},
                    "conditions": [{"field": "record.stage", "op": "equals", "value": "captured"}],
                    "effects": [
                        {"kind": "record.transition", "to_stage": "reviewing"},
                        {"kind": "overlay.set", "key": "assignee", "value": "alice"},
                    ],
                },
                {
                    "name": "convert",
                    "trigger": {"source_kind": "operator.decision", "decision": "convert"},
                    "conditions": [{"field": "record.stage", "op": "equals", "value": "reviewing"}],
                    "effects": [{"kind": "record.transition", "to_stage": "converted"}],
                },
            ],
        }
    )


def _engine(tmp_path: Path, host: AutomateHost | None = None) -> WorkflowEngine:
    store = WorkflowStore(tmp_path / "automate" / "workflow.db")
    store.initialize()
    return WorkflowEngine(store=store, host=host or _licensed_host(tmp_path))


def test_decision_matches_definition_and_applies_effects(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    workflow = _workflow()
    record = engine.create_record(workflow, now=NOW)
    outcome = engine.submit_decision(
        workflow,
        record.record_id,
        decision="start_review",
        operation_key="op-1",
        request={"by": "alice"},
        expected_version=1,
        now=NOW,
    )
    assert outcome.matched is True
    assert outcome.applied is True
    assert outcome.definition_name == "start-review"
    assert outcome.record.stage == "reviewing"
    assert engine._store.get_overlays(record.record_id) == {"assignee": "alice"}


def test_decision_that_matches_no_definition_is_a_noop(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    workflow = _workflow()
    record = engine.create_record(workflow, now=NOW)
    # "convert" only applies at the reviewing stage; the record is still captured.
    outcome = engine.submit_decision(
        workflow,
        record.record_id,
        decision="convert",
        operation_key="op-1",
        request={},
        expected_version=1,
        now=NOW,
    )
    assert outcome.matched is False
    assert outcome.applied is False
    assert outcome.record.stage == "captured"
    assert outcome.record.state_version == 1


def test_replay_same_decision_is_idempotent(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    workflow = _workflow()
    record = engine.create_record(workflow, now=NOW)
    first = engine.submit_decision(
        workflow,
        record.record_id,
        decision="start_review",
        operation_key="op-1",
        request={"by": "alice"},
        expected_version=1,
        now=NOW,
    )
    replay = engine.submit_decision(
        workflow,
        record.record_id,
        decision="start_review",
        operation_key="op-1",
        request={"by": "alice"},
        expected_version=1,
        now=NOW,
    )
    assert first.applied is True
    assert replay.applied is False
    assert replay.event_id == first.event_id
    assert engine._store.get_record(record.record_id).state_version == 2


def test_same_key_changed_request_conflicts(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    workflow = _workflow()
    record = engine.create_record(workflow, now=NOW)
    engine.submit_decision(
        workflow,
        record.record_id,
        decision="start_review",
        operation_key="op-1",
        request={"by": "alice"},
        expected_version=1,
        now=NOW,
    )
    with pytest.raises(OperationConflict):
        engine.submit_decision(
            workflow,
            record.record_id,
            decision="start_review",
            operation_key="op-1",
            request={"by": "bob"},
            expected_version=2,
            now=NOW,
        )


def test_ambiguous_decision_is_refused(tmp_path: Path) -> None:
    workflow = Workflow.model_validate(
        {
            "name": "lead-funnel",
            "stages": ["captured", "reviewing"],
            "initial_stage": "captured",
            "definitions": [
                {
                    "name": "a",
                    "trigger": {"source_kind": "operator.decision", "decision": "go"},
                    "conditions": [{"field": "record.stage", "op": "equals", "value": "captured"}],
                    "effects": [{"kind": "record.transition", "to_stage": "reviewing"}],
                },
                {
                    "name": "b",
                    "trigger": {"source_kind": "operator.decision", "decision": "go"},
                    "conditions": [{"field": "record.stage", "op": "equals", "value": "captured"}],
                    "effects": [{"kind": "overlay.set", "key": "flag", "value": True}],
                },
            ],
        }
    )
    engine = _engine(tmp_path)
    record = engine.create_record(workflow, now=NOW)
    with pytest.raises(AmbiguousDecision):
        engine.submit_decision(
            workflow,
            record.record_id,
            decision="go",
            operation_key="op-1",
            request={},
            expected_version=1,
            now=NOW,
        )


def test_decision_on_record_of_another_workflow_is_rejected(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    workflow = _workflow()
    record = engine.create_record(workflow, now=NOW)
    other = Workflow.model_validate(
        {
            "name": "other-flow",
            "stages": ["a", "b"],
            "initial_stage": "a",
            "definitions": [
                {
                    "name": "go",
                    "trigger": {"source_kind": "operator.decision", "decision": "go"},
                    "conditions": [],
                    "effects": [{"kind": "record.transition", "to_stage": "b"}],
                }
            ],
        }
    )
    with pytest.raises(WorkflowMismatch):
        engine.submit_decision(
            other,
            record.record_id,
            decision="go",
            operation_key="op-1",
            request={},
            expected_version=1,
            now=NOW,
        )


def test_unknown_record_is_rejected(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    workflow = _workflow()
    with pytest.raises(UnknownRecord):
        engine.submit_decision(
            workflow,
            "11111111-1111-4111-8111-111111111111",
            decision="start_review",
            operation_key="op-1",
            request={},
            expected_version=1,
            now=NOW,
        )


def test_unlicensed_host_refuses_create_and_decision(tmp_path: Path) -> None:
    host = _host(tmp_path, [entitlement.CONNECT_FEATURE_ID])  # missing automations feature
    engine = _engine(tmp_path, host=host)
    workflow = _workflow()
    with pytest.raises(AutomateLicenseError):
        engine.create_record(workflow, now=NOW)


def test_decision_refused_when_license_absent_even_for_existing_record(tmp_path: Path) -> None:
    # Create the record while licensed, then prove a decision is refused once the license
    # is gone: the engine revalidates at each admission boundary, not only at startup.
    licensed = _licensed_host(tmp_path)
    store = WorkflowStore(tmp_path / "automate" / "workflow.db")
    store.initialize()
    workflow = _workflow()
    record = WorkflowEngine(store=store, host=licensed).create_record(workflow, now=NOW)

    unlicensed = _host(tmp_path / "gone", None)
    engine = WorkflowEngine(store=store, host=unlicensed)
    with pytest.raises(AutomateLicenseError):
        engine.submit_decision(
            workflow,
            record.record_id,
            decision="start_review",
            operation_key="op-1",
            request={},
            expected_version=1,
            now=NOW,
        )


def test_stale_expected_version_is_rejected_before_matching(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    workflow = _workflow()
    record = engine.create_record(workflow, now=NOW)  # state_version 1
    with pytest.raises(StaleRecord):
        engine.submit_decision(
            workflow,
            record.record_id,
            decision="start_review",
            operation_key="op-1",
            request={},
            expected_version=2,  # ahead of the record's actual version
            now=NOW,
        )


def test_no_match_retry_after_advance_stays_a_no_match(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    workflow = _workflow()
    record = engine.create_record(workflow, now=NOW)  # captured, v1
    # "convert" does not match at captured: a no-match that reserves the key.
    first = engine.submit_decision(
        workflow,
        record.record_id,
        decision="convert",
        operation_key="k",
        request={"by": "a"},
        expected_version=1,
        now=NOW,
    )
    assert first.matched is False
    # Advance to reviewing via a different decision and key.
    engine.submit_decision(
        workflow,
        record.record_id,
        decision="start_review",
        operation_key="sr",
        request={},
        expected_version=1,
        now=NOW,
    )
    # Retrying "convert" with the SAME key must replay the no-match, not apply now that the
    # record is at reviewing where convert would otherwise match.
    replay = engine.submit_decision(
        workflow,
        record.record_id,
        decision="convert",
        operation_key="k",
        request={"by": "a"},
        expected_version=2,
        now=NOW,
    )
    assert replay.matched is False
    assert replay.applied is False
    assert engine._store.get_record(record.record_id).stage == "reviewing"
