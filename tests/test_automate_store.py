from __future__ import annotations

import os
import sqlite3
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eom_email_watcher.automate.store import (
    InvalidEffect,
    OperationConflict,
    StaleRecord,
    UnknownRecord,
    WorkflowStore,
    request_fingerprint,
)

NOW = datetime(2026, 6, 1, tzinfo=UTC)
STAGES = frozenset({"captured", "reviewing", "converted"})


def _store(tmp_path: Path) -> WorkflowStore:
    store = WorkflowStore(tmp_path / "automate" / "workflow.db")
    store.initialize()
    return store


def test_create_record_starts_at_version_one(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_record("lead-funnel", "captured", now=NOW, allowed_stages=STAGES)
    assert record.stage == "captured"
    assert record.state_version == 1
    assert store.get_record(record.record_id).stage == "captured"


def test_create_rejects_stage_outside_allowed_set(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.create_record("lead-funnel", "nope", now=NOW, allowed_stages=STAGES)


def test_transition_advances_under_cas(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_record("lead-funnel", "captured", now=NOW, allowed_stages=STAGES)
    outcome = store.transition(
        record.record_id,
        "reviewing",
        operation_key="op-1",
        operation_name="start_review",
        request={"by": "alice"},
        expected_version=1,
        now=NOW + timedelta(minutes=1),
        allowed_stages=STAGES,
    )
    assert outcome.applied is True
    assert outcome.record.stage == "reviewing"
    assert outcome.record.state_version == 2


def test_transition_with_stale_version_conflicts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_record("lead-funnel", "captured", now=NOW, allowed_stages=STAGES)
    with pytest.raises(StaleRecord) as excinfo:
        store.transition(
            record.record_id,
            "reviewing",
            operation_key="op-1",
            operation_name="start_review",
            request={"by": "alice"},
            expected_version=99,
            now=NOW,
            allowed_stages=STAGES,
        )
    assert excinfo.value.actual == 1
    assert store.get_record(record.record_id).state_version == 1


def test_replay_same_key_same_request_is_noop(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_record("lead-funnel", "captured", now=NOW, allowed_stages=STAGES)
    first = store.transition(
        record.record_id,
        "reviewing",
        operation_key="op-1",
        operation_name="start_review",
        request={"by": "alice"},
        expected_version=1,
        now=NOW,
        allowed_stages=STAGES,
    )
    replay = store.transition(
        record.record_id,
        "reviewing",
        operation_key="op-1",
        operation_name="start_review",
        request={"by": "alice"},
        expected_version=1,
        now=NOW + timedelta(hours=1),
        allowed_stages=STAGES,
    )
    assert first.applied is True
    assert replay.applied is False
    assert replay.event_id == first.event_id
    # No second event, and the record did not advance again.
    assert store.get_record(record.record_id).state_version == 2
    with store.connection() as db:
        count = db.execute(
            "SELECT COUNT(*) AS n FROM workflow_events WHERE record_id = ?",
            (record.record_id,),
        ).fetchone()["n"]
    assert count == 2  # the bootstrap event plus one transition


def test_replay_same_key_changed_request_conflicts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_record("lead-funnel", "captured", now=NOW, allowed_stages=STAGES)
    store.transition(
        record.record_id,
        "reviewing",
        operation_key="op-1",
        operation_name="start_review",
        request={"by": "alice"},
        expected_version=1,
        now=NOW,
        allowed_stages=STAGES,
    )
    with pytest.raises(OperationConflict):
        store.transition(
            record.record_id,
            "reviewing",
            operation_key="op-1",
            operation_name="start_review",
            request={"by": "bob"},
            expected_version=2,
            now=NOW,
            allowed_stages=STAGES,
        )
    assert store.get_record(record.record_id).state_version == 2


def test_transition_unknown_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(UnknownRecord):
        store.transition(
            "11111111-1111-4111-8111-111111111111",
            "reviewing",
            operation_key="op-1",
            operation_name="start_review",
            request={},
            expected_version=1,
            now=NOW,
        )


def test_events_are_immutable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_record("lead-funnel", "captured", now=NOW, allowed_stages=STAGES)
    with pytest.raises(sqlite3.IntegrityError), store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "UPDATE workflow_events SET next_stage = 'tamper' WHERE record_id = ?",
            (record.record_id,),
        )
    with pytest.raises(sqlite3.IntegrityError), store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM workflow_events WHERE record_id = ?", (record.record_id,))


def test_request_fingerprint_is_order_independent() -> None:
    assert request_fingerprint({"a": 1, "b": 2}) == request_fingerprint({"b": 2, "a": 1})
    assert request_fingerprint({"a": 1}) != request_fingerprint({"a": 2})


def test_replay_after_advance_returns_prior_outcome(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_record("lead-funnel", "captured", now=NOW, allowed_stages=STAGES)
    first = store.transition(
        record.record_id,
        "reviewing",
        operation_key="op-1",
        operation_name="start_review",
        request={"by": "alice"},
        expected_version=1,
        now=NOW,
        allowed_stages=STAGES,
    )
    store.transition(
        record.record_id,
        "converted",
        operation_key="op-2",
        operation_name="convert",
        request={"by": "alice"},
        expected_version=2,
        now=NOW,
        allowed_stages=STAGES,
    )
    # The record is now at version 3; replaying op-1 must report the version-2 outcome.
    replay = store.transition(
        record.record_id,
        "reviewing",
        operation_key="op-1",
        operation_name="start_review",
        request={"by": "alice"},
        expected_version=1,
        now=NOW,
        allowed_stages=STAGES,
    )
    assert replay.applied is False
    assert replay.event_id == first.event_id
    assert replay.record.stage == "reviewing"
    assert replay.record.state_version == 2
    assert store.get_record(record.record_id).state_version == 3


def test_replay_same_key_changed_destination_conflicts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_record("lead-funnel", "captured", now=NOW, allowed_stages=STAGES)
    store.transition(
        record.record_id,
        "reviewing",
        operation_key="op-1",
        operation_name="start_review",
        request={"by": "alice"},
        expected_version=1,
        now=NOW,
        allowed_stages=STAGES,
    )
    # Same key, name, and request but a different destination stage is a different intent.
    with pytest.raises(OperationConflict):
        store.transition(
            record.record_id,
            "converted",
            operation_key="op-1",
            operation_name="start_review",
            request={"by": "alice"},
            expected_version=2,
            now=NOW,
            allowed_stages=STAGES,
        )
    assert store.get_record(record.record_id).stage == "reviewing"


def test_events_cannot_be_deleted_even_after_record_removed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_record("lead-funnel", "captured", now=NOW, allowed_stages=STAGES)
    with pytest.raises(sqlite3.IntegrityError), store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM workflow_records WHERE record_id = ?", (record.record_id,))
        db.execute("DELETE FROM workflow_events WHERE record_id = ?", (record.record_id,))


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_database_file_is_owner_only(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def test_apply_effects_sets_overlay_and_advances_stage_atomically(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_record("lead-funnel", "captured", now=NOW, allowed_stages=STAGES)
    outcome = store.apply_effects(
        record.record_id,
        [
            {"kind": "record.transition", "to_stage": "reviewing"},
            {"kind": "overlay.set", "key": "assignee", "value": "alice"},
            {"kind": "overlay.set", "key": "priority", "value": 3},
        ],
        operation_key="op-1",
        operation_name="start_review",
        request={"by": "alice"},
        expected_version=1,
        now=NOW,
        allowed_stages=STAGES,
    )
    assert outcome.applied is True
    assert outcome.record.stage == "reviewing"
    # One event for the whole batch, so exactly one version bump.
    assert outcome.record.state_version == 2
    assert store.get_overlays(record.record_id) == {"assignee": "alice", "priority": 3}


def test_overlay_only_batch_bumps_version_without_changing_stage(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_record("lead-funnel", "captured", now=NOW, allowed_stages=STAGES)
    outcome = store.apply_effects(
        record.record_id,
        [{"kind": "overlay.set", "key": "note", "value": "called back"}],
        operation_key="op-1",
        operation_name="annotate",
        request={},
        expected_version=1,
        now=NOW,
        allowed_stages=STAGES,
    )
    assert outcome.record.stage == "captured"
    assert outcome.record.state_version == 2
    assert store.get_overlays(record.record_id) == {"note": "called back"}


def test_overlay_set_is_last_writer_wins_per_key(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_record("lead-funnel", "captured", now=NOW, allowed_stages=STAGES)
    store.apply_effects(
        record.record_id,
        [{"kind": "overlay.set", "key": "assignee", "value": "alice"}],
        operation_key="op-1",
        operation_name="assign",
        request={"to": "alice"},
        expected_version=1,
        now=NOW,
        allowed_stages=STAGES,
    )
    store.apply_effects(
        record.record_id,
        [{"kind": "overlay.set", "key": "assignee", "value": "bob"}],
        operation_key="op-2",
        operation_name="assign",
        request={"to": "bob"},
        expected_version=2,
        now=NOW,
        allowed_stages=STAGES,
    )
    assert store.get_overlays(record.record_id) == {"assignee": "bob"}


def test_apply_effects_replay_same_key_changed_effects_conflicts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_record("lead-funnel", "captured", now=NOW, allowed_stages=STAGES)
    store.apply_effects(
        record.record_id,
        [{"kind": "overlay.set", "key": "assignee", "value": "alice"}],
        operation_key="op-1",
        operation_name="assign",
        request={"to": "alice"},
        expected_version=1,
        now=NOW,
        allowed_stages=STAGES,
    )
    # Same key, name, and request but a changed effect batch is a different intent.
    with pytest.raises(OperationConflict):
        store.apply_effects(
            record.record_id,
            [{"kind": "overlay.set", "key": "assignee", "value": "bob"}],
            operation_key="op-1",
            operation_name="assign",
            request={"to": "alice"},
            expected_version=2,
            now=NOW,
            allowed_stages=STAGES,
        )
    assert store.get_overlays(record.record_id) == {"assignee": "alice"}


def test_apply_effects_replay_same_batch_is_noop(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_record("lead-funnel", "captured", now=NOW, allowed_stages=STAGES)
    batch = [
        {"kind": "record.transition", "to_stage": "reviewing"},
        {"kind": "overlay.set", "key": "assignee", "value": "alice"},
    ]
    first = store.apply_effects(
        record.record_id,
        batch,
        operation_key="op-1",
        operation_name="start_review",
        request={"by": "alice"},
        expected_version=1,
        now=NOW,
        allowed_stages=STAGES,
    )
    replay = store.apply_effects(
        record.record_id,
        batch,
        operation_key="op-1",
        operation_name="start_review",
        request={"by": "alice"},
        expected_version=1,
        now=NOW + timedelta(hours=1),
        allowed_stages=STAGES,
    )
    assert replay.applied is False
    assert replay.event_id == first.event_id
    assert store.get_record(record.record_id).state_version == 2


def test_apply_effects_rejects_empty_batch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_record("lead-funnel", "captured", now=NOW, allowed_stages=STAGES)
    with pytest.raises(InvalidEffect):
        store.apply_effects(
            record.record_id,
            [],
            operation_key="op-1",
            operation_name="noop",
            request={},
            expected_version=1,
            now=NOW,
            allowed_stages=STAGES,
        )


def test_apply_effects_rejects_two_transitions_in_one_batch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_record("lead-funnel", "captured", now=NOW, allowed_stages=STAGES)
    with pytest.raises(InvalidEffect):
        store.apply_effects(
            record.record_id,
            [
                {"kind": "record.transition", "to_stage": "reviewing"},
                {"kind": "record.transition", "to_stage": "converted"},
            ],
            operation_key="op-1",
            operation_name="double",
            request={},
            expected_version=1,
            now=NOW,
            allowed_stages=STAGES,
        )


def test_apply_effects_rejects_unknown_effect_kind(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_record("lead-funnel", "captured", now=NOW, allowed_stages=STAGES)
    with pytest.raises(InvalidEffect):
        store.apply_effects(
            record.record_id,
            [{"kind": "record.delete"}],
            operation_key="op-1",
            operation_name="bad",
            request={},
            expected_version=1,
            now=NOW,
            allowed_stages=STAGES,
        )


def test_get_overlays_unknown_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(UnknownRecord):
        store.get_overlays("11111111-1111-4111-8111-111111111111")


def test_apply_effects_rejects_extra_member_on_an_effect(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_record("lead-funnel", "captured", now=NOW, allowed_stages=STAGES)
    # A smuggled to_stage on an overlay.set must fail closed, not silently drop.
    with pytest.raises(InvalidEffect):
        store.apply_effects(
            record.record_id,
            [{"kind": "overlay.set", "key": "flag", "value": True, "to_stage": "reviewing"}],
            operation_key="op-1",
            operation_name="x",
            request={},
            expected_version=1,
            now=NOW,
            allowed_stages=STAGES,
        )


def test_reserve_no_match_twice_is_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_record("lead-funnel", "captured", now=NOW, allowed_stages=STAGES)
    store.reserve_no_match(
        record.record_id, "op-1", operation_name="convert", request={"by": "a"}, now=NOW
    )
    # A second identical reservation is a no-op, not a primary-key violation.
    store.reserve_no_match(
        record.record_id, "op-1", operation_name="convert", request={"by": "a"}, now=NOW
    )


def test_reserve_no_match_changed_request_conflicts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_record("lead-funnel", "captured", now=NOW, allowed_stages=STAGES)
    store.reserve_no_match(
        record.record_id, "op-1", operation_name="convert", request={"by": "a"}, now=NOW
    )
    with pytest.raises(OperationConflict):
        store.reserve_no_match(
            record.record_id, "op-1", operation_name="convert", request={"by": "b"}, now=NOW
        )


def test_apply_effects_on_a_no_match_key_conflicts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_record("lead-funnel", "captured", now=NOW, allowed_stages=STAGES)
    store.reserve_no_match(
        record.record_id, "op-1", operation_name="convert", request={"by": "a"}, now=NOW
    )
    # Reusing the reserved no-match key to apply effects is changed intent, not a replay.
    with pytest.raises(OperationConflict):
        store.transition(
            record.record_id,
            "reviewing",
            operation_key="op-1",
            operation_name="convert",
            request={"by": "a"},
            expected_version=1,
            now=NOW,
            allowed_stages=STAGES,
        )


def test_reserve_no_match_is_replayed_by_lookup(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_record("lead-funnel", "captured", now=NOW, allowed_stages=STAGES)
    store.reserve_no_match(
        record.record_id, "op-1", operation_name="convert", request={"by": "a"}, now=NOW
    )
    replay = store.lookup_operation(record.record_id, "op-1")
    assert replay is not None
    assert replay.matched is False
    assert replay.event_id is None
    assert replay.operation_name == "convert"
    # The record was not changed by a no-match reservation.
    assert store.get_record(record.record_id).state_version == 1
