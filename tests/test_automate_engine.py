from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from connect_automate import entitlement
from connect_automate.automate import (
    AmbiguousDecision,
    AutomateHost,
    AutomateLicenseError,
    PackOwnershipError,
    PackVersionError,
    RequestBindingError,
    Workflow,
    WorkflowEngine,
    WorkflowMismatch,
)
from connect_automate.automate.store import (
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


def test_over_long_decision_is_rejected(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    workflow = _workflow()
    record = engine.create_record(workflow, now=NOW)
    with pytest.raises(ValueError):
        engine.submit_decision(
            workflow,
            record.record_id,
            decision="x" * 81,  # longer than the 80-char trigger-name limit
            operation_key="op-1",
            request={},
            expected_version=1,
            now=NOW,
        )


def test_completed_operation_replays_after_a_revision_advanced_the_record(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    v1 = Workflow.model_validate(
        {
            "name": "flow",
            "stages": ["captured", "reviewing"],
            "initial_stage": "captured",
            "definitions": [
                {
                    "name": "start",
                    "trigger": {"source_kind": "operator.decision", "decision": "start"},
                    "conditions": [{"field": "record.stage", "op": "equals", "value": "captured"}],
                    "effects": [{"kind": "record.transition", "to_stage": "reviewing"}],
                }
            ],
        }
    )
    record = engine.create_record(v1, now=NOW)
    first = engine.submit_decision(
        v1,
        record.record_id,
        decision="start",
        operation_key="k1",
        request={"by": "a"},
        expected_version=1,
        now=NOW,
    )
    assert first.record.stage == "reviewing"
    # A same-named revision drops "captured" and adds "archived", then advances the record.
    v2 = Workflow.model_validate(
        {
            "name": "flow",
            "stages": ["reviewing", "archived"],
            "initial_stage": "reviewing",
            "definitions": [
                {
                    "name": "archive",
                    "trigger": {"source_kind": "operator.decision", "decision": "archive"},
                    "conditions": [{"field": "record.stage", "op": "equals", "value": "reviewing"}],
                    "effects": [{"kind": "record.transition", "to_stage": "archived"}],
                }
            ],
        }
    )
    engine.submit_decision(
        v2,
        record.record_id,
        decision="archive",
        operation_key="k2",
        request={},
        expected_version=2,
        now=NOW,
    )
    assert engine._store.get_record(record.record_id).stage == "archived"
    # Retrying the v1 operation must replay its recorded outcome, even though the record's
    # current stage ("archived") is not declared by v1.
    replay = engine.submit_decision(
        v1,
        record.record_id,
        decision="start",
        operation_key="k1",
        request={"by": "a"},
        expected_version=1,
        now=NOW,
    )
    assert replay.applied is False
    assert replay.event_id == first.event_id
    assert replay.record.stage == "reviewing"


def test_non_string_operation_key_is_rejected_before_lookup(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    workflow = _workflow()
    record = engine.create_record(workflow, now=NOW)
    with pytest.raises(ValueError):
        engine.submit_decision(
            workflow,
            record.record_id,
            decision="start_review",
            operation_key=7,  # type: ignore[arg-type]
            request={},
            expected_version=1,
            now=NOW,
        )


def test_non_mapping_request_is_rejected(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    workflow = _workflow()
    record = engine.create_record(workflow, now=NOW)
    with pytest.raises(ValueError):
        engine.submit_decision(
            workflow,
            record.record_id,
            decision="start_review",
            operation_key="op-1",
            request=None,  # type: ignore[arg-type]
            expected_version=1,
            now=NOW,
        )


def test_record_stage_outside_supplied_workflow_is_rejected(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    workflow = _workflow()  # stages: captured, reviewing, converted
    record = engine.create_record(workflow, now=NOW)  # created at "captured"
    # A same-named workflow revision whose stage set omits the record's current stage must
    # not be able to describe or transition it.
    replacement = Workflow.model_validate(
        {
            "name": "lead-funnel",
            "stages": ["intake", "done"],
            "initial_stage": "intake",
            "definitions": [
                {
                    "name": "advance",
                    "trigger": {"source_kind": "operator.decision", "decision": "advance"},
                    "conditions": [],
                    "effects": [{"kind": "record.transition", "to_stage": "done"}],
                }
            ],
        }
    )
    with pytest.raises(WorkflowMismatch):
        engine.submit_decision(
            replacement,
            record.record_id,
            decision="advance",
            operation_key="op-1",
            request={},
            expected_version=1,
            now=NOW,
        )


def test_empty_decision_is_rejected_cleanly(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    workflow = _workflow()
    record = engine.create_record(workflow, now=NOW)
    # An empty decision matches no trigger and must surface a domain ValueError rather than
    # a raw sqlite3.IntegrityError from the operation_name constraint.
    with pytest.raises(ValueError):
        engine.submit_decision(
            workflow,
            record.record_id,
            decision="",
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


def test_create_record_binds_the_pack_id(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    workflow = _workflow()
    record = engine.create_record(workflow, now=NOW, pack_id="pack-a", pack_version=1)
    assert record.pack_id == "pack-a"
    assert engine._store.get_record(record.record_id).pack_id == "pack-a"


def test_create_record_leaves_pack_id_none_by_default(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    record = engine.create_record(_workflow(), now=NOW)
    assert record.pack_id is None


def test_submit_decision_accepts_the_owning_pack(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    workflow = _workflow()
    record = engine.create_record(workflow, now=NOW, pack_id="pack-a", pack_version=1)
    outcome = engine.submit_decision(
        workflow,
        record.record_id,
        decision="start_review",
        operation_key="op-1",
        request={"by": "alice"},
        expected_version=1,
        now=NOW,
        expected_pack_id="pack-a",
    )
    assert outcome.matched is True
    assert outcome.applied is True


def test_submit_decision_refuses_a_foreign_pack(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    workflow = _workflow()
    record = engine.create_record(workflow, now=NOW, pack_id="pack-a", pack_version=1)
    with pytest.raises(PackOwnershipError):
        engine.submit_decision(
            workflow,
            record.record_id,
            decision="start_review",
            operation_key="op-1",
            request={"by": "mallory"},
            expected_version=1,
            now=NOW,
            expected_pack_id="pack-b",
        )
    # The record did not advance: the foreign pack was refused before any mutation.
    assert engine._store.get_record(record.record_id).stage == "captured"
    assert engine._store.get_record(record.record_id).state_version == 1


def test_submit_decision_refuses_a_foreign_pack_even_on_replay(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    workflow = _workflow()
    record = engine.create_record(workflow, now=NOW, pack_id="pack-a", pack_version=1)
    engine.submit_decision(
        workflow,
        record.record_id,
        decision="start_review",
        operation_key="op-1",
        request={"by": "alice"},
        expected_version=1,
        now=NOW,
        expected_pack_id="pack-a",
    )
    # A foreign pack replaying the same operation key must be refused before the replay lookup,
    # so it cannot read another pack's recorded outcome.
    with pytest.raises(PackOwnershipError):
        engine.submit_decision(
            workflow,
            record.record_id,
            decision="start_review",
            operation_key="op-1",
            request={"by": "alice"},
            expected_version=1,
            now=NOW,
            expected_pack_id="pack-b",
        )


def test_submit_decision_refuses_when_the_record_is_unbound_but_a_pack_is_expected(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    workflow = _workflow()
    # A bare-workflow record (pack_id None) cannot be driven by a pack-scoped caller.
    record = engine.create_record(workflow, now=NOW)
    with pytest.raises(PackOwnershipError):
        engine.submit_decision(
            workflow,
            record.record_id,
            decision="start_review",
            operation_key="op-1",
            request={},
            expected_version=1,
            now=NOW,
            expected_pack_id="pack-a",
        )


def test_submit_decision_without_expected_pack_id_skips_the_check(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    workflow = _workflow()
    # A record bound to a pack can still be driven by a bare-workflow caller (expected_pack_id
    # None): the check is opt-in, preserving the pre-pack engine behavior.
    record = engine.create_record(workflow, now=NOW, pack_id="pack-a", pack_version=1)
    outcome = engine.submit_decision(
        workflow,
        record.record_id,
        decision="start_review",
        operation_key="op-1",
        request={},
        expected_version=1,
        now=NOW,
    )
    assert outcome.applied is True


def test_submit_decision_accepts_the_matching_pack_version(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    workflow = _workflow()
    record = engine.create_record(workflow, now=NOW, pack_id="pack-a", pack_version=2)
    assert record.pack_version == 2
    outcome = engine.submit_decision(
        workflow,
        record.record_id,
        decision="start_review",
        operation_key="op-1",
        request={"by": "alice"},
        expected_version=1,
        now=NOW,
        expected_pack_id="pack-a",
        expected_pack_version=2,
    )
    assert outcome.applied is True


def test_submit_decision_refuses_a_different_pack_version(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    workflow = _workflow()
    record = engine.create_record(workflow, now=NOW, pack_id="pack-a", pack_version=1)
    # Same pack, upgraded version: the record is frozen to the version it started under.
    with pytest.raises(PackVersionError):
        engine.submit_decision(
            workflow,
            record.record_id,
            decision="start_review",
            operation_key="op-1",
            request={"by": "alice"},
            expected_version=1,
            now=NOW,
            expected_pack_id="pack-a",
            expected_pack_version=2,
        )
    # The record did not advance: the newer version was refused before any mutation.
    assert engine._store.get_record(record.record_id).stage == "captured"
    assert engine._store.get_record(record.record_id).state_version == 1


def test_submit_decision_refuses_a_different_pack_version_even_on_replay(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    workflow = _workflow()
    record = engine.create_record(workflow, now=NOW, pack_id="pack-a", pack_version=1)
    engine.submit_decision(
        workflow,
        record.record_id,
        decision="start_review",
        operation_key="op-1",
        request={"by": "alice"},
        expected_version=1,
        now=NOW,
        expected_pack_id="pack-a",
        expected_pack_version=1,
    )
    # An upgraded runtime replaying the same key is refused before the replay lookup.
    with pytest.raises(PackVersionError):
        engine.submit_decision(
            workflow,
            record.record_id,
            decision="start_review",
            operation_key="op-1",
            request={"by": "alice"},
            expected_version=1,
            now=NOW,
            expected_pack_id="pack-a",
            expected_pack_version=2,
        )


def test_submit_decision_without_expected_pack_version_skips_the_version_check(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    workflow = _workflow()
    record = engine.create_record(workflow, now=NOW, pack_id="pack-a", pack_version=5)
    # expected_pack_version None skips the version freeze (opt-in), like the ownership check.
    outcome = engine.submit_decision(
        workflow,
        record.record_id,
        decision="start_review",
        operation_key="op-1",
        request={},
        expected_version=1,
        now=NOW,
        expected_pack_id="pack-a",
    )
    assert outcome.applied is True


ARTIFACT_ID = "5aaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _connect_invoke_action(parameters: dict) -> dict:
    return {
        "action": "connect.invoke",
        "request": {
            "capability": {"id": "lead.customer-handoff", "version": "1.0"},
            "input": {
                "artifact_id": ARTIFACT_ID,
                "media_type": "application/json",
                "filename": "request.json",
            },
            "parameters": parameters,
        },
    }


def _binding_workflow(convert_parameters: dict, *, set_lead_id_on: str) -> Workflow:
    """A two-stage workflow whose convert decision emits an overlay-bound connect.invoke.

    ``set_lead_id_on`` chooses which decision sets the ``lead_id`` overlay: "start_review"
    (a prior decision) or "convert" (the same decision that binds it).
    """
    start_effects: list[dict] = [{"kind": "record.transition", "to_stage": "reviewing"}]
    convert_effects: list[dict] = [{"kind": "record.transition", "to_stage": "converted"}]
    if set_lead_id_on == "start_review":
        start_effects.append({"kind": "overlay.set", "key": "lead_id", "value": "L-42"})
    elif set_lead_id_on == "convert":
        convert_effects.append({"kind": "overlay.set", "key": "lead_id", "value": "L-42"})
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
                    "effects": start_effects,
                },
                {
                    "name": "convert",
                    "trigger": {"source_kind": "operator.decision", "decision": "convert"},
                    "conditions": [{"field": "record.stage", "op": "equals", "value": "reviewing"}],
                    "effects": convert_effects,
                    "actions": [_connect_invoke_action(convert_parameters)],
                },
            ],
        }
    )


def _advance_to_reviewing(engine: WorkflowEngine, workflow: Workflow) -> str:
    record = engine.create_record(workflow, now=NOW)
    engine.submit_decision(
        workflow,
        record.record_id,
        decision="start_review",
        operation_key="op-start",
        request={},
        expected_version=1,
        now=NOW,
    )
    return record.record_id


def _frozen_connect_invoke_request(engine: WorkflowEngine, record_id: str) -> dict:
    actions = [a for a in engine._store.list_actions(record_id) if a.kind == "connect.invoke"]
    assert len(actions) == 1
    return actions[0].request


def test_connect_invoke_binding_resolves_against_a_prior_decisions_overlay(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    workflow = _binding_workflow(
        {"lead-id": {"overlay": "lead_id"}}, set_lead_id_on="start_review"
    )
    record_id = _advance_to_reviewing(engine, workflow)
    engine.submit_decision(
        workflow,
        record_id,
        decision="convert",
        operation_key="op-convert",
        request={},
        expected_version=2,
        now=NOW,
    )
    # The frozen action request carries the resolved value, not the binding.
    assert _frozen_connect_invoke_request(engine, record_id)["parameters"] == {"lead-id": "L-42"}


def test_connect_invoke_binding_resolves_against_the_same_decisions_overlay(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    workflow = _binding_workflow(
        {"lead-id": {"overlay": "lead_id"}}, set_lead_id_on="convert"
    )
    record_id = _advance_to_reviewing(engine, workflow)
    engine.submit_decision(
        workflow,
        record_id,
        decision="convert",
        operation_key="op-convert",
        request={},
        expected_version=2,
        now=NOW,
    )
    # A value set by this same decision's overlay.set is visible to its connect.invoke binding.
    assert _frozen_connect_invoke_request(engine, record_id)["parameters"] == {"lead-id": "L-42"}


def test_connect_invoke_binding_to_an_unset_overlay_fails_the_decision(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    workflow = _binding_workflow({"lead-id": {"overlay": "lead_id"}}, set_lead_id_on="none")
    record_id = _advance_to_reviewing(engine, workflow)
    with pytest.raises(RequestBindingError):
        engine.submit_decision(
            workflow,
            record_id,
            decision="convert",
            operation_key="op-convert",
            request={},
            expected_version=2,
            now=NOW,
        )
    # The decision did not apply: the record is still at reviewing and nothing was admitted.
    assert engine._store.get_record(record_id).stage == "reviewing"
    assert engine._store.list_actions(record_id) == []


def test_connect_invoke_input_content_binding_resolves_and_encodes(tmp_path: Path) -> None:
    import base64

    engine = _engine(tmp_path)
    action = {
        "action": "connect.invoke",
        "request": {
            "capability": {"id": "lead.customer-handoff", "version": "1.0"},
            "input": {
                "artifact_id": ARTIFACT_ID,
                "media_type": "application/json",
                "filename": "request.json",
                "content_base64": {"overlay": "payload"},
            },
        },
    }
    workflow = Workflow.model_validate(
        {
            "name": "lead-funnel",
            "stages": ["captured", "reviewing", "converted"],
            "initial_stage": "captured",
            "definitions": [
                {
                    "name": "start-review",
                    "trigger": {"source_kind": "operator.decision", "decision": "start_review"},
                    "conditions": [{"field": "record.stage", "op": "equals", "value": "captured"}],
                    "effects": [{"kind": "record.transition", "to_stage": "reviewing"}],
                },
                {
                    "name": "convert",
                    "trigger": {"source_kind": "operator.decision", "decision": "convert"},
                    "conditions": [{"field": "record.stage", "op": "equals", "value": "reviewing"}],
                    "effects": [
                        {"kind": "record.transition", "to_stage": "converted"},
                        {"kind": "overlay.set", "key": "payload", "value": '{"lead":"L-42"}'},
                    ],
                    "actions": [action],
                },
            ],
        }
    )
    record_id = _advance_to_reviewing(engine, workflow)
    engine.submit_decision(
        workflow,
        record_id,
        decision="convert",
        operation_key="op-convert",
        request={},
        expected_version=2,
        now=NOW,
    )
    frozen = _frozen_connect_invoke_request(engine, record_id)
    # The overlay's raw payload is base64-encoded into the frozen input artifact.
    assert frozen["input"]["content_base64"] == base64.b64encode(b'{"lead":"L-42"}').decode("ascii")
