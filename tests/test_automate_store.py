from __future__ import annotations

import os
import sqlite3
import stat
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from connect_automate.automate.store import (
    ActionConflict,
    InvalidEffect,
    OperationConflict,
    StaleRecord,
    UnknownRecord,
    UnresolvedAction,
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


def test_apply_effects_rejects_a_surrogate_string_value(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_record("lead-funnel", "captured", now=NOW, allowed_stages=STAGES)
    # A lone surrogate is not UTF-8 encodable; the store must reject it, not leak
    # UnicodeEncodeError.
    with pytest.raises(InvalidEffect):
        store.apply_effects(
            record.record_id,
            [{"kind": "overlay.set", "key": "k", "value": "\ud800"}],
            operation_key="op-1",
            operation_name="x",
            request={},
            expected_version=1,
            now=NOW,
            allowed_stages=STAGES,
        )


def test_apply_effects_rejects_an_oversized_integer_value(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_record("lead-funnel", "captured", now=NOW, allowed_stages=STAGES)
    # An integer past the interpreter digit limit cannot be serialized; reject it as an
    # InvalidEffect rather than leaking ValueError from json.dumps.
    with pytest.raises(InvalidEffect):
        store.apply_effects(
            record.record_id,
            [{"kind": "overlay.set", "key": "k", "value": 10**5000}],
            operation_key="op-1",
            operation_name="x",
            request={},
            expected_version=1,
            now=NOW,
            allowed_stages=STAGES,
        )


def test_reserve_no_match_replays_a_matching_applied_operation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_record("lead-funnel", "captured", now=NOW, allowed_stages=STAGES)
    # Model the race: the key was already applied by an identical submission. A subsequent
    # reserve_no_match with the same name and request must replay that outcome, not conflict.
    outcome = store.apply_effects(
        record.record_id,
        [{"kind": "record.transition", "to_stage": "reviewing"}],
        operation_key="k",
        operation_name="advance",
        request={"by": "a"},
        expected_version=1,
        now=NOW,
        allowed_stages=STAGES,
    )
    replay = store.reserve_no_match(
        record.record_id,
        "k",
        operation_name="advance",
        request={"by": "a"},
        expected_version=1,
        now=NOW,
    )
    assert replay is not None
    assert replay.matched is True
    assert replay.event_id == outcome.event_id
    assert replay.record.stage == "reviewing"


def test_lookup_operation_rejects_a_non_string_key(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_record("lead-funnel", "captured", now=NOW, allowed_stages=STAGES)
    with pytest.raises(ValueError):
        store.lookup_operation(record.record_id, 7)  # type: ignore[arg-type]


def test_request_fingerprint_rejects_a_non_mapping() -> None:
    with pytest.raises(ValueError):
        request_fingerprint(None)  # type: ignore[arg-type]


def test_apply_effects_rejects_a_batch_over_the_cap(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_record("lead-funnel", "captured", now=NOW, allowed_stages=STAGES)
    effects = [{"kind": "overlay.set", "key": f"k{i}", "value": i} for i in range(9)]
    with pytest.raises(InvalidEffect):
        store.apply_effects(
            record.record_id,
            effects,
            operation_key="op-1",
            operation_name="x",
            request={},
            expected_version=1,
            now=NOW,
            allowed_stages=STAGES,
        )


def test_apply_effects_rejects_empty_operation_key(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_record("lead-funnel", "captured", now=NOW, allowed_stages=STAGES)
    with pytest.raises(ValueError):
        store.apply_effects(
            record.record_id,
            [{"kind": "overlay.set", "key": "k", "value": 1}],
            operation_key="",
            operation_name="x",
            request={},
            expected_version=1,
            now=NOW,
            allowed_stages=STAGES,
        )


def test_apply_effects_rejects_a_non_object_effect_element(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_record("lead-funnel", "captured", now=NOW, allowed_stages=STAGES)
    # A scalar/None element must fail closed with InvalidEffect, not a raw AttributeError.
    with pytest.raises(InvalidEffect):
        store.apply_effects(
            record.record_id,
            [None],
            operation_key="op-1",
            operation_name="x",
            request={},
            expected_version=1,
            now=NOW,
            allowed_stages=STAGES,
        )


def test_apply_effects_rejects_a_non_sequence_batch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_record("lead-funnel", "captured", now=NOW, allowed_stages=STAGES)
    with pytest.raises(InvalidEffect):
        store.apply_effects(
            record.record_id,
            {"kind": "overlay.set", "key": "k", "value": 1},  # a mapping, not a batch
            operation_key="op-1",
            operation_name="x",
            request={},
            expected_version=1,
            now=NOW,
            allowed_stages=STAGES,
        )


def test_apply_effects_rejects_an_oversized_overlay_payload(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_record("lead-funnel", "captured", now=NOW, allowed_stages=STAGES)
    with pytest.raises(InvalidEffect):
        store.apply_effects(
            record.record_id,
            [{"kind": "overlay.set", "key": "blob", "value": "x" * 20000}],
            operation_key="op-1",
            operation_name="x",
            request={},
            expected_version=1,
            now=NOW,
            allowed_stages=STAGES,
        )


def test_apply_effects_rejects_a_non_string_operation_name(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_record("lead-funnel", "captured", now=NOW, allowed_stages=STAGES)
    with pytest.raises(ValueError):
        store.apply_effects(
            record.record_id,
            [{"kind": "overlay.set", "key": "k", "value": 1}],
            operation_key="op-1",
            operation_name=7,  # type: ignore[arg-type]
            request={},
            expected_version=1,
            now=NOW,
            allowed_stages=STAGES,
        )


def test_apply_effects_rejects_empty_operation_name(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_record("lead-funnel", "captured", now=NOW, allowed_stages=STAGES)
    with pytest.raises(ValueError):
        store.apply_effects(
            record.record_id,
            [{"kind": "overlay.set", "key": "k", "value": 1}],
            operation_key="op-1",
            operation_name="",
            request={},
            expected_version=1,
            now=NOW,
            allowed_stages=STAGES,
        )


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
        record.record_id,
        "op-1",
        operation_name="convert",
        request={"by": "a"},
        expected_version=1,
        now=NOW,
    )
    # A second identical reservation is a no-op, not a primary-key violation.
    store.reserve_no_match(
        record.record_id,
        "op-1",
        operation_name="convert",
        request={"by": "a"},
        expected_version=1,
        now=NOW,
    )


def test_reserve_no_match_changed_request_conflicts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_record("lead-funnel", "captured", now=NOW, allowed_stages=STAGES)
    store.reserve_no_match(
        record.record_id,
        "op-1",
        operation_name="convert",
        request={"by": "a"},
        expected_version=1,
        now=NOW,
    )
    with pytest.raises(OperationConflict):
        store.reserve_no_match(
            record.record_id,
            "op-1",
            operation_name="convert",
            request={"by": "b"},
            expected_version=1,
            now=NOW,
        )


def test_reserve_no_match_with_stale_version_conflicts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_record("lead-funnel", "captured", now=NOW, allowed_stages=STAGES)
    store.transition(
        record.record_id,
        "reviewing",
        operation_key="advance",
        operation_name="start_review",
        request={},
        expected_version=1,
        now=NOW,
        allowed_stages=STAGES,
    )
    # The record is now at version 2; a no-match reservation against version 1 must not pin
    # the key to a stale outcome.
    with pytest.raises(StaleRecord):
        store.reserve_no_match(
            record.record_id,
            "op-1",
            operation_name="convert",
            request={},
            expected_version=1,
            now=NOW,
        )


def test_apply_effects_on_a_no_match_key_conflicts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_record("lead-funnel", "captured", now=NOW, allowed_stages=STAGES)
    store.reserve_no_match(
        record.record_id,
        "op-1",
        operation_name="convert",
        request={"by": "a"},
        expected_version=1,
        now=NOW,
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
        record.record_id,
        "op-1",
        operation_name="convert",
        request={"by": "a"},
        expected_version=1,
        now=NOW,
    )
    replay = store.lookup_operation(record.record_id, "op-1")
    assert replay is not None
    assert replay.matched is False
    assert replay.event_id is None
    assert replay.operation_name == "convert"
    # The record was not changed by a no-match reservation.
    assert store.get_record(record.record_id).state_version == 1


def _make_record(store: WorkflowStore) -> str:
    return store.create_record("lead-funnel", "captured", now=NOW, allowed_stages=STAGES).record_id


def test_admit_action_inserts_a_pending_row(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record_id = _make_record(store)
    admission = store.admit_action(
        record_id, kind="notify.local", dedupe_key="d1", request={"title": "t"}, now=NOW
    )
    assert admission.admitted is True
    assert admission.view.status == "pending"
    assert admission.view.kind == "notify.local"
    assert admission.view.request == {"title": "t"}


def test_settle_action_records_the_result(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record_id = _make_record(store)
    admission = store.admit_action(
        record_id, kind="notify.local", dedupe_key="d1", request={"title": "t"}, now=NOW
    )
    settled = store.settle_action(admission.view.action_id, result={"ok": True}, now=NOW)
    assert settled.status == "settled"
    assert settled.result == {"ok": True}


def test_admit_replays_a_settled_action(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record_id = _make_record(store)
    admission = store.admit_action(
        record_id, kind="notify.local", dedupe_key="d1", request={"title": "t"}, now=NOW
    )
    store.settle_action(admission.view.action_id, result={"ok": True}, now=NOW)
    replay = store.admit_action(
        record_id, kind="notify.local", dedupe_key="d1", request={"title": "t"}, now=NOW
    )
    assert replay.admitted is False
    assert replay.view.status == "settled"
    assert replay.view.result == {"ok": True}


def test_admit_pending_action_is_unresolved(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record_id = _make_record(store)
    store.admit_action(
        record_id, kind="notify.local", dedupe_key="d1", request={"title": "t"}, now=NOW
    )
    # A second admit while still pending (a prior unsettled dispatch) must not re-dispatch.
    with pytest.raises(UnresolvedAction):
        store.admit_action(
            record_id, kind="notify.local", dedupe_key="d1", request={"title": "t"}, now=NOW
        )


def test_admit_conflict_on_changed_kind_or_request(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record_id = _make_record(store)
    admission = store.admit_action(
        record_id, kind="notify.local", dedupe_key="d1", request={"title": "t"}, now=NOW
    )
    store.settle_action(admission.view.action_id, result={"ok": True}, now=NOW)
    with pytest.raises(ActionConflict):
        store.admit_action(
            record_id, kind="notify.local", dedupe_key="d1", request={"title": "other"}, now=NOW
        )


def test_fail_action_is_terminal(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record_id = _make_record(store)
    admission = store.admit_action(
        record_id, kind="mail.send", dedupe_key="d1", request={"to": "a"}, now=NOW
    )
    failed = store.fail_action(admission.view.action_id, error="boom", now=NOW)
    assert failed.status == "failed"
    assert failed.last_error == "boom"
    # A settled transition off a failed action is refused.
    with pytest.raises(UnknownRecord):
        store.settle_action(admission.view.action_id, result={"ok": True}, now=NOW)


def test_admit_action_unknown_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(UnknownRecord):
        store.admit_action(
            "11111111-1111-4111-8111-111111111111",
            kind="notify.local",
            dedupe_key="d1",
            request={"title": "t"},
            now=NOW,
        )


def test_admit_action_rejects_a_non_mapping_request(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record_id = _make_record(store)
    with pytest.raises(InvalidEffect):
        store.admit_action(
            record_id,
            kind="notify.local",
            dedupe_key="d1",
            request=None,
            now=NOW,  # type: ignore[arg-type]
        )


def test_admit_action_rejects_an_oversized_request(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record_id = _make_record(store)
    with pytest.raises(InvalidEffect):
        store.admit_action(
            record_id,
            kind="notify.local",
            dedupe_key="d1",
            request={"body": "x" * 20000},
            now=NOW,
        )


def test_list_actions_is_empty_for_a_fresh_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record_id = _make_record(store)
    assert store.list_actions(record_id) == []


def test_dedupe_key_is_scoped_per_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    a = _make_record(store)
    b = _make_record(store)
    first = store.admit_action(
        a, kind="notify.local", dedupe_key="shared", request={"title": "t"}, now=NOW
    )
    second = store.admit_action(
        b, kind="notify.local", dedupe_key="shared", request={"title": "t"}, now=NOW
    )
    # The same dedupe key on two records is two independent actions, not a replay.
    assert first.admitted is True
    assert second.admitted is True
    assert first.view.action_id != second.view.action_id
    assert second.view.record_id == b


def test_admit_action_rejects_a_non_finite_value(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record_id = _make_record(store)
    with pytest.raises(InvalidEffect):
        store.admit_action(
            record_id,
            kind="notify.local",
            dedupe_key="d1",
            request={"value": float("nan")},
            now=NOW,
        )


def test_admit_action_rejects_a_non_string_object_key(tmp_path: Path) -> None:
    # A non-string key would be silently coerced by json.dumps ({1: "a"} -> {"1": "a"}),
    # colliding the canonical dedupe identity with a genuine {"1": "a"} request. Reject it.
    store = _store(tmp_path)
    record_id = _make_record(store)
    with pytest.raises(InvalidEffect):
        store.admit_action(
            record_id,
            kind="notify.local",
            dedupe_key="d1",
            request={"meta": {1: "a"}},  # nested non-string key
            now=NOW,
        )


def test_apply_effects_admits_action_intents_atomically(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record_id = _make_record(store)
    outcome = store.apply_effects(
        record_id,
        [{"kind": "record.transition", "to_stage": "reviewing"}],
        operation_key="op-1",
        operation_name="review",
        request={},
        expected_version=1,
        now=NOW,
        allowed_stages=STAGES,
        actions=[{"kind": "notify.local", "request": {"title": "t", "body": "b"}}],
    )
    feed = store.list_actions(record_id)
    assert len(feed) == 1
    assert feed[0].status == "pending"
    assert feed[0].kind == "notify.local"
    assert feed[0].dedupe_key == f"pack.action:{outcome.event_id}:0"
    assert feed[0].request == {"title": "t", "body": "b"}


def test_apply_effects_replay_does_not_readmit_actions(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record_id = _make_record(store)
    args = {
        "operation_key": "op-1",
        "operation_name": "review",
        "request": {},
        "expected_version": 1,
        "now": NOW,
        "allowed_stages": STAGES,
        "actions": [{"kind": "notify.local", "request": {"title": "t", "body": "b"}}],
    }
    effects = [{"kind": "record.transition", "to_stage": "reviewing"}]
    store.apply_effects(record_id, effects, **args)
    # An idempotent replay (same key, effects, request) admits no second action row.
    replay = store.apply_effects(record_id, effects, **args)
    assert replay.applied is False
    assert len(store.list_actions(record_id)) == 1


def test_apply_effects_rejects_an_unsupported_action_kind(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record_id = _make_record(store)
    with pytest.raises(InvalidEffect):
        store.apply_effects(
            record_id,
            [{"kind": "record.transition", "to_stage": "reviewing"}],
            operation_key="op-1",
            operation_name="review",
            request={},
            expected_version=1,
            now=NOW,
            allowed_stages=STAGES,
            actions=[{"kind": "mail.blast", "request": {}}],
            allowed_action_kinds=frozenset({"notify.local"}),
        )
    # The record did not advance: the unsupported kind was rejected before the transaction.
    assert store.get_record(record_id).stage == "captured"


def test_apply_effects_rejects_a_non_sequence_actions_batch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record_id = _make_record(store)
    for bad in ({}, "", b""):
        with pytest.raises(InvalidEffect):
            store.apply_effects(
                record_id,
                [{"kind": "record.transition", "to_stage": "reviewing"}],
                operation_key="op-1",
                operation_name="review",
                request={},
                expected_version=1,
                now=NOW,
                allowed_stages=STAGES,
                actions=bad,  # malformed: falsy but not None -- must not be silently dropped
            )
    assert store.get_record(record_id).stage == "captured"


def test_apply_effects_rejects_an_unknown_action_intent_field(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record_id = _make_record(store)
    with pytest.raises(InvalidEffect):
        store.apply_effects(
            record_id,
            [{"kind": "record.transition", "to_stage": "reviewing"}],
            operation_key="op-1",
            operation_name="review",
            request={},
            expected_version=1,
            now=NOW,
            allowed_stages=STAGES,
            actions=[{"kind": "notify.local", "requests": {"title": "t"}}],  # typo'd field
        )
    assert store.get_record(record_id).stage == "captured"


def test_admit_action_rejects_the_reserved_dedupe_prefix(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record_id = _make_record(store)
    with pytest.raises(ValueError):
        store.admit_action(
            record_id,
            kind="notify.local",
            dedupe_key="pack.action:some-event:0",  # reserved for decision-emitted actions
            request={"title": "t", "body": "b"},
            now=NOW,
        )


def test_apply_effects_rejects_too_many_actions(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record_id = _make_record(store)
    with pytest.raises(InvalidEffect):
        store.apply_effects(
            record_id,
            [{"kind": "record.transition", "to_stage": "reviewing"}],
            operation_key="op-1",
            operation_name="review",
            request={},
            expected_version=1,
            now=NOW,
            allowed_stages=STAGES,
            actions=[{"kind": "notify.local", "request": {}} for _ in range(9)],
        )


def test_admit_action_rejects_a_circular_reference(tmp_path: Path) -> None:
    # A self-referential payload would recurse forever in the key walk; it must fail closed
    # as InvalidEffect (as json.dumps's own circular-reference check would), not RecursionError.
    store = _store(tmp_path)
    record_id = _make_record(store)
    request: dict[str, object] = {"title": "t"}
    request["self"] = request
    with pytest.raises(InvalidEffect):
        store.admit_action(
            record_id, kind="notify.local", dedupe_key="d1", request=request, now=NOW
        )


def test_list_actions_preserves_admission_order_on_timestamp_tie(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record_id = _make_record(store)
    for index in range(5):
        store.admit_action(
            record_id,
            kind="notify.local",
            dedupe_key=f"d{index}",
            request={"n": index},
            now=NOW,  # identical timestamp for every action
        )
    actions = store.list_actions(record_id)
    assert [action.dedupe_key for action in actions] == ["d0", "d1", "d2", "d3", "d4"]


def test_list_actions_orders_by_admission_not_timestamp_text(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record_id = _make_record(store)
    # A later admission whose created_at text sorts *earlier* than the first: a different
    # UTC offset (12:00+02:00 is the instant 10:00Z, admitted before 11:00Z), the same class
    # of misordering a backward clock jump would cause. Lexicographic created_at ordering
    # would reverse these two; admission order (rowid) must not.
    first = store.admit_action(
        record_id,
        kind="notify.local",
        dedupe_key="first",
        request={"n": 0},
        now=datetime(2026, 6, 1, 12, 0, tzinfo=timezone(timedelta(hours=2))),  # 10:00Z
    )
    second = store.admit_action(
        record_id,
        kind="notify.local",
        dedupe_key="second",
        request={"n": 1},
        now=datetime(2026, 6, 1, 11, 0, tzinfo=UTC),  # later instant, earlier-sorting text
    )
    assert first.view.created_at > second.view.created_at  # the stored text misorders them
    actions = store.list_actions(record_id)
    assert [action.dedupe_key for action in actions] == ["first", "second"]


def test_release_action_frees_a_pending_key(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record_id = _make_record(store)
    admission = store.admit_action(
        record_id, kind="mail.send", dedupe_key="d1", request={"to": "a"}, now=NOW
    )
    store.release_action(admission.view.action_id)
    # The dedupe key is free again after release.
    readmitted = store.admit_action(
        record_id, kind="mail.send", dedupe_key="d1", request={"to": "a"}, now=NOW
    )
    assert readmitted.admitted is True


def test_release_action_refuses_a_settled_action(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record_id = _make_record(store)
    admission = store.admit_action(
        record_id, kind="notify.local", dedupe_key="d1", request={"title": "t"}, now=NOW
    )
    store.settle_action(admission.view.action_id, result={"ok": True}, now=NOW)
    with pytest.raises(UnknownRecord):
        store.release_action(admission.view.action_id)


def test_list_pending_actions_returns_only_pending_in_admission_order(tmp_path: Path) -> None:
    store = _store(tmp_path)
    r1 = _make_record(store)
    r2 = _make_record(store)
    first = store.admit_action(
        r1, kind="notify.local", dedupe_key="a", request={"title": "1"}, now=NOW
    )
    store.admit_action(r2, kind="notify.local", dedupe_key="b", request={"title": "2"}, now=NOW)
    store.admit_action(r1, kind="notify.local", dedupe_key="c", request={"title": "3"}, now=NOW)
    # Terminalize the first (settled): it must not appear; the other two stay pending and are
    # returned in rowid admission order, interleaved across records rather than grouped by one.
    store.settle_action(first.view.action_id, result={"ok": True}, now=NOW)
    pending = store.list_pending_actions()
    assert [view.request for view in pending] == [{"title": "2"}, {"title": "3"}]


def test_list_pending_actions_is_empty_when_nothing_is_pending(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record_id = _make_record(store)
    admission = store.admit_action(
        record_id, kind="notify.local", dedupe_key="a", request={"title": "1"}, now=NOW
    )
    store.settle_action(admission.view.action_id, result={"ok": True}, now=NOW)
    assert store.list_pending_actions() == []


def test_create_record_persists_and_returns_the_pack_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_record(
        "lead-funnel", "captured", now=NOW, allowed_stages=STAGES, pack_id="pack-a", pack_version=7
    )
    assert record.pack_id == "pack-a"
    assert record.pack_version == 7
    persisted = store.get_record(record.record_id)
    assert persisted.pack_id == "pack-a"
    assert persisted.pack_version == 7


def test_create_record_pack_id_defaults_to_none(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.create_record("lead-funnel", "captured", now=NOW, allowed_stages=STAGES)
    assert record.pack_id is None
    assert record.pack_version is None
    persisted = store.get_record(record.record_id)
    assert persisted.pack_id is None
    assert persisted.pack_version is None


def test_create_record_rejects_pack_id_and_version_given_apart(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # A version without an identity, or an identity without a version, is meaningless.
    with pytest.raises(ValueError):
        store.create_record(
            "lead-funnel", "captured", now=NOW, allowed_stages=STAGES, pack_id="pack-a"
        )
    with pytest.raises(ValueError):
        store.create_record(
            "lead-funnel", "captured", now=NOW, allowed_stages=STAGES, pack_version=1
        )


def test_create_record_rejects_a_non_positive_pack_version(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.create_record(
            "lead-funnel", "captured", now=NOW, allowed_stages=STAGES, pack_id="pack-a",
            pack_version=0,
        )


def test_create_record_rejects_an_empty_pack_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.create_record(
            "lead-funnel", "captured", now=NOW, allowed_stages=STAGES, pack_id=""
        )


def test_fail_action_persists_surrogate_error_text_without_raising(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record_id = _make_record(store)
    admission = store.admit_action(
        record_id, kind="notify.local", dedupe_key="d1", request={"title": "t"}, now=NOW
    )
    # str(exc) can carry lone surrogates (e.g. a message built from bytes decoded with
    # errors="surrogateescape"). fail_action must still terminalize the row, not raise and
    # strand it pending.
    failed = store.fail_action(
        admission.view.action_id, error="boom \udce9 tail", now=NOW
    )
    assert failed.status == "failed"
    assert store.get_action(admission.view.action_id).status == "failed"
    # The stored text is UTF-8-safe (the surrogate became U+FFFD) and still readable.
    assert failed.last_error is not None
    assert "\udce9" not in failed.last_error
    assert failed.last_error.startswith("boom ")


def test_fail_action_truncates_long_error_text(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record_id = _make_record(store)
    admission = store.admit_action(
        record_id, kind="notify.local", dedupe_key="d1", request={"title": "t"}, now=NOW
    )
    failed = store.fail_action(admission.view.action_id, error="x" * 900, now=NOW)
    assert failed.last_error is not None
    assert len(failed.last_error) == 500


def test_fail_action_bounds_a_long_surrogate_laden_error(tmp_path: Path) -> None:
    # A large error carrying surrogates: truncation happens before the UTF-8 round trip (so a
    # pathologically large message is bounded before transcoding), and the surrogates in the
    # kept prefix are still replaced. The result stays within the 500 cap and does not raise.
    store = _store(tmp_path)
    record_id = _make_record(store)
    admission = store.admit_action(
        record_id, kind="notify.local", dedupe_key="d1", request={"title": "t"}, now=NOW
    )
    failed = store.fail_action(admission.view.action_id, error="\udce9" * 900, now=NOW)
    assert failed.status == "failed"
    assert failed.last_error is not None
    assert len(failed.last_error) == 500
    assert "\udce9" not in failed.last_error
