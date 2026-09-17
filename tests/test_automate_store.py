from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eom_email_watcher.automate.store import (
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
