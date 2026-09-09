import base64
import hashlib
import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eom_email_watcher import db as db_module
from eom_email_watcher import locking
from eom_email_watcher.config import MAX_RETENTION_DAYS
from eom_email_watcher.db import (
    CONNECT_QUEUE_ADMISSION_WINDOW,
    CONNECT_QUEUE_MAX_JOBS,
    MAX_CONNECT_REQUEST_BYTES,
    SCHEDULING_AUTOMATION_ID,
    SCHEDULING_AUTOMATION_VERSION,
    SCHEDULING_EXTRACTION_SCHEMA_VERSION,
    SCHEMA_VERSION,
    AutomationSourceChanged,
    CalendarEventMutation,
    CalendarEventProjection,
    ConnectQueueFull,
    Store,
)
from eom_email_watcher.mailbox import scoped_message_id
from eom_email_watcher.microsoft_calendar import MicrosoftPrincipal
from eom_email_watcher.mime import AttachmentDescriptor

CALENDAR_PRINCIPAL_KEY = "a" * 64
DELETE_MESSAGE_PROBE = """
import sys
from pathlib import Path

from eom_email_watcher.db import Store

print("waiting", flush=True)
deleted = Store(Path(sys.argv[1])).delete_message(sys.argv[2])
print("deleted" if deleted else "missing", flush=True)
"""


def admitted_scheduling_run(
    store: Store,
    *,
    message_id: str = "local-scheduling",
    provider_message_id: str = "provider-scheduling",
    principal_key: str = CALENDAR_PRINCIPAL_KEY,
):
    account_id = f"microsoft365-{'a' * 32}"
    if store.mail_account("microsoft365", account_id) is None:
        store.register_mail_account(
            "microsoft365",
            account_id,
            display_name="Microsoft 365",
            address="owner@example.com",
            active=True,
        )
    assert store.add_message(
        message_id=message_id,
        provider="microsoft365",
        account_id=account_id,
        provider_message_id=provider_message_id,
        thread_id=None,
        sender="sender@example.com",
        sender_name="Sender",
        subject="Can we meet?",
        received_at="2026-09-07T12:00:00+00:00",
    )
    store.mark_analyzed(
        message_id,
        scheduling_analysis(),
        scheduling_automation_principal_key=principal_key,
        now=datetime(2026, 9, 7, 12, 1, tzinfo=UTC),
    )
    run = store.automation_run_for_message(message_id)
    assert run is not None
    return run


def scheduling_analysis() -> dict[str, object]:
    return {
        "category": "scheduling",
        "priority": "normal",
        "summary": "A meeting was requested.",
        "action_required": True,
        "suggested_action": "Review the requested meeting.",
        "deadline_text": None,
        "deadline_iso": None,
        "confidence": 0.9,
    }


def proposing_scheduling_run(
    store: Store,
    *,
    message_id: str = "local-scheduling",
    provider_message_id: str = "provider-scheduling",
    principal_key: str = CALENDAR_PRINCIPAL_KEY,
):
    detected = admitted_scheduling_run(
        store,
        message_id=message_id,
        provider_message_id=provider_message_id,
        principal_key=principal_key,
    )
    payload = store.reserve_automation_extraction(
        detected.run_id,
        detected.state_version,
        source_content_sha256="b" * 64,
        context_at="2026-09-07T08:00:00-05:00",
        timezone="America/Chicago",
        body_char_limit=20_000,
        organizer_address="owner@example.com",
    )
    extracting = store.automation_run(detected.run_id)
    assert extracting is not None
    result_json = json.dumps(
        {
            "intent": "new_meeting",
            "proposed_times": [
                {
                    "start": "2026-09-08T10:00:00-05:00",
                    "end": "2026-09-08T10:30:00-05:00",
                    "timezone": "America/Chicago",
                }
            ],
            "attendees": [{"email": "jane@example.com"}],
        },
        separators=(",", ":"),
    ).encode()
    proposing = store.record_automation_extraction(
        detected.run_id,
        extracting.state_version,
        payload_id=payload.payload_id,
        result_sha256=hashlib.sha256(result_json).hexdigest(),
        result_json=result_json,
        violations=[],
        accepted_state="proposing",
    )
    return proposing, payload


def test_proposable_automation_runs_page_after_stable_cursor(tmp_path: Path) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    proposing_scheduling_run(
        store,
        message_id="local-scheduling-1",
        provider_message_id="provider-scheduling-1",
    )
    proposing_scheduling_run(
        store,
        message_id="local-scheduling-2",
        provider_message_id="provider-scheduling-2",
    )

    ordered = store.proposable_automation_runs(2)
    assert len(ordered) == 2
    first = ordered[0].run

    remaining = store.proposable_automation_runs(
        2,
        after=(first.created_at, first.run_id),
    )
    assert [item.run.run_id for item in remaining] == [ordered[1].run.run_id]


def test_cursor_dedup_and_summary_lifecycle(tmp_path: Path) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    store.set_state("100", datetime(2026, 7, 18, tzinfo=UTC))
    assert store.state() == ("100", "2026-07-18T00:00:00+00:00")
    values = dict(
        message_id="m1",
        thread_id="t1",
        sender="trusted@example.com",
        sender_name="Trusted",
        subject="Invoice",
        received_at="2026-07-18T14:00:00+00:00",
    )
    assert store.add_message(**values)
    assert not store.add_message(**values)
    assert [item.message_id for item in store.pending()] == ["m1"]
    store.mark_analyzed(
        "m1",
        {
            "category": "invoice",
            "priority": "normal",
            "summary": "Invoice received.",
            "action_required": True,
            "suggested_action": "Review it.",
            "deadline_text": None,
            "deadline_iso": None,
            "confidence": 0.9,
        },
    )
    assert store.pending() == []
    assert store.pending_delivery()[0].summary == "Invoice received."
    store.mark_delivery_complete("m1", notified=True)
    assert store.pending_delivery() == []
    recent = store.recent(1)[0]
    assert recent["message_id"] == "m1"
    assert recent["summary"] == "Invoice received."
    assert recent["attachments"] == []
    assert "deadline_text" in recent
    assert recent["notified_at"] is not None


def test_scheduling_analysis_atomically_admits_one_durable_run(tmp_path: Path) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    assert store.add_message(
        message_id="local-1",
        provider="microsoft365",
        account_id="account-1",
        provider_message_id="provider-message-1",
        thread_id=None,
        sender="trusted@example.com",
        sender_name="Trusted",
        subject="Can we meet?",
        received_at="2026-09-07T12:00:00+00:00",
    )

    committed_at = datetime(2026, 9, 7, 12, 1, tzinfo=UTC)
    store.mark_analyzed(
        "local-1",
        scheduling_analysis(),
        scheduling_automation_principal_key=CALENDAR_PRINCIPAL_KEY,
        now=committed_at,
    )

    run = store.automation_run_for_message("local-1")
    assert run is not None
    assert (
        run.source_message_key
        == hashlib.sha256(b"microsoft365\0account-1\0provider-message-1").hexdigest()
    )
    assert (run.automation_id, run.automation_version, run.extraction_schema_version) == (
        SCHEDULING_AUTOMATION_ID,
        SCHEDULING_AUTOMATION_VERSION,
        SCHEDULING_EXTRACTION_SCHEMA_VERSION,
    )
    assert (run.state, run.state_version, run.created_at, run.updated_at) == (
        "detected",
        1,
        committed_at.isoformat(),
        committed_at.isoformat(),
    )
    assert run.calendar_principal_key == CALENDAR_PRINCIPAL_KEY
    events = store.automation_events(run.run_id)
    assert [event.calendar_principal_key for event in events] == [CALENDAR_PRINCIPAL_KEY]
    assert [
        (
            event.sequence_no,
            event.previous_state,
            event.next_state,
            event.state_version,
            event.transition_kind,
        )
        for event in events
    ] == [(0, None, "detected", 1, "detected")]
    with store.connection() as db, pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """INSERT INTO automation_runs(
                    run_id, provider, account_id, calendar_principal_key,
                    source_message_key, automation_id, automation_version,
                    extraction_schema_version, state, state_version, failure_code,
                    expires_at, created_at, updated_at
                )
                SELECT '11111111-1111-4111-8111-111111111111', provider,
                    account_id, calendar_principal_key, source_message_key, automation_id,
                    automation_version, extraction_schema_version, state,
                    state_version, failure_code, expires_at, created_at, updated_at
                FROM automation_runs WHERE run_id = ?""",
            (run.run_id,),
        )


def test_scheduling_admission_rolls_back_analysis_when_event_append_fails(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    assert store.add_message(
        message_id="m1",
        thread_id=None,
        sender="trusted@example.com",
        sender_name=None,
        subject="Meeting",
        received_at="2026-09-07T12:00:00+00:00",
    )
    with store.connection() as db:
        db.execute(
            """CREATE TRIGGER reject_detected_event
            BEFORE INSERT ON automation_events
            BEGIN
                SELECT RAISE(ABORT, 'injected event failure');
            END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected event failure"):
        store.mark_analyzed(
            "m1",
            scheduling_analysis(),
            scheduling_automation_principal_key=CALENDAR_PRINCIPAL_KEY,
        )

    assert [message.message_id for message in store.pending()] == ["m1"]
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM automation_runs").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM automation_events").fetchone()[0] == 0


def test_scheduling_extraction_reservation_and_retry_budget_survive_restart(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    detected = admitted_scheduling_run(store)
    work = store.recoverable_automation_runs()
    assert len(work) == 1
    assert work[0].organizer_address == "owner@example.com"

    first = store.reserve_automation_extraction(
        detected.run_id,
        detected.state_version,
        source_content_sha256="b" * 64,
        context_at="2026-09-07T08:00:00-05:00",
        timezone="America/Chicago",
        body_char_limit=20_000,
        organizer_address="owner@example.com",
    )
    extracting = store.automation_run(detected.run_id)
    assert extracting is not None
    assert (extracting.state, extracting.state_version) == ("extracting", 2)
    repeated = store.reserve_automation_extraction(
        detected.run_id,
        extracting.state_version,
        source_content_sha256="b" * 64,
        context_at="2026-09-07T08:00:00-05:00",
        timezone="America/Chicago",
        body_char_limit=20_000,
        organizer_address="owner@example.com",
    )
    assert repeated == first

    rejected_result = b'{"intent":"new_meeting"}'
    rejected_once = store.record_automation_extraction(
        detected.run_id,
        extracting.state_version,
        payload_id=first.payload_id,
        result_sha256=hashlib.sha256(rejected_result).hexdigest(),
        result_json=rejected_result,
        violations=[{"code": "schema_missing_field", "path": "proposed_times"}],
    )
    assert (rejected_once.state, rejected_once.state_version, rejected_once.failure_code) == (
        "extracting",
        3,
        "validation_rejected_once",
    )
    store = Store(store.path)
    store.initialize()
    second = store.reserve_automation_extraction(
        detected.run_id,
        rejected_once.state_version,
        source_content_sha256="b" * 64,
        context_at="2026-09-07T08:00:00-05:00",
        timezone="America/Chicago",
        body_char_limit=20_000,
        organizer_address="owner@example.com",
    )
    assert second.attempt_no == 2
    second_reserved = store.automation_run(detected.run_id)
    assert second_reserved is not None
    final = store.record_automation_extraction(
        detected.run_id,
        second_reserved.state_version,
        payload_id=second.payload_id,
        result_sha256=hashlib.sha256(rejected_result).hexdigest(),
        result_json=rejected_result,
        violations=[{"code": "evidence_not_found", "path": "intent_evidence"}],
    )
    assert (final.state, final.failure_code) == ("manual_review", "validation_rejected")
    with pytest.raises(RuntimeError, match="expected-state"):
        store.reserve_automation_extraction(
            detected.run_id,
            final.state_version,
            source_content_sha256="b" * 64,
            context_at="2026-09-07T08:00:00-05:00",
            timezone="America/Chicago",
            body_char_limit=20_000,
            organizer_address="owner@example.com",
        )
    assert [item.attempt_no for item in store.automation_extraction_payloads(detected.run_id)] == [
        1,
        2,
    ]
    assert [event.next_state for event in store.automation_events(detected.run_id)] == [
        "detected",
        "extracting",
        "extracting",
        "extracting",
        "manual_review",
    ]


def test_extraction_transport_failure_preserves_request_and_retry_schedule(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    detected = admitted_scheduling_run(store)
    payload = store.reserve_automation_extraction(
        detected.run_id,
        detected.state_version,
        source_content_sha256="b" * 64,
        context_at="2026-09-07T08:00:00-05:00",
        timezone="America/Chicago",
        body_char_limit=20_000,
        organizer_address="owner@example.com",
    )
    extracting = store.automation_run(detected.run_id)
    assert extracting is not None
    failed_at = datetime(2026, 9, 7, 13, tzinfo=UTC)

    retrying = store.record_automation_extraction_failure(
        detected.run_id,
        extracting.state_version,
        payload_id=payload.payload_id,
        error_code="worker_unavailable",
        retryable=True,
        retry_after_seconds=30,
        now=failed_at,
    )

    assert retrying.state == "extracting"
    assert store.recoverable_automation_runs(now=failed_at + timedelta(seconds=29)) == []
    due = store.recoverable_automation_runs(now=failed_at + timedelta(seconds=30))
    assert [item.run.run_id for item in due] == [detected.run_id]
    repeated = store.reserve_automation_extraction(
        detected.run_id,
        retrying.state_version,
        source_content_sha256="b" * 64,
        context_at="2026-09-07T08:00:00-05:00",
        timezone="America/Chicago",
        body_char_limit=20_000,
        organizer_address="owner@example.com",
    )
    assert repeated.request_id == payload.request_id
    persisted = store.automation_extraction_payloads(detected.run_id)[0]
    assert persisted.failure_count == 1
    assert persisted.last_error_code == "worker_unavailable"
    assert persisted.next_retry_at == "2026-09-07T13:00:30+00:00"


def test_schema_14_payload_table_migrates_retry_and_organizer_fields(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    with store.connection() as db:
        for column in (
            "organizer_address",
            "failure_count",
            "next_retry_at",
            "last_error_code",
        ):
            db.execute(f"ALTER TABLE automation_extraction_payloads DROP COLUMN {column}")
        db.execute("PRAGMA user_version = 14")

    store.initialize()

    with store.connection() as db:
        columns = {
            str(row["name"]): str(row["type"])
            for row in db.execute("PRAGMA table_info(automation_extraction_payloads)").fetchall()
        }
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert {
        "organizer_address",
        "failure_count",
        "next_retry_at",
        "last_error_code",
    } <= columns.keys()


@pytest.mark.parametrize(
    ("next_state", "failure_code"),
    [
        ("proposing", None),
        ("manual_review", "reschedule_not_supported"),
        ("manual_review", "cancellation_not_supported"),
        ("ambiguous", "ambiguous_extraction"),
    ],
)
def test_valid_extraction_atomically_records_payload_and_outcome(
    tmp_path: Path,
    next_state: str,
    failure_code: str | None,
) -> None:
    store = Store(tmp_path / next_state / "watcher.sqlite3")
    store.initialize()
    detected = admitted_scheduling_run(store)
    payload = store.reserve_automation_extraction(
        detected.run_id,
        detected.state_version,
        source_content_sha256="b" * 64,
        context_at="2026-09-07T08:00:00-05:00",
        timezone="America/Chicago",
        body_char_limit=20_000,
        organizer_address="owner@example.com",
    )
    extracting = store.automation_run(detected.run_id)
    assert extracting is not None

    result_json = b'{"intent":"new_meeting"}'
    result_sha256 = hashlib.sha256(result_json).hexdigest()
    result = store.record_automation_extraction(
        detected.run_id,
        extracting.state_version,
        payload_id=payload.payload_id,
        result_sha256=result_sha256,
        result_json=result_json,
        violations=[],
        accepted_state=next_state,
        accepted_code=failure_code,
    )

    assert (result.state, result.failure_code, result.current_payload_sha256) == (
        next_state,
        failure_code,
        result_sha256,
    )
    persisted = store.automation_extraction_payloads(detected.run_id)
    assert len(persisted) == 1
    assert persisted[0].status == "accepted"
    assert persisted[0].violations_json == b"[]"
    event = store.automation_events(detected.run_id)[-1]
    assert (event.next_state, event.payload_id, event.payload_sha256) == (
        next_state,
        payload.payload_id,
        result_sha256,
    )


def test_extraction_payload_hash_mismatch_cannot_enter_the_immutable_ledger(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    detected = admitted_scheduling_run(store)
    payload = store.reserve_automation_extraction(
        detected.run_id,
        detected.state_version,
        source_content_sha256="b" * 64,
        context_at="2026-09-07T08:00:00-05:00",
        timezone="America/Chicago",
        body_char_limit=20_000,
        organizer_address="owner@example.com",
    )
    extracting = store.automation_run(detected.run_id)
    assert extracting is not None

    with pytest.raises(ValueError, match="result is not bounded"):
        store.record_automation_extraction(
            detected.run_id,
            extracting.state_version,
            payload_id=payload.payload_id,
            result_sha256="c" * 64,
            result_json=b"{}",
            violations=[{"code": "schema_missing_field", "path": "intent"}],
        )

    unchanged = store.automation_run(detected.run_id)
    assert unchanged == extracting
    assert store.automation_extraction_payloads(detected.run_id)[0].status == "reserved"
    assert [event.next_state for event in store.automation_events(detected.run_id)] == [
        "detected",
        "extracting",
    ]


def test_changed_source_fails_closed_without_reserving_another_attempt(tmp_path: Path) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    detected = admitted_scheduling_run(store)
    first = store.reserve_automation_extraction(
        detected.run_id,
        detected.state_version,
        source_content_sha256="b" * 64,
        context_at="2026-09-07T08:00:00-05:00",
        timezone="America/Chicago",
        body_char_limit=20_000,
        organizer_address="owner@example.com",
    )
    extracting = store.automation_run(detected.run_id)
    assert extracting is not None
    result_json = b"{}"
    store.record_automation_extraction(
        detected.run_id,
        extracting.state_version,
        payload_id=first.payload_id,
        result_sha256=hashlib.sha256(result_json).hexdigest(),
        result_json=result_json,
        violations=[{"code": "schema_missing_field", "path": "intent"}],
    )
    rejected = store.automation_run(detected.run_id)
    assert rejected is not None

    with pytest.raises(AutomationSourceChanged):
        store.reserve_automation_extraction(
            detected.run_id,
            rejected.state_version,
            source_content_sha256="e" * 64,
            context_at="2026-09-07T08:00:00-05:00",
            timezone="America/Chicago",
            body_char_limit=20_000,
            organizer_address="owner@example.com",
        )

    assert len(store.automation_extraction_payloads(detected.run_id)) == 1


def test_source_cleanup_deletes_extraction_payload_and_transitions_active_run(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    detected = admitted_scheduling_run(store)
    store.reserve_automation_extraction(
        detected.run_id,
        detected.state_version,
        source_content_sha256="b" * 64,
        context_at="2026-09-07T08:00:00-05:00",
        timezone="America/Chicago",
        body_char_limit=20_000,
        organizer_address="owner@example.com",
    )

    assert store.delete_message("local-scheduling") is True

    tombstone = store.automation_run(detected.run_id)
    assert tombstone is not None
    assert (tombstone.state, tombstone.failure_code) == (
        "source_unavailable",
        "source_unavailable",
    )
    assert store.automation_extraction_payloads(detected.run_id) == []


def test_calendar_proposal_is_durable_and_atomically_awaits_confirmation(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    proposing, extraction = proposing_scheduling_run(store)
    observed_at = datetime(2026, 9, 7, 13, tzinfo=UTC)

    awaiting = store.record_automation_proposal(
        proposing.run_id,
        proposing.state_version,
        extraction_payload_id=extraction.payload_id,
        request_sha256="c" * 64,
        subject="Meeting request",
        attendees=("jane@example.com",),
        start="2026-09-08T10:00:00-05:00",
        end="2026-09-08T10:30:00-05:00",
        timezone="America/Chicago",
        suggestion_reason="All attendees are available.",
        empty_reason=None,
        observed_at=observed_at,
    )

    assert (awaiting.state, awaiting.failure_code) == ("awaiting_confirmation", None)
    store = Store(store.path)
    store.initialize()
    proposal = store.automation_proposal(proposing.run_id)
    assert proposal is not None
    assert proposal.status == "accepted"
    assert proposal.attendees == ("jane@example.com",)
    assert proposal.request_sha256 == "c" * 64
    assert len(proposal.proposal_sha256) == 64
    assert proposal.expires_at == "2026-09-07T13:15:00+00:00"
    assert store.automation_proposal_for_message("local-scheduling") == proposal
    preview = store.recent(1)[0]["calendar_proposal"]
    assert preview == {
        "run_id": proposing.run_id,
        "state": "awaiting_confirmation",
        "state_version": awaiting.state_version,
        "proposal_version": 1,
        "proposal_sha256": proposal.proposal_sha256,
        "status": "accepted",
        "provider": "microsoft365",
        "account_id": f"microsoft365-{'a' * 32}",
        "account_display_name": "Microsoft 365",
        "account_address": "owner@example.com",
        "subject": "Meeting request",
        "attendees": ["jane@example.com"],
        "start": "2026-09-08T10:00:00-05:00",
        "end": "2026-09-08T10:30:00-05:00",
        "timezone": "America/Chicago",
        "suggestion_reason": "All attendees are available.",
        "empty_reason": None,
        "observed_at": "2026-09-07T13:00:00+00:00",
        "expires_at": "2026-09-07T13:15:00+00:00",
        "write_status": None,
        "graph_event_id": None,
    }
    assert "principal" not in preview
    event = store.automation_events(proposing.run_id)[-1]
    assert (event.next_state, event.payload_id, event.payload_sha256) == (
        "awaiting_confirmation",
        proposal.payload_id,
        proposal.proposal_sha256,
    )


def test_no_calendar_suggestion_becomes_durable_review_outcome(tmp_path: Path) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    proposing, extraction = proposing_scheduling_run(store)

    reviewed = store.record_automation_proposal(
        proposing.run_id,
        proposing.state_version,
        extraction_payload_id=extraction.payload_id,
        request_sha256="d" * 64,
        subject="Meeting request",
        attendees=("jane@example.com",),
        start=None,
        end=None,
        timezone=None,
        suggestion_reason=None,
        empty_reason="No times satisfy every attendee.",
        observed_at=datetime(2026, 9, 7, 13, tzinfo=UTC),
    )

    assert (reviewed.state, reviewed.failure_code) == (
        "manual_review",
        "proposal_no_suggestions",
    )
    proposal = store.automation_proposal(proposing.run_id)
    assert proposal is not None
    assert proposal.status == "no_suggestions"
    assert proposal.empty_reason == "No times satisfy every attendee."
    preview = store.recent(1)[0]["calendar_proposal"]
    assert preview is not None
    assert preview["state"] == "manual_review"
    assert preview["status"] == "no_suggestions"
    assert preview["empty_reason"] == "No times satisfy every attendee."
    assert preview["start"] is None
    assert preview["expires_at"] is None
    assert any(
        intent.kind == "automation_review" and intent.subject_id == proposing.run_id
        for intent in store.notification_intents()
    )


def test_calendar_proposal_compare_and_swap_prevents_duplicate_payloads(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    proposing, extraction = proposing_scheduling_run(store)
    arguments = {
        "extraction_payload_id": extraction.payload_id,
        "request_sha256": "e" * 64,
        "subject": "Meeting request",
        "attendees": ("jane@example.com",),
        "start": "2026-09-08T10:00:00-05:00",
        "end": "2026-09-08T10:30:00-05:00",
        "timezone": "America/Chicago",
        "suggestion_reason": "All attendees are available.",
        "empty_reason": None,
        "observed_at": datetime(2026, 9, 7, 13, tzinfo=UTC),
    }

    store.record_automation_proposal(
        proposing.run_id,
        proposing.state_version,
        **arguments,
    )
    with pytest.raises(RuntimeError, match="expected-state race"):
        store.record_automation_proposal(
            proposing.run_id,
            proposing.state_version,
            **arguments,
        )

    with store.connection() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM automation_proposal_payloads WHERE run_id = ?",
            (proposing.run_id,),
        ).fetchone()[0] == 1


def test_calendar_proposal_and_state_transition_roll_back_together(tmp_path: Path) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    proposing, extraction = proposing_scheduling_run(store)
    with store.connection() as db:
        db.execute(
            """CREATE TRIGGER reject_calendar_proposal_event
            BEFORE INSERT ON automation_events
            WHEN NEW.next_state = 'awaiting_confirmation'
            BEGIN
                SELECT RAISE(ABORT, 'injected proposal event failure');
            END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected proposal event failure"):
        store.record_automation_proposal(
            proposing.run_id,
            proposing.state_version,
            extraction_payload_id=extraction.payload_id,
            request_sha256="a" * 64,
            subject="Meeting request",
            attendees=("jane@example.com",),
            start="2026-09-08T10:00:00-05:00",
            end="2026-09-08T10:30:00-05:00",
            timezone="America/Chicago",
            suggestion_reason="All attendees are available.",
            empty_reason=None,
            observed_at=datetime(2026, 9, 7, 13, tzinfo=UTC),
        )

    assert store.automation_run(proposing.run_id) == proposing
    assert store.automation_proposal(proposing.run_id) is None


@pytest.mark.parametrize(
    "override",
    [
        {"request_sha256": False},
        {"request_sha256": "g" * 64},
        {"attendees": ("jane@example.com", "JANE@example.com")},
        {"attendees": tuple(f"person-{index}@example.com" for index in range(65))},
        {"suggestion_reason": "x" * 513},
        {"suggestion_reason": ""},
        {"start": "2026-09-08T10:00:00+00:00"},
        {"observed_at": datetime(2026, 9, 7, 13)},
    ],
)
def test_calendar_proposal_persistence_rejects_unbounded_or_ambiguous_input(
    tmp_path: Path,
    override: dict[str, object],
) -> None:
    store = Store(tmp_path / hashlib.sha256(repr(override).encode()).hexdigest() / "watcher.db")
    store.initialize()
    proposing, extraction = proposing_scheduling_run(store)
    arguments: dict[str, object] = {
        "extraction_payload_id": extraction.payload_id,
        "request_sha256": "a" * 64,
        "subject": "Meeting request",
        "attendees": ("jane@example.com",),
        "start": "2026-09-08T10:00:00-05:00",
        "end": "2026-09-08T10:30:00-05:00",
        "timezone": "America/Chicago",
        "suggestion_reason": "All attendees are available.",
        "empty_reason": None,
        "observed_at": datetime(2026, 9, 7, 13, tzinfo=UTC),
    }
    arguments.update(override)

    with pytest.raises(ValueError):
        store.record_automation_proposal(
            proposing.run_id,
            proposing.state_version,
            **arguments,
        )

    assert store.automation_run(proposing.run_id) == proposing
    assert store.automation_proposal(proposing.run_id) is None


def test_calendar_proposal_persistence_accepts_maximum_attendee_count(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    proposing, extraction = proposing_scheduling_run(store)
    attendees = tuple(f"person-{index}@example.com" for index in range(64))

    reviewed = store.record_automation_proposal(
        proposing.run_id,
        proposing.state_version,
        extraction_payload_id=extraction.payload_id,
        request_sha256="a" * 64,
        subject="Meeting request",
        attendees=attendees,
        start=None,
        end=None,
        timezone=None,
        suggestion_reason=None,
        empty_reason="No times satisfy every attendee.",
        observed_at=datetime(2026, 9, 7, 13, tzinfo=UTC),
    )

    assert reviewed.state == "manual_review"
    proposal = store.automation_proposal(proposing.run_id)
    assert proposal is not None and proposal.attendees == attendees


def test_source_cleanup_deletes_calendar_proposal_and_tombstones_review(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    proposing, extraction = proposing_scheduling_run(store)
    store.record_automation_proposal(
        proposing.run_id,
        proposing.state_version,
        extraction_payload_id=extraction.payload_id,
        request_sha256="f" * 64,
        subject="Meeting request",
        attendees=("jane@example.com",),
        start="2026-09-08T10:00:00-05:00",
        end="2026-09-08T10:30:00-05:00",
        timezone="America/Chicago",
        suggestion_reason="All attendees are available.",
        empty_reason=None,
        observed_at=datetime(2026, 9, 7, 13, tzinfo=UTC),
    )

    assert store.delete_message("local-scheduling") is True

    tombstone = store.automation_run(proposing.run_id)
    assert tombstone is not None
    assert (tombstone.state, tombstone.failure_code) == (
        "source_unavailable",
        "source_unavailable",
    )
    assert store.automation_proposal(proposing.run_id) is None


def test_calendar_proposal_decline_is_atomic_and_idempotent(tmp_path: Path) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    proposing, extraction = proposing_scheduling_run(store)
    awaiting = store.record_automation_proposal(
        proposing.run_id,
        proposing.state_version,
        extraction_payload_id=extraction.payload_id,
        request_sha256="a" * 64,
        subject="Meeting request",
        attendees=("jane@example.com",),
        start="2026-09-08T10:00:00-05:00",
        end="2026-09-08T10:30:00-05:00",
        timezone="America/Chicago",
        suggestion_reason="All attendees are available.",
        empty_reason=None,
        observed_at=datetime(2026, 9, 7, 13, tzinfo=UTC),
    )
    proposal = store.automation_proposal(proposing.run_id)
    assert proposal is not None

    declined = store.decide_automation_proposal(
        proposing.run_id,
        awaiting.state_version,
        proposal_version=proposal.proposal_version,
        proposal_sha256=proposal.proposal_sha256,
        decision="decline",
        now=datetime(2026, 9, 7, 13, 5, tzinfo=UTC),
    )
    repeated = store.decide_automation_proposal(
        proposing.run_id,
        awaiting.state_version,
        proposal_version=proposal.proposal_version,
        proposal_sha256=proposal.proposal_sha256,
        decision="decline",
        now=datetime(2026, 9, 7, 13, 6, tzinfo=UTC),
    )

    assert declined.state == repeated.state == "declined"
    assert store.automation_calendar_write(proposing.run_id) is None
    events = store.automation_events(proposing.run_id)
    assert [event.decision for event in events].count("declined") == 1


def test_calendar_confirmation_persists_one_transaction_before_write(tmp_path: Path) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    proposing, extraction = proposing_scheduling_run(store)
    awaiting = store.record_automation_proposal(
        proposing.run_id,
        proposing.state_version,
        extraction_payload_id=extraction.payload_id,
        request_sha256="b" * 64,
        subject="Meeting request",
        attendees=("jane@example.com",),
        start="2026-09-08T10:00:00-05:00",
        end="2026-09-08T10:30:00-05:00",
        timezone="America/Chicago",
        suggestion_reason="All attendees are available.",
        empty_reason=None,
        observed_at=datetime(2026, 9, 7, 13, tzinfo=UTC),
    )
    proposal = store.automation_proposal(proposing.run_id)
    assert proposal is not None

    authorized = store.decide_automation_proposal(
        proposing.run_id,
        awaiting.state_version,
        proposal_version=proposal.proposal_version,
        proposal_sha256=proposal.proposal_sha256,
        decision="confirm",
        now=datetime(2026, 9, 7, 13, 5, tzinfo=UTC),
    )
    write = store.automation_calendar_write(proposing.run_id)
    repeated = store.decide_automation_proposal(
        proposing.run_id,
        awaiting.state_version,
        proposal_version=proposal.proposal_version,
        proposal_sha256=proposal.proposal_sha256,
        decision="confirm",
        now=datetime(2026, 9, 7, 13, 6, tzinfo=UTC),
    )

    assert authorized.state == repeated.state == "write_authorized"
    assert write is not None and write.status == "authorized"
    assert len(write.transaction_id) == 36
    events = store.automation_events(proposing.run_id)
    confirmation = events[-1]
    assert (confirmation.decision, confirmation.transaction_id) == (
        "confirmed",
        write.transaction_id,
    )
    with store.connection() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM automation_calendar_writes WHERE run_id = ?",
            (proposing.run_id,),
        ).fetchone()[0] == 1


def test_expired_confirmation_reproposes_with_a_new_version(tmp_path: Path) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    proposing, extraction = proposing_scheduling_run(store)
    awaiting = store.record_automation_proposal(
        proposing.run_id,
        proposing.state_version,
        extraction_payload_id=extraction.payload_id,
        request_sha256="c" * 64,
        subject="Meeting request",
        attendees=("jane@example.com",),
        start="2026-09-08T10:00:00-05:00",
        end="2026-09-08T10:30:00-05:00",
        timezone="America/Chicago",
        suggestion_reason="All attendees are available.",
        empty_reason=None,
        observed_at=datetime(2026, 9, 7, 13, tzinfo=UTC),
    )
    first = store.automation_proposal(proposing.run_id)
    assert first is not None

    expired = store.decide_automation_proposal(
        proposing.run_id,
        awaiting.state_version,
        proposal_version=first.proposal_version,
        proposal_sha256=first.proposal_sha256,
        decision="confirm",
        now=datetime(2026, 9, 7, 13, 15, tzinfo=UTC),
    )
    refreshed = store.record_automation_proposal(
        proposing.run_id,
        expired.state_version,
        extraction_payload_id=extraction.payload_id,
        request_sha256="d" * 64,
        subject="Meeting request",
        attendees=("jane@example.com",),
        start="2026-09-08T11:00:00-05:00",
        end="2026-09-08T11:30:00-05:00",
        timezone="America/Chicago",
        suggestion_reason="All attendees are available.",
        empty_reason=None,
        observed_at=datetime(2026, 9, 7, 13, 16, tzinfo=UTC),
    )

    assert (expired.state, expired.failure_code) == ("proposing", "proposal_expired")
    accepted_extraction = store.automation_extraction_payloads(proposing.run_id)[0]
    assert expired.current_payload_id == accepted_extraction.payload_id
    assert expired.current_payload_sha256 == accepted_extraction.result_sha256
    assert refreshed.state == "awaiting_confirmation"
    second = store.automation_proposal(proposing.run_id)
    assert second is not None and second.proposal_version == 2
    assert second.proposal_sha256 != first.proposal_sha256
    assert store.automation_calendar_write(proposing.run_id) is None


def test_expired_confirmation_can_enter_review_when_refresh_is_invalid(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    proposing, extraction = proposing_scheduling_run(store)
    awaiting = store.record_automation_proposal(
        proposing.run_id,
        proposing.state_version,
        extraction_payload_id=extraction.payload_id,
        request_sha256="c" * 64,
        subject="Meeting request",
        attendees=("jane@example.com",),
        start="2026-09-08T10:00:00-05:00",
        end="2026-09-08T10:30:00-05:00",
        timezone="America/Chicago",
        suggestion_reason="All attendees are available.",
        empty_reason=None,
        observed_at=datetime(2026, 9, 7, 13, tzinfo=UTC),
    )
    proposal = store.automation_proposal(proposing.run_id)
    assert proposal is not None
    expired = store.decide_automation_proposal(
        proposing.run_id,
        awaiting.state_version,
        proposal_version=proposal.proposal_version,
        proposal_sha256=proposal.proposal_sha256,
        decision="confirm",
        now=datetime(2026, 9, 7, 13, 15, tzinfo=UTC),
    )

    work = store.proposable_automation_runs()[0]
    assert work.run.current_payload_id == work.extraction_payload.payload_id
    reviewed = store.transition_automation_to_review(
        work.run.run_id,
        expired.state_version,
        next_state="manual_review",
        failure_code="proposal_invalid",
    )

    assert (reviewed.state, reviewed.failure_code) == ("manual_review", "proposal_invalid")


def test_schema_16_calendar_proposal_migration_preserves_payload(tmp_path: Path) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    proposing, extraction = proposing_scheduling_run(store)
    store.record_automation_proposal(
        proposing.run_id,
        proposing.state_version,
        extraction_payload_id=extraction.payload_id,
        request_sha256="d" * 64,
        subject="Meeting request",
        attendees=("jane@example.com",),
        start="2026-09-08T10:00:00-05:00",
        end="2026-09-08T10:30:00-05:00",
        timezone="America/Chicago",
        suggestion_reason="All attendees are available.",
        empty_reason=None,
        observed_at=datetime(2026, 9, 7, 13, tzinfo=UTC),
    )
    before = store.automation_proposal(proposing.run_id)
    assert before is not None
    with store.connection() as db:
        db.execute("PRAGMA user_version = 16")

    store.initialize()

    after = store.automation_proposal(proposing.run_id)
    assert after == before
    with store.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        table_sql = db.execute(
            """SELECT sql FROM sqlite_master
            WHERE type = 'table' AND name = 'automation_proposal_payloads'"""
        ).fetchone()[0]
    assert "UNIQUE (run_id, proposal_version)" in table_sql
    assert "proposal_version = 1" not in table_sql


def test_schema_17_migrates_every_durable_microsoft_principal_reference(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    account_id = f"microsoft365-{'a' * 32}"
    home_account_id = "home-account-id"
    tenant_id = "tenant-id"
    object_id = "OBJECT-ID"
    legacy_key = hashlib.sha256(
        "\0".join((home_account_id, tenant_id, object_id)).encode()
    ).hexdigest()
    current_key = MicrosoftPrincipal(
        home_account_id=home_account_id,
        tenant_id=tenant_id,
        object_id=object_id.casefold(),
        email_address="owner@example.com",
    ).key
    identity = {
        "principal_key": legacy_key,
        "home_account_id": home_account_id,
        "tenant_id": tenant_id,
        "object_id": object_id,
        "email_address": "owner@example.com",
    }
    for profile in ("read", "proposal", "write"):
        store.set_calendar_grant(account_id, profile, "ready", **identity)
    store.set_calendar_grant(
        account_id,
        "write",
        "ready",
        **{**identity, "principal_key": current_key},
    )
    store.commit_calendar_round(
        account_id=account_id,
        principal_key=legacy_key,
        window_start="2026-09-01T00:00:00.000000Z",
        window_end="2026-10-01T00:00:00.000000Z",
        cursor="https://graph.microsoft.com/v1.0/me/calendarView/delta?$deltatoken=one",
        changes=(),
        replace=True,
    )
    run = admitted_scheduling_run(store, principal_key=legacy_key)
    with store.connection() as db:
        db.execute(
            """INSERT INTO automation_calendar_writes(
                run_id, transaction_id, proposal_version, proposal_sha256,
                calendar_principal_key, calendar_id, start, end, timezone, status,
                confirmed_at, updated_at
            ) VALUES (?, ?, 1, ?, ?, 'primary', ?, ?, ?, 'authorized', ?, ?)""",
            (
                run.run_id,
                "11111111-1111-4111-8111-111111111111",
                "b" * 64,
                legacy_key,
                "2026-09-08T10:00:00-05:00",
                "2026-09-08T10:30:00-05:00",
                "America/Chicago",
                "2026-09-07T13:00:00+00:00",
                "2026-09-07T13:00:00+00:00",
            ),
        )
        db.execute("PRAGMA user_version = 17")

    store.initialize()

    with store.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert {
            str(row[0])
            for row in db.execute(
                "SELECT principal_key FROM microsoft_calendar_grants WHERE account_id = ?",
                (account_id,),
            ).fetchall()
        } == {current_key}
        assert db.execute(
            "SELECT principal_key FROM microsoft_calendar_windows WHERE account_id = ?",
            (account_id,),
        ).fetchone()[0] == current_key
        assert db.execute(
            "SELECT calendar_principal_key FROM automation_runs WHERE run_id = ?",
            (run.run_id,),
        ).fetchone()[0] == current_key
        assert {
            str(row[0])
            for row in db.execute(
                "SELECT calendar_principal_key FROM automation_events WHERE run_id = ?",
                (run.run_id,),
            ).fetchall()
        } == {current_key}
        assert db.execute(
            "SELECT calendar_principal_key FROM automation_calendar_writes WHERE run_id = ?",
            (run.run_id,),
        ).fetchone()[0] == current_key
        with pytest.raises(sqlite3.IntegrityError, match="automation_events are immutable"):
            db.execute(
                "UPDATE automation_events SET calendar_principal_key = ? WHERE run_id = ?",
                (legacy_key, run.run_id),
            )


@pytest.mark.parametrize("principal_key_kind", ["current", "unknown"])
def test_schema_17_principal_migration_does_not_rewrite_unrecognized_keys(
    tmp_path: Path,
    principal_key_kind: str,
) -> None:
    store = Store(tmp_path / principal_key_kind / "watcher.sqlite3")
    store.initialize()
    home_account_id = "home-account-id"
    object_id = "object-id"
    current_key = MicrosoftPrincipal(
        home_account_id=home_account_id,
        tenant_id="tenant-id",
        object_id=object_id,
        email_address="owner@example.com",
    ).key
    principal_key = current_key if principal_key_kind == "current" else "f" * 64
    account_id = f"microsoft365-{principal_key_kind}"
    store.set_calendar_grant(
        account_id,
        "read",
        "ready",
        principal_key=principal_key,
        home_account_id=home_account_id,
        tenant_id="tenant-id",
        object_id=object_id,
        email_address="owner@example.com",
    )
    with store.connection() as db:
        db.execute("PRAGMA user_version = 17")

    store.initialize()

    assert store.calendar_grant(account_id).principal_key == principal_key  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("legacy_key", "current_key"),
    [
        ("", "b" * 64),
        ("a" * 63, "b" * 64),
        ("g" * 64, "b" * 64),
        ("a" * 64, "z" * 64),
    ],
)
def test_runtime_principal_migration_rejects_non_sha256_keys(
    legacy_key: str,
    current_key: str,
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()

    with pytest.raises(ValueError, match="SHA-256 hex digests"):
        store.migrate_calendar_principal_references(
            "microsoft365-account",
            (legacy_key,),
            current_key,
        )


def test_failed_schema_17_proposal_rebuild_rolls_back_and_can_retry(tmp_path: Path) -> None:
    database = tmp_path / "state" / "watcher.sqlite3"
    store = Store(database)
    store.initialize()
    proposing, extraction = proposing_scheduling_run(store)
    store.record_automation_proposal(
        proposing.run_id,
        proposing.state_version,
        extraction_payload_id=extraction.payload_id,
        request_sha256="d" * 64,
        subject="Meeting request",
        attendees=("jane@example.com",),
        start="2026-09-08T10:00:00-05:00",
        end="2026-09-08T10:30:00-05:00",
        timezone="America/Chicago",
        suggestion_reason="All attendees are available.",
        empty_reason=None,
        observed_at=datetime(2026, 9, 7, 13, tzinfo=UTC),
    )
    with store.connection() as db:
        table_sql = str(
            db.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'automation_proposal_payloads'"
            ).fetchone()[0]
        )
        db.execute("DROP TRIGGER automation_runs_delete_proposal_payloads")
        db.execute(
            "ALTER TABLE automation_proposal_payloads "
            "RENAME TO automation_proposal_payloads_current"
        )
        db.execute(table_sql.replace("UNIQUE (run_id, proposal_version),", ""))
        db.execute(
            "INSERT INTO automation_proposal_payloads "
            "SELECT * FROM automation_proposal_payloads_current"
        )
        db.execute("DROP TABLE automation_proposal_payloads_current")
        db.execute(
            """CREATE TRIGGER automation_runs_delete_proposal_payloads
            AFTER DELETE ON automation_runs
            BEGIN
                DELETE FROM automation_proposal_payloads WHERE run_id = OLD.run_id;
            END"""
        )
        db.execute(
            """INSERT INTO automation_proposal_payloads
            SELECT '11111111-1111-4111-8111-111111111111', run_id,
                proposal_version, status, request_sha256, proposal_sha256,
                subject, attendees_json, start, end, timezone, suggestion_reason,
                empty_reason, observed_at, expires_at, created_at
            FROM automation_proposal_payloads LIMIT 1"""
        )
        db.execute("PRAGMA user_version = 16")

    with pytest.raises(sqlite3.IntegrityError):
        Store(database).initialize()

    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 16
        assert db.execute("SELECT COUNT(*) FROM automation_proposal_payloads").fetchone()[0] == 2
        assert (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'automation_proposal_payloads_v16'"
            ).fetchone()
            is None
        )
        db.execute(
            "DELETE FROM automation_proposal_payloads "
            "WHERE payload_id = '11111111-1111-4111-8111-111111111111'"
        )

    Store(database).initialize()

    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert db.execute("SELECT COUNT(*) FROM automation_proposal_payloads").fetchone()[0] == 1


def test_calendar_write_lifecycle_preserves_transaction_and_event_identity(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    proposing, extraction = proposing_scheduling_run(store)
    awaiting = store.record_automation_proposal(
        proposing.run_id,
        proposing.state_version,
        extraction_payload_id=extraction.payload_id,
        request_sha256="e" * 64,
        subject="Meeting request",
        attendees=("jane@example.com",),
        start="2026-09-08T10:00:00-05:00",
        end="2026-09-08T10:30:00-05:00",
        timezone="America/Chicago",
        suggestion_reason="All attendees are available.",
        empty_reason=None,
        observed_at=datetime(2026, 9, 7, 13, tzinfo=UTC),
    )
    proposal = store.automation_proposal(proposing.run_id)
    assert proposal is not None
    authorized = store.decide_automation_proposal(
        proposing.run_id,
        awaiting.state_version,
        proposal_version=proposal.proposal_version,
        proposal_sha256=proposal.proposal_sha256,
        decision="confirm",
        now=datetime(2026, 9, 7, 13, 5, tzinfo=UTC),
    )
    writing = store.begin_automation_calendar_write(
        proposing.run_id,
        authorized.state_version,
        now=datetime(2026, 9, 7, 13, 6, tzinfo=UTC),
    )
    unresolved = store.transition_automation_calendar_write(
        proposing.run_id,
        writing.state_version,
        next_state="unresolved",
        failure_code="write_outcome_unknown",
    )
    reconciling = store.transition_automation_calendar_write(
        proposing.run_id,
        unresolved.state_version,
        next_state="reconciling",
    )
    completed = store.transition_automation_calendar_write(
        proposing.run_id,
        reconciling.state_version,
        next_state="completed",
        graph_event_id="immutable-event-id",
    )

    write = store.automation_calendar_write(proposing.run_id)
    assert completed.state == "completed"
    assert write is not None
    assert (write.status, write.graph_event_id) == ("completed", "immutable-event-id")
    transaction_ids = {
        event.transaction_id
        for event in store.automation_events(proposing.run_id)
        if event.transaction_id is not None
    }
    assert transaction_ids == {write.transaction_id}
    preview = store.recent(1)[0]["calendar_proposal"]
    assert preview is not None
    assert (preview["state"], preview["graph_event_id"]) == (
        "completed",
        "immutable-event-id",
    )


def test_expired_unresolved_calendar_write_stops_retrying_and_is_purged(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    proposing, extraction = proposing_scheduling_run(store)
    awaiting = store.record_automation_proposal(
        proposing.run_id,
        proposing.state_version,
        extraction_payload_id=extraction.payload_id,
        request_sha256="e" * 64,
        subject="Meeting request",
        attendees=("jane@example.com",),
        start="2026-09-08T10:00:00-05:00",
        end="2026-09-08T10:30:00-05:00",
        timezone="America/Chicago",
        suggestion_reason="All attendees are available.",
        empty_reason=None,
        observed_at=datetime(2026, 9, 7, 13, tzinfo=UTC),
    )
    proposal = store.automation_proposal(proposing.run_id)
    assert proposal is not None
    authorized = store.decide_automation_proposal(
        proposing.run_id,
        awaiting.state_version,
        proposal_version=proposal.proposal_version,
        proposal_sha256=proposal.proposal_sha256,
        decision="confirm",
        now=datetime(2026, 9, 7, 13, 5, tzinfo=UTC),
    )
    writing = store.begin_automation_calendar_write(
        proposing.run_id,
        authorized.state_version,
        now=datetime(2026, 9, 7, 13, 6, tzinfo=UTC),
    )
    store.transition_automation_calendar_write(
        proposing.run_id,
        writing.state_version,
        next_state="unresolved",
        failure_code="write_outcome_unknown",
        now=datetime(2026, 9, 7, 13, 7, tzinfo=UTC),
    )
    expires_at = datetime(2026, 9, 8, 13, tzinfo=UTC)
    with store.connection() as db:
        db.execute(
            "UPDATE automation_runs SET expires_at = ? WHERE run_id = ?",
            (expires_at.isoformat(), proposing.run_id),
        )

    assert store.pending_automation_calendar_writes(now=expires_at) == []
    store.purge_with_outcome(MAX_RETENTION_DAYS, now=expires_at)

    assert store.automation_run(proposing.run_id) is None
    assert store.automation_calendar_write(proposing.run_id) is None
    assert store.automation_events(proposing.run_id) == []


def test_expired_writing_calendar_write_survives_until_outcome_is_recorded(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    proposing, extraction = proposing_scheduling_run(store)
    awaiting = store.record_automation_proposal(
        proposing.run_id,
        proposing.state_version,
        extraction_payload_id=extraction.payload_id,
        request_sha256="e" * 64,
        subject="Meeting request",
        attendees=("jane@example.com",),
        start="2026-09-08T10:00:00-05:00",
        end="2026-09-08T10:30:00-05:00",
        timezone="America/Chicago",
        suggestion_reason="All attendees are available.",
        empty_reason=None,
        observed_at=datetime(2026, 9, 7, 13, tzinfo=UTC),
    )
    proposal = store.automation_proposal(proposing.run_id)
    assert proposal is not None
    authorized = store.decide_automation_proposal(
        proposing.run_id,
        awaiting.state_version,
        proposal_version=proposal.proposal_version,
        proposal_sha256=proposal.proposal_sha256,
        decision="confirm",
        now=datetime(2026, 9, 7, 13, 5, tzinfo=UTC),
    )
    writing = store.begin_automation_calendar_write(
        proposing.run_id,
        authorized.state_version,
        now=datetime(2026, 9, 7, 13, 6, tzinfo=UTC),
    )
    expires_at = datetime(2026, 9, 8, 13, tzinfo=UTC)
    with store.connection() as db:
        db.execute(
            "UPDATE automation_runs SET expires_at = ? WHERE run_id = ?",
            (expires_at.isoformat(), proposing.run_id),
        )

    store.purge_with_outcome(MAX_RETENTION_DAYS, now=expires_at)

    pending = store.pending_automation_calendar_writes(now=expires_at)
    assert [item.run.run_id for item in pending] == [proposing.run_id]
    unresolved = store.transition_automation_calendar_write(
        proposing.run_id,
        writing.state_version,
        next_state="unresolved",
        failure_code="write_outcome_unknown",
        now=expires_at,
    )
    store.purge_with_outcome(MAX_RETENTION_DAYS, now=expires_at)

    assert unresolved.state == "unresolved"
    assert store.automation_run(proposing.run_id) is None
    assert store.automation_calendar_write(proposing.run_id) is None


def test_source_cleanup_cancels_authorized_write_before_submission(tmp_path: Path) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    proposing, extraction = proposing_scheduling_run(store)
    awaiting = store.record_automation_proposal(
        proposing.run_id,
        proposing.state_version,
        extraction_payload_id=extraction.payload_id,
        request_sha256="f" * 64,
        subject="Meeting request",
        attendees=(),
        start="2026-09-08T10:00:00-05:00",
        end="2026-09-08T10:30:00-05:00",
        timezone="America/Chicago",
        suggestion_reason="The organizer is available.",
        empty_reason=None,
        observed_at=datetime(2026, 9, 7, 13, tzinfo=UTC),
    )
    proposal = store.automation_proposal(proposing.run_id)
    assert proposal is not None
    store.decide_automation_proposal(
        proposing.run_id,
        awaiting.state_version,
        proposal_version=proposal.proposal_version,
        proposal_sha256=proposal.proposal_sha256,
        decision="confirm",
        now=datetime(2026, 9, 7, 13, 5, tzinfo=UTC),
    )

    assert store.delete_message("local-scheduling") is True

    run = store.automation_run(proposing.run_id)
    write = store.automation_calendar_write(proposing.run_id)
    assert run is not None and run.state == "source_unavailable"
    assert write is not None and write.status == "cancelled"
    assert store.automation_proposal(proposing.run_id) is None


def test_automation_review_notification_is_durable_state_checked_and_idempotent(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    detected = admitted_scheduling_run(store)
    reviewed = store.transition_automation_to_review(
        detected.run_id,
        detected.state_version,
        next_state="manual_review",
        failure_code="source_invalid",
    )

    intent = next(item for item in store.notification_intents() if item.kind == "automation_review")
    assert (intent.subject_type, intent.subject_id, intent.revision) == (
        "automation_run",
        detected.run_id,
        str(reviewed.state_version),
    )
    assert intent.message_id == detected.run_id
    assert "could not be processed safely" in str(intent.summary)
    assert (
        store.acknowledge_notification(
            message_id=intent.message_id,
            kind=intent.kind,
            analysis_at=intent.analysis_at,
            subject_type=intent.subject_type,
            subject_id=intent.subject_id,
            revision=intent.revision,
        )
        == "acknowledged"
    )
    assert (
        store.acknowledge_notification(
            message_id=intent.message_id,
            kind=intent.kind,
            analysis_at=intent.analysis_at,
            subject_type=intent.subject_type,
            subject_id=intent.subject_id,
            revision=intent.revision,
        )
        == "already_acknowledged"
    )
    assert all(item.subject_id != detected.run_id for item in store.notification_intents())


def test_source_unavailable_notification_survives_source_deletion(tmp_path: Path) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    detected = admitted_scheduling_run(store)

    assert store.delete_message("local-scheduling") is True

    intent = next(item for item in store.notification_intents() if item.kind == "automation_review")
    assert intent.subject_id == detected.run_id
    assert intent.sender == "Email Watcher"
    assert "no longer available" in str(intent.summary)


def test_automation_events_are_immutable_and_source_delete_is_atomic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    raw_gmail_id = "raw-provider-message-id"
    assert store.add_message(
        message_id=raw_gmail_id,
        provider="gmail",
        account_id="gmail-default",
        provider_message_id=raw_gmail_id,
        thread_id=None,
        sender="trusted@example.com",
        sender_name=None,
        subject="Meeting",
        received_at="2026-09-07T12:00:00+00:00",
    )
    with store.connection() as db:
        db.execute(
            "UPDATE messages SET discovered_at = ? WHERE message_id = ?",
            ("2026-09-07T12:00:00+00:00", raw_gmail_id),
        )
    monkeypatch.setattr(db_module, "MAX_RETENTION_DAYS", 30)
    monkeypatch.setattr(db_module, "SCHEDULING_AUTOMATION_VERSION", 7)
    monkeypatch.setattr(db_module, "SCHEDULING_EXTRACTION_SCHEMA_VERSION", 3)
    store.mark_analyzed(
        raw_gmail_id,
        scheduling_analysis(),
        scheduling_automation_principal_key=CALENDAR_PRINCIPAL_KEY,
        now=datetime(2026, 9, 7, 12, 1, tzinfo=UTC),
    )
    detected = store.automation_run_for_message(raw_gmail_id)
    assert detected is not None
    assert detected.expires_at == "2026-10-07T12:00:00+00:00"
    monkeypatch.setattr(db_module, "SCHEDULING_AUTOMATION_VERSION", 8)
    monkeypatch.setattr(db_module, "SCHEDULING_EXTRACTION_SCHEMA_VERSION", 4)

    with store.connection() as db:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            db.execute(
                "UPDATE automation_runs SET state = 'invented' WHERE run_id = ?", (detected.run_id,)
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE automation_events SET transition_kind = 'source_unavailable'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("DELETE FROM automation_events")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute(
                "INSERT OR REPLACE INTO automation_events SELECT * FROM automation_events "
                "WHERE event_id = ?",
                (store.automation_events(detected.run_id)[0].event_id,),
            )

    removed_at = datetime(2026, 9, 7, 13, tzinfo=UTC)
    assert store.delete_message(raw_gmail_id, now=removed_at)
    tombstone = store.automation_run(detected.run_id)
    assert tombstone is not None
    assert (tombstone.state, tombstone.state_version) == ("source_unavailable", 2)
    assert tombstone.failure_code == "source_unavailable"
    events = store.automation_events(detected.run_id)
    assert [event.next_state for event in events] == [
        "detected",
        "source_unavailable",
    ]
    assert [(event.automation_version, event.extraction_schema_version) for event in events] == [
        (7, 3),
        (7, 3),
    ]
    assert [event.calendar_principal_key for event in events] == [
        CALENDAR_PRINCIPAL_KEY,
        CALENDAR_PRINCIPAL_KEY,
    ]
    with store.connection() as db:
        durable_automation = " ".join(
            str(value)
            for row in db.execute("SELECT * FROM automation_runs").fetchall()
            for value in row
        ) + " ".join(
            str(value)
            for row in db.execute("SELECT * FROM automation_events").fetchall()
            for value in row
        )
    assert raw_gmail_id not in durable_automation
    assert store.purge(30, now=datetime(2026, 10, 8, 13, tzinfo=UTC)) == 0
    assert store.automation_run(detected.run_id) is None
    assert store.automation_events(detected.run_id) == []


def test_non_scheduling_or_unentitled_analysis_creates_no_automation_run(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    for message_id in ("unentitled", "informational"):
        assert store.add_message(
            message_id=message_id,
            thread_id=None,
            sender="trusted@example.com",
            sender_name=None,
            subject=message_id,
            received_at="2026-09-07T12:00:00+00:00",
        )
    store.mark_analyzed("unentitled", scheduling_analysis())
    informational = scheduling_analysis()
    informational["category"] = "informational"
    store.mark_analyzed(
        "informational",
        informational,
        scheduling_automation_principal_key=CALENDAR_PRINCIPAL_KEY,
    )

    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM automation_runs").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM automation_events").fetchone()[0] == 0


@pytest.mark.parametrize("principal_key", ["", "a" * 63, "a" * 65])
def test_scheduling_admission_rejects_invalid_calendar_principal_before_analysis(
    tmp_path: Path,
    principal_key: str,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    assert store.add_message(
        message_id="m1",
        thread_id=None,
        sender="trusted@example.com",
        sender_name=None,
        subject="Meeting",
        received_at="2026-09-07T12:00:00+00:00",
    )

    with pytest.raises(ValueError, match="principal key"):
        store.mark_analyzed(
            "m1",
            scheduling_analysis(),
            scheduling_automation_principal_key=principal_key,
        )

    assert [message.message_id for message in store.pending()] == ["m1"]
    assert store.automation_run_for_message("m1") is None


@pytest.mark.parametrize("cleanup", ["clear", "purge"])
def test_bulk_cleanup_makes_detected_automation_source_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup: str,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    run_ids: list[str] = []
    for sequence in range(3):
        message_id = f"m{sequence}"
        assert store.add_message(
            message_id=message_id,
            thread_id=None,
            sender="trusted@example.com",
            sender_name=None,
            subject="Meeting",
            received_at="2026-01-01T12:00:00+00:00",
        )
        store.mark_analyzed(
            message_id,
            scheduling_analysis(),
            scheduling_automation_principal_key=CALENDAR_PRINCIPAL_KEY,
        )
        detected = store.automation_run_for_message(message_id)
        assert detected is not None
        run_ids.append(detected.run_id)
    monkeypatch.setattr(db_module, "AUTOMATION_CLEANUP_CHUNK_SIZE", 1)

    cleanup_at = datetime(2026, 9, 7, 13, tzinfo=UTC)
    if cleanup == "clear":
        assert store.clear_messages(now=cleanup_at) == 3
    else:
        assert store.purge(30, now=cleanup_at) == 3

    for run_id in run_ids:
        tombstone = store.automation_run(run_id)
        assert tombstone is not None
        assert (tombstone.state, tombstone.state_version) == ("source_unavailable", 2)
        assert [event.transition_kind for event in store.automation_events(run_id)] == [
            "detected",
            "source_unavailable",
        ]


@pytest.mark.parametrize("cleanup", ["delete", "clear"])
def test_manual_cleanup_purges_automation_after_source_expiry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup: str,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    assert store.add_message(
        message_id="m1",
        thread_id=None,
        sender="trusted@example.com",
        sender_name=None,
        subject="Meeting",
        received_at="2026-01-01T12:00:00+00:00",
    )
    with store.connection() as db:
        db.execute(
            "UPDATE messages SET discovered_at = ? WHERE message_id = ?",
            ("2026-01-01T12:00:00+00:00", "m1"),
        )
    monkeypatch.setattr(db_module, "MAX_RETENTION_DAYS", 30)
    store.mark_analyzed(
        "m1",
        scheduling_analysis(),
        scheduling_automation_principal_key=CALENDAR_PRINCIPAL_KEY,
        now=datetime(2026, 1, 1, 12, 1, tzinfo=UTC),
    )
    run = store.automation_run_for_message("m1")
    assert run is not None

    cleanup_at = datetime(2026, 3, 1, 12, tzinfo=UTC)
    if cleanup == "delete":
        assert store.delete_message("m1", now=cleanup_at)
    else:
        assert store.clear_messages(now=cleanup_at) == 1

    assert store.automation_run(run.run_id) is None
    assert store.automation_events(run.run_id) == []


def test_mailbox_state_identity_and_suppression_are_account_scoped(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    store.set_state("gmail-cursor", provider="gmail", account_id="gmail-default")
    store.set_state("graph-cursor", provider="microsoft365", account_id="account-2")

    assert store.state(provider="gmail", account_id="gmail-default")[0] == "gmail-cursor"
    assert store.state(provider="microsoft365", account_id="account-2")[0] == "graph-cursor"

    source_id = "same-provider-id"
    assert store.add_message(
        message_id=scoped_message_id("gmail", "gmail-default", source_id),
        provider="gmail",
        account_id="gmail-default",
        provider_message_id=source_id,
        thread_id=None,
        sender="first@example.com",
        sender_name=None,
        subject="Gmail",
        received_at="2026-08-31T12:00:00+00:00",
    )
    assert store.delete_message(source_id)
    assert store.has_seen_message(source_id, provider="gmail", account_id="gmail-default")
    assert not store.has_seen_message(source_id, provider="microsoft365", account_id="account-2")

    graph_local_id = scoped_message_id("microsoft365", "account-2", source_id)
    assert store.add_message(
        message_id=graph_local_id,
        provider="microsoft365",
        account_id="account-2",
        provider_message_id=source_id,
        thread_id=None,
        sender="second@example.com",
        sender_name=None,
        subject="Microsoft 365",
        received_at="2026-08-31T13:00:00+00:00",
    )
    assert not store.add_message(
        message_id="different-local-id",
        provider="microsoft365",
        account_id="account-2",
        provider_message_id=source_id,
        thread_id=None,
        sender="second@example.com",
        sender_name=None,
        subject="Duplicate source identity",
        received_at="2026-08-31T13:00:00+00:00",
    )
    assert [
        item.message_id for item in store.pending(provider="microsoft365", account_id="account-2")
    ] == [graph_local_id]
    items, _ = store.query_inbox(limit=10, account_id="account-2")
    assert [(item["provider"], item["account_id"], item["message_id"]) for item in items] == [
        ("microsoft365", "account-2", graph_local_id)
    ]


def test_mail_account_registry_seeds_legacy_gmail_and_switches_atomically(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()

    legacy = store.active_mail_account()
    assert legacy is not None
    assert (legacy.provider, legacy.account_id, legacy.address, legacy.active) == (
        "gmail",
        "gmail-default",
        None,
        True,
    )

    graph = store.register_mail_account(
        "microsoft365",
        "microsoft365-default",
        display_name="Microsoft 365",
        address="owner@example.com",
    )
    assert graph.active is False
    activated = store.activate_mail_account(graph.provider, graph.account_id)

    assert activated.active is True
    assert store.active_mail_account() == activated
    assert [account.active for account in store.mail_accounts()] == [True, False]
    with store.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_mail_account_registry_rejects_duplicate_provider_identity(tmp_path: Path) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    store.update_mail_account_identity(
        "gmail",
        "gmail-default",
        display_name="Gmail",
        address="owner@example.com",
    )

    with pytest.raises(sqlite3.IntegrityError):
        store.register_mail_account(
            "gmail",
            "gmail-second",
            display_name="Gmail",
            address="owner@example.com",
        )

    assert [(account.account_id, account.address) for account in store.mail_accounts()] == [
        ("gmail-default", "owner@example.com")
    ]


def test_calendar_disconnect_clears_only_selected_read_state(tmp_path: Path) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    account_id = "microsoft365-" + "c" * 32
    identity = {
        "principal_key": "d" * 64,
        "home_account_id": "home.tenant",
        "tenant_id": "tenant",
        "object_id": "object",
        "email_address": "owner@example.com",
    }
    store.set_calendar_grant(account_id, "read", "ready", **identity)
    store.set_calendar_grant(account_id, "proposal", "ready", **identity)

    disconnected = store.disconnect_calendar_read(account_id)

    assert disconnected.state == "not_requested"
    assert disconnected.principal_key is None
    assert store.calendar_grant(account_id, "proposal").state == "ready"


def test_calendar_revocation_compare_and_set_preserves_newer_state(tmp_path: Path) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    account_id = "microsoft365-" + "e" * 32
    identity = {
        "principal_key": "f" * 64,
        "home_account_id": "home.tenant",
        "tenant_id": "tenant",
        "object_id": "object",
        "email_address": "owner@example.com",
    }
    ready = store.set_calendar_grant(account_id, "read", "ready", **identity)

    assert store.revoke_calendar_grant_if_current(ready) is True
    revoked = store.calendar_grant(account_id)
    assert revoked is not None
    assert revoked.state == "revoked"
    assert revoked.principal_key == identity["principal_key"]

    stale = store.set_calendar_grant(account_id, "read", "ready", **identity)
    store.set_calendar_grant(account_id, "read", "consent_pending", **identity)

    assert store.revoke_calendar_grant_if_current(stale) is False
    assert store.calendar_grant(account_id).state == "consent_pending"


def projected_event(event_id: str, subject: str = "Planning") -> CalendarEventProjection:
    return CalendarEventProjection(
        event_id=event_id,
        subject=subject,
        start_date_time="2026-09-07T09:00:00.0000000",
        start_time_zone="UTC",
        end_date_time="2026-09-07T10:00:00.0000000",
        end_time_zone="UTC",
        is_all_day=False,
        location="Office",
    )


def test_calendar_round_commits_projection_cursor_and_ordered_replays_atomically(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    account_id = "microsoft365-" + "a" * 32
    principal_key = "b" * 64
    start = "2026-09-01T00:00:00.000000Z"
    end = "2026-10-01T00:00:00.000000Z"

    initial = store.commit_calendar_round(
        account_id=account_id,
        principal_key=principal_key,
        window_start=start,
        window_end=end,
        cursor="https://graph.microsoft.com/v1.0/me/calendarView/delta?$deltatoken=one",
        changes=(
            CalendarEventMutation("event-1", projected_event("event-1")),
            CalendarEventMutation("event-2", projected_event("event-2")),
        ),
        replace=True,
    )
    assert initial.cursor.endswith("one")
    assert [event.event_id for event in store.calendar_events(account_id)] == [
        "event-1",
        "event-2",
    ]
    projected_window, projected_events = store.calendar_projection(account_id)
    assert projected_window == initial
    assert [event.event_id for event in projected_events] == ["event-1", "event-2"]

    updated = store.commit_calendar_round(
        account_id=account_id,
        principal_key=principal_key,
        window_start=start,
        window_end=end,
        cursor="https://graph.microsoft.com/v1.0/me/calendarView/delta?$deltatoken=two",
        changes=(
            CalendarEventMutation("event-1", projected_event("event-1", "Updated")),
            CalendarEventMutation("event-2", None),
            CalendarEventMutation("event-1", projected_event("event-1", "Final")),
        ),
        replace=False,
    )

    assert updated.cursor.endswith("two")
    assert [(event.event_id, event.subject) for event in store.calendar_events(account_id)] == [
        ("event-1", "Final")
    ]

    with pytest.raises(sqlite3.IntegrityError):
        store.commit_calendar_round(
            account_id=account_id,
            principal_key=principal_key,
            window_start=start,
            window_end=end,
            cursor="https://graph.microsoft.com/v1.0/me/calendarView/delta?$deltatoken=bad",
            changes=(
                CalendarEventMutation(
                    "event-3",
                    projected_event("event-3", "x" * 513),
                ),
            ),
            replace=False,
        )

    assert store.calendar_window(account_id).cursor.endswith("two")  # type: ignore[union-attr]
    assert [(event.event_id, event.subject) for event in store.calendar_events(account_id)] == [
        ("event-1", "Final")
    ]


def test_calendar_disconnect_removes_only_read_projection(tmp_path: Path) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    account_id = "microsoft365-" + "b" * 32
    principal_key = "c" * 64
    store.commit_calendar_round(
        account_id=account_id,
        principal_key=principal_key,
        window_start="2026-09-01T00:00:00.000000Z",
        window_end="2026-10-01T00:00:00.000000Z",
        cursor="https://graph.microsoft.com/v1.0/me/calendarView/delta?$deltatoken=one",
        changes=(CalendarEventMutation("event-1", projected_event("event-1")),),
        replace=True,
    )

    store.disconnect_calendar_grant(account_id, "proposal")
    assert store.calendar_window(account_id) is not None
    assert len(store.calendar_events(account_id)) == 1

    store.disconnect_calendar_grant(account_id, "read")
    assert store.calendar_window(account_id) is None
    assert store.calendar_events(account_id) == []


def test_inbox_query_keyset_paginates_equal_timestamps_without_gaps(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    stamp = "2026-08-31T12:00:00+00:00"
    with store.connection() as db:
        db.executemany(
            """INSERT INTO messages(
                    message_id, provider, account_id, provider_message_id,
                    sender, subject, received_at, discovered_at, category
                ) VALUES (
                    ?1, 'gmail', 'gmail-default', ?1,
                    'sender@example.com', 'Update', ?2, ?3, 'informational'
                )""",
            [(f"message-{index:03}", stamp, stamp) for index in range(55)],
        )

    cursor: tuple[str, str] | None = None
    message_ids: list[str] = []
    while True:
        items, cursor = store.query_inbox(limit=7, cursor=cursor)
        message_ids.extend(str(item["message_id"]) for item in items)
        assert all(item["category"] == "informational" for item in items)
        if cursor is None:
            break

    assert message_ids == [f"message-{index:03}" for index in reversed(range(55))]
    assert len(message_ids) == len(set(message_ids))


def test_inbox_query_combines_filters_before_limiting_and_matches_literals(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    with store.connection() as db:
        db.executemany(
            """INSERT INTO messages(
                    message_id, provider, account_id, provider_message_id,
                    sender, sender_name, subject, received_at, discovered_at,
                    status, category, priority, summary
                ) VALUES (
                    ?1, 'gmail', 'gmail-default', ?1,
                    ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10
                )""",
            [
                (
                    f"noise-{index:03}",
                    "noise@example.com",
                    "Noise",
                    "Routine update",
                    f"2026-08-31T13:{index:02}:00+00:00",
                    "2026-08-31T14:00:00+00:00",
                    "analyzed",
                    "invoice",
                    "high",
                    "Nothing actionable.",
                )
                for index in range(60)
            ]
            + [
                (
                    "target",
                    "billing@acme.example",
                    "ACME Billing",
                    "Overdue % balance",
                    "2026-08-30T12:00:00+00:00",
                    "2026-08-31T14:00:00+00:00",
                    "analyzed",
                    "invoice",
                    "high",
                    "Please review the balance.",
                ),
                (
                    "untriaged",
                    "billing@acme.example",
                    None,
                    "Waiting",
                    "2026-08-29T12:00:00+00:00",
                    "2026-08-31T14:00:00+00:00",
                    "pending",
                    None,
                    None,
                    None,
                ),
            ],
        )

    items, cursor = store.query_inbox(
        limit=10,
        sender_query="acme",
        priority="high",
        category="invoice",
        status="analyzed",
        keyword="OVERDUE % BALANCE",
    )
    assert [item["message_id"] for item in items] == ["target"]
    assert cursor is None
    percent_items, _ = store.query_inbox(limit=10, keyword="%")
    assert [item["message_id"] for item in percent_items] == ["target"]
    untriaged_items, _ = store.query_inbox(limit=10, priority="untriaged", category="unclassified")
    assert [item["message_id"] for item in untriaged_items] == ["untriaged"]


def test_analysis_notification_ack_is_state_checked_and_idempotent(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    store.add_message(
        message_id="m1",
        thread_id=None,
        sender="a@b.com",
        sender_name="A",
        subject="Action",
        received_at="2026-07-18T14:00:00+00:00",
    )
    store.mark_analyzed(
        "m1",
        {
            "category": "customer_request",
            "priority": "high",
            "summary": "Please respond.",
            "action_required": True,
            "suggested_action": "Reply.",
            "deadline_text": None,
            "deadline_iso": None,
            "confidence": 0.9,
        },
    )
    intent = store.notification_intents()[0]
    assert intent.kind == "analysis"
    assert intent.analysis_at is not None

    with pytest.raises(RuntimeError, match="no longer current"):
        store.acknowledge_notification(
            message_id="m1", kind="analysis", analysis_at="stale-version"
        )
    assert store.recent(1)[0]["status"] == "analyzed"

    assert (
        store.acknowledge_notification(
            message_id="m1", kind="analysis", analysis_at=intent.analysis_at
        )
        == "acknowledged"
    )
    assert store.notification_intents() == []
    assert (
        store.acknowledge_notification(
            message_id="m1", kind="analysis", analysis_at=intent.analysis_at
        )
        == "already_acknowledged"
    )


def test_fallback_ack_does_not_ack_later_analysis(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    store.add_message(
        message_id="m1",
        thread_id=None,
        sender="a@b.com",
        sender_name=None,
        subject="Update",
        received_at="2026-07-18T14:00:00+00:00",
    )
    store.record_failure("m1", "local model unavailable", 0)
    fallback = store.notification_intents()[0]
    assert fallback.kind == "fallback"
    assert store.acknowledge_notification(message_id="m1", kind="fallback") == "acknowledged"
    assert store.notification_intents() == []

    store.mark_analyzed(
        "m1",
        {
            "category": "informational",
            "priority": "normal",
            "summary": "Recovered summary.",
            "action_required": False,
            "suggested_action": None,
            "deadline_text": None,
            "deadline_iso": None,
            "confidence": 0.9,
        },
    )
    analysis = store.notification_intents()[0]
    assert analysis.kind == "analysis"
    assert analysis.summary == "Recovered summary."
    assert (
        store.acknowledge_notification(message_id="m1", kind="fallback") == "already_acknowledged"
    )
    assert store.notification_intents()[0].kind == "analysis"


def test_fallback_ack_requires_current_notification_intent(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    store.add_message(
        message_id="m1",
        thread_id=None,
        sender="a@b.com",
        sender_name=None,
        subject="Update",
        received_at="2026-07-18T14:00:00+00:00",
    )

    with pytest.raises(RuntimeError, match="no longer current"):
        store.acknowledge_notification(message_id="m1", kind="fallback")
    assert store.recent(1)[0]["fallback_notified_at"] is None

    store.record_failure("m1", "local model unavailable", 0)
    assert store.acknowledge_notification(message_id="m1", kind="fallback") == "acknowledged"


def test_notification_intent_count_is_not_limited_to_retrieval_page(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    stamp = datetime.now(UTC).isoformat()
    with store.connection() as db:
        db.executemany(
            """INSERT INTO messages (
                    message_id, provider, account_id, provider_message_id,
                    sender, subject, received_at, discovered_at, status, last_error
                ) VALUES (
                    ?1, 'gmail', 'gmail-default', ?1,
                    'a@b.com', 'Update', ?2, ?3, 'pending', 'model unavailable'
                )""",
            [(f"m{index}", stamp, stamp) for index in range(501)],
        )

    assert len(store.notification_intents(limit=500)) == 500
    assert store.notification_intent_count() == 501


def test_purge_is_a_hard_source_time_ceiling_including_notification_intents(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    for message_id in ("analysis", "fallback", "ordinary"):
        store.add_message(
            message_id=message_id,
            thread_id=None,
            sender="a@b.com",
            sender_name=None,
            subject=message_id,
            received_at="2026-08-30T12:00:00+00:00",
        )
    store.mark_analyzed(
        "analysis",
        {
            "category": "informational",
            "priority": "normal",
            "summary": "Summary.",
            "action_required": False,
            "suggested_action": None,
            "deadline_text": None,
            "deadline_iso": None,
            "confidence": 0.9,
        },
    )
    store.record_failure("fallback", "local model unavailable", 0)
    assert store.notification_intent_count() == 2
    assert store.purge(1, now=datetime(2026, 9, 1, 12, tzinfo=UTC)) == 3
    assert store.recent(10) == []
    assert store.notification_intents() == []


def test_purge_uses_source_received_time_not_local_discovery_time(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    store.add_message(
        message_id="source-old",
        thread_id=None,
        sender="a@b.com",
        sender_name=None,
        subject="Old source",
        received_at="2026-08-01T00:00:00+00:00",
    )
    store.add_message(
        message_id="source-current",
        thread_id=None,
        sender="a@b.com",
        sender_name=None,
        subject="Current source",
        received_at="2026-09-01T00:00:00+00:00",
    )
    with store.connection() as db:
        db.execute(
            "UPDATE messages SET discovered_at = '2020-01-01T00:00:00+00:00' "
            "WHERE message_id = 'source-current'"
        )

    assert store.purge(7, now=datetime(2026, 9, 1, 12, tzinfo=UTC)) == 1
    assert [row["message_id"] for row in store.recent(10)] == ["source-current"]


def test_purge_rejects_timezone_less_source_timestamp(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    store.add_message(
        message_id="timezone-less",
        thread_id=None,
        sender="a@b.com",
        sender_name=None,
        subject="Malformed source time",
        received_at="2026-09-01T11:00:00",
    )
    store.mark_analyzed(
        "timezone-less",
        {
            "category": "informational",
            "priority": "normal",
            "summary": "Summary.",
            "action_required": False,
            "suggested_action": None,
            "deadline_text": None,
            "deadline_iso": None,
            "confidence": 0.9,
        },
    )

    assert store.purge(7, now=datetime(2026, 9, 1, 12, tzinfo=UTC)) == 1
    assert store.recent(10) == []
    assert store.notification_intents() == []


def test_purge_uses_one_parser_for_second_precision_timezone_offsets(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    for message_id, received_at in (
        ("old-offset", "2026-08-01T12:00:00+00:00:30"),
        ("current-offset", "2026-09-01T11:00:00+00:00:30"),
    ):
        assert store.add_message(
            message_id=message_id,
            thread_id=None,
            sender="a@b.com",
            sender_name=None,
            subject=message_id,
            received_at=received_at,
        )

    assert store.purge(7, now=datetime(2026, 9, 1, 12, tzinfo=UTC)) == 1
    assert [row["message_id"] for row in store.recent(10)] == ["current-offset"]


def test_purge_bounds_legacy_future_source_time_by_discovery_time(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    store.add_message(
        message_id="future-source",
        thread_id=None,
        sender="a@b.com",
        sender_name=None,
        subject="Malformed future source time",
        received_at="2036-09-01T00:00:00+00:00",
    )
    with store.connection() as db:
        db.execute(
            "UPDATE messages SET discovered_at = ? WHERE message_id = ?",
            ("2026-08-01T00:00:00+00:00", "future-source"),
        )

    assert store.purge(7, now=datetime(2026, 9, 1, 12, tzinfo=UTC)) == 1
    assert store.recent(10) == []


def test_purge_rejects_future_source_and_discovery_times(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    store.add_message(
        message_id="future-source-and-discovery",
        thread_id=None,
        sender="a@b.com",
        sender_name=None,
        subject="Future local timestamps",
        received_at="2036-09-01T00:00:00+00:00",
    )
    with store.connection() as db:
        db.execute(
            "UPDATE messages SET discovered_at = ? WHERE message_id = ?",
            ("2036-09-01T00:00:00+00:00", "future-source-and-discovery"),
        )

    assert store.purge(7, now=datetime(2026, 9, 1, 12, tzinfo=UTC)) == 1
    assert store.recent(10) == []


def test_delete_message_cascades_local_state_and_prevents_rediscovery(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    store.set_state("100", datetime(2026, 9, 1, tzinfo=UTC))
    seed_pdf_attachment(store)
    create_connect_job(store, "33333333-3333-4333-8333-333333333333")
    store.record_failure("m1", "model unavailable", 0)

    assert store.delete_message("m1", now=datetime(2026, 9, 1, 12, tzinfo=UTC))
    assert not store.delete_message("m1")
    assert store.recent(10) == []
    assert store.notification_intents() == []
    assert store.state() == ("100", "2026-09-01T00:00:00+00:00")
    assert not store.add_message(
        message_id="m1",
        thread_id=None,
        sender="a@b.com",
        sender_name=None,
        subject="Rediscovered",
        received_at="2026-08-29T12:00:00+00:00",
    )
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM message_attachments").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM connect_attachment_jobs").fetchone()[0] == 0
        suppression = db.execute("SELECT message_key FROM suppressed_messages").fetchone()[0]
    assert suppression == hashlib.sha256(b"gmail\0gmail-default\0m1").hexdigest()
    assert "m1" not in suppression


def test_delete_message_waits_for_cross_process_source_handoff(tmp_path: Path) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    seed_pdf_attachment(store)
    lock_path = locking.connect_source_lock_path(store.path, "m1")

    with locking.connect_operation_lock(lock_path, "source busy"):
        cleanup = subprocess.Popen(
            [sys.executable, "-c", DELETE_MESSAGE_PROBE, str(store.path), "m1"],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert cleanup.stdout is not None
            assert cleanup.stdout.readline().strip() == "waiting"
            with pytest.raises(subprocess.TimeoutExpired):
                cleanup.wait(timeout=0.2)
        except Exception:
            cleanup.kill()
            cleanup.wait(timeout=10)
            raise

    assert cleanup.stdout is not None
    assert cleanup.stdout.readline().strip() == "deleted"
    assert cleanup.wait(timeout=10) == 0
    assert store.has_message("m1") is False


def test_delete_message_bounds_suppression_when_source_time_conversion_overflows(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    assert store.add_message(
        message_id="overflowing-source-time",
        thread_id=None,
        sender="a@b.com",
        sender_name=None,
        subject="Malformed source time",
        received_at="0001-01-01T00:00:00+23:59",
    )
    now = datetime(2026, 9, 1, 12, tzinfo=UTC)

    assert store.delete_message("overflowing-source-time", now=now)

    with store.connection() as db:
        expires_at = db.execute("SELECT expires_at FROM suppressed_messages").fetchone()[0]
    assert expires_at == (now + timedelta(days=MAX_RETENTION_DAYS)).isoformat()


def test_clear_messages_preserves_mailbox_and_outbound_state(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    store.set_state("200", datetime(2026, 9, 1, tzinfo=UTC))
    for message_id in ("m1", "m2"):
        store.add_message(
            message_id=message_id,
            thread_id=None,
            sender="a@b.com",
            sender_name=None,
            subject=message_id,
            received_at="2026-09-01T00:00:00+00:00",
        )
    with store.connection() as db:
        db.execute(
            """INSERT INTO outbound_sends(
                dedupe_key, recipient, subject, gmail_message_id, sent_at
            ) VALUES ('monthly-hours:2026-08', 'owner@example.com', 'Hours', 'sent-id', ?)""",
            (datetime(2026, 9, 1, tzinfo=UTC).isoformat(),),
        )

    assert store.clear_messages(now=datetime(2026, 9, 1, 12, tzinfo=UTC)) == 2
    assert store.recent(10) == []
    assert store.state() == ("200", "2026-09-01T00:00:00+00:00")
    assert store.outbound_status("monthly-hours:2026-08") == "sent"
    assert not store.add_message(
        message_id="m2",
        thread_id=None,
        sender="a@b.com",
        sender_name=None,
        subject="Rediscovered",
        received_at="2026-09-01T00:00:00+00:00",
    )


def test_manual_delete_suppression_overlaps_maximum_retention_boundary(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    received_at = datetime(2020, 1, 1, tzinfo=UTC)
    expires_at = received_at + timedelta(days=MAX_RETENTION_DAYS)
    store.add_message(
        message_id="boundary-message",
        thread_id=None,
        sender="a@b.com",
        sender_name=None,
        subject="Boundary",
        received_at=received_at.isoformat(),
    )

    assert store.delete_message("boundary-message", now=received_at)
    assert store.purge(MAX_RETENTION_DAYS, now=expires_at) == 0
    assert not store.add_message(
        message_id="boundary-message",
        thread_id=None,
        sender="a@b.com",
        sender_name=None,
        subject="Boundary replay",
        received_at=received_at.isoformat(),
    )


def test_retry_is_not_immediately_due(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    store.add_message(
        message_id="m1",
        thread_id=None,
        sender="a@b.com",
        sender_name=None,
        subject="S",
        received_at=datetime.now(UTC).isoformat(),
    )
    store.record_failure("m1", "safe error", 0)
    assert store.pending() == []
    assert len(store.pending(now=datetime.now(UTC) + timedelta(minutes=6))) == 1


def test_analysis_request_and_server_retry_delay_survive_reopen(tmp_path: Path) -> None:
    database = tmp_path / "db.sqlite3"
    store = Store(database)
    store.initialize()
    store.add_message(
        message_id="m1",
        thread_id=None,
        sender="a@b.com",
        sender_name=None,
        subject="S",
        received_at="2026-08-29T12:00:00+00:00",
    )
    now = datetime(2026, 8, 29, 13, 0, tzinfo=UTC)

    first = store.reserve_analysis_request("m1", 20_000, now)
    reopened = Store(database)
    reopened.initialize()
    second = reopened.reserve_analysis_request("m1", 99_999, now + timedelta(hours=1))

    assert second == first
    reopened.record_analysis_failure(
        "m1",
        "Inference gateway error: capacity_limited",
        0,
        retryable=True,
        error_code="capacity_limited",
        retry_after_seconds=90,
        now=now,
    )
    assert reopened.pending(now=now + timedelta(seconds=89)) == []
    due = reopened.pending(now=now + timedelta(seconds=90))[0]
    assert due.analysis_request_id == first.request_id
    recent = reopened.recent(1)[0]
    assert recent["analysis_retryable"] == 1
    assert recent["analysis_error_code"] == "capacity_limited"
    assert recent["analysis_retry_after_seconds"] == 90


def test_permanent_analysis_failure_requires_explicit_requeue(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    store.add_message(
        message_id="m1",
        thread_id=None,
        sender="a@b.com",
        sender_name=None,
        subject="S",
        received_at="2026-08-29T12:00:00+00:00",
    )
    first = store.reserve_analysis_request("m1", 20_000)
    store.record_analysis_failure(
        "m1",
        "Inference gateway error: forbidden",
        0,
        retryable=False,
        error_code="forbidden",
    )

    assert store.pending(now=datetime.now(UTC) + timedelta(days=365)) == []
    assert store.notification_intents()[0].kind == "fallback"
    assert store.requeue_analysis("m1") == "requeued"
    assert store.notification_intents()[0].kind == "fallback"
    assert store.pending()[0].analysis_request_id is None
    with pytest.raises(RuntimeError, match="permanently paused"):
        store.requeue_analysis("m1")
    second = store.reserve_analysis_request("m1", 20_000)
    assert second.request_id != first.request_id


def test_initialize_migrates_current_schema_without_losing_messages(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state" / "watcher.sqlite3"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as db:
        db.executescript(
            """
            CREATE TABLE mailbox_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                history_id TEXT NOT NULL,
                last_success_at TEXT NOT NULL
            );
            CREATE TABLE messages (
                message_id TEXT PRIMARY KEY,
                thread_id TEXT,
                sender TEXT NOT NULL,
                sender_name TEXT,
                subject TEXT NOT NULL,
                received_at TEXT NOT NULL,
                discovered_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_retry_at TEXT,
                fallback_notified_at TEXT,
                notified_at TEXT,
                category TEXT,
                priority TEXT,
                summary TEXT,
                action_required INTEGER,
                suggested_action TEXT,
                deadline_text TEXT,
                deadline_iso TEXT,
                confidence REAL,
                last_error TEXT
            );
            CREATE INDEX idx_messages_pending ON messages(status, next_retry_at);
            CREATE TABLE suppressed_messages (
                message_key TEXT PRIMARY KEY CHECK (length(message_key) = 64),
                expires_at TEXT NOT NULL
            );
            CREATE TABLE message_attachments (
                message_id TEXT NOT NULL,
                part_id TEXT NOT NULL,
                attachment_id TEXT,
                filename TEXT NOT NULL,
                media_type TEXT NOT NULL,
                byte_size INTEGER NOT NULL,
                position INTEGER NOT NULL,
                PRIMARY KEY (message_id, part_id),
                UNIQUE (message_id, position)
            );
            CREATE TABLE outbound_sends (
                dedupe_key TEXT PRIMARY KEY,
                recipient TEXT NOT NULL,
                subject TEXT NOT NULL,
                gmail_message_id TEXT NOT NULL,
                sent_at TEXT NOT NULL
            );
            INSERT INTO mailbox_state(id, history_id, last_success_at)
            VALUES (1, 'legacy-cursor', '2026-07-18T14:01:00+00:00');
            INSERT INTO messages(
                message_id, sender, subject, received_at, discovered_at
            ) VALUES (
                'legacy-message', 'trusted@example.com', 'Legacy',
                '2026-07-18T14:00:00+00:00', '2026-07-18T14:01:00+00:00'
            );
            INSERT INTO message_attachments(
                message_id, part_id, attachment_id, filename, media_type,
                byte_size, position
            ) VALUES (
                'legacy-message', '2', 'legacy-attachment', 'legacy.pdf',
                'application/pdf', 42, 0
            );
            """
        )
        db.execute(
            "INSERT INTO suppressed_messages(message_key, expires_at) VALUES (?, ?)",
            (
                hashlib.sha256(b"legacy-deleted-message").hexdigest(),
                "2030-01-01T00:00:00+00:00",
            ),
        )
        db.execute("PRAGMA user_version = 2")
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2

    Store(database).initialize()

    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
        row = db.execute(
            """SELECT status, analysis_at, provider, account_id, provider_message_id
            FROM messages WHERE message_id = 'legacy-message'"""
        ).fetchone()
        state = db.execute(
            """SELECT provider, account_id, cursor
            FROM mailbox_state"""
        ).fetchall()
        attachment_table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_attachments'"
        ).fetchone()
        connect_table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='connect_attachment_jobs'"
        ).fetchone()
        automation_tables = db.execute(
            """SELECT name FROM sqlite_master
            WHERE type='table' AND name IN (
                'automation_runs', 'automation_events', 'automation_extraction_payloads',
                'automation_proposal_payloads'
            )
            ORDER BY name"""
        ).fetchall()
        suppression_table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='suppressed_messages'"
        ).fetchone()
        account = db.execute(
            """SELECT provider, account_id, display_name, address, active
            FROM mail_accounts"""
        ).fetchone()
    assert "analysis_at" in columns
    assert row == (
        "pending",
        None,
        "gmail",
        "gmail-default",
        "legacy-message",
    )
    assert state == [("gmail", "gmail-default", "legacy-cursor")]
    assert attachment_table == (1,)
    assert connect_table == (1,)
    assert automation_tables == [
        ("automation_events",),
        ("automation_extraction_payloads",),
        ("automation_proposal_payloads",),
        ("automation_runs",),
    ]
    assert suppression_table == (1,)
    assert account == ("gmail", "gmail-default", "Gmail", None, 1)
    migrated = Store(database)
    assert migrated.attachment("legacy-message", "2").filename == "legacy.pdf"
    assert migrated.has_seen_message("legacy-deleted-message")


def test_initialize_migrates_v1_outbound_schema_without_losing_sends(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state" / "watcher.sqlite3"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as db:
        db.executescript(
            """
            PRAGMA user_version = 1;
            CREATE TABLE outbound_sends (
                dedupe_key TEXT PRIMARY KEY,
                recipient TEXT NOT NULL,
                subject TEXT NOT NULL,
                gmail_message_id TEXT NOT NULL,
                sent_at TEXT NOT NULL
            );
            INSERT INTO outbound_sends(
                dedupe_key, recipient, subject, gmail_message_id, sent_at
            ) VALUES (
                'monthly-hours:2026-07', 'maria@example.com', 'Subject',
                'gmail-id', '2026-08-01T12:00:00+00:00'
            );
            """
        )

    Store(database).initialize()

    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        sent = db.execute(
            "SELECT gmail_message_id FROM outbound_sends WHERE dedupe_key = ?",
            ("monthly-hours:2026-07",),
        ).fetchone()
        reservation_table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='outbound_reservations'"
        ).fetchone()
    assert sent == ("gmail-id",)
    assert reservation_table == (1,)


def test_attachment_inventory_replaces_in_order_and_is_purged_with_message(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    store.add_message(
        message_id="m1",
        thread_id=None,
        sender="a@b.com",
        sender_name=None,
        subject="Documents",
        received_at="2026-07-18T14:00:00+00:00",
    )
    store.replace_attachments(
        "m1",
        (
            AttachmentDescriptor("10", "gmail-b", "contract.pdf", "application/pdf", 20, 1),
            AttachmentDescriptor("2", "gmail-a", "invoice.pdf", "application/pdf", 10, 0),
        ),
    )

    assert store.recent(1)[0]["attachments"] == [
        {
            "part_id": "2",
            "attachment_id": "gmail-a",
            "filename": "invoice.pdf",
            "media_type": "application/pdf",
            "byte_size": 10,
        },
        {
            "part_id": "10",
            "attachment_id": "gmail-b",
            "filename": "contract.pdf",
            "media_type": "application/pdf",
            "byte_size": 20,
        },
    ]
    assert store.attachment("m1", "2") == AttachmentDescriptor(
        "2", "gmail-a", "invoice.pdf", "application/pdf", 10, 0
    )
    with pytest.raises(KeyError):
        store.attachment("m1", "missing")

    store.replace_attachments(
        "m1",
        (AttachmentDescriptor("4", None, "notes.txt", "text/plain", 5, 0),),
    )
    assert [item["part_id"] for item in store.recent(1)[0]["attachments"]] == ["4"]

    old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    with store.connection() as db:
        db.execute("UPDATE messages SET received_at = ?", (old,))
    assert store.purge(1) == 1
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM message_attachments").fetchone()[0] == 0


def test_attachment_inventory_rejects_unknown_message_without_partial_rows(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()

    with pytest.raises(KeyError):
        store.replace_attachments(
            "missing",
            (AttachmentDescriptor("1", None, "invoice.pdf", "application/pdf", 10, 0),),
        )

    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM message_attachments").fetchone()[0] == 0


def seed_pdf_attachment(store: Store) -> None:
    store.add_message(
        message_id="m1",
        thread_id=None,
        sender="a@b.com",
        sender_name=None,
        subject="Document",
        received_at="2026-08-29T12:00:00+00:00",
    )
    store.replace_attachments(
        "m1",
        (AttachmentDescriptor("2", "gmail-a", "invoice.pdf", "application/pdf", 20, 0),),
    )


def create_connect_job(store: Store, job_id: str) -> None:
    store.create_connect_job(
        job_id=job_id,
        message_id="m1",
        part_id="2",
        capability_id="document.summarize",
        capability_version="1.0",
        provider_app_id="alternate-provider",
        provider_instance_id="11111111-1111-4111-8111-111111111111",
        input_artifact_id="22222222-2222-4222-8222-222222222222",
        input_media_type="application/pdf",
        input_byte_size=20,
        input_sha256="a" * 64,
    )


def create_v2_connect_job(
    store: Store,
    job_id: str = "33333333-3333-4333-8333-333333333333",
    *,
    capability_id: str = "document.translate",
    provider_app_id: str = "translation-provider",
    provider_app_version: str = "0.1.0",
    provider_instance_id: str = "11111111-1111-4111-8111-111111111111",
    artifact_id: str = "22222222-2222-4222-8222-222222222222",
    input_byte_size: int = 20,
    input_sha256: str = "a" * 64,
    parameters: dict[str, object] | None = None,
    now: datetime | None = None,
) -> bytes:
    parameter_values = {"target-language": "Spanish"} if parameters is None else parameters
    request = {
        "protocol_version": 2,
        "job_id": job_id,
        "capability": {"id": capability_id, "version": "1.0"},
        "inputs": [
            {
                "artifact_id": artifact_id,
                "media_type": "application/pdf",
                "byte_size": input_byte_size,
                "sha256": input_sha256,
                "display_name": "invoice.pdf",
                "source_app_id": "email-watcher",
            }
        ],
        "parameters": parameter_values,
    }
    request_json = json.dumps(
        request,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    store.create_connect_job(
        job_id=job_id,
        message_id="m1",
        part_id="2",
        protocol_version=2,
        capability_id=capability_id,
        capability_version="1.0",
        provider_app_id=provider_app_id,
        provider_app_version=provider_app_version,
        provider_instance_id=provider_instance_id,
        input_artifact_id=artifact_id,
        input_media_type="application/pdf",
        input_byte_size=input_byte_size,
        input_sha256=input_sha256,
        input_display_name="invoice.pdf",
        source_app_id="email-watcher",
        request_json=request_json,
        now=now,
    )
    return request_json


def v2_result() -> tuple[dict[str, object], tuple[bytes, bytes]]:
    translated = b"Translated invoice: $1,247.17"
    empty = b""
    outputs = (
        {
            "artifact_id": "44444444-4444-4444-8444-444444444444",
            "media_type": "text/plain",
            "display_name": "invoice-es.txt",
            "byte_size": len(translated),
            "sha256": hashlib.sha256(translated).hexdigest(),
            "payload_base64": base64.b64encode(translated).decode(),
        },
        {
            "artifact_id": "55555555-5555-4555-8555-555555555555",
            "media_type": "text/plain",
            "display_name": "empty.txt",
            "byte_size": 0,
            "sha256": hashlib.sha256(empty).hexdigest(),
            "payload_base64": "",
        },
    )
    return {"outputs": list(outputs)}, (translated, empty)


def downgrade_connect_jobs_to_v5(database: Path) -> None:
    legacy_columns = """
        job_id, message_id, part_id, capability_id, capability_version,
        provider_app_id, provider_instance_id, input_artifact_id,
        input_media_type, input_byte_size, input_sha256, status,
        output_artifact_id, output_media_type, output_byte_size, output_sha256,
        summary_version, summary_text, warnings_json,
        error_code, error_message, error_retryable, created_at, updated_at
    """
    with sqlite3.connect(database) as db:
        db.execute("DROP TRIGGER messages_delete_connect_attachment_jobs")
        db.execute("DROP INDEX idx_connect_attachment_jobs_lookup")
        db.execute("DROP INDEX idx_connect_attachment_jobs_active")
        db.execute("ALTER TABLE connect_attachment_jobs RENAME TO connect_attachment_jobs_v6")
        db.execute(
            f"CREATE TABLE connect_attachment_jobs AS "
            f"SELECT {legacy_columns} FROM connect_attachment_jobs_v6"
        )
        db.execute("DROP TABLE connect_attachment_jobs_v6")
        db.execute("PRAGMA user_version = 5")


def test_initialize_migrates_v5_connect_jobs_without_losing_terminal_state(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state" / "watcher.sqlite3"
    store = Store(database)
    store.initialize()
    seed_pdf_attachment(store)
    job_id = "33333333-3333-4333-8333-333333333333"
    create_connect_job(store, job_id)
    failed = store.transition_connect_job(
        job_id=job_id,
        expected_state="requested",
        next_state="failed",
        provider_app_id="alternate-provider",
        provider_instance_id="11111111-1111-4111-8111-111111111111",
        error={"code": "PDF_MALFORMED", "message": "Invalid PDF", "retryable": False},
    )
    downgrade_connect_jobs_to_v5(database)

    Store(database).initialize()

    reopened = Store(database)
    restored = reopened.connect_job(job_id)
    assert restored is not None
    assert restored.protocol_version == 1
    assert restored.status == failed.status
    assert restored.error_code == "PDF_MALFORMED"
    assert restored.error_message == "Invalid PDF"
    assert restored.error_retryable == 0
    assert restored.source_app_id == "email-watcher"
    assert restored.provider_app_version is None
    assert restored.invocation_fingerprint == "v1"
    assert restored.request_json is None
    assert restored.result_json is None
    assert restored.result_metadata_json is None
    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'connect_attachment_jobs_v5'"
            ).fetchone()
            is None
        )
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'trigger' "
            "AND name = 'messages_delete_connect_attachment_jobs'"
        ).fetchone() == (1,)


def test_failed_v5_connect_migration_rolls_back_without_losing_legacy_rows(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state" / "watcher.sqlite3"
    store = Store(database)
    store.initialize()
    seed_pdf_attachment(store)
    job_id = "33333333-3333-4333-8333-333333333333"
    create_connect_job(store, job_id)
    downgrade_connect_jobs_to_v5(database)
    with sqlite3.connect(database) as db:
        db.execute(
            "UPDATE connect_attachment_jobs SET input_sha256 = 'invalid' WHERE job_id = ?",
            (job_id,),
        )

    with pytest.raises(sqlite3.IntegrityError):
        Store(database).initialize()

    with sqlite3.connect(database) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(connect_attachment_jobs)")}
        assert "protocol_version" not in columns
        assert db.execute("SELECT job_id FROM connect_attachment_jobs").fetchall() == [(job_id,)]
        assert db.execute("PRAGMA user_version").fetchone()[0] == 5
        assert (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'connect_attachment_jobs_v5'"
            ).fetchone()
            is None
        )


def test_connect_v2_request_and_generic_outputs_survive_reopen(tmp_path: Path) -> None:
    database = tmp_path / "state" / "watcher.sqlite3"
    store = Store(database)
    store.initialize()
    seed_pdf_attachment(store)
    job_id = "33333333-3333-4333-8333-333333333333"
    request_json = create_v2_connect_job(store, job_id)
    result, payloads = v2_result()
    lookup = {
        "message_id": "m1",
        "part_id": "2",
        "capability_id": "document.translate",
        "capability_version": "1.0",
    }
    v2_lookup = {
        **lookup,
        "protocol_version": 2,
        "provider_app_id": "translation-provider",
        "provider_app_version": "0.1.0",
        "provider_instance_id": "11111111-1111-4111-8111-111111111111",
        "request_json": request_json,
    }
    assert store.active_connect_job(**lookup) is None
    assert store.active_connect_job(**v2_lookup) is not None
    store.transition_connect_job(
        job_id=job_id,
        expected_state="requested",
        next_state="accepted",
        provider_app_id="translation-provider",
        provider_instance_id="11111111-1111-4111-8111-111111111111",
    )
    store.transition_connect_job(
        job_id=job_id,
        expected_state="accepted",
        next_state="processing",
        provider_app_id="translation-provider",
        provider_instance_id="11111111-1111-4111-8111-111111111111",
    )
    completed = store.transition_connect_job(
        job_id=job_id,
        expected_state="processing",
        next_state="completed",
        provider_app_id="translation-provider",
        provider_instance_id="11111111-1111-4111-8111-111111111111",
        result=result,
    )

    reopened = Store(database)
    reopened.initialize()
    restored = reopened.connect_job(job_id)
    assert restored == completed
    assert restored is not None
    assert restored.request_json == request_json
    assert restored.provider_app_version == "0.1.0"
    assert restored.input_display_name == "invoice.pdf"
    assert restored.source_app_id == "email-watcher"
    assert reopened.completed_connect_job(**lookup) is None
    assert reopened.completed_connect_job(**v2_lookup) == restored
    outputs = reopened.completed_connect_outputs(restored)
    assert tuple(output.payload for output in outputs) == payloads
    assert [output.byte_size for output in outputs] == [len(payloads[0]), 0]

    projected = reopened.recent(1)[0]["attachments"][0]["capability_results"][0]
    assert projected == {
        "job_id": job_id,
        "capability_id": "document.translate",
        "capability_version": "1.0",
        "status": "completed",
        "updated_at": completed.updated_at,
        "protocol_version": 2,
        "provider": {
            "app_id": "translation-provider",
            "version": "0.1.0",
            "instance_id": "11111111-1111-4111-8111-111111111111",
        },
        "parameters": {"target-language": "Spanish"},
        "outputs": [output.metadata() for output in outputs],
    }
    assert "payload_base64" not in json.dumps(projected)


def test_connect_v2_active_identity_scopes_protocol_provider_and_parameters(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    seed_pdf_attachment(store)
    create_connect_job(store, "33333333-3333-4333-8333-333333333333")
    spanish_request = create_v2_connect_job(
        store,
        "44444444-4444-4444-8444-444444444444",
        capability_id="document.summarize",
        parameters={"target-language": "Spanish"},
    )
    french_request = create_v2_connect_job(
        store,
        "55555555-5555-4555-8555-555555555555",
        capability_id="document.summarize",
        parameters={"target-language": "French"},
    )
    other_provider_request = create_v2_connect_job(
        store,
        "66666666-6666-4666-8666-666666666666",
        capability_id="document.summarize",
        provider_app_id="other-provider",
        provider_instance_id="99999999-9999-4999-8999-999999999999",
        parameters={"target-language": "Spanish"},
    )

    base_lookup = {
        "message_id": "m1",
        "part_id": "2",
        "capability_id": "document.summarize",
        "capability_version": "1.0",
    }
    assert store.active_connect_job(**base_lookup).job_id == (  # type: ignore[union-attr]
        "33333333-3333-4333-8333-333333333333"
    )
    for request_json, provider_app_id, provider_instance_id, expected_job_id in (
        (
            spanish_request,
            "translation-provider",
            "11111111-1111-4111-8111-111111111111",
            "44444444-4444-4444-8444-444444444444",
        ),
        (
            french_request,
            "translation-provider",
            "11111111-1111-4111-8111-111111111111",
            "55555555-5555-4555-8555-555555555555",
        ),
        (
            other_provider_request,
            "other-provider",
            "99999999-9999-4999-8999-999999999999",
            "66666666-6666-4666-8666-666666666666",
        ),
    ):
        restored = store.active_connect_job(
            **base_lookup,
            protocol_version=2,
            provider_app_id=provider_app_id,
            provider_app_version="0.1.0",
            provider_instance_id=provider_instance_id,
            request_json=request_json,
        )
        assert restored is not None
        assert restored.job_id == expected_job_id

    create_v2_connect_job(
        store,
        "77777777-7777-4777-8777-777777777777",
        capability_id="document.summarize",
        parameters={"target-language": "Spanish"},
    )
    assert store.connect_job("44444444-4444-4444-8444-444444444444") is not None
    assert store.connect_job("77777777-7777-4777-8777-777777777777") is None

    projected = store.recent(1)[0]["attachments"][0]["capability_results"]
    v2_results = [result for result in projected if result.get("protocol_version") == 2]
    assert {result["job_id"] for result in v2_results} == {
        "44444444-4444-4444-8444-444444444444",
        "55555555-5555-4555-8555-555555555555",
        "66666666-6666-4666-8666-666666666666",
    }
    assert {json.dumps(result["parameters"], sort_keys=True) for result in v2_results} == {
        '{"target-language": "French"}',
        '{"target-language": "Spanish"}',
    }


def test_connect_v2_enqueue_persists_dispatch_and_deduplicates_active_identity(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    seed_pdf_attachment(store)
    created_at = datetime(2026, 9, 8, 12, tzinfo=UTC)
    first_id = "33333333-3333-4333-8333-333333333333"
    create_v2_connect_job(store, first_id, now=created_at)
    create_v2_connect_job(
        store,
        "44444444-4444-4444-8444-444444444444",
        now=created_at + timedelta(minutes=1),
    )

    dispatch = store.connect_dispatch(first_id)
    assert dispatch is not None
    assert dispatch.state == "waiting"
    assert dispatch.submission_possible is False
    assert dispatch.source_available is True
    assert dispatch.admission_deadline == (created_at + CONNECT_QUEUE_ADMISSION_WINDOW).isoformat()
    assert store.connect_job("44444444-4444-4444-8444-444444444444") is None
    with pytest.raises(sqlite3.IntegrityError, match="identity already exists"):
        create_v2_connect_job(store, first_id, now=created_at + timedelta(minutes=2))


def test_connect_v2_lane_cap_checks_replay_before_boundary(tmp_path: Path) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    seed_pdf_attachment(store)
    created_at = datetime(2026, 9, 8, 12, tzinfo=UTC)
    job_ids = []
    for index in range(CONNECT_QUEUE_MAX_JOBS):
        job_id = f"00000000-0000-4000-8000-{index:012d}"
        job_ids.append(job_id)
        create_v2_connect_job(
            store,
            job_id,
            parameters={"sequence": index},
            now=created_at,
        )

    create_v2_connect_job(
        store,
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        parameters={"sequence": 0},
        now=created_at + timedelta(minutes=1),
    )
    assert store.connect_job("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa") is None
    with pytest.raises(ConnectQueueFull, match="queue is full"):
        create_v2_connect_job(
            store,
            "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            parameters={"sequence": CONNECT_QUEUE_MAX_JOBS},
            now=created_at + timedelta(minutes=1),
        )

    replacement_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    create_v2_connect_job(
        store,
        replacement_id,
        parameters={"sequence": CONNECT_QUEUE_MAX_JOBS + 1},
        now=created_at + CONNECT_QUEUE_ADMISSION_WINDOW,
    )
    assert store.connect_job(replacement_id) is not None
    assert all(store.connect_job(job_id).status == "failed" for job_id in job_ids)  # type: ignore[union-attr]


def test_expired_waiting_tail_is_failed_behind_claimed_head(tmp_path: Path) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    seed_pdf_attachment(store)
    created_at = datetime(2026, 9, 8, 12, tzinfo=UTC)
    first_id = "33333333-3333-4333-8333-333333333333"
    second_id = "44444444-4444-4444-8444-444444444444"
    create_v2_connect_job(store, first_id, parameters={"sequence": 1}, now=created_at)
    create_v2_connect_job(store, second_id, parameters={"sequence": 2}, now=created_at)

    claimed = store.claim_connect_lane_head(
        provider_app_id="translation-provider",
        provider_instance_id="11111111-1111-4111-8111-111111111111",
        now=created_at,
    )
    assert claimed is not None and claimed[0].job_id == first_id
    assert claimed[1].state == "dispatching"
    assert claimed[1].submission_possible is True
    assert claimed[1].attempt_count == 1
    recovered = store.claim_connect_lane_head(
        provider_app_id="translation-provider",
        provider_instance_id="11111111-1111-4111-8111-111111111111",
        now=created_at + timedelta(seconds=1),
    )
    assert recovered is not None and recovered[0].job_id == first_id
    expired = store.expire_waiting_connect_jobs(now=created_at + CONNECT_QUEUE_ADMISSION_WINDOW)

    assert expired == (second_id,)
    assert store.connect_dispatch(first_id).state == "reconciling"  # type: ignore[union-attr]
    assert store.connect_dispatch(first_id).submission_possible is True  # type: ignore[union-attr]
    assert store.connect_job(first_id).status == "requested"  # type: ignore[union-attr]
    assert store.connect_dispatch(second_id).state == "terminal"  # type: ignore[union-attr]
    assert store.connect_job(second_id).error_code == (  # type: ignore[union-attr]
        "connect_queue_deadline_exceeded"
    )


def test_lane_claim_honors_due_time_deadline_and_expected_head(tmp_path: Path) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    seed_pdf_attachment(store)
    created_at = datetime(2026, 9, 8, 12, tzinfo=UTC)
    first_id = "33333333-3333-4333-8333-333333333333"
    second_id = "44444444-4444-4444-8444-444444444444"
    create_v2_connect_job(store, first_id, parameters={"sequence": 1}, now=created_at)
    create_v2_connect_job(store, second_id, parameters={"sequence": 2}, now=created_at)

    assert (
        store.claim_connect_lane_head(
            provider_app_id="translation-provider",
            provider_instance_id="11111111-1111-4111-8111-111111111111",
            expected_job_id=second_id,
            now=created_at,
        )
        is None
    )
    assert store.connect_dispatch(first_id).state == "waiting"  # type: ignore[union-attr]
    assert store.connect_dispatch(second_id).state == "waiting"  # type: ignore[union-attr]

    with store.connection() as db:
        db.execute(
            """UPDATE connect_job_dispatch
            SET state = 'reconciling', submission_possible = 1, next_attempt_at = ?
            WHERE job_id = ?""",
            ((created_at + timedelta(seconds=30)).isoformat(), first_id),
        )
    assert (
        store.claim_connect_lane_head(
            provider_app_id="translation-provider",
            provider_instance_id="11111111-1111-4111-8111-111111111111",
            now=created_at,
        )
        is None
    )
    assert (
        store.claim_connect_lane_head(
            provider_app_id="translation-provider",
            provider_instance_id="11111111-1111-4111-8111-111111111111",
            expected_job_id=first_id,
            now=created_at,
        )
        is None
    )
    assert store.connect_dispatch(first_id).next_attempt_at is not None  # type: ignore[union-attr]
    due = store.claim_connect_lane_head(
        provider_app_id="translation-provider",
        provider_instance_id="11111111-1111-4111-8111-111111111111",
        expected_job_id=first_id,
        now=created_at + timedelta(seconds=30),
    )
    assert due is not None and due[0].job_id == first_id
    assert due[1].state == "reconciling"
    assert due[1].next_attempt_at is None

    expired_at = created_at + CONNECT_QUEUE_ADMISSION_WINDOW
    expired_claim = store.claim_connect_lane_head(
        provider_app_id="translation-provider",
        provider_instance_id="11111111-1111-4111-8111-111111111111",
        expected_job_id=second_id,
        now=expired_at,
    )
    assert expired_claim is None
    assert store.connect_job(second_id).status == "failed"  # type: ignore[union-attr]
    assert store.connect_job(second_id).error_code == (  # type: ignore[union-attr]
        "connect_queue_deadline_exceeded"
    )
    assert store.connect_dispatch(second_id).state == "terminal"  # type: ignore[union-attr]


def test_deferred_lane_head_is_not_due_early_and_preserves_bounded_diagnostic(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    seed_pdf_attachment(store)
    created_at = datetime(2026, 9, 8, 12, tzinfo=UTC)
    job_id = "33333333-3333-4333-8333-333333333333"
    create_v2_connect_job(store, job_id, now=created_at)
    claimed = store.claim_connect_lane_head(
        provider_app_id="translation-provider",
        provider_instance_id="11111111-1111-4111-8111-111111111111",
        now=created_at,
    )
    assert claimed is not None

    deferred = store.defer_connect_job(
        job_id=job_id,
        expected_dispatch_state="dispatching",
        next_dispatch_state="waiting",
        error_code="PROVIDER_BUSY",
        error_message="busy",
        delay_seconds=2,
        now=created_at,
    )

    assert deferred.next_attempt_at == (created_at + timedelta(seconds=2)).isoformat()
    assert deferred.last_error_code == "PROVIDER_BUSY"
    assert deferred.last_error_message == "busy"
    assert store.due_connect_lane_heads(now=created_at + timedelta(seconds=1)) == ()
    assert [
        job.job_id
        for job in store.due_connect_lane_heads(now=created_at + timedelta(seconds=2))
    ] == [job_id]
    near_deadline = created_at + CONNECT_QUEUE_ADMISSION_WINDOW - timedelta(seconds=1)
    claimed_again = store.claim_connect_lane_head(
        provider_app_id="translation-provider",
        provider_instance_id="11111111-1111-4111-8111-111111111111",
        now=near_deadline,
    )
    assert claimed_again is not None
    clamped = store.defer_connect_job(
        job_id=job_id,
        expected_dispatch_state="dispatching",
        next_dispatch_state="waiting",
        error_code="PROVIDER_BUSY",
        error_message="still busy",
        delay_seconds=30,
        now=near_deadline,
    )
    assert clamped.next_attempt_at == (created_at + CONNECT_QUEUE_ADMISSION_WINDOW).isoformat()


def test_due_connect_lane_heads_returns_only_authoritative_head_per_provider(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    seed_pdf_attachment(store)
    created_at = datetime(2026, 9, 8, 12, tzinfo=UTC)
    first_id = "33333333-3333-4333-8333-333333333333"
    second_id = "44444444-4444-4444-8444-444444444444"
    other_lane_id = "55555555-5555-4555-8555-555555555555"
    create_v2_connect_job(store, first_id, parameters={"sequence": 1}, now=created_at)
    create_v2_connect_job(store, second_id, parameters={"sequence": 2}, now=created_at)
    create_v2_connect_job(
        store,
        other_lane_id,
        provider_instance_id="22222222-2222-4222-8222-222222222222",
        parameters={"sequence": 3},
        now=created_at,
    )

    due = store.due_connect_lane_heads(now=created_at)

    assert [job.job_id for job in due] == [first_id, other_lane_id]
    assert second_id not in {job.job_id for job in due}


def test_authoritative_provider_progress_resets_reconciliation_backoff(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    seed_pdf_attachment(store)
    job_id = "33333333-3333-4333-8333-333333333333"
    create_v2_connect_job(store, job_id)
    claimed = store.claim_connect_lane_head(
        provider_app_id="translation-provider",
        provider_instance_id="11111111-1111-4111-8111-111111111111",
    )
    assert claimed is not None
    store.defer_connect_job(
        job_id=job_id,
        expected_dispatch_state="dispatching",
        next_dispatch_state="reconciling",
        error_code="PROVIDER_UNAVAILABLE",
        error_message="lost response",
        delay_seconds=2,
    )

    accepted = store.transition_connect_job(
        job_id=job_id,
        expected_state="requested",
        next_state="accepted",
        provider_app_id="translation-provider",
        provider_instance_id="11111111-1111-4111-8111-111111111111",
    )
    dispatch = store.connect_dispatch(job_id)

    assert accepted.status == "accepted"
    assert dispatch is not None
    assert dispatch.state == "provider_owned"
    assert dispatch.reconciliation_failure_count == 0
    assert dispatch.next_attempt_at is None
    assert dispatch.last_error_code is None
    assert dispatch.last_error_message is None


def test_initialize_migrates_v2_jobs_to_fail_closed_dispatch_states(tmp_path: Path) -> None:
    database = tmp_path / "state" / "watcher.sqlite3"
    store = Store(database)
    store.initialize()
    seed_pdf_attachment(store)
    requested_id = "33333333-3333-4333-8333-333333333333"
    accepted_id = "44444444-4444-4444-8444-444444444444"
    create_v2_connect_job(store, requested_id, parameters={"sequence": 1})
    create_v2_connect_job(store, accepted_id, parameters={"sequence": 2})
    store.transition_connect_job(
        job_id=accepted_id,
        expected_state="requested",
        next_state="accepted",
        provider_app_id="translation-provider",
        provider_instance_id="11111111-1111-4111-8111-111111111111",
    )
    with store.connection() as db:
        db.execute("DROP TRIGGER connect_jobs_delete_dispatch")
        db.execute("DROP TABLE connect_job_dispatch")
        db.execute("PRAGMA user_version = 18")

    Store(database).initialize()

    reopened = Store(database)
    requested = reopened.connect_dispatch(requested_id)
    accepted = reopened.connect_dispatch(accepted_id)
    assert requested is not None and requested.state == "reconciling"
    assert requested.submission_possible is True
    assert accepted is not None and accepted.state == "provider_owned"
    assert accepted.highest_provider_state == "accepted"
    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_initialize_preserves_legacy_active_v2_duplicates_until_reconciled(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state" / "watcher.sqlite3"
    store = Store(database)
    store.initialize()
    seed_pdf_attachment(store)
    first_id = "33333333-3333-4333-8333-333333333333"
    second_id = "44444444-4444-4444-8444-444444444444"
    third_id = "55555555-5555-4555-8555-555555555555"
    request_json = create_v2_connect_job(store, first_id)
    duplicate_request = json.loads(request_json)
    duplicate_request["job_id"] = second_id
    duplicate_json = json.dumps(duplicate_request, separators=(",", ":")).encode()
    with store.connection() as db:
        db.execute("DROP INDEX idx_connect_attachment_jobs_active_v2")
        row = dict(
            db.execute(
                "SELECT * FROM connect_attachment_jobs WHERE job_id = ?", (first_id,)
            ).fetchone()
        )
        row["job_id"] = second_id
        row["request_json"] = duplicate_json
        columns = tuple(row)
        placeholders = ", ".join("?" for _ in columns)
        db.execute(
            f"INSERT INTO connect_attachment_jobs ({', '.join(columns)}) "
            f"VALUES ({placeholders})",
            tuple(row[column] for column in columns),
        )
        db.execute("DROP TRIGGER connect_jobs_delete_dispatch")
        db.execute("DROP TABLE connect_job_dispatch")
        db.execute("PRAGMA user_version = 18")

    Store(database).initialize()

    reopened = Store(database)
    assert reopened.connect_dispatch(first_id).state == "reconciling"  # type: ignore[union-attr]
    assert reopened.connect_dispatch(second_id).state == "reconciling"  # type: ignore[union-attr]
    create_v2_connect_job(reopened, third_id)
    assert reopened.connect_job(third_id) is None
    with reopened.connection() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type = 'index' AND name = 'idx_connect_attachment_jobs_active_v2'"
            ).fetchone()[0]
            == 0
        )

    reopened.transition_connect_job(
        job_id=second_id,
        expected_state="requested",
        next_state="failed",
        provider_app_id="translation-provider",
        provider_instance_id="11111111-1111-4111-8111-111111111111",
        error={"code": "LEGACY_DUPLICATE", "message": "Reconciled.", "retryable": False},
    )
    reopened.initialize()
    with reopened.connection() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type = 'index' AND name = 'idx_connect_attachment_jobs_active_v2'"
            ).fetchone()[0]
            == 1
        )


def test_message_delete_preserves_provider_owned_job_as_content_free_tombstone(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    seed_pdf_attachment(store)
    job_id = "33333333-3333-4333-8333-333333333333"
    create_v2_connect_job(store, job_id)
    store.transition_connect_job(
        job_id=job_id,
        expected_state="requested",
        next_state="accepted",
        provider_app_id="translation-provider",
        provider_instance_id="11111111-1111-4111-8111-111111111111",
    )

    assert store.delete_message("m1") is True

    assert store.connect_job(job_id) is not None
    dispatch = store.connect_dispatch(job_id)
    assert dispatch is not None
    assert dispatch.state == "provider_owned"
    assert dispatch.source_available is False
    with pytest.raises(KeyError):
        store.attachment("m1", "2")

    with pytest.raises(ValueError, match="result is invalid"):
        store.transition_connect_job(
            job_id=job_id,
            expected_state="accepted",
            next_state="completed",
            provider_app_id="translation-provider",
            provider_instance_id="11111111-1111-4111-8111-111111111111",
            result={"outputs": []},
        )
    assert store.connect_job(job_id).status == "accepted"  # type: ignore[union-attr]
    assert store.connect_dispatch(job_id).source_available is False  # type: ignore[union-attr]

    result, _ = v2_result()
    late_terminal = store.transition_connect_job(
        job_id=job_id,
        expected_state="accepted",
        next_state="completed",
        provider_app_id="translation-provider",
        provider_instance_id="11111111-1111-4111-8111-111111111111",
        result=result,
    )
    assert late_terminal.status == "completed"
    assert store.connect_job(job_id) is None
    assert store.connect_dispatch(job_id) is None


def test_initialize_replaces_v6_active_index_without_losing_jobs(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state" / "watcher.sqlite3"
    store = Store(database)
    store.initialize()
    seed_pdf_attachment(store)
    create_v2_connect_job(
        store,
        "44444444-4444-4444-8444-444444444444",
        capability_id="document.summarize",
        parameters={"target-language": "Spanish"},
    )
    with store.connection() as db:
        db.execute("DROP INDEX idx_connect_attachment_jobs_active")
        db.execute(
            """CREATE UNIQUE INDEX idx_connect_attachment_jobs_active
            ON connect_attachment_jobs(
                message_id, part_id, protocol_version, capability_id,
                capability_version, invocation_fingerprint
            )
            WHERE status IN ('requested', 'accepted', 'processing')"""
        )
        db.execute("PRAGMA user_version = 6")

    Store(database).initialize()
    create_v2_connect_job(
        store,
        "77777777-7777-4777-8777-777777777777",
        capability_id="document.summarize",
        parameters={"target-language": "Spanish"},
    )

    with store.connection() as db:
        index_sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' "
            "AND name = 'idx_connect_attachment_jobs_active'"
        ).fetchone()[0]
        v2_index_sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' "
            "AND name = 'idx_connect_attachment_jobs_active_v2'"
        ).fetchone()[0]
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert db.execute("SELECT COUNT(*) FROM connect_attachment_jobs").fetchone()[0] == 1
    assert "protocol_version = 1" in index_sql
    assert "protocol_version = 2" in v2_index_sql


def test_connect_v2_persists_maximum_generated_request_and_zero_byte_input(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    seed_pdf_attachment(store)
    parameters = {(f"p{index:02d}-" + "x" * 96): "😀" * 1000 for index in range(16)}
    large_request = create_v2_connect_job(
        store,
        "33333333-3333-4333-8333-333333333333",
        parameters=parameters,
    )
    assert 64 * 1024 < len(large_request) <= MAX_CONNECT_REQUEST_BYTES

    empty_request = create_v2_connect_job(
        store,
        "44444444-4444-4444-8444-444444444444",
        input_byte_size=0,
        input_sha256=hashlib.sha256(b"").hexdigest(),
        parameters={},
    )
    empty = store.connect_job("44444444-4444-4444-8444-444444444444")
    assert empty is not None
    assert empty.input_byte_size == 0
    assert empty.request_json == empty_request


def test_connect_v2_rejects_mismatched_request_and_corrupt_result(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state" / "watcher.sqlite3"
    store = Store(database)
    store.initialize()
    seed_pdf_attachment(store)
    job_id = "33333333-3333-4333-8333-333333333333"
    request_json = create_v2_connect_job(store, job_id)
    request = json.loads(request_json)
    request["job_id"] = "99999999-9999-4999-8999-999999999999"
    mismatched = json.dumps(request, separators=(",", ":")).encode()
    with pytest.raises(ValueError, match="provenance"):
        store.create_connect_job(
            job_id="66666666-6666-4666-8666-666666666666",
            message_id="m1",
            part_id="2",
            protocol_version=2,
            capability_id="document.translate",
            capability_version="1.0",
            provider_app_id="translation-provider",
            provider_app_version="0.1.0",
            provider_instance_id="11111111-1111-4111-8111-111111111111",
            input_artifact_id="22222222-2222-4222-8222-222222222222",
            input_media_type="application/pdf",
            input_byte_size=20,
            input_sha256="a" * 64,
            input_display_name="invoice.pdf",
            source_app_id="email-watcher",
            request_json=mismatched,
        )

    result, _ = v2_result()
    completed = store.transition_connect_job(
        job_id=job_id,
        expected_state="requested",
        next_state="completed",
        provider_app_id="translation-provider",
        provider_instance_id="11111111-1111-4111-8111-111111111111",
        result=result,
    )
    with store.connection() as db:
        db.execute(
            "UPDATE connect_attachment_jobs SET result_json = ? WHERE job_id = ?",
            (b'{"outputs":[]}', job_id),
        )
    corrupted = store.connect_job(job_id)
    assert corrupted is not None
    assert corrupted.result_json != completed.result_json
    projected = store.recent(1)[0]["attachments"][0]["capability_results"][0]
    assert projected["outputs"] == [
        output.metadata() for output in store.completed_connect_outputs(completed)
    ]
    with pytest.raises(RuntimeError, match="invalid"):
        store.completed_connect_outputs(corrupted)


def test_connect_job_state_result_and_integrity_survive_reopen(tmp_path: Path) -> None:
    database = tmp_path / "state" / "watcher.sqlite3"
    store = Store(database)
    store.initialize()
    seed_pdf_attachment(store)
    job_id = "33333333-3333-4333-8333-333333333333"
    create_connect_job(store, job_id)
    accepted = store.transition_connect_job(
        job_id=job_id,
        expected_state="requested",
        next_state="accepted",
        provider_app_id="alternate-provider",
        provider_instance_id="11111111-1111-4111-8111-111111111111",
    )
    assert accepted.status == "accepted"

    with pytest.raises(RuntimeError, match="expected-state"):
        store.transition_connect_job(
            job_id=job_id,
            expected_state="requested",
            next_state="processing",
            provider_app_id="alternate-provider",
            provider_instance_id="11111111-1111-4111-8111-111111111111",
        )
    assert store.connect_job(job_id).status == "accepted"  # type: ignore[union-attr]

    store.transition_connect_job(
        job_id=job_id,
        expected_state="accepted",
        next_state="processing",
        provider_app_id="alternate-provider",
        provider_instance_id="11111111-1111-4111-8111-111111111111",
    )
    warnings = [{"code": "REVIEW", "message": "Human review required."}]
    content = {
        "summary_version": "1.0",
        "text": "Exact $1,247.17 summary.",
        "warnings": warnings,
        "input_artifact": {
            "artifact_id": "22222222-2222-4222-8222-222222222222",
            "media_type": "application/pdf",
            "byte_size": 20,
            "sha256": "a" * 64,
        },
    }
    encoded = json.dumps(content, separators=(",", ":"), ensure_ascii=False).encode()
    completed = store.transition_connect_job(
        job_id=job_id,
        expected_state="processing",
        next_state="completed",
        provider_app_id="alternate-provider",
        provider_instance_id="11111111-1111-4111-8111-111111111111",
        result={
            "output": {
                "artifact_id": "44444444-4444-4444-8444-444444444444",
                "media_type": "application/vnd.local-connect.document-summary+json",
                "byte_size": len(encoded),
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "summary_version": "1.0",
                "text": "Exact $1,247.17 summary.",
                "warnings": warnings,
            }
        },
    )
    assert completed.status == "completed"

    reopened = Store(database)
    reopened.initialize()
    restored = reopened.connect_job(job_id)
    assert restored == completed
    attachment = reopened.recent(1)[0]["attachments"][0]
    assert attachment["capability_results"] == [
        {
            "job_id": job_id,
            "capability_id": "document.summarize",
            "capability_version": "1.0",
            "status": "completed",
            "updated_at": completed.updated_at,
            "summary": {
                "summary_version": "1.0",
                "text": "Exact $1,247.17 summary.",
                "warnings": [{"code": "REVIEW", "message": "Human review required."}],
            },
        }
    ]


def test_connect_job_atomic_constraints_and_single_active_request(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    seed_pdf_attachment(store)
    first_job = "33333333-3333-4333-8333-333333333333"
    create_connect_job(store, first_job)

    with pytest.raises(sqlite3.IntegrityError):
        create_connect_job(store, "44444444-4444-4444-8444-444444444444")
    assert store.connect_job(first_job).status == "requested"  # type: ignore[union-attr]

    with pytest.raises(ValueError, match="failed validation"):
        store.transition_connect_job(
            job_id=first_job,
            expected_state="requested",
            next_state="completed",
            provider_app_id="alternate-provider",
            provider_instance_id="11111111-1111-4111-8111-111111111111",
            result={
                "output": {
                    "artifact_id": None,
                    "media_type": None,
                    "byte_size": None,
                    "sha256": None,
                    "summary_version": None,
                    "text": None,
                    "warnings": [],
                }
            },
        )
    assert store.connect_job(first_job).status == "requested"  # type: ignore[union-attr]


def test_connect_job_resubmission_reset_is_atomic_and_provider_scoped(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    seed_pdf_attachment(store)
    job_id = "33333333-3333-4333-8333-333333333333"
    create_connect_job(store, job_id)
    store.transition_connect_job(
        job_id=job_id,
        expected_state="requested",
        next_state="processing",
        provider_app_id="alternate-provider",
        provider_instance_id="11111111-1111-4111-8111-111111111111",
    )

    with pytest.raises(RuntimeError, match="expected-state"):
        store.reset_connect_job_for_resubmission(
            job_id=job_id,
            expected_state="processing",
            provider_app_id="alternate-provider",
            provider_instance_id="99999999-9999-4999-8999-999999999999",
        )
    assert store.connect_job(job_id).status == "processing"  # type: ignore[union-attr]

    reset = store.reset_connect_job_for_resubmission(
        job_id=job_id,
        expected_state="processing",
        provider_app_id="alternate-provider",
        provider_instance_id="11111111-1111-4111-8111-111111111111",
    )
    assert reset.status == "requested"

    with pytest.raises(RuntimeError, match="expected-state"):
        store.reset_connect_job_for_resubmission(
            job_id=job_id,
            expected_state="processing",
            provider_app_id="alternate-provider",
            provider_instance_id="11111111-1111-4111-8111-111111111111",
        )


def test_connect_v2_resubmission_reset_updates_dispatch_and_preserves_acceptance(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    seed_pdf_attachment(store)
    job_id = "33333333-3333-4333-8333-333333333333"
    create_v2_connect_job(store, job_id)
    with store.connection() as db:
        db.execute(
            """UPDATE connect_job_dispatch
            SET state = 'reconciling', submission_possible = 1
            WHERE job_id = ?""",
            (job_id,),
        )

    reset = store.reset_connect_job_for_resubmission(
        job_id=job_id,
        expected_state="requested",
        provider_app_id="translation-provider",
        provider_instance_id="11111111-1111-4111-8111-111111111111",
    )
    dispatch = store.connect_dispatch(job_id)
    assert reset.status == "requested"
    assert dispatch is not None and dispatch.state == "waiting"
    assert dispatch.submission_possible is False

    store.transition_connect_job(
        job_id=job_id,
        expected_state="requested",
        next_state="accepted",
        provider_app_id="translation-provider",
        provider_instance_id="11111111-1111-4111-8111-111111111111",
    )
    with pytest.raises(RuntimeError, match="authoritative provider acceptance"):
        store.reset_connect_job_for_resubmission(
            job_id=job_id,
            expected_state="accepted",
            provider_app_id="translation-provider",
            provider_instance_id="11111111-1111-4111-8111-111111111111",
        )
    assert store.connect_job(job_id).status == "accepted"  # type: ignore[union-attr]
    assert store.connect_dispatch(job_id).state == "provider_owned"  # type: ignore[union-attr]
