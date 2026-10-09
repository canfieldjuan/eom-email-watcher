import base64
import hashlib
import json
import sqlite3
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from connect_automate import locking

from eom_email_watcher import db as db_module
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
)
from eom_email_watcher.db import (
    Store as ProductionStore,
)
from eom_email_watcher.mailbox import scoped_message_id
from eom_email_watcher.microsoft_calendar import MicrosoftPrincipal
from eom_email_watcher.mime import AttachmentDescriptor

CALENDAR_PRINCIPAL_KEY = "a" * 64


class Store(ProductionStore):
    """Legacy DB-test adapter that makes the mailbox epoch explicit."""

    @staticmethod
    def _test_mailbox_identity(provider: str, account_id: str) -> str:
        return hashlib.sha256(f"test-mailbox\0{provider}\0{account_id}".encode()).hexdigest()

    def add_message(self, **values: object) -> bool:
        provider = str(values.get("provider", "gmail"))
        account_id = str(values.get("account_id", "gmail-default"))
        account = self.mail_account(provider, account_id)
        if account is None:
            account = self.register_mail_account(
                provider,
                account_id,
                display_name=provider,
                address="owner@example.com",
            )
        if "mailbox_identity_key" not in values:
            identity_key = account.mailbox_identity_key or self._test_mailbox_identity(
                provider, account_id
            )
            if account.mailbox_identity_key is None:
                self.reconcile_mailbox_identity(
                    provider,
                    account_id,
                    identity_key,
                    legacy_status="replacement",
                    preserve_cursor=True,
                )
            values["mailbox_identity_key"] = identity_key
        if "admission" not in values:
            values["admission"] = db_module.AdmissionProvenance(
                kind="exact_sender",
                selector_id=f"sender:{values.get('sender', 'sender@example.com')}",
                display_name=(
                    str(values["sender_name"])
                    if values.get("sender_name") is not None
                    else None
                ),
                mailbox_identity_key=str(values["mailbox_identity_key"]),
                admitted_at="2026-09-19T12:00:00+00:00",
            )
        return super().add_message(**values)  # type: ignore[arg-type]

    def mark_analyzed(
        self,
        message_id: str,
        result: dict[str, object],
        **values: object,
    ) -> None:
        source = self.message_source(message_id)
        if source.mailbox_identity_key is not None:
            values.setdefault("mailbox_identity_key", source.mailbox_identity_key)
        values.setdefault("body_chars", None)
        values.setdefault("body_source_chars", None)
        super().mark_analyzed(message_id, result, **values)  # type: ignore[arg-type]

    def has_seen_message(
        self,
        provider_message_id: str,
        *,
        provider: str = "gmail",
        account_id: str = "gmail-default",
        mailbox_identity_key: str | None = None,
    ) -> bool:
        account = self.mail_account(provider, account_id)
        return super().has_seen_message(
            provider_message_id,
            provider=provider,
            account_id=account_id,
            mailbox_identity_key=(
                mailbox_identity_key
                if mailbox_identity_key is not None
                else account.mailbox_identity_key
                if account is not None
                else None
            ),
        )


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


def test_pending_mailbox_work_requires_current_identity_and_ignores_retry_deadline(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    old_identity = "a" * 64
    current_identity = "b" * 64
    store.reconcile_mailbox_identity(
        "gmail",
        "gmail-default",
        old_identity,
        legacy_status="replacement",
        preserve_cursor=False,
    )
    assert store.add_message(
        message_id="old-pending",
        provider="gmail",
        account_id="gmail-default",
        provider_message_id="old-pending",
        mailbox_identity_key=old_identity,
        thread_id=None,
        sender="trusted@example.com",
        sender_name="Trusted",
        subject="Old pending",
        received_at="2026-09-20T12:00:00+00:00",
    )
    assert store.has_current_pending_mailbox_work(
        "gmail", "gmail-default", old_identity
    )

    store.reconcile_mailbox_identity(
        "gmail",
        "gmail-default",
        current_identity,
        legacy_status="replacement",
        preserve_cursor=False,
    )
    assert not store.has_current_pending_mailbox_work(
        "gmail", "gmail-default", old_identity
    )
    assert store.add_message(
        message_id="current-pending",
        provider="gmail",
        account_id="gmail-default",
        provider_message_id="current-pending",
        mailbox_identity_key=current_identity,
        thread_id=None,
        sender="trusted@example.com",
        sender_name="Trusted",
        subject="Current pending",
        received_at="2026-09-20T12:00:00+00:00",
    )
    store.record_analysis_failure(
        "current-pending",
        "retry later",
        0,
        retryable=True,
        now=datetime.now(UTC),
    )

    assert [message.message_id for message in store.pending()] == ["old-pending"]
    assert store.has_current_pending_mailbox_work(
        "gmail", "gmail-default", current_identity
    )


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
    mailbox_identity_key = Store._test_mailbox_identity("microsoft365", "account-1")
    assert (
        run.source_message_key
        == hashlib.sha256(
            f"microsoft365\0account-1\0{mailbox_identity_key}\0provider-message-1".encode()
        ).hexdigest()
    )
    with store.connection() as connection:
        companion = connection.execute(
            """SELECT mailbox_identity_key FROM automation_run_source_identities
            WHERE run_id = ?""",
            (run.run_id,),
        ).fetchone()
    assert companion is not None
    assert companion["mailbox_identity_key"] == mailbox_identity_key
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
        assert (
            db.execute(
                "SELECT COUNT(*) FROM automation_proposal_payloads WHERE run_id = ?",
                (proposing.run_id,),
            ).fetchone()[0]
            == 1
        )


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
        assert (
            db.execute(
                "SELECT COUNT(*) FROM automation_calendar_writes WHERE run_id = ?",
                (proposing.run_id,),
            ).fetchone()[0]
            == 1
        )


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
        assert (
            db.execute(
                "SELECT principal_key FROM microsoft_calendar_windows WHERE account_id = ?",
                (account_id,),
            ).fetchone()[0]
            == current_key
        )
        assert (
            db.execute(
                "SELECT calendar_principal_key FROM automation_runs WHERE run_id = ?",
                (run.run_id,),
            ).fetchone()[0]
            == current_key
        )
        assert {
            str(row[0])
            for row in db.execute(
                "SELECT calendar_principal_key FROM automation_events WHERE run_id = ?",
                (run.run_id,),
            ).fetchall()
        } == {current_key}
        assert (
            db.execute(
                "SELECT calendar_principal_key FROM automation_calendar_writes WHERE run_id = ?",
                (run.run_id,),
            ).fetchone()[0]
            == current_key
        )
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


def test_selector_set_tracks_current_identity_and_replacement_increments_revision_once(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    first_identity = "1" * 64
    replacement_identity = "2" * 64

    store.reconcile_mailbox_identity("gmail", "gmail-default", first_identity)
    initial = store.gmail_label_selector_set("gmail-default")
    assert initial is not None
    assert initial.current_mailbox_identity_key == first_identity
    assert initial.revision == 0

    store.reconcile_mailbox_identity("gmail", "gmail-default", first_identity)
    assert store.gmail_label_selector_set("gmail-default") == initial

    store.reconcile_mailbox_identity("gmail", "gmail-default", replacement_identity)
    replaced = store.gmail_label_selector_set("gmail-default")
    assert replaced is not None
    assert replaced.current_mailbox_identity_key == replacement_identity
    assert replaced.revision == 1

    store.reconcile_mailbox_identity("gmail", "gmail-default", replacement_identity)
    assert store.gmail_label_selector_set("gmail-default") == replaced


def _gmail_selector_store(tmp_path: Path) -> tuple[Store, str]:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    identity = "a" * 64
    store.reconcile_mailbox_identity("gmail", "gmail-default", identity)
    return store, identity


def test_gmail_validation_schema_bump_rejects_previous_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "state" / "watcher.sqlite3"
    store = Store(database)
    store.initialize()

    with store.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 30

    monkeypatch.setattr(db_module, "SCHEMA_VERSION", 26)
    with pytest.raises(RuntimeError, match="newer than supported version 26"):
        Store(database).initialize()


def test_schema_25_migrates_validation_tables_fail_closed_without_losing_selectors(
    tmp_path: Path,
) -> None:
    store, identity = _gmail_selector_store(tmp_path)
    revision, selector = store.add_gmail_label_selector(
        "gmail-default", identity, "Label_1", "Invoices", 0
    )
    with store.connection() as db:
        db.execute("DROP TABLE gmail_label_selector_validations")
        db.execute("DROP TABLE gmail_label_validation_sets")
        db.execute("PRAGMA user_version = 25")

    migrated = Store(store.path)
    migrated.initialize()

    with migrated.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 30
        tables = {
            str(row["name"])
            for row in db.execute(
                """SELECT name FROM sqlite_schema
                WHERE type = 'table' AND name LIKE 'gmail_label_%validation%'"""
            ).fetchall()
        }
    assert tables == {
        "gmail_label_selector_validations",
        "gmail_label_validation_sets",
    }
    assert migrated.gmail_label_selectors("gmail-default") == (selector,)
    assert migrated.gmail_label_validation_snapshot("gmail-default", identity, revision) is None


def test_schema_26_migrates_current_validation_with_explicit_catalog_state(
    tmp_path: Path,
) -> None:
    store, identity = _gmail_selector_store(tmp_path)
    revision, _selector = store.add_gmail_label_selector(
        "gmail-default", identity, "Label_1", "Invoices", 0
    )
    expected = store.persist_gmail_label_validation(
        "gmail-default",
        identity,
        revision,
        (("Label_1", "Invoices", "user"),),
    )
    with store.connection() as db:
        db.execute("ALTER TABLE gmail_label_validation_sets RENAME TO validation_sets_v27")
        db.execute(
            """CREATE TABLE gmail_label_validation_sets (
                provider TEXT NOT NULL,
                account_id TEXT NOT NULL,
                mailbox_identity_key TEXT NOT NULL,
                selector_revision INTEGER NOT NULL,
                validated_at TEXT NOT NULL,
                PRIMARY KEY (provider, account_id)
            )"""
        )
        db.execute(
            """INSERT INTO gmail_label_validation_sets(
                provider, account_id, mailbox_identity_key,
                selector_revision, validated_at
            ) SELECT provider, account_id, mailbox_identity_key,
                selector_revision, validated_at
            FROM validation_sets_v27"""
        )
        db.execute("DROP TABLE validation_sets_v27")
        db.execute("PRAGMA user_version = 26")

    migrated = Store(store.path)
    migrated.initialize()

    with migrated.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 30
        assert db.execute(
            "SELECT catalog_state FROM gmail_label_validation_sets"
        ).fetchone()[0] == "current"
    assert migrated.gmail_label_validation_snapshot(
        "gmail-default", identity, revision
    ) == expected


def test_gmail_label_validation_is_revision_bound_durable_and_invalidated_by_mutation(
    tmp_path: Path,
) -> None:
    store, identity = _gmail_selector_store(tmp_path)
    revision, first = store.add_gmail_label_selector(
        "gmail-default", identity, "Label_1", "Invoices", 0
    )
    snapshot = store.persist_gmail_label_validation(
        "gmail-default",
        identity,
        revision,
        (("Label_1", "Renamed invoices", "user"),),
    )
    assert snapshot.selector_revision == revision
    assert snapshot.selectors == (
        db_module.GmailLabelSelectorValidation(
            selector_id=first.selector_id,
            label_id="Label_1",
            status="active",
            display_name="Renamed invoices",
        ),
    )

    restarted = Store(store.path)
    restarted.initialize()
    assert restarted.gmail_label_validation_snapshot(
        "gmail-default", identity, revision
    ) == snapshot

    revision, second = restarted.add_gmail_label_selector(
        "gmail-default", identity, "Label_2", "Second", revision
    )
    assert restarted.gmail_label_validation_snapshot(
        "gmail-default", identity, revision
    ) is None
    with pytest.raises(db_module.GmailLabelStoreError, match="stale_revision"):
        restarted.persist_gmail_label_validation(
            "gmail-default",
            identity,
            revision - 1,
            (("Label_1", "Invoices", "user"),),
        )

    refreshed = restarted.persist_gmail_label_validation(
        "gmail-default",
        identity,
        revision,
        (("Label_2", "System second", "system"),),
    )
    assert {item.selector_id: item for item in refreshed.selectors} == {
        first.selector_id: db_module.GmailLabelSelectorValidation(
            selector_id=first.selector_id,
            label_id="Label_1",
            status="deleted",
            display_name="Invoices",
        ),
        second.selector_id: db_module.GmailLabelSelectorValidation(
            selector_id=second.selector_id,
            label_id="Label_2",
            status="not_user",
            display_name="System second",
        ),
    }
    revision = restarted.remove_gmail_label_selector(
        "gmail-default", identity, second.selector_id, revision
    )
    assert restarted.gmail_label_validation_snapshot(
        "gmail-default", identity, revision
    ) is None


def test_gmail_label_selector_cas_bounds_and_immutable_identity(tmp_path: Path) -> None:
    store, identity = _gmail_selector_store(tmp_path)

    revision, first = store.add_gmail_label_selector(
        "gmail-default",
        identity,
        "L" * 512,
        "N" * 1024,
        0,
    )
    assert revision == 1
    assert store.gmail_label_selector_is_current(
        "gmail-default", identity, first.selector_id, first.label_id
    )

    invalid_fields = (
        ("", "Name"),
        ("L" * 513, "Name"),
        ("L2", ""),
        ("L2", "N" * 1025),
    )
    for label_id, display_name in invalid_fields:
        with pytest.raises(ValueError):
            store.add_gmail_label_selector(
                "gmail-default", identity, label_id, display_name, revision
            )
    assert store.gmail_label_selector_set("gmail-default").revision == revision  # type: ignore[union-attr]

    with pytest.raises(db_module.GmailLabelStoreError, match="stale_revision"):
        store.add_gmail_label_selector("gmail-default", identity, "stale", "Stale", 0)
    with pytest.raises(db_module.GmailLabelStoreError, match="conflict"):
        store.add_gmail_label_selector(
            "gmail-default", identity, first.label_id, "Duplicate", revision
        )

    for index in range(1, 100):
        revision, _selector = store.add_gmail_label_selector(
            "gmail-default",
            identity,
            f"Label_{index}",
            f"Label {index}",
            revision,
        )
    assert revision == 100
    assert len(store.gmail_label_selectors("gmail-default")) == 100
    with pytest.raises(db_module.GmailLabelStoreError, match="limit_exceeded"):
        store.add_gmail_label_selector(
            "gmail-default", identity, "Label_101", "Label 101", revision
        )
    assert store.gmail_label_selector_set("gmail-default").revision == 100  # type: ignore[union-attr]

    with store.connection() as db, pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "UPDATE gmail_label_selectors SET label_id='rebound' WHERE selector_id=?",
            (first.selector_id,),
        )

    with pytest.raises(db_module.GmailLabelStoreError, match="stale_revision"):
        store.remove_gmail_label_selector(
            "gmail-default", identity, first.selector_id, revision - 1
        )
    revision = store.remove_gmail_label_selector(
        "gmail-default", identity, first.selector_id, revision
    )
    assert revision == 101
    assert not store.gmail_label_selector_is_current(
        "gmail-default", identity, first.selector_id, first.label_id
    )


def test_identity_reconciliation_removes_recovery_without_advancing_mailbox_cursor(
    tmp_path: Path,
) -> None:
    store, identity = _gmail_selector_store(tmp_path)
    revision, selector = store.add_gmail_label_selector(
        "gmail-default", identity, "Label_1", "Invoices", 0
    )
    store.set_state(
        "old-history",
        provider="gmail",
        account_id="gmail-default",
        mailbox_identity_key=identity,
    )
    store.create_gmail_recovery_state(
        "gmail-default",
        identity,
        revision,
        [],
        [selector],
        10,
        20,
        "replacement-history",
    )

    replacement = "b" * 64
    store.reconcile_mailbox_identity(
        "gmail", "gmail-default", replacement, preserve_cursor=True
    )

    selector_set = store.gmail_label_selector_set("gmail-default")
    assert selector_set is not None
    assert selector_set.current_mailbox_identity_key == replacement
    assert selector_set.revision == revision + 1
    assert store.gmail_recovery_state("gmail-default") is None
    assert store.state(provider="gmail", account_id="gmail-default")[0] == "old-history"
    retained = store.gmail_label_selectors("gmail-default")
    assert retained == (selector,)
    assert store.gmail_current_label_selectors("gmail-default", replacement) == ()


def test_label_admission_and_recovery_snapshot_recheck_current_selector_grants(
    tmp_path: Path,
) -> None:
    store, identity = _gmail_selector_store(tmp_path)
    revision, selector = store.add_gmail_label_selector(
        "gmail-default", identity, "Label_1", "Invoices", 0
    )
    fabricated = db_module.GmailLabelSelectorSnapshot(
        selector_id=str(uuid.uuid4()),
        label_id="Label_2",
        display_name="Fabricated",
    )
    with pytest.raises(
        db_module.GmailLabelStoreError, match="gmail_recovery_grant_revoked"
    ):
        store.create_gmail_recovery_state(
            "gmail-default", identity, revision, [], [fabricated], 1, 2, "replacement"
        )

    revision = store.remove_gmail_label_selector(
        "gmail-default", identity, selector.selector_id, revision
    )
    assert revision == 2
    with pytest.raises(
        db_module.GmailLabelStoreError, match="gmail_recovery_grant_revoked"
    ):
        ProductionStore.add_message(
            store,
            message_id="message-1",
            provider="gmail",
            account_id="gmail-default",
            provider_message_id="provider-1",
            mailbox_identity_key=identity,
            thread_id=None,
            sender="sender@example.com",
            sender_name=None,
            subject="Revoked",
            received_at="2026-09-19T11:59:00+00:00",
            admission=db_module.AdmissionProvenance(
                kind="gmail_user_label",
                selector_id=selector.selector_id,
                display_name="Invoices",
                mailbox_identity_key=identity,
                admitted_at="2026-09-19T12:00:00+00:00",
            ),
        )
    assert not store.has_message("message-1")


def test_recovery_page_200_max_escaped_ids_is_exactly_205401_and_fits_column() -> None:
    message_ids = []
    for index in range(200):
        bits = f"{index:08b}"
        prefix = "".join('"' if bit == "0" else "\\" for bit in bits)
        message_ids.append(prefix + '"' * (512 - len(prefix)))

    encoded = db_module.encode_gmail_recovery_page(message_ids)

    assert len(encoded) == 205_401
    assert db_module.decode_gmail_recovery_page(encoded) == tuple(message_ids)


def test_recovery_state_is_one_strict_account_row_with_bounded_page_and_index(
    tmp_path: Path,
) -> None:
    store, identity = _gmail_selector_store(tmp_path)
    state = store.create_gmail_recovery_state(
        "gmail-default",
        identity,
        0,
        [("sender@example.com", "Sender")],
        [],
        100,
        200,
        "replacement-history",
    )
    assert state.current_page_ids == ()
    assert state.page_loaded is False
    with pytest.raises(db_module.GmailLabelStoreError, match="conflict"):
        store.create_gmail_recovery_state(
            "gmail-default", identity, 0, [], [], 100, 200, "other-history"
        )

    with pytest.raises(db_module.GmailLabelStoreError, match="gmail_recovery_page_invalid"):
        store.store_gmail_recovery_page(
            "gmail-default", identity, [f"m{index}" for index in range(201)], None
        )
    with pytest.raises(db_module.GmailLabelStoreError, match="gmail_recovery_page_invalid"):
        store.store_gmail_recovery_page("gmail-default", identity, ["valid", "bad\x85"], None)
    assert store.gmail_recovery_state("gmail-default") == state

    ids = [f"message-{index}" for index in range(200)]
    loaded = store.store_gmail_recovery_page(
        "gmail-default", identity, ids, "next-page"
    )
    assert loaded.current_page_ids == tuple(ids)
    assert loaded.page_count == 1
    assert loaded.page_loaded is True
    assert loaded.next_index == 0

    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "UPDATE gmail_recovery_state SET current_page_ids_json=? WHERE account_id=?",
            (b" " * 524_288, "gmail-default"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "UPDATE gmail_recovery_state SET current_page_ids_json=? WHERE account_id=?",
                (b" " * 524_289, "gmail-default"),
            )
        db.execute(
            "UPDATE gmail_recovery_state SET current_page_ids_json=? WHERE account_id=?",
            (db_module.encode_gmail_recovery_page(ids), "gmail-default"),
        )

    with store.connection() as db, pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "UPDATE gmail_recovery_state SET replacement_history_cursor='changed' "
            "WHERE account_id='gmail-default'"
        )


def test_selector_snapshot_worst_case_fits_and_oversized_sender_snapshot_writes_nothing(
    tmp_path: Path,
) -> None:
    selectors = []
    for index in range(100):
        selector_id = str(uuid.UUID(int=index + 1, version=4))
        selectors.append(
            db_module.GmailLabelSelectorSnapshot(
                selector_id=selector_id,
                label_id=('"\\' * 256),
                display_name=('"\\' * 512),
            )
        )
    encoded = db_module.encode_gmail_selector_snapshot(selectors)
    assert len(encoded) <= 1_048_576
    assert db_module.decode_gmail_selector_snapshot(encoded) == tuple(selectors)
    with pytest.raises(RuntimeError, match="selector snapshot is invalid"):
        db_module.decode_gmail_selector_snapshot(b" " * 1_048_577)
    with pytest.raises(RuntimeError, match="sender snapshot is invalid"):
        db_module.decode_gmail_sender_snapshot(b" " * 1_048_577)
    with pytest.raises(RuntimeError, match="recovery page is invalid"):
        db_module.decode_gmail_recovery_page(b" " * 524_289)

    store, identity = _gmail_selector_store(tmp_path)
    with pytest.raises(
        db_module.GmailLabelStoreError, match="gmail_recovery_snapshot_too_large"
    ):
        store.create_gmail_recovery_state(
            "gmail-default",
            identity,
            0,
            [("sender@example.com", "X" * 1_048_576)],
            [],
            1,
            2,
            "replacement-history",
        )
    assert store.gmail_recovery_state("gmail-default") is None

    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        values = (
            "gmail",
            "raw-cap-account",
            identity,
            0,
            b"[]",
            b" " * 1_048_576,
            1,
            2,
            "1970-01-01T00:00:01+00:00",
            "replacement",
            b"[]",
            "2026-09-19T12:00:00+00:00",
        )
        db.execute(
            """INSERT INTO gmail_recovery_state(
                provider, account_id, mailbox_identity_key, selector_revision,
                sender_snapshot_json, selector_snapshot_json,
                recovery_after_exclusive_epoch, recovery_before_exclusive_epoch,
                retention_cutoff, replacement_history_cursor, current_page_ids_json,
                created_at, updated_at, state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'collecting')""",
            (*values, values[-1]),
        )
        db.execute("DELETE FROM gmail_recovery_state WHERE account_id='raw-cap-account'")
        oversized = list(values)
        oversized[5] = b" " * 1_048_577
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                """INSERT INTO gmail_recovery_state(
                    provider, account_id, mailbox_identity_key, selector_revision,
                    sender_snapshot_json, selector_snapshot_json,
                    recovery_after_exclusive_epoch, recovery_before_exclusive_epoch,
                    retention_cutoff, replacement_history_cursor, current_page_ids_json,
                    created_at, updated_at, state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'collecting')""",
                (*oversized, oversized[-1]),
            )


def test_recovery_counters_have_no_normal_cap_and_overflow_fails_closed(
    tmp_path: Path,
) -> None:
    store, identity = _gmail_selector_store(tmp_path)
    store.create_gmail_recovery_state(
        "gmail-default", identity, 0, [], [], 1, 2, "replacement-history"
    )
    with store.connection() as db:
        db.execute(
            "UPDATE gmail_recovery_state SET page_count=? WHERE account_id='gmail-default'",
            (db_module.SQLITE_MAX_INTEGER - 1,),
        )
    state = store.store_gmail_recovery_page("gmail-default", identity, [], None)
    assert state.page_count == db_module.SQLITE_MAX_INTEGER
    assert store.finish_gmail_recovery_page("gmail-default", identity) is True
    assert store.complete_gmail_recovery("gmail-default", identity) == "replacement-history"

    store.create_gmail_recovery_state(
        "gmail-default", identity, 0, [], [], 2, 3, "second-history"
    )
    with store.connection() as db:
        db.execute(
            "UPDATE gmail_recovery_state SET page_count=? WHERE account_id='gmail-default'",
            (db_module.SQLITE_MAX_INTEGER,),
        )
    before = store.gmail_recovery_state("gmail-default")
    with pytest.raises(
        db_module.GmailLabelStoreError, match="gmail_recovery_counter_overflow"
    ):
        store.store_gmail_recovery_page("gmail-default", identity, [], None)
    assert store.gmail_recovery_state("gmail-default") == before

    terminal_store, terminal_identity = _gmail_selector_store(tmp_path / "terminal")
    terminal_store.create_gmail_recovery_state(
        "gmail-default", terminal_identity, 0, [], [], 1, 2, "terminal-history"
    )
    terminal_store.store_gmail_recovery_page(
        "gmail-default", terminal_identity, ["candidate"], None
    )
    with terminal_store.connection() as db:
        db.execute(
            "UPDATE gmail_recovery_state SET terminal_candidate_count=? "
            "WHERE account_id='gmail-default'",
            (db_module.SQLITE_MAX_INTEGER,),
        )
    terminal_before = terminal_store.gmail_recovery_state("gmail-default")
    with pytest.raises(
        db_module.GmailLabelStoreError, match="gmail_recovery_counter_overflow"
    ):
        terminal_store.finish_gmail_recovery_candidate(
            "gmail-default", terminal_identity, "candidate"
        )
    assert terminal_store.gmail_recovery_state("gmail-default") == terminal_before

    token_store, token_identity = _gmail_selector_store(tmp_path / "token")
    token_store.create_gmail_recovery_state(
        "gmail-default", token_identity, 0, [], [], 1, 2, "token-history"
    )
    with token_store.connection() as db:
        db.execute(
            "UPDATE gmail_recovery_state SET invalid_page_token_count=? "
            "WHERE account_id='gmail-default'",
            (db_module.SQLITE_MAX_INTEGER,),
        )
    token_before = token_store.gmail_recovery_state("gmail-default")
    with pytest.raises(
        db_module.GmailLabelStoreError, match="gmail_recovery_counter_overflow"
    ):
        token_store.record_gmail_recovery_invalid_page_token(
            "gmail-default",
            token_identity,
            next_retry_at="2026-09-19T12:01:00+00:00",
        )
    assert token_store.gmail_recovery_state("gmail-default") == token_before


def test_recovery_backoff_is_durable_bounded_and_clears_on_success(tmp_path: Path) -> None:
    store, identity = _gmail_selector_store(tmp_path)
    store.create_gmail_recovery_state(
        "gmail-default", identity, 0, [], [], 1, 2, "replacement-history"
    )
    backed_off = store.record_gmail_recovery_backoff(
        "gmail-default",
        identity,
        failure_code="gmail_recovery_provider_unavailable",
        next_retry_at="2026-09-19T12:01:00+00:00",
    )
    assert (backed_off.state, backed_off.consecutive_retry_count) == ("backoff", 1)
    assert backed_off.next_retry_at == "2026-09-19T12:01:00+00:00"

    cleared = store.clear_gmail_recovery_backoff("gmail-default", identity)
    assert (cleared.state, cleared.consecutive_retry_count, cleared.next_retry_at) == (
        "collecting",
        1,
        None,
    )

    for count in range(1, 6):
        reset = store.record_gmail_recovery_invalid_page_token(
            "gmail-default",
            identity,
            next_retry_at=f"2026-09-19T12:0{count}:00+00:00",
        )
        assert reset.invalid_page_token_count == count
        assert reset.state == ("degraded" if count >= 5 else "backoff")
        assert reset.page_loaded is False
        assert reset.current_page_ids == ()

    resumed = store.store_gmail_recovery_page("gmail-default", identity, [], None)
    assert resumed.state == "collecting"
    assert resumed.failure_code is None
    assert resumed.next_retry_at is None
    assert resumed.consecutive_retry_count == 0
    assert resumed.invalid_page_token_count == 5


def test_due_recovery_backoff_preserves_retry_count_until_progress(tmp_path: Path) -> None:
    store, identity = _gmail_selector_store(tmp_path)
    store.create_gmail_recovery_state(
        "gmail-default", identity, 0, [], [], 1, 2, "replacement-history"
    )
    first = store.record_gmail_recovery_backoff(
        "gmail-default",
        identity,
        failure_code="gmail_recovery_provider_unavailable",
        next_retry_at="2026-09-19T12:01:00+00:00",
    )
    assert first.consecutive_retry_count == 1

    due = store.clear_gmail_recovery_backoff("gmail-default", identity)
    assert due.consecutive_retry_count == 1
    second = store.record_gmail_recovery_backoff(
        "gmail-default",
        identity,
        failure_code="gmail_recovery_provider_unavailable",
        next_retry_at="2026-09-19T12:03:00+00:00",
    )
    assert second.consecutive_retry_count == 2

    progressed = store.store_gmail_recovery_page(
        "gmail-default", identity, [], None
    )
    assert progressed.consecutive_retry_count == 0


def test_open_recovery_purge_uses_frozen_cutoff_and_preserves_account_dedupe(
    tmp_path: Path,
) -> None:
    store, identity = _gmail_selector_store(tmp_path)
    now = datetime(2026, 9, 20, 12, tzinfo=UTC)
    frozen_cutoff = now - timedelta(days=7)
    admission = db_module.AdmissionProvenance(
        kind="exact_sender",
        selector_id="sender:trusted@example.com",
        display_name="Trusted",
        mailbox_identity_key=identity,
        admitted_at=now.isoformat(),
    )
    store.create_gmail_recovery_state(
        "gmail-default",
        identity,
        0,
        (("trusted@example.com", "Trusted"),),
        (),
        int(frozen_cutoff.timestamp()) - 1,
        int(now.timestamp()) + 1,
        "replacement-history",
        retention_cutoff=frozen_cutoff,
        now=now,
    )
    for message_id, received_at in (
        ("inside-frozen-window", frozen_cutoff + timedelta(hours=1)),
        ("before-frozen-window", frozen_cutoff - timedelta(hours=1)),
    ):
        assert store.add_message(
            message_id=message_id,
            provider="gmail",
            account_id="gmail-default",
            provider_message_id=message_id,
            mailbox_identity_key=identity,
            thread_id=None,
            sender="trusted@example.com",
            sender_name="Trusted",
            subject=message_id,
            received_at=received_at.isoformat(),
            admission=admission,
        )
    with store.connection() as db:
        db.execute(
            """INSERT INTO suppressed_messages(
                provider, account_id, message_key, expires_at
            ) VALUES ('gmail', 'gmail-default', ?, ?)""",
            ("d" * 64, (now - timedelta(minutes=1)).isoformat()),
        )

    assert store.purge(1, now=now) == 1
    assert store.has_seen_message(
        "inside-frozen-window",
        provider="gmail",
        account_id="gmail-default",
        mailbox_identity_key=identity,
    )
    with store.connection() as db:
        assert db.execute(
            "SELECT 1 FROM suppressed_messages WHERE message_key=?", ("d" * 64,)
        ).fetchone()

    store.store_gmail_recovery_page("gmail-default", identity, (), None, now=now)
    assert store.finish_gmail_recovery_page("gmail-default", identity, now=now)
    store.complete_gmail_recovery("gmail-default", identity, now=now)

    assert store.purge(1, now=now) == 1
    assert not store.has_seen_message(
        "inside-frozen-window",
        provider="gmail",
        account_id="gmail-default",
        mailbox_identity_key=identity,
    )
    with store.connection() as db:
        assert db.execute(
            "SELECT 1 FROM suppressed_messages WHERE message_key=?", ("d" * 64,)
        ).fetchone() is None


def test_message_insert_atomically_persists_deterministic_admission_provenance(
    tmp_path: Path,
) -> None:
    store, identity = _gmail_selector_store(tmp_path)
    admission = db_module.AdmissionProvenance(
        kind="exact_sender",
        selector_id="sender:trusted@example.com",
        display_name="Trusted",
        mailbox_identity_key=identity,
        admitted_at="2026-09-19T12:00:00+00:00",
    )
    assert ProductionStore.add_message(
        store,
        message_id="message-1",
        provider="gmail",
        account_id="gmail-default",
        provider_message_id="provider-1",
        mailbox_identity_key=identity,
        thread_id=None,
        sender="trusted@example.com",
        sender_name="Trusted",
        subject="Invoice",
        received_at="2026-09-19T11:59:00+00:00",
        admission=admission,
    )
    assert store.recent(1)[0]["admission"] == {
        "kind": "exact_sender",
        "selector_id": "sender:trusted@example.com",
        "display_name": "Trusted",
        "admitted_at": "2026-09-19T12:00:00+00:00",
    }
    with store.connection() as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                """INSERT INTO messages(
                    message_id, provider, account_id, mailbox_identity_key,
                    provider_message_id, sender, subject, received_at, discovered_at
                ) VALUES (
                    'missing-provenance', 'gmail', 'gmail-default', ?,
                    'provider-2', 'trusted@example.com', 'Missing',
                    '2026-09-19T11:59:00+00:00', '2026-09-19T12:00:00+00:00'
                )""",
                (identity,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "UPDATE messages SET admission_selector_id='sender:other@example.com' "
                "WHERE message_id='message-1'"
            )


def test_legacy_message_admission_provenance_rejects_partial_population(
    tmp_path: Path,
) -> None:
    store, identity = _gmail_selector_store(tmp_path)
    with store.connection() as db:
        db.execute("DROP TRIGGER messages_require_admission_provenance_insert")
        db.execute(
            """INSERT INTO messages(
                message_id, provider, account_id, mailbox_identity_key,
                provider_message_id, sender, subject, received_at, discovered_at
            ) VALUES (
                'legacy-null-admission', 'gmail', 'gmail-default', ?,
                'legacy-provider-id', 'trusted@example.com', 'Legacy',
                '2026-09-19T11:59:00+00:00', '2026-09-19T12:00:00+00:00'
            )""",
            (identity,),
        )
        for update in (
            "admission_kind = 'exact_sender'",
            "admission_selector_id = 'sender:trusted@example.com'",
            "admission_display_name = 'Trusted'",
            f"admission_mailbox_identity_key = '{identity}'",
            "admitted_at = '2026-09-19T12:00:00+00:00'",
            "admission_kind = 'exact_sender', "
            "admission_selector_id = 'sender:trusted@example.com'",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="admission provenance"):
                db.execute(
                    f"UPDATE messages SET {update} "
                    "WHERE message_id = 'legacy-null-admission'"
                )

        replacement_identity = "f" * 64
        db.execute(
            "UPDATE messages SET mailbox_identity_key = ? "
            "WHERE message_id = 'legacy-null-admission'",
            (replacement_identity,),
        )
        row = db.execute(
            """SELECT mailbox_identity_key, admission_kind, admission_selector_id,
                admission_display_name, admission_mailbox_identity_key, admitted_at
            FROM messages WHERE message_id = 'legacy-null-admission'"""
        ).fetchone()
    assert tuple(row) == (replacement_identity, None, None, None, None, None)


def test_legacy_partial_admission_row_cannot_change_or_rebind_identity(
    tmp_path: Path,
) -> None:
    store, identity = _gmail_selector_store(tmp_path)
    with store.connection() as db:
        db.execute("DROP TRIGGER messages_require_admission_provenance_insert")
        db.execute("DROP TRIGGER messages_admission_provenance_immutable")
        db.execute(
            """INSERT INTO messages(
                message_id, provider, account_id, mailbox_identity_key,
                provider_message_id, sender, subject, received_at, discovered_at,
                admission_display_name
            ) VALUES (
                'legacy-partial-admission', 'gmail', 'gmail-default', ?,
                'legacy-partial-provider-id', 'trusted@example.com', 'Legacy partial',
                '2026-09-19T11:59:00+00:00', '2026-09-19T12:00:00+00:00',
                'Historical name'
            )""",
            (identity,),
        )

    store.initialize()
    with store.connection() as db:
        for update in (
            "admission_display_name = NULL",
            "admission_kind = 'exact_sender'",
            f"mailbox_identity_key = '{'f' * 64}'",
            "admission_display_name = NULL, admission_kind = 'exact_sender'",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="admission provenance"):
                db.execute(
                    f"UPDATE messages SET {update} "
                    "WHERE message_id = 'legacy-partial-admission'"
                )


def test_recovery_terminal_insert_revocation_and_cursor_commit_are_atomic(
    tmp_path: Path,
) -> None:
    store, identity = _gmail_selector_store(tmp_path)
    revision, selector = store.add_gmail_label_selector(
        "gmail-default", identity, "Label_1", "Invoices", 0
    )
    store.set_state(
        "old-history",
        provider="gmail",
        account_id="gmail-default",
        mailbox_identity_key=identity,
    )
    store.create_gmail_recovery_state(
        "gmail-default",
        identity,
        revision,
        [],
        [selector],
        1,
        2,
        "replacement-history",
    )
    store.store_gmail_recovery_page("gmail-default", identity, ["m1", "m2"], None)
    admission = db_module.AdmissionProvenance(
        kind="gmail_user_label",
        selector_id=selector.selector_id,
        display_name="Invoices",
        mailbox_identity_key=identity,
        admitted_at="2026-09-19T12:00:00+00:00",
    )
    message = db_module.GmailRecoveryMessage(
        message_id="local-m1",
        thread_id=None,
        sender="sender@example.com",
        sender_name=None,
        subject="Invoice",
        received_at="2026-09-19T11:59:00+00:00",
        locations=frozenset({"inbox"}),
    )

    assert store.finish_gmail_recovery_candidate(
        "gmail-default",
        identity,
        "m1",
        message=message,
        admission=admission,
        metadata_label_ids=frozenset({"INBOX", "Label_1"}),
    )
    state = store.gmail_recovery_state("gmail-default")
    assert state is not None
    assert (state.next_index, state.terminal_candidate_count) == (1, 1)
    item = store.recent(1)[0]
    assert item["admission"] == {
        "kind": "gmail_user_label",
        "selector_id": selector.selector_id,
        "display_name": "Invoices",
        "admitted_at": "2026-09-19T12:00:00+00:00",
    }

    revision = store.remove_gmail_label_selector(
        "gmail-default", identity, selector.selector_id, revision
    )
    assert revision == 2
    with pytest.raises(
        db_module.GmailLabelStoreError, match="gmail_recovery_grant_revoked"
    ):
        store.finish_gmail_recovery_candidate(
            "gmail-default",
            identity,
            "m2",
            message=db_module.GmailRecoveryMessage(
                message_id="local-m2",
                thread_id=None,
                sender="sender@example.com",
                sender_name=None,
                subject="Revoked",
                received_at="2026-09-19T11:59:30+00:00",
                locations=frozenset({"inbox"}),
            ),
            admission=admission,
            metadata_label_ids=frozenset({"INBOX", "Label_1"}),
        )
    unchanged = store.gmail_recovery_state("gmail-default")
    assert unchanged is not None
    assert (unchanged.next_index, unchanged.terminal_candidate_count) == (1, 1)
    assert not store.has_message("local-m2")

    assert not store.finish_gmail_recovery_candidate(
        "gmail-default", identity, "m2"
    )
    assert store.finish_gmail_recovery_page("gmail-default", identity) is True
    assert store.complete_gmail_recovery("gmail-default", identity) == "replacement-history"
    assert store.gmail_recovery_state("gmail-default") is None
    assert store.state(provider="gmail", account_id="gmail-default")[0] == (
        "replacement-history"
    )


def test_recovery_cursor_commit_keeps_capture_timestamp_after_long_drain(
    tmp_path: Path,
) -> None:
    store, identity = _gmail_selector_store(tmp_path)
    captured_at = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
    completed_at = captured_at + timedelta(days=2)
    store.set_state(
        "old-history",
        at=captured_at - timedelta(days=1),
        provider="gmail",
        account_id="gmail-default",
        mailbox_identity_key=identity,
    )
    recovery = store.create_gmail_recovery_state(
        "gmail-default",
        identity,
        0,
        [],
        [],
        1,
        2,
        "replacement-history",
        now=captured_at,
    )
    assert recovery.created_at == captured_at.isoformat()

    store.store_gmail_recovery_page(
        "gmail-default", identity, [], None, now=completed_at
    )
    assert store.finish_gmail_recovery_page(
        "gmail-default", identity, now=completed_at
    )
    assert (
        store.complete_gmail_recovery(
            "gmail-default", identity, now=completed_at
        )
        == "replacement-history"
    )

    assert store.state(provider="gmail", account_id="gmail-default") == (
        "replacement-history",
        captured_at.isoformat(),
    )
    assert store.gmail_recovery_state("gmail-default") is None


def test_recovery_terminal_recomputes_smallest_current_frozen_label_winner(
    tmp_path: Path,
) -> None:
    store, identity = _gmail_selector_store(tmp_path)
    revision, first = store.add_gmail_label_selector(
        "gmail-default", identity, "Label_1", "First", 0
    )
    revision, second = store.add_gmail_label_selector(
        "gmail-default", identity, "Label_2", "Second", revision
    )
    smaller, larger = sorted((first, second), key=lambda selector: selector.selector_id)
    store.create_gmail_recovery_state(
        "gmail-default",
        identity,
        revision,
        [],
        [first, second],
        1,
        2,
        "replacement-history",
    )
    store.store_gmail_recovery_page("gmail-default", identity, ["m1"], None)
    message = db_module.GmailRecoveryMessage(
        message_id="local-m1",
        thread_id=None,
        sender="sender@example.com",
        sender_name=None,
        subject="Overlap",
        received_at="2026-09-19T11:59:00+00:00",
        locations=frozenset({"inbox"}),
    )
    metadata_labels = frozenset({"INBOX", first.label_id, second.label_id})

    with pytest.raises(
        db_module.GmailLabelStoreError, match="gmail_recovery_grant_revoked"
    ):
        store.finish_gmail_recovery_candidate(
            "gmail-default",
            identity,
            "m1",
            message=message,
            admission=db_module.AdmissionProvenance(
                kind="gmail_user_label",
                selector_id=larger.selector_id,
                display_name=larger.selected_display_name,
                mailbox_identity_key=identity,
                admitted_at="2026-09-19T12:00:00+00:00",
            ),
            metadata_label_ids=metadata_labels,
        )
    unchanged = store.gmail_recovery_state("gmail-default")
    assert unchanged is not None
    assert (unchanged.next_index, unchanged.terminal_candidate_count) == (0, 0)
    assert not store.has_message("local-m1")

    assert store.finish_gmail_recovery_candidate(
        "gmail-default",
        identity,
        "m1",
        message=message,
        admission=db_module.AdmissionProvenance(
            kind="gmail_user_label",
            selector_id=smaller.selector_id,
            display_name=smaller.selected_display_name,
            mailbox_identity_key=identity,
            admitted_at="2026-09-19T12:00:00+00:00",
        ),
        metadata_label_ids=metadata_labels,
    )


def test_inactive_recovery_account_cannot_advance_or_commit_cursor(tmp_path: Path) -> None:
    store, identity = _gmail_selector_store(tmp_path)
    store.set_state(
        "old-history",
        provider="gmail",
        account_id="gmail-default",
        mailbox_identity_key=identity,
    )
    store.create_gmail_recovery_state(
        "gmail-default", identity, 0, [], [], 1, 2, "replacement-history"
    )
    store.store_gmail_recovery_page("gmail-default", identity, [], None)
    assert store.finish_gmail_recovery_page("gmail-default", identity) is True
    store.register_mail_account(
        "microsoft365",
        "other-account",
        display_name="Other",
        address="other@example.com",
    )
    store.activate_mail_account("microsoft365", "other-account")

    with pytest.raises(db_module.GmailLabelStoreError, match="account_not_active"):
        store.complete_gmail_recovery("gmail-default", identity)
    assert store.state(provider="gmail", account_id="gmail-default")[0] == "old-history"
    assert store.gmail_recovery_state("gmail-default") is not None

    store.activate_mail_account("gmail", "gmail-default")
    assert store.complete_gmail_recovery("gmail-default", identity) == "replacement-history"


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
    store.reconcile_mailbox_identity(
        "gmail",
        "gmail-default",
        Store._test_mailbox_identity("gmail", "gmail-default"),
        legacy_status="replacement",
    )
    with store.connection() as db:
        db.executemany(
                """INSERT INTO messages(
                        message_id, provider, account_id, mailbox_identity_key,
                        provider_message_id,
                        sender, subject, received_at, discovered_at, category,
                        admission_kind, admission_selector_id,
                        admission_mailbox_identity_key, admitted_at
                    ) VALUES (
                        ?1, 'gmail', 'gmail-default',
                        (SELECT mailbox_identity_key FROM mail_accounts
                         WHERE provider = 'gmail' AND account_id = 'gmail-default'), ?1,
                        'sender@example.com', 'Update', ?2, ?3, 'informational',
                        'exact_sender', 'sender:sender@example.com',
                        (SELECT mailbox_identity_key FROM mail_accounts
                         WHERE provider = 'gmail' AND account_id = 'gmail-default'), ?3
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


def test_inbox_query_exact_sender_pages_and_combines_filters(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    expected = []
    for index in range(150):
        sender = (
            "bob@acme.com" if index < 130
            else "jimbob@acme.com" if index < 145 else "other@acme.com"
        )
        stamp = (
            datetime(2026, 8, 31, tzinfo=UTC) + timedelta(seconds=max(0, index - 19))
        ).isoformat()
        store.add_message(
            message_id=f"exact-{index:03}", thread_id=None, sender=sender,
            sender_name="bob@acme.com" if index >= 145 else "Billing",
            subject="Update", received_at=stamp,
        )
        if index < 130:
            expected.append((stamp, f"exact-{index:03}"))
    cursor = None
    actual = []
    while True:
        items, cursor = store.query_inbox(limit=7, cursor=cursor, sender="bob@acme.com")
        assert all(item["sender"] == "bob@acme.com" for item in items)
        actual.extend((item["received_at"], item["message_id"]) for item in items)
        if cursor is None:
            break
    assert actual == sorted(expected, reverse=True)
    assert len(actual) == len(set(actual)) == 130
    items, _ = store.query_inbox(
        limit=100, sender="bob@acme.com", sender_query="BILL", keyword="UPDATE",
        priority="untriaged", category="unclassified", status="pending",
        provider="gmail", account_id="gmail-default",
    )
    assert len(items) == 100
    for field, value in {"sender_query": "jimbob", "keyword": "missing",
                         "priority": "high", "category": "invoice", "status": "analyzed",
                         "provider": "imap", "account_id": "other"}.items():
        items, _ = store.query_inbox(limit=7, sender="bob@acme.com", **{field: value})
        assert items == [], field


def test_inbox_query_combines_filters_before_limiting_and_matches_literals(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    store.reconcile_mailbox_identity(
        "gmail",
        "gmail-default",
        Store._test_mailbox_identity("gmail", "gmail-default"),
        legacy_status="replacement",
    )
    with store.connection() as db:
        db.executemany(
            """INSERT INTO messages(
                    message_id, provider, account_id, mailbox_identity_key,
                    provider_message_id,
                        sender, sender_name, subject, received_at, discovered_at,
                        status, category, priority, summary,
                        admission_kind, admission_selector_id,
                        admission_mailbox_identity_key, admitted_at
                    ) VALUES (
                    ?1, 'gmail', 'gmail-default',
                    (SELECT mailbox_identity_key FROM mail_accounts
                     WHERE provider = 'gmail' AND account_id = 'gmail-default'), ?1,
                        ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10,
                        'exact_sender', ('sender:' || ?2),
                        (SELECT mailbox_identity_key FROM mail_accounts
                         WHERE provider = 'gmail' AND account_id = 'gmail-default'), ?6
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
    store.reconcile_mailbox_identity(
        "gmail",
        "gmail-default",
        Store._test_mailbox_identity("gmail", "gmail-default"),
        legacy_status="replacement",
    )
    with store.connection() as db:
        db.executemany(
            """INSERT INTO messages (
                    message_id, provider, account_id, mailbox_identity_key,
                        provider_message_id,
                        sender, subject, received_at, discovered_at, status, last_error,
                        admission_kind, admission_selector_id,
                        admission_mailbox_identity_key, admitted_at
                    ) VALUES (
                    ?1, 'gmail', 'gmail-default',
                    (SELECT mailbox_identity_key FROM mail_accounts
                     WHERE provider = 'gmail' AND account_id = 'gmail-default'), ?1,
                        'a@b.com', 'Update', ?2, ?3, 'pending', 'model unavailable',
                        'exact_sender', 'sender:a@b.com',
                        (SELECT mailbox_identity_key FROM mail_accounts
                         WHERE provider = 'gmail' AND account_id = 'gmail-default'), ?3
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
    identity_key = Store._test_mailbox_identity("gmail", "gmail-default")
    assert (
        suppression
        == hashlib.sha256(f"gmail\0gmail-default\0{identity_key}\0m1".encode()).hexdigest()
    )
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


def test_clear_messages_bounds_simultaneously_held_source_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    for index in range(5):
        assert store.add_message(
            message_id=f"message-{index}",
            thread_id=None,
            sender="a@b.com",
            sender_name=None,
            subject="Bounded cleanup",
            received_at="2026-09-01T00:00:00+00:00",
        )
    active = 0
    maximum = 0

    class TrackingLock:
        def __enter__(self):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)

        def __exit__(self, *args):
            nonlocal active
            active -= 1

    monkeypatch.setattr(db_module, "SOURCE_CLEANUP_LOCK_BATCH_SIZE", 2)
    monkeypatch.setattr(
        db_module,
        "connect_operation_lock",
        lambda *args, **kwargs: TrackingLock(),
    )

    assert store.clear_messages() == 5
    assert maximum == 2
    assert active == 0


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
    capability_produces: tuple[str, ...] = ("text/plain",),
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
        capability_produces=capability_produces,
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
        restored_connect_triggers = {
            str(name): str(sql)
            for name, sql in db.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
                "AND name IN (?, ?, ?, ?)",
                (
                    "messages_delete_connect_attachment_jobs",
                    "connect_jobs_delete_dispatch",
                    "messages_delete_pending_automation_fires",
                    "connect_jobs_delete_linked_automation_fires",
                ),
            ).fetchall()
        }
        assert set(restored_connect_triggers) == {
            "messages_delete_connect_attachment_jobs",
            "connect_jobs_delete_dispatch",
            "messages_delete_pending_automation_fires",
            "connect_jobs_delete_linked_automation_fires",
        }
        assert all(
            "connect_attachment_jobs_v5" not in sql and "connect_attachment_jobs_v6" not in sql
            for sql in restored_connect_triggers.values()
        )


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


def test_schema_19_to_20_marks_history_and_installs_cross_version_fences(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "watcher.sqlite3")
    store.initialize()
    assert store.add_message(
        message_id="already-analyzed",
        thread_id=None,
        sender="trusted@example.com",
        sender_name=None,
        subject="Historical",
        received_at="2026-09-01T00:00:00+00:00",
    )
    store.mark_analyzed("already-analyzed", scheduling_analysis())
    assert store.add_message(
        message_id="still-pending",
        thread_id=None,
        sender="trusted@example.com",
        sender_name=None,
        subject="Pending",
        received_at="2026-09-01T00:01:00+00:00",
    )

    with store.connection() as db:
        scheduling_columns_before = [
            row["name"] for row in db.execute("PRAGMA table_info(automation_runs)")
        ]
        db.execute(
            """INSERT INTO suppressed_messages(provider, account_id, message_key, expires_at)
            VALUES ('gmail', 'gmail-default', ?, '2026-10-01T00:00:00+00:00')""",
            ("a" * 64,),
        )
        db.execute("DROP TRIGGER messages_require_mailbox_identity_insert")
        db.execute("DROP TRIGGER messages_require_rule_revision_completion")
        db.execute("DROP TRIGGER messages_require_admission_provenance_insert")
        db.execute("DROP TRIGGER messages_admission_provenance_immutable")
        db.execute("DROP TRIGGER messages_delete_pending_automation_fires")
        db.execute("DROP TABLE automation_fire_attempts")
        db.execute("DROP TABLE automation_fires")
        db.execute("DROP TABLE automation_rule_versions")
        db.execute("DROP TABLE automation_rules")
        db.execute("DROP TABLE automation_rule_set")
        db.execute("DROP TABLE automation_run_source_identities")
        db.execute("DROP TABLE legacy_mailbox_markers")
        db.execute("DROP INDEX idx_messages_source_identity_v20")
        db.execute(
            """CREATE UNIQUE INDEX idx_messages_source_identity
            ON messages(provider, account_id, provider_message_id)"""
        )
        db.execute(
            """UPDATE messages
            SET mailbox_identity_key = NULL, rules_revision_at_analysis = NULL,
                rules_evaluation_error = NULL, admission_kind = NULL,
                admission_selector_id = NULL, admission_display_name = NULL,
                admission_mailbox_identity_key = NULL, admitted_at = NULL"""
        )
        db.execute(
            """UPDATE mail_accounts
            SET mailbox_identity_key = NULL, legacy_identity_status = 'unresolved',
                legacy_identity_key = NULL"""
        )
        db.execute("PRAGMA user_version = 19")

    store.initialize()

    retention_cutoff = datetime(2026, 9, 2, tzinfo=UTC)
    assert store.has_unexpired_legacy_mailbox_markers(
        "gmail",
        "gmail-default",
        retention_cutoff=retention_cutoff,
        now=datetime(2026, 9, 15, tzinfo=UTC),
    )
    assert not store.has_unexpired_legacy_mailbox_markers(
        "gmail",
        "gmail-default",
        retention_cutoff=retention_cutoff,
        now=datetime(2026, 10, 2, tzinfo=UTC),
    )

    with store.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        markers = db.execute(
            """SELECT message_id, status, mailbox_identity_key,
                rules_revision_at_analysis
            FROM messages ORDER BY message_id"""
        ).fetchall()
        assert [tuple(row) for row in markers] == [
            ("already-analyzed", "analyzed", None, 0),
            ("still-pending", "pending", None, None),
        ]
        assert [row["name"] for row in db.execute("PRAGMA table_info(automation_runs)")] == (
            scheduling_columns_before
        )
        assert db.execute("SELECT revision FROM automation_rule_set").fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError, match="admission provenance"):
            db.execute(
                """INSERT INTO messages(
                    message_id, provider, account_id, provider_message_id,
                    sender, subject, received_at, discovered_at
                ) VALUES (
                    'late-v19', 'gmail', 'gmail-default', 'late-v19',
                    'trusted@example.com', 'Late',
                    '2026-09-01T00:02:00+00:00',
                    '2026-09-01T00:02:00+00:00'
                )"""
            )
        with pytest.raises(sqlite3.IntegrityError, match="rule revision"):
            db.execute("UPDATE messages SET status = 'analyzed' WHERE message_id = 'still-pending'")


def test_schema_20_to_21_preserves_pending_fire_and_attempt_identity(
    tmp_path: Path,
) -> None:
    database = tmp_path / "watcher.sqlite3"
    store = Store(database)
    store.initialize()
    seed_pdf_attachment(store)
    store.put_automation_rule(
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
                    "app_id": "document-summarizer",
                    "version": "0.1.0",
                    "instance_id": "11111111-1111-4111-8111-111111111111",
                },
                "parameters": {"mode": "contract"},
            },
            "confirm_each": False,
        }
    )
    store.mark_analyzed("m1", scheduling_analysis())
    original = store.automation_fires_for_message("m1")[0]
    original_attempt = store.automation_fire_attempts(original.fire_id)[0]

    with store.connection() as connection:
        connection.executescript(
            """
            DROP TRIGGER connect_jobs_delete_linked_automation_fires;
            DROP TRIGGER messages_delete_pending_automation_fires;
            DROP TRIGGER automation_fire_attempts_immutable_update;
            DROP TRIGGER automation_fire_attempts_require_fire;
            DROP TRIGGER automation_fires_require_sources;
            DROP INDEX idx_automation_fires_state;
            DROP INDEX idx_automation_fires_message;
            ALTER TABLE automation_fire_attempts RENAME TO automation_fire_attempts_v21;
            ALTER TABLE automation_fires RENAME TO automation_fires_v21;
            CREATE TABLE automation_fires (
                fire_id TEXT PRIMARY KEY CHECK (length(fire_id) = 36),
                event_id TEXT NOT NULL CHECK (length(event_id) = 64),
                rule_id TEXT NOT NULL CHECK (length(rule_id) = 36),
                rule_version INTEGER NOT NULL CHECK (rule_version >= 1),
                message_id TEXT NOT NULL CHECK (message_id <> ''),
                part_id TEXT NOT NULL,
                action_kind TEXT NOT NULL CHECK (action_kind = 'connect.invoke'),
                state TEXT NOT NULL CHECK (state = 'pending_dispatch'),
                state_version INTEGER NOT NULL CHECK (state_version = 1),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (message_id, part_id, rule_id, rule_version)
            );
            CREATE TABLE automation_fire_attempts (
                fire_id TEXT NOT NULL CHECK (length(fire_id) = 36),
                attempt_no INTEGER NOT NULL CHECK (attempt_no = 1),
                dispatch_request_id TEXT NOT NULL UNIQUE CHECK (length(dispatch_request_id) = 36),
                created_at TEXT NOT NULL,
                PRIMARY KEY (fire_id, attempt_no)
            );
            INSERT INTO automation_fires(
                fire_id, event_id, rule_id, rule_version, message_id, part_id,
                action_kind, state, state_version, created_at, updated_at
            ) SELECT fire_id, event_id, rule_id, rule_version, message_id, part_id,
                action_kind, state, state_version, created_at, updated_at
              FROM automation_fires_v21;
            INSERT INTO automation_fire_attempts(
                fire_id, attempt_no, dispatch_request_id, created_at
            ) SELECT fire_id, attempt_no, dispatch_request_id, created_at
              FROM automation_fire_attempts_v21;
            DROP TABLE automation_fire_attempts_v21;
            DROP TABLE automation_fires_v21;
            DROP TABLE automation_fire_confirmations;
            PRAGMA user_version = 20;
            """
        )

    migration_started = datetime.now(UTC)
    store.initialize()

    migrated = store.automation_fire(original.fire_id)
    assert migrated is not None
    assert migrated.state == "pending_dispatch"
    assert migrated.state_version == 1
    assert migrated.pending_since is not None
    assert datetime.fromisoformat(migrated.pending_since) >= migration_started
    assert migrated.pending_since != original.updated_at
    assert migrated.current_attempt_no == 1
    assert migrated.job_id is None
    migrated_attempts = store.automation_fire_attempts(original.fire_id)
    assert len(migrated_attempts) == 1
    assert migrated_attempts[0].dispatch_request_id == original_attempt.dispatch_request_id
    assert migrated_attempts[0].job_id is None
    with store.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        trigger = connection.execute(
            "SELECT 1 AS installed FROM sqlite_master WHERE type = 'trigger' "
            "AND name = 'connect_jobs_delete_linked_automation_fires'"
        ).fetchone()
        assert trigger is not None
        assert trigger["installed"] == 1
        indexes = {
            str(row["name"])
            for row in connection.execute("PRAGMA index_list(automation_fires)").fetchall()
        }
        assert "idx_automation_fires_state" in indexes
        assert "idx_automation_fires_message" in indexes


def test_schema_21_to_22_widens_prepared_identity_constraint(tmp_path: Path) -> None:
    database = tmp_path / "watcher.sqlite3"
    store = Store(database)
    store.initialize()
    seed_pdf_attachment(store)
    store.put_automation_rule(
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
                    "app_id": "document-summarizer",
                    "version": "0.1.0",
                    "instance_id": "11111111-1111-4111-8111-111111111111",
                },
                "parameters": {"mode": "contract"},
            },
            "confirm_each": False,
        }
    )
    store.mark_analyzed("m1", scheduling_analysis())
    original = store.automation_fires_for_message("m1")[0]
    original_attempt = store.automation_fire_attempts(original.fire_id)[0]

    with store.connection() as connection:
        connection.execute("PRAGMA writable_schema = ON")
        changed = connection.execute(
            """UPDATE sqlite_schema
            SET sql = replace(sql, 'AND 32768', 'AND 8192')
            WHERE type = 'table' AND name = 'automation_fires'"""
        )
        connection.execute("PRAGMA writable_schema = OFF")
        connection.execute("PRAGMA user_version = 21")
        assert changed.rowcount == 1

    store.initialize()

    migrated = store.automation_fire(original.fire_id)
    assert migrated == original
    assert store.automation_fire_attempts(original.fire_id) == [original_attempt]
    prepared_identity = b'{"parameters":{"memo":"' + (b"x" * 9000) + b'"}}'
    with store.connection() as connection:
        connection.execute(
            """UPDATE automation_fires
            SET prepared_identity_sha256 = ?, prepared_identity_json = ?
            WHERE fire_id = ?""",
            (hashlib.sha256(prepared_identity).hexdigest(), prepared_identity, original.fire_id),
        )
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_schema_22_marks_incomplete_legacy_dispatch_capability_authority_unknown(
    tmp_path: Path,
) -> None:
    database = tmp_path / "watcher.sqlite3"
    store = Store(database)
    store.initialize()
    seed_pdf_attachment(store)
    legacy_job_id = "33333333-3333-4333-8333-333333333333"
    create_v2_connect_job(store, legacy_job_id)

    with store.connection() as connection:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(connect_job_dispatch)").fetchall()
        }
        connection.execute("DROP TRIGGER messages_delete_pending_automation_fires")
        if "capability_produces_json" in columns:
            connection.execute(
                "ALTER TABLE connect_job_dispatch DROP COLUMN capability_produces_json"
            )
        if "capability_authority_known" in columns:
            connection.execute(
                "ALTER TABLE connect_job_dispatch DROP COLUMN capability_authority_known"
            )
        connection.execute("PRAGMA user_version = 22")

    store.initialize()

    legacy = store.connect_dispatch(legacy_job_id)
    assert legacy is not None
    assert legacy.capability_authority_known is False
    assert legacy.capability_produces == ()
    assert legacy.interactive_authorized_at is None
    new_job_id = "44444444-4444-4444-8444-444444444444"
    create_v2_connect_job(store, new_job_id, parameters={"target-language": "French"})
    current = store.connect_dispatch(new_job_id)
    assert current is not None
    assert current.capability_authority_known is True
    assert current.capability_produces == ("text/plain",)
    assert current.interactive_authorized_at is None


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
        "dispatch_state": "terminal",
        "queue_ahead": 0,
        "next_attempt_at": None,
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


def test_connect_v2_lane_cap_excludes_paused_waiting_automation_jobs(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    seed_pdf_attachment(store)
    created_at = datetime(2026, 9, 8, 12, tzinfo=UTC)
    for index in range(CONNECT_QUEUE_MAX_JOBS):
        create_v2_connect_job(
            store,
            f"00000000-0000-4000-8000-{index:012d}",
            parameters={"sequence": index},
            now=created_at,
        )
    with store.connection() as db:
        db.execute(
            """UPDATE connect_job_dispatch
            SET automation_paused_at = ? WHERE state = 'waiting'""",
            ((created_at + timedelta(minutes=1)).isoformat(),),
        )

    interactive_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    create_v2_connect_job(
        store,
        interactive_id,
        parameters={"sequence": CONNECT_QUEUE_MAX_JOBS},
        now=created_at + timedelta(minutes=1),
    )

    created = store.connect_job(interactive_id)
    dispatch = store.connect_dispatch(interactive_id)
    assert created is not None
    assert dispatch is not None
    assert dispatch.automation_paused_at is None


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
        job.job_id for job in store.due_connect_lane_heads(now=created_at + timedelta(seconds=2))
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


def test_connect_queue_wakeup_and_inbox_projection_use_durable_dispatch_state(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    seed_pdf_attachment(store)
    created_at = datetime(2026, 9, 8, 12, tzinfo=UTC)
    first_id = "33333333-3333-4333-8333-333333333333"
    second_id = "44444444-4444-4444-8444-444444444444"
    create_v2_connect_job(store, first_id, parameters={"sequence": 1}, now=created_at)
    create_v2_connect_job(
        store,
        second_id,
        parameters={"sequence": 2},
        now=created_at + timedelta(seconds=1),
    )
    claimed = store.claim_connect_lane_head(
        provider_app_id="translation-provider",
        provider_instance_id="11111111-1111-4111-8111-111111111111",
        now=created_at,
    )
    assert claimed is not None
    deferred = store.defer_connect_job(
        job_id=first_id,
        expected_dispatch_state="dispatching",
        next_dispatch_state="waiting",
        error_code="PROVIDER_BUSY",
        error_message="Another job is running.",
        delay_seconds=30,
        now=created_at,
    )

    assert store.connect_queue_ahead(first_id) == 0
    assert store.connect_queue_ahead(second_id) == 1
    assert min(wakeup for _, wakeup in store.connect_queue_wakeups(now=created_at)) == (
        datetime.fromisoformat(deferred.next_attempt_at or "")
    )

    results = store.recent(1)[0]["attachments"][0]["capability_results"]
    by_job_id = {result["job_id"]: result for result in results}
    assert by_job_id[first_id]["dispatch_state"] == "waiting"
    assert by_job_id[first_id]["queue_ahead"] == 0
    assert by_job_id[first_id]["next_attempt_at"] == deferred.next_attempt_at
    assert by_job_id[first_id]["dispatch_error"] == {
        "code": "PROVIDER_BUSY",
        "message": "Another job is running.",
    }
    assert by_job_id[second_id]["queue_ahead"] == 1

    with store.connection() as db:
        db.execute(
            """UPDATE connect_job_dispatch SET automation_paused_at = ?
            WHERE job_id = ?""",
            (created_at.isoformat(), first_id),
        )

    assert store.connect_queue_ahead(second_id) == 0
    results = store.recent(1)[0]["attachments"][0]["capability_results"]
    by_job_id = {result["job_id"]: result for result in results}
    assert by_job_id[second_id]["queue_ahead"] == 0


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
            f"INSERT INTO connect_attachment_jobs ({', '.join(columns)}) VALUES ({placeholders})",
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
    assert store.connect_dispatch(empty.job_id).capability_produces == ("text/plain",)  # type: ignore[union-attr]


def test_connect_v2_output_contract_boundaries(tmp_path: Path) -> None:
    store = Store(tmp_path / "state" / "watcher.sqlite3")
    store.initialize()
    seed_pdf_attachment(store)

    with pytest.raises(ValueError, match="output authority"):
        create_v2_connect_job(store, capability_produces=())

    maximum = tuple(f"application/x{index:02d}-" + "a" * 111 for index in range(16))
    create_v2_connect_job(store, capability_produces=maximum)
    assert store.connect_dispatch(
        "33333333-3333-4333-8333-333333333333"
    ).capability_produces == maximum  # type: ignore[union-attr]

    with pytest.raises(ValueError, match="output authority"):
        create_v2_connect_job(
            store,
            capability_produces=maximum + ("text/plain",),
        )


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
            capability_produces=("text/plain",),
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


def _truncation_analysis() -> dict[str, object]:
    return {
        "category": "informational",
        "priority": "low",
        "summary": "Long update.",
        "action_required": False,
        "suggested_action": None,
        "deadline_text": None,
        "deadline_iso": None,
        "confidence": 0.9,
    }


def _truncation_store(tmp_path: Path, *message_ids: str) -> Store:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    for message_id in message_ids:
        store.add_message(
            message_id=message_id,
            thread_id=None,
            sender="a@b.com",
            sender_name=None,
            subject="S",
            received_at="2026-08-29T12:00:00+00:00",
        )
    return store


@pytest.mark.parametrize(
    ("chars", "source", "truncated"),
    [(20_000, 20_001, True), (20_000, 20_000, False), (0, 0, False)],
)
def test_body_counts_persist_with_the_analysis(
    tmp_path: Path, chars: int, source: int, truncated: bool
) -> None:
    store = _truncation_store(tmp_path, "m1")

    store.mark_analyzed("m1", _truncation_analysis(), body_chars=chars, body_source_chars=source)

    item = store.recent(1)[0]
    assert item["summary"] == "Long update."
    assert (item["body_analyzed_chars"], item["body_source_chars"]) == (chars, source)
    assert item["body_truncated"] is truncated
    [delivery] = store.pending_delivery()
    assert (delivery.analysis_body_chars, delivery.analysis_body_source_chars) == (chars, source)
    [intent] = store.notification_intents(kind="analysis")
    assert (intent.analysis_body_chars, intent.analysis_body_source_chars) == (chars, source)


@pytest.mark.parametrize(
    ("chars", "source"), [(None, 10), (10, None), (11, 10), (-1, 10), (0, -1)]
)
def test_inconsistent_body_counts_roll_back_the_analysis(
    tmp_path: Path, chars: int | None, source: int | None
) -> None:
    store = _truncation_store(tmp_path, "m1")

    with pytest.raises(sqlite3.IntegrityError):
        store.mark_analyzed(
            "m1", _truncation_analysis(), body_chars=chars, body_source_chars=source
        )

    item = store.recent(1)[0]
    assert item["status"] == "pending"
    assert item["summary"] is None
    assert item["body_truncated"] is None


def test_unanalyzed_and_uncounted_rows_report_unknown_truncation(tmp_path: Path) -> None:
    store = _truncation_store(tmp_path, "m1", "m2")
    store.mark_analyzed("m2", _truncation_analysis())

    items = {str(item["message_id"]): item for item in store.recent(10)}

    for message_id in ("m1", "m2"):
        item = items[message_id]
        assert item["body_analyzed_chars"] is None
        assert item["body_source_chars"] is None
        assert item["body_truncated"] is None


def test_deleting_a_message_removes_its_body_counts(tmp_path: Path) -> None:
    store = _truncation_store(tmp_path, "m1")
    store.mark_analyzed("m1", _truncation_analysis(), body_chars=5, body_source_chars=9)

    assert store.delete_message("m1") is True

    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_schema_27_database_gains_unknown_body_counts(tmp_path: Path) -> None:
    store = _truncation_store(tmp_path, "m1")
    store.mark_analyzed("m1", _truncation_analysis())
    with store.connection() as db:
        db.execute("DROP TRIGGER messages_body_counts_consistent_insert")
        db.execute("DROP TRIGGER messages_body_counts_consistent_update")
        db.execute("ALTER TABLE messages DROP COLUMN analysis_body_chars")
        db.execute("ALTER TABLE messages DROP COLUMN analysis_body_source_chars")
        db.execute("PRAGMA user_version = 27")

    migrated = Store(store.path)
    migrated.initialize()

    with migrated.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 30
        triggers = {
            str(row["name"])
            for row in db.execute(
                """SELECT name FROM sqlite_schema
                WHERE type = 'trigger' AND name LIKE 'messages_body_counts_%'"""
            ).fetchall()
        }
    assert triggers == {
        "messages_body_counts_consistent_insert",
        "messages_body_counts_consistent_update",
    }
    item = migrated.recent(1)[0]
    assert item["summary"] == "Long update."
    assert item["body_truncated"] is None
    with pytest.raises(sqlite3.IntegrityError), migrated.connection() as db:
        db.execute("UPDATE messages SET analysis_body_chars = 3 WHERE message_id = 'm1'")


def test_body_count_schema_bump_rejects_previous_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "db.sqlite3"
    Store(database).initialize()

    monkeypatch.setattr(db_module, "SCHEMA_VERSION", 27)
    with pytest.raises(RuntimeError, match="newer than supported version 27"):
        Store(database).initialize()


def test_production_store_requires_explicit_body_counts(tmp_path: Path) -> None:
    store = _truncation_store(tmp_path, "m1")
    identity = store.message_source("m1").mailbox_identity_key
    assert identity is not None

    with pytest.raises(TypeError, match="body_chars"):
        ProductionStore(store.path).mark_analyzed(  # type: ignore[call-arg]
            "m1", _truncation_analysis(), mailbox_identity_key=identity
        )

    assert store.recent(1)[0]["status"] == "pending"


# Thread view M1: vendor records and thread keys (plans/PR-Thread-M1-Vendors-Thread-Keys.md).


def test_vendor_records_enforce_one_vendor_per_address(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    acme = store.create_vendor("Acme Supply")
    other = store.create_vendor("Other Co")

    assert store.add_vendor_address(acme, "billing@acme.com") is True
    assert store.add_vendor_address(acme, "billing@acme.com") is False
    with pytest.raises(db_module.VendorAddressConflict, match="Acme Supply"):
        store.add_vendor_address(other, "billing@acme.com")
    with pytest.raises(KeyError):
        store.add_vendor_address("00000000-0000-4000-8000-000000000000", "x@acme.com")

    store.rename_vendor(acme, "Acme Supplies")
    assert [(v["display_name"], v["addresses"]) for v in store.list_vendors()] == [
        ("Acme Supplies", ["billing@acme.com"]),
        ("Other Co", []),
    ]
    assert store.vendor_for_address("billing@acme.com")["vendor_id"] == acme
    assert store.remove_vendor_address(acme, "billing@acme.com") is True
    assert store.remove_vendor_address(acme, "billing@acme.com") is False
    assert store.add_vendor_address(other, "billing@acme.com") is True
    assert store.delete_vendor(other) == ["billing@acme.com"]
    assert store.vendor_for_address("billing@acme.com") is None
    with pytest.raises(KeyError):
        store.delete_vendor(other)


@pytest.mark.parametrize("name", ["", "x" * 201])
def test_vendor_display_name_is_bounded_in_storage(tmp_path: Path, name: str) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    with pytest.raises(sqlite3.IntegrityError):
        store.create_vendor(name)


def _imap_message(
    store: Store, uid: str, own: str | None, replies: tuple[str, ...] = ()
) -> str:
    message_id = f"imap-message-{uid}"
    store.add_message(
        message_id=message_id,
        provider="imap",
        account_id="imap-account",
        provider_message_id=f"imap:mailbox:44:{uid}",
        thread_id=f"<{own}>" if own else None,
        sender="a@b.com",
        sender_name=None,
        subject="S",
        received_at="2026-08-29T12:00:00+00:00",
        rfc_message_id=own,
        reply_ids=replies,
    )
    return message_id


def _retained_duplicate(store: Store, uid: str, own: str) -> str:
    """Store a second row for a Message-ID the way a pre-M2 database retained it.

    Capture records a known logical identity's second copy as a location, not a
    row (contract D-identity), so the duplicate is written through SQL.
    """
    message_id = _imap_message(store, uid, f"placeholder-{uid}@dup")
    with store.connection() as db:
        db.execute(
            "UPDATE messages SET rfc_message_id = ?, thread_id = ? WHERE message_id = ?",
            (own, f"<{own}>", message_id),
        )
    return message_id


def _thread_keys(store: Store) -> dict[str, str | None]:
    with store.connection() as db:
        rows = db.execute("SELECT message_id, thread_key FROM messages").fetchall()
    return {str(row["message_id"]): row["thread_key"] for row in rows}


@pytest.mark.parametrize("order", [("a", "b", "c"), ("c", "b", "a"), ("b", "c", "a")])
def test_imap_thread_key_is_one_component_in_any_arrival_order(
    tmp_path: Path, order: tuple[str, ...]
) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    chain = {"a": ("1", "a@x", ()), "b": ("2", "b@x", ("a@x",)), "c": ("3", "c@x", ("b@x", "a@x"))}
    for name in order:
        uid, own, replies = chain[name]
        _imap_message(store, uid, own, replies)

    keys = set(_thread_keys(store).values())
    assert len(keys) == 1 and None not in keys


def test_imap_merge_keeps_the_component_of_the_smallest_member(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    first = _imap_message(store, "1", "root-one@x")
    second = _imap_message(store, "2", "root-two@x")
    before = _thread_keys(store)
    assert before[first] != before[second]

    bridge = _imap_message(store, "3", "bridge@x", ("root-two@x", "root-one@x"))
    after = _thread_keys(store)

    # Canonical order is the source identity: imap:mailbox:44:1 sorts first.
    assert after[first] == after[second] == after[bridge] == before[first]
    with store.connection() as db:
        aliases = db.execute("SELECT old_key, survivor_key FROM thread_key_aliases").fetchall()
    assert [(row["old_key"], row["survivor_key"]) for row in aliases] == [
        (before[second], before[first])
    ]


def test_imap_components_without_message_id_merge_deterministically(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    left = _imap_message(store, "5", None, ("thread-left@x",))
    right = _imap_message(store, "4", None, ("thread-right@x",))
    before = _thread_keys(store)
    bridge = _imap_message(store, "6", None, ("thread-left@x", "thread-right@x"))
    after = _thread_keys(store)

    assert after[left] == after[right] == after[bridge] == before[right]


def test_imap_message_without_ids_forms_its_own_component(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    one = _imap_message(store, "1", None)
    two = _imap_message(store, "2", None)
    keys = _thread_keys(store)
    assert keys[one] and keys[two] and keys[one] != keys[two]


def test_deleting_a_components_last_message_releases_its_ids(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    only = _imap_message(store, "1", "solo@x")
    assert store.delete_message(only) is True
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM imap_thread_ids").fetchone()[0] == 0
    fresh = _imap_message(store, "2", "reply@x", ("solo@x",))
    assert _thread_keys(store)[fresh]


def test_gmail_and_microsoft_thread_keys_are_the_provider_thread_ids(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    for provider, thread in (("gmail", "gmail-thread"), ("microsoft365", "conversation-1")):
        store.add_message(
            message_id=f"{provider}-1",
            provider=provider,
            account_id=f"{provider}-account",
            provider_message_id=f"{provider}-id",
            thread_id=thread,
            sender="a@b.com",
            sender_name=None,
            subject="S",
            received_at="2026-08-29T12:00:00+00:00",
        )
    keys = _thread_keys(store)
    assert keys["gmail-1"] == "gmail-thread"
    assert keys["microsoft365-1"] == "conversation-1"


def _reset_to_schema_29(store: Store) -> None:
    """Undo schema 30 (Sent capture) so a migration test can start from v29."""
    with store.connection() as db:
        for statement in (
            "DROP VIEW IF EXISTS logical_messages",
            "DROP TRIGGER messages_delete_recipients_and_locations",
            "DROP INDEX idx_messages_logical_identity",
            "DROP INDEX idx_messages_logical_of",
            "DROP INDEX idx_message_locations_message",
            "DROP TABLE message_recipients",
            "DROP TABLE message_locations",
            "DROP TABLE mailbox_folder_state",
            "DROP TABLE mailbox_sent_scope",
            "ALTER TABLE messages DROP COLUMN logical_of",
            "ALTER TABLE messages DROP COLUMN capture_timezone",
        ):
            db.execute(statement)
        db.execute("PRAGMA user_version = 29")


def _reset_to_schema_28(store: Store) -> None:
    _reset_to_schema_29(store)
    with store.connection() as db:
        db.execute("UPDATE messages SET thread_key = NULL, rfc_message_id = NULL")
        db.execute("DELETE FROM imap_thread_ids")
        db.execute("DROP TRIGGER messages_release_imap_component")
        db.execute("DROP INDEX idx_messages_thread_key")
        db.execute("DROP INDEX idx_messages_rfc_message_id")
        db.execute("ALTER TABLE messages DROP COLUMN thread_key")
        db.execute("ALTER TABLE messages DROP COLUMN rfc_message_id")
        db.execute("PRAGMA user_version = 28")


def test_schema_28_database_keys_retained_rows(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    store.add_message(
        message_id="gmail-old", thread_id="gmail-thread", sender="a@b.com", sender_name=None,
        subject="S", received_at="2026-08-29T12:00:00+00:00",
    )
    old = _imap_message(store, "1", "old-root@x")
    first_copy = _imap_message(store, "3", "dup@x")
    second_copy = _retained_duplicate(store, "4", "dup@x")
    idless = _imap_message(store, "5", None)
    malformed = _imap_message(store, "6", None)
    store.add_message(
        message_id="microsoft-old", provider="microsoft365", account_id="m-account",
        provider_message_id="m-id", thread_id=None, sender="a@b.com", sender_name=None,
        subject="S", received_at="2026-08-29T12:00:00+00:00",
    )
    with store.connection() as db:
        db.execute(
            "UPDATE messages SET thread_id = '<not-an-id>' WHERE message_id = ?", (malformed,)
        )
    _reset_to_schema_28(store)

    migrated = Store(store.path)
    migrated.initialize()

    keys = _thread_keys(migrated)
    assert keys["gmail-old"] == "gmail-thread"
    assert None not in keys.values()
    assert keys[first_copy] == keys[second_copy]
    assert len({keys[old], keys[first_copy], keys[idless], keys[malformed]}) == 4
    assert keys["microsoft-old"] not in {keys[old], keys[idless], keys[malformed]}
    with migrated.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 30
        assert db.execute(
            "SELECT rfc_message_id FROM messages WHERE message_id = ?", (old,)
        ).fetchone()[0] == "old-root@x"
        assert db.execute(
            "SELECT rfc_message_id FROM messages WHERE message_id = ?", (malformed,)
        ).fetchone()[0] is None
        stored = db.execute(
            "SELECT rfc_message_id FROM messages WHERE message_id IN (?, ?)",
            (first_copy, second_copy),
        ).fetchall()
        assert [row[0] for row in stored] == ["dup@x", "dup@x"]
    with migrated.connection() as db:
        gaps = {row[0] for row in db.execute("SELECT message_id FROM imap_reply_header_gaps")}
    assert gaps == {old, first_copy, second_copy, idless, malformed}
    # The retained row stored the raw header <old-root@x>; a reply names it bare.
    reply = _imap_message(migrated, "2", "reply@x", ("old-root@x",))
    assert _thread_keys(migrated)[reply] == keys[old]
    assert migrated.delete_message(old) is True
    with migrated.connection() as db:
        assert db.execute(
            "SELECT 1 FROM imap_reply_header_gaps WHERE message_id = ?", (old,)
        ).fetchone() is None


def test_thread_identity_schema_bump_rejects_previous_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "db.sqlite3"
    Store(database).initialize()

    monkeypatch.setattr(db_module, "SCHEMA_VERSION", 28)
    with pytest.raises(RuntimeError, match="newer than supported version 28"):
        Store(database).initialize()


def test_vendor_address_removal_records_a_dismissal_that_leaves_with_its_vendor(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    acme = store.create_vendor("Acme")
    store.add_vendor_address(acme, "billing@acme.com")

    def dismissals() -> list[tuple[str, str]]:
        with store.connection() as db:
            rows = db.execute("SELECT vendor_id, address FROM vendor_address_dismissals")
            return [(row["vendor_id"], row["address"]) for row in rows.fetchall()]

    assert store.remove_vendor_address(acme, "other@acme.com") is False
    assert dismissals() == []
    assert store.remove_vendor_address(acme, "billing@acme.com") is True
    assert dismissals() == [(acme, "billing@acme.com")]
    store.delete_vendor(acme)
    assert dismissals() == []


def test_imap_merges_repoint_earlier_aliases(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    a = _imap_message(store, "3", "a@x")
    b = _imap_message(store, "2", "b@x")
    first = _thread_keys(store)
    _imap_message(store, "4", "ab@x", ("a@x", "b@x"))
    c = _imap_message(store, "1", "c@x")
    second = _thread_keys(store)
    _imap_message(store, "5", None, ("b@x", "c@x"))

    # Canonical order: imap:mailbox:44:1 < :2 < :3, so c's component survives both.
    assert set(_thread_keys(store).values()) == {second[c]}
    with store.connection() as db:
        rows = db.execute("SELECT old_key, survivor_key FROM thread_key_aliases").fetchall()
    aliases = {row["old_key"]: row["survivor_key"] for row in rows}
    assert aliases == {first[a]: second[c], first[b]: second[c]}


def test_malformed_ids_never_join_components(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    one = _imap_message(store, "1", None, ("not-an-id", "@host"))
    two = _imap_message(store, "2", None, ("not-an-id", "left@"))
    keys = _thread_keys(store)
    assert keys[one] != keys[two]
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM imap_thread_ids").fetchone()[0] == 0


def test_messages_without_a_provider_thread_id_get_their_own_key(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    for index in (1, 2):
        store.add_message(
            message_id=f"microsoft-{index}", provider="microsoft365", account_id="m-account",
            provider_message_id=f"m-{index}", thread_id=None, sender="a@b.com",
            sender_name=None, subject="S", received_at="2026-08-29T12:00:00+00:00",
        )
    keys = _thread_keys(store)
    assert keys["microsoft-1"] and keys["microsoft-2"]
    assert keys["microsoft-1"] != keys["microsoft-2"]


@pytest.mark.parametrize("status", ["continuity_proven", "unresolved"])
def test_schema_28_legacy_rows_without_a_mailbox_identity_upgrade(
    tmp_path: Path, status: str
) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    legacy_root = _imap_message(store, "1", "legacy-root@x")
    legacy_copy = _retained_duplicate(store, "2", "legacy-root@x")
    with store.connection() as db:
        identity = db.execute(
            "SELECT mailbox_identity_key FROM messages WHERE message_id = ?", (legacy_root,)
        ).fetchone()[0]
        # Rows from before mailbox identities and admission provenance existed.
        db.execute("DROP TRIGGER messages_admission_provenance_immutable")
        db.execute(
            """UPDATE messages SET mailbox_identity_key = NULL, admission_kind = NULL,
                admission_selector_id = NULL, admission_display_name = NULL,
                admission_mailbox_identity_key = NULL, admitted_at = NULL"""
        )
        db.execute(
            """UPDATE mail_accounts SET legacy_identity_status = ?, legacy_identity_key = ?
            WHERE account_id = 'imap-account'""",
            (status, identity if status == "continuity_proven" else None),
        )
    _reset_to_schema_28(store)

    migrated = Store(store.path)
    migrated.initialize()

    keys = _thread_keys(migrated)
    assert None not in keys.values()
    reply = _imap_message(migrated, "3", "reply@x", ("legacy-root@x",))
    keys = _thread_keys(migrated)
    if status == "continuity_proven":
        # Proven continuous with today's mailbox: the legacy rows thread with new mail.
        assert keys[legacy_root] == keys[legacy_copy] == keys[reply]
    else:
        # Unknown mailbox: legacy rows never join current mail.
        assert keys[reply] not in {keys[legacy_root], keys[legacy_copy]}
        assert keys[legacy_root] != keys[legacy_copy]


def test_a_reply_to_two_parents_merges_their_components(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    one = _imap_message(store, "1", "parent-one@x")
    two = _imap_message(store, "2", "parent-two@x")
    reply = _imap_message(store, "3", "reply@x", ("parent-one@x", "parent-two@x"))
    keys = _thread_keys(store)
    assert keys[one] == keys[two] == keys[reply]


def test_a_suppressed_bridging_message_changes_no_component(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    one = _imap_message(store, "1", "root-one@x")
    two = _imap_message(store, "2", "root-two@x")
    bridge = _imap_message(store, "3", "bridge@x", ("root-one@x", "root-two@x"))
    assert store.delete_message(bridge) is True
    before = _thread_keys(store)
    with store.connection() as db:
        registered = db.execute("SELECT COUNT(*) FROM imap_thread_ids").fetchone()[0]
        aliases = db.execute("SELECT COUNT(*) FROM thread_key_aliases").fetchone()[0]

    # Recapturing the deleted bridge is suppressed: no row, no ids, no merge.
    assert _imap_message(store, "3", "bridge-again@x", ("root-one@x", "root-two@x")) == bridge
    with store.connection() as db:
        assert db.execute(
            "SELECT 1 FROM messages WHERE message_id = ?", (bridge,)
        ).fetchone() is None
        assert db.execute("SELECT COUNT(*) FROM imap_thread_ids").fetchone()[0] == registered
        assert db.execute("SELECT COUNT(*) FROM thread_key_aliases").fetchone()[0] == aliases
        assert db.execute(
            "SELECT 1 FROM imap_thread_ids WHERE rfc_id = 'bridge-again@x'"
        ).fetchone() is None
    assert _thread_keys(store) == before
    assert before[one] == before[two]


def test_a_suppressed_message_cannot_bridge_separate_components(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    one = _imap_message(store, "1", "root-one@x")
    two = _imap_message(store, "2", "root-two@x")
    lone = _imap_message(store, "3", "lone@x")
    assert store.delete_message(lone) is True
    before = _thread_keys(store)
    assert before[one] != before[two]

    # The deleted source identity is suppressed, so its new ids merge nothing.
    _imap_message(store, "3", "lone@x", ("root-one@x", "root-two@x"))

    after = _thread_keys(store)
    assert lone not in after
    assert after == before
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM thread_key_aliases").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("statement", "index"),
    [
        (
            "UPDATE messages SET thread_key = 'k' WHERE provider = 'imap'"
            " AND account_id = 'a' AND thread_key = 'old'",
            "idx_messages_thread_key",
        ),
        (
            "UPDATE imap_thread_ids SET thread_key = 'k' WHERE provider = 'imap'"
            " AND account_id = 'a' AND thread_key = 'old'",
            "idx_imap_thread_ids_key",
        ),
        (
            "UPDATE thread_key_aliases SET survivor_key = 'k' WHERE survivor_key = 'old'",
            "idx_thread_key_aliases_survivor",
        ),
    ],
)
def test_component_queries_use_key_led_indexes(
    tmp_path: Path, statement: str, index: str
) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    with store.connection() as db:
        plan = " ".join(str(row[-1]) for row in db.execute(f"EXPLAIN QUERY PLAN {statement}"))
    assert index in plan, plan


def test_an_oversized_provider_thread_id_still_keys_its_thread(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    long_thread = "t" * 600
    for index in (1, 2):
        store.add_message(
            message_id=f"gmail-long-{index}", provider="gmail", account_id="g-account",
            provider_message_id=f"g-{index}", thread_id=long_thread, sender="a@b.com",
            sender_name=None, subject="S", received_at="2026-08-29T12:00:00+00:00",
        )
    keys = _thread_keys(store)
    assert keys["gmail-long-1"] == keys["gmail-long-2"]
    assert len(keys["gmail-long-1"].encode()) <= 512


def test_schema_28_upgrade_keys_an_oversized_provider_thread_id(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    for index in (1, 2):
        store.add_message(
            message_id=f"gmail-old-{index}", provider="gmail", account_id="g-account",
            provider_message_id=f"g-{index}", thread_id="short", sender="a@b.com",
            sender_name=None, subject="S", received_at="2026-08-29T12:00:00+00:00",
        )
    _reset_to_schema_28(store)
    with store.connection() as db:
        db.execute("UPDATE messages SET thread_id = ?", ("t" * 600,))

    migrated = Store(store.path)
    migrated.initialize()

    keys = _thread_keys(migrated)
    assert keys["gmail-old-1"] == keys["gmail-old-2"]
    assert len(keys["gmail-old-1"].encode()) <= 512


# Thread view M2.1: recipients, locations, folder cursors, Sent scope (schema 30).


def _registered_imap_store(tmp_path: Path) -> tuple[Store, str]:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    identity = "a" * 64
    store.register_mail_account(
        "imap", "imap-account-2", display_name="IMAP", address="owner@example.com", active=True
    )
    store.reconcile_mailbox_identity(
        "imap", "imap-account-2", identity, legacy_status="replacement", preserve_cursor=True
    )
    return store, identity


def test_capture_records_recipients_locations_and_date_context(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    store.add_message(
        message_id="gmail-both", thread_id="t", sender="a@b.com", sender_name=None,
        subject="S", received_at="2026-08-29T12:00:00+00:00",
        to=("a@vendor.com", "b@vendor.com"), cc=("c@other.com",),
        locations=frozenset({"inbox", "sent"}), capture_timezone="America/Chicago",
    )
    with store.connection() as db:
        recipients = db.execute(
            """SELECT field, position, address FROM message_recipients
            WHERE message_id = 'gmail-both' ORDER BY field DESC, position"""
        ).fetchall()
        zone = db.execute(
            "SELECT capture_timezone FROM messages WHERE message_id = 'gmail-both'"
        ).fetchone()[0]
    assert [tuple(row) for row in recipients] == [
        ("to", 0, "a@vendor.com"), ("to", 1, "b@vendor.com"), ("cc", 0, "c@other.com"),
    ]
    assert store.message_locations("gmail-both") == ["inbox", "sent"]
    assert zone == "America/Chicago"


def test_a_message_without_locations_is_recorded_in_the_inbox(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    only = _imap_message(store, "1", "only@x")
    assert store.message_locations(only) == ["inbox"]


def test_a_second_copy_of_a_logical_message_is_a_location_not_a_row(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    first = _imap_message(store, "1", "same@x")

    inserted = store.add_message(
        message_id="imap-message-sent-copy",
        provider="imap",
        account_id="imap-account",
        provider_message_id="imap:sent:77:3",
        thread_id="<same@x>",
        sender="a@b.com",
        sender_name=None,
        subject="S",
        received_at="2026-08-29T12:00:00+00:00",
        rfc_message_id="same@x",
        locations=frozenset({"sent"}),
    )

    assert inserted is False
    with store.connection() as db:
        assert db.execute(
            "SELECT 1 FROM messages WHERE message_id = 'imap-message-sent-copy'"
        ).fetchone() is None
        copies = db.execute(
            "SELECT provider_message_id, location FROM message_locations WHERE message_id = ?",
            (first,),
        ).fetchall()
    assert sorted((row[0], row[1]) for row in copies) == [
        ("imap:mailbox:44:1", "inbox"), ("imap:sent:77:3", "sent"),
    ]
    assert store.message_locations(first) == ["inbox", "sent"]


def test_record_message_location_adds_only_new_locations(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    store.add_message(
        message_id="gmail-1", thread_id="t", sender="a@b.com", sender_name=None,
        subject="S", received_at="2026-08-29T12:00:00+00:00", locations=frozenset({"inbox"}),
    )
    with store.connection() as db:
        row = db.execute(
            "SELECT provider, account_id, mailbox_identity_key, provider_message_id FROM messages"
        ).fetchone()
    scope = {
        "provider": row[0], "account_id": row[1], "mailbox_identity_key": row[2],
        "provider_message_id": row[3],
    }
    assert store.message_locations("gmail-1") == ["inbox"]
    assert store.record_message_location(**scope, locations=frozenset({"inbox", "sent"})) == 1
    assert store.record_message_location(**scope, locations=frozenset({"sent"})) == 0
    assert store.message_locations("gmail-1") == ["inbox", "sent"]
    assert store.record_message_location(
        **{**scope, "provider_message_id": "unknown"}, locations=frozenset({"sent"})
    ) == 0


def test_schema_29_upgrade_locates_retained_rows_and_coalesces_duplicates(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    root = _imap_message(store, "1", "root@x")
    duplicate = _retained_duplicate(store, "2", "root@x")
    lone = _imap_message(store, "3", None)
    store.add_message(
        message_id="gmail-old", thread_id="gmail-thread", sender="a@b.com", sender_name=None,
        subject="S", received_at="2026-08-29T12:00:00+00:00",
    )
    _reset_to_schema_29(store)

    migrated = Store(store.path)
    migrated.initialize()

    with migrated.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 30
        logical = {
            row[0]: row[1]
            for row in db.execute("SELECT message_id, logical_of FROM messages").fetchall()
        }
        zones = {row[0] for row in db.execute("SELECT capture_timezone FROM messages")}
        duplicate_locations = db.execute(
            "SELECT COUNT(*) FROM message_locations WHERE message_id = ?", (duplicate,)
        ).fetchone()[0]
    assert logical == {root: None, duplicate: root, lone: None, "gmail-old": None}
    assert zones == {None}
    assert migrated.message_locations(root) == ["inbox"]
    assert duplicate_locations == 0
    assert migrated.message_locations(lone) == ["inbox"]
    assert migrated.message_locations("gmail-old") == ["inbox"]
    # Running again changes nothing.
    Store(store.path).initialize()
    assert migrated.message_locations(root) == ["inbox"]


def test_folder_cursors_are_per_folder_and_identity_checked(tmp_path: Path) -> None:
    store, identity = _registered_imap_store(tmp_path)
    scope = {
        "provider": "imap", "account_id": "imap-account-2",
        "mailbox_identity_key": identity, "folder": "sent",
    }
    assert store.folder_state(**scope) is None
    store.set_folder_state("cursor-1", **scope)
    assert store.folder_state(**scope)[0] == "cursor-1"
    store.set_folder_state("cursor-2", **scope)
    assert store.folder_state(**scope)[0] == "cursor-2"
    assert store.state(provider="imap", account_id="imap-account-2") is None
    with pytest.raises(db_module.MailboxIdentityChanged):
        store.set_folder_state("cursor-3", **{**scope, "mailbox_identity_key": "b" * 64})


def test_sent_scope_defaults_to_not_polled(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    store.register_mail_account(
        "imap", "imap-account", display_name="A", address="a@example.com", active=True
    )
    assert store.sent_scope("imap", "imap-account") == "not_polled"
    store.set_sent_scope("imap", "imap-account", "unavailable")
    assert store.sent_scope("imap", "imap-account") == "unavailable"
    store.set_sent_scope("imap", "imap-account", "available")
    assert store.sent_scope("imap", "imap-account") == "available"
    with pytest.raises(sqlite3.IntegrityError):
        store.set_sent_scope("imap", "imap-account", "maybe")


def test_sent_capture_schema_bump_rejects_previous_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "db.sqlite3"
    Store(database).initialize()

    monkeypatch.setattr(db_module, "SCHEMA_VERSION", 29)
    with pytest.raises(RuntimeError, match="newer than supported version 29"):
        Store(database).initialize()


def test_imap_merge_ranks_by_the_smallest_recorded_location(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    a = _imap_message(store, "5", "a@x")
    b = _imap_message(store, "4", "b@x")
    before = _thread_keys(store)
    with store.connection() as db:
        identity = db.execute(
            "SELECT mailbox_identity_key FROM messages WHERE message_id = ?", (a,)
        ).fetchone()[0]
    # A Sent copy of a, whose source identity sorts before b's (…:1 < …:4).
    assert store.record_message_location(
        provider="imap", account_id="imap-account", mailbox_identity_key=identity,
        provider_message_id="imap:mailbox:44:5", locations=frozenset({"sent"}),
    ) == 1
    with store.connection() as db:
        db.execute(
            "UPDATE message_locations SET provider_message_id = 'imap:mailbox:44:1' "
            "WHERE message_id = ? AND location = 'sent'", (a,),
        )

    bridge = _imap_message(store, "6", "bridge@x", ("a@x", "b@x"))

    after = _thread_keys(store)
    assert after[a] == after[b] == after[bridge] == before[a]


def test_deleting_a_message_removes_its_recipients_and_locations(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    store.add_message(
        message_id="gmail-gone", thread_id="t", sender="a@b.com", sender_name=None,
        subject="S", received_at="2026-08-29T12:00:00+00:00",
        to=("a@vendor.com",), locations=frozenset({"inbox", "sent"}),
    )
    assert store.delete_message("gmail-gone") is True
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM message_recipients").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM message_locations").fetchone()[0] == 0


def test_deleting_any_row_of_a_logical_message_removes_the_unit_and_suppresses_every_copy(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    root = _imap_message(store, "1", "root@x")
    duplicate = _retained_duplicate(store, "2", "root@x")
    _reset_to_schema_29(store)
    migrated = Store(store.path)
    migrated.initialize()
    with migrated.connection() as db:
        identity = db.execute(
            "SELECT mailbox_identity_key FROM messages WHERE message_id = ?", (root,)
        ).fetchone()[0]
        assert db.execute(
            "SELECT logical_of FROM messages WHERE message_id = ?", (duplicate,)
        ).fetchone()[0] == root
    # A Sent copy captured later is a third source identity of the same message.
    store_for_copy = migrated
    assert store_for_copy.add_message(
        message_id="imap-sent-copy-9",
        provider="imap",
        account_id="imap-account",
        provider_message_id="imap:sent:77:9",
        thread_id="<root@x>",
        sender="a@b.com",
        sender_name=None,
        subject="S",
        received_at="2026-08-29T12:00:00+00:00",
        rfc_message_id="root@x",
        locations=frozenset({"sent"}),
    ) is False

    # Deleting through the duplicate's id resolves the unit.
    assert migrated.delete_message(duplicate) is True

    with migrated.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM message_locations").fetchone()[0] == 0
    for source in ("imap:mailbox:44:1", "imap:mailbox:44:2", "imap:sent:77:9"):
        assert migrated.has_seen_message(
            source, provider="imap", account_id="imap-account", mailbox_identity_key=identity
        ), source
    assert migrated.delete_message(root) is False


def test_clearing_messages_suppresses_recorded_locations_too(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    only = _imap_message(store, "1", "only@x")
    with store.connection() as db:
        identity = db.execute(
            "SELECT mailbox_identity_key FROM messages WHERE message_id = ?", (only,)
        ).fetchone()[0]
    store_for_copy = store
    assert store_for_copy.add_message(
        message_id="imap-sent-copy-4",
        provider="imap",
        account_id="imap-account",
        provider_message_id="imap:sent:77:4",
        thread_id="<only@x>",
        sender="a@b.com",
        sender_name=None,
        subject="S",
        received_at="2026-08-29T12:00:00+00:00",
        rfc_message_id="only@x",
        locations=frozenset({"sent"}),
    ) is False

    assert store.clear_messages() == 1

    for source in ("imap:mailbox:44:1", "imap:sent:77:4"):
        assert store.has_seen_message(
            source, provider="imap", account_id="imap-account", mailbox_identity_key=identity
        ), source


def test_purging_a_canonical_row_takes_its_logical_children(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    root = _imap_message(store, "1", "root@x")
    duplicate = _retained_duplicate(store, "2", "root@x")
    _reset_to_schema_29(store)
    migrated = Store(store.path)
    migrated.initialize()

    # Any deletion path, not only delete_message: a bare DELETE stands in for the purge.
    with migrated.connection() as db:
        db.execute("DELETE FROM messages WHERE message_id = ?", (root,))
        remaining = [row[0] for row in db.execute("SELECT message_id FROM messages")]
    assert duplicate not in remaining and root not in remaining


def test_gmail_recovery_stores_the_metadata_polling_stores(tmp_path: Path) -> None:
    store, identity = _gmail_selector_store(tmp_path)
    revision, selector = store.add_gmail_label_selector(
        "gmail-default", identity, "Label_1", "Invoices", 0
    )
    store.set_state(
        "old-history", provider="gmail", account_id="gmail-default", mailbox_identity_key=identity
    )
    store.create_gmail_recovery_state(
        "gmail-default", identity, revision, [], [selector], 1, 2, "replacement-history"
    )
    store.store_gmail_recovery_page("gmail-default", identity, ["m1"], None)
    admission = db_module.AdmissionProvenance(
        kind="gmail_user_label",
        selector_id=selector.selector_id,
        display_name="Invoices",
        mailbox_identity_key=identity,
        admitted_at="2026-09-19T12:00:00+00:00",
    )
    message = db_module.GmailRecoveryMessage(
        message_id="local-m1",
        thread_id="t1",
        sender="sender@example.com",
        sender_name=None,
        subject="Invoice",
        received_at="2026-09-19T11:59:00+00:00",
        rfc_message_id="m1@vendor.example",
        reply_ids=("p@vendor.example",),
        to=("billing@vendor.example", "sales@vendor.example"),
        cc=("cc@other.example",),
        locations=frozenset({"sent"}),
        capture_timezone="America/Chicago",
    )

    assert store.finish_gmail_recovery_candidate(
        "gmail-default",
        identity,
        "m1",
        message=message,
        admission=admission,
        metadata_label_ids=frozenset({"SENT", "Label_1"}),
    )

    assert store.message_locations("local-m1") == ["sent"]
    with store.connection() as db:
        recipients = db.execute(
            """SELECT field, position, address FROM message_recipients
            WHERE message_id = 'local-m1' ORDER BY field DESC, position"""
        ).fetchall()
        row = db.execute(
            "SELECT rfc_message_id, capture_timezone FROM messages WHERE message_id = 'local-m1'"
        ).fetchone()
    assert [tuple(r) for r in recipients] == [
        ("to", 0, "billing@vendor.example"),
        ("to", 1, "sales@vendor.example"),
        ("cc", 0, "cc@other.example"),
    ]
    assert tuple(row) == ("m1@vendor.example", "America/Chicago")


def test_record_message_location_fills_missing_recipients_once(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    store.add_message(
        message_id="gmail-1", thread_id="t", sender="a@b.com", sender_name=None,
        subject="S", received_at="2026-08-29T12:00:00+00:00", locations=frozenset({"inbox"}),
    )
    with store.connection() as db:
        row = db.execute(
            "SELECT provider, account_id, mailbox_identity_key, provider_message_id FROM messages"
        ).fetchone()
    scope = {
        "provider": row[0], "account_id": row[1], "mailbox_identity_key": row[2],
        "provider_message_id": row[3],
    }

    def recipients() -> list[tuple[str, int, str]]:
        with store.connection() as db:
            rows = db.execute(
                """SELECT field, position, address FROM message_recipients
                WHERE message_id = 'gmail-1' ORDER BY field DESC, position"""
            ).fetchall()
        return [tuple(r) for r in rows]

    assert recipients() == []
    expected = [("to", 0, "a@vendor.com"), ("to", 1, "b@vendor.com"), ("cc", 0, "c@o.com")]
    assert store.record_message_location(
        **scope,
        locations=frozenset({"sent"}),
        to=("a@vendor.com", "b@vendor.com"),
        cc=("c@o.com",),
    ) == 1
    assert recipients() == expected
    # A later fetch never replaces what a row already has.
    store.record_message_location(
        **scope, locations=frozenset({"sent"}), to=("other@vendor.com",)
    )
    assert recipients() == expected


def test_a_second_copy_fills_the_recipients_its_logical_message_lacks(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    first = _imap_message(store, "1", "same@x")

    assert store.add_message(
        message_id="imap-message-sent-copy",
        provider="imap",
        account_id="imap-account",
        provider_message_id="imap:sent:77:3",
        thread_id="<same@x>",
        sender="a@b.com",
        sender_name=None,
        subject="S",
        received_at="2026-08-29T12:00:00+00:00",
        rfc_message_id="same@x",
        to=("billing@vendor.com",),
        locations=frozenset({"sent"}),
    ) is False

    with store.connection() as db:
        rows = db.execute(
            "SELECT message_id, field, position, address FROM message_recipients"
        ).fetchall()
    assert [tuple(r) for r in rows] == [(first, "to", 0, "billing@vendor.com")]


def test_purge_keeps_a_logical_message_until_every_row_has_expired(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    root = _imap_message(store, "1", "root@x")
    duplicate = _retained_duplicate(store, "2", "root@x")
    _reset_to_schema_29(store)
    store.initialize()
    with store.connection() as db:
        assert db.execute(
            "SELECT logical_of FROM messages WHERE message_id = ?", (duplicate,)
        ).fetchone()[0] == root
        db.execute(
            "UPDATE messages SET received_at = '2026-01-01T12:00:00+00:00' WHERE message_id = ?",
            (root,),
        )
        db.execute(
            "UPDATE messages SET received_at = '2026-09-01T12:00:00+00:00' WHERE message_id = ?",
            (duplicate,),
        )
    now = datetime(2026, 9, 10, tzinfo=UTC)

    # The canonical row has expired, its later-dated copy has not: the unit stays whole.
    outcome = store.purge_with_outcome(30, now=now)
    assert outcome.messages == 0
    assert store.has_message(root) and store.has_message(duplicate)
    assert store.message_locations(root) == ["inbox"]

    # Once every row has expired the unit goes together, locations included.
    outcome = store.purge_with_outcome(5, now=now)
    assert outcome.messages == 2
    assert not store.has_message(root) and not store.has_message(duplicate)
    assert store.message_locations(root) == []


def _coalesced_pair(tmp_path: Path) -> tuple[Store, str, str]:
    """A canonical row and a retained duplicate pointing at it, as the upgrade leaves them."""
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    root = _imap_message(store, "1", "root@x")
    duplicate = _retained_duplicate(store, "2", "root@x")
    _reset_to_schema_29(store)
    store.initialize()
    with store.connection() as db:
        assert db.execute(
            "SELECT logical_of FROM messages WHERE message_id = ?", (duplicate,)
        ).fetchone()[0] == root
    return store, root, duplicate


def _source_scope(store: Store, message_id: str) -> dict[str, str]:
    with store.connection() as db:
        row = db.execute(
            """SELECT provider, account_id, mailbox_identity_key, provider_message_id
            FROM messages WHERE message_id = ?""",
            (message_id,),
        ).fetchone()
    return {
        "provider": row[0], "account_id": row[1], "mailbox_identity_key": row[2],
        "provider_message_id": row[3],
    }


def test_location_count_is_per_source_identity(tmp_path: Path) -> None:
    store, root, duplicate = _coalesced_pair(tmp_path)
    root_scope = _source_scope(store, root)
    duplicate_scope = _source_scope(store, duplicate)
    def per_source(scope: dict[str, str]) -> list[str]:
        with store.connection() as db:
            rows = db.execute(
                """SELECT location FROM message_locations WHERE provider = ? AND account_id = ?
                AND mailbox_identity_key = ? AND provider_message_id = ? ORDER BY location""",
                (
                    scope["provider"], scope["account_id"], scope["mailbox_identity_key"],
                    scope["provider_message_id"],
                ),
            ).fetchall()
        return [row[0] for row in rows]

    # Both copies were assumed in the Inbox; each has its own folders.
    assert per_source(root_scope) == ["inbox"]
    assert per_source(duplicate_scope) == ["inbox"]

    assert store.record_message_location(**duplicate_scope, locations=frozenset({"sent"})) == 1
    assert per_source(duplicate_scope) == ["inbox", "sent"]
    assert per_source(root_scope) == ["inbox"]
    assert store.message_locations(root) == ["inbox", "sent"]


def test_upgrade_assumes_inbox_and_the_first_observation_stamps_it(tmp_path: Path) -> None:
    store, root, _duplicate = _coalesced_pair(tmp_path)
    scope = _source_scope(store, root)

    def recorded() -> list[str | None]:
        with store.connection() as db:
            rows = db.execute(
                """SELECT recorded_at FROM message_locations
                WHERE provider_message_id = ? ORDER BY location""",
                (scope["provider_message_id"],),
            ).fetchall()
        return [row[0] for row in rows]

    assert recorded() == [None]
    observed_at = datetime(2026, 9, 20, 12, tzinfo=UTC)
    assert (
        store.record_message_location(**scope, locations=frozenset({"inbox"}), now=observed_at)
        == 0
    )
    assert recorded() == [observed_at.isoformat()]


def test_clear_messages_removes_logical_units_whole(tmp_path: Path) -> None:
    store, root, duplicate = _coalesced_pair(tmp_path)
    lone = _imap_message(store, "3", None)
    root_scope = _source_scope(store, root)
    duplicate_scope = _source_scope(store, duplicate)

    assert store.clear_messages() == 3
    assert not store.has_message(root)
    assert not store.has_message(duplicate)
    assert not store.has_message(lone)
    # Every source identity of the unit is suppressed, the child's included.
    for scope in (root_scope, duplicate_scope):
        assert store.has_seen_message(
            scope["provider_message_id"],
            provider=scope["provider"],
            account_id=scope["account_id"],
            mailbox_identity_key=scope["mailbox_identity_key"],
        )


def test_inbox_lists_a_coalesced_message_once(tmp_path: Path) -> None:
    store, root, duplicate = _coalesced_pair(tmp_path)
    lone = _imap_message(store, "3", None)

    listed = [item["message_id"] for item in store.recent(10)]
    assert sorted(listed) == sorted([root, lone])
    # The duplicate's own row, and whatever it carries, stays stored.
    assert store.has_message(duplicate)
    items, next_cursor = store.query_inbox(limit=1)
    assert len(items) == 1 and next_cursor is not None
    items, _ = store.query_inbox(limit=1, cursor=next_cursor)
    assert [item["message_id"] for item in items] != [duplicate]


def test_locations_observed_with_sent_out_of_scope_are_not_stamped(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    store.add_message(
        message_id="gmail-1", thread_id="t", sender="a@b.com", sender_name=None,
        subject="S", received_at="2026-08-29T12:00:00+00:00", locations=frozenset({"inbox"}),
        scope_complete=False,
    )
    with store.connection() as db:
        row = db.execute(
            "SELECT provider, account_id, mailbox_identity_key, provider_message_id FROM messages"
        ).fetchone()
    scope = {
        "provider": row[0], "account_id": row[1], "mailbox_identity_key": row[2],
        "provider_message_id": row[3],
    }

    def stamps() -> list[str | None]:
        with store.connection() as db:
            rows = db.execute(
                """SELECT recorded_at FROM message_locations
                WHERE message_id = 'gmail-1' ORDER BY location"""
            ).fetchall()
        return [row[0] for row in rows]

    assert stamps() == [None]
    observed_at = datetime(2026, 9, 20, 12, tzinfo=UTC)
    assert store.record_message_location(
        **scope, locations=frozenset({"inbox", "sent"}), now=observed_at
    ) == 1
    assert stamps() == [observed_at.isoformat(), observed_at.isoformat()]


def test_a_coalesced_copy_recorded_as_a_location_counts_as_seen(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    _imap_message(store, "1", "same@x")
    assert store.add_message(
        message_id="imap-message-sent-copy",
        provider="imap",
        account_id="imap-account",
        provider_message_id="imap:sent:77:3",
        thread_id="<same@x>",
        sender="a@b.com",
        sender_name=None,
        subject="S",
        received_at="2026-08-29T12:00:00+00:00",
        rfc_message_id="same@x",
        locations=frozenset({"sent"}),
    ) is False
    with store.connection() as db:
        identity = db.execute(
            "SELECT mailbox_identity_key FROM messages WHERE message_id = 'imap-message-1'"
        ).fetchone()[0]

    # The copy has no row of its own, yet polling must not fetch and analyze it again.
    assert store.has_seen_message(
        "imap:sent:77:3",
        provider="imap",
        account_id="imap-account",
        mailbox_identity_key=identity,
    )


def test_work_queues_see_one_logical_message(tmp_path: Path) -> None:
    store, root, duplicate = _coalesced_pair(tmp_path)
    now = datetime(2026, 9, 10, 12, tzinfo=UTC)
    # The copy was analyzed before the upgrade and never delivered.
    store.mark_analyzed(
        duplicate,
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

    assert [m.message_id for m in store.pending(now)] == [root]
    assert all(m.message_id != duplicate for m in store.pending_delivery(now))
    assert all(i.message_id != duplicate for i in store.notification_intents())
    assert store.notification_intent_count() == 0
    with store.connection() as db:
        assert db.execute(
            "SELECT status FROM messages WHERE message_id = ?", (duplicate,)
        ).fetchone()[0] == "analyzed"


def test_an_incomplete_observation_clears_the_stamp_and_may_name_no_folder(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    store.add_message(
        message_id="gmail-1", thread_id="t", sender="a@b.com", sender_name=None,
        subject="S", received_at="2026-08-29T12:00:00+00:00", locations=frozenset({"inbox"}),
    )
    with store.connection() as db:
        row = db.execute(
            "SELECT provider, account_id, mailbox_identity_key, provider_message_id FROM messages"
        ).fetchone()
    scope = {
        "provider": row[0], "account_id": row[1], "mailbox_identity_key": row[2],
        "provider_message_id": row[3],
    }

    def stamps() -> list[str | None]:
        with store.connection() as db:
            rows = db.execute(
                "SELECT recorded_at FROM message_locations WHERE message_id = 'gmail-1'"
            ).fetchall()
        return [row[0] for row in rows]

    assert stamps() != [None]
    # Observed with Sent out of scope, in no folder in scope: nothing new, stamp cleared.
    assert store.record_message_location(**scope, locations=frozenset(), scope_complete=False) == 0
    assert stamps() == [None]
    # A later complete observation stamps it again.
    observed_at = datetime(2026, 9, 20, 12, tzinfo=UTC)
    store.record_message_location(**scope, locations=frozenset({"inbox"}), now=observed_at)
    assert stamps() == [observed_at.isoformat()]


def test_a_change_record_never_stamps_a_retained_row(tmp_path: Path) -> None:
    store, root, _duplicate = _coalesced_pair(tmp_path)
    scope = _source_scope(store, root)

    def stamps() -> list[str | None]:
        with store.connection() as db:
            rows = db.execute(
                """SELECT recorded_at FROM message_locations
                WHERE provider_message_id = ? ORDER BY location""",
                (scope["provider_message_id"],),
            ).fetchall()
        return [row[0] for row in rows]

    observed_at = datetime(2026, 9, 20, 12, tzinfo=UTC)
    # A star on the retained row names its folders, but its headers were never fetched.
    store.record_message_location(
        **scope, locations=frozenset({"inbox", "sent"}), headers_observed=False, now=observed_at
    )
    assert store.message_locations(root) == ["inbox", "sent"]
    assert stamps() == [None, None]
    # The fetch that brings its headers completes the observation.
    store.record_message_location(
        **scope, locations=frozenset({"inbox", "sent"}), to=("a@v.com",), now=observed_at
    )
    assert stamps() == [observed_at.isoformat(), observed_at.isoformat()]


def test_a_coalesced_copy_s_later_folder_is_recorded_on_its_logical_message(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    first = _imap_message(store, "1", "same@x")
    store.add_message(
        message_id="imap-message-sent-copy",
        provider="imap",
        account_id="imap-account",
        provider_message_id="imap:sent:77:3",
        thread_id="<same@x>",
        sender="a@b.com",
        sender_name=None,
        subject="S",
        received_at="2026-08-29T12:00:00+00:00",
        rfc_message_id="same@x",
        locations=frozenset({"sent"}),
    )
    with store.connection() as db:
        identity = db.execute(
            "SELECT mailbox_identity_key FROM messages WHERE message_id = ?", (first,)
        ).fetchone()[0]

    # The copy has no row; a later observation of it still reaches the logical message.
    assert store.record_message_location(
        provider="imap",
        account_id="imap-account",
        mailbox_identity_key=identity,
        provider_message_id="imap:sent:77:3",
        locations=frozenset({"inbox", "sent"}),
    ) == 1
    assert store.message_locations(first) == ["inbox", "sent"]


def test_a_sent_copy_s_reply_headers_merge_imap_components(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    root = _imap_message(store, "1", "root@x")
    other = _imap_message(store, "2", "other@x")
    assert _thread_keys(store)[root] != _thread_keys(store)[other]

    # The Sent copy of root carries reply headers naming the other component.
    assert store.add_message(
        message_id="imap-message-sent-copy",
        provider="imap",
        account_id="imap-account",
        provider_message_id="imap:sent:77:9",
        thread_id="<root@x>",
        sender="a@b.com",
        sender_name=None,
        subject="S",
        received_at="2026-08-29T12:00:00+00:00",
        rfc_message_id="root@x",
        reply_ids=("other@x",),
        locations=frozenset({"sent"}),
    ) is False

    keys = _thread_keys(store)
    assert keys[root] == keys[other]
    assert store.message_locations(root) == ["inbox", "sent"]


def test_message_sources_list_every_copy_canonical_first(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    first = _imap_message(store, "1", "same@x")
    store.add_message(
        message_id="imap-message-sent-copy",
        provider="imap",
        account_id="imap-account",
        provider_message_id="imap:sent:77:3",
        thread_id="<same@x>",
        sender="a@b.com",
        sender_name=None,
        subject="S",
        received_at="2026-08-29T12:00:00+00:00",
        rfc_message_id="same@x",
        locations=frozenset({"sent"}),
    )

    sources = store.message_sources(first)
    assert [s.provider_message_id for s in sources] == ["imap:mailbox:44:1", "imap:sent:77:3"]
    assert all(s.message_id == first for s in sources)


def test_sent_scope_reads_not_polled_for_an_inactive_account(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    store.register_mail_account(
        "imap", "imap-a", display_name="A", address="a@example.com", active=True
    )
    store.set_sent_scope("imap", "imap-a", "available")
    assert store.sent_scope("imap", "imap-a") == "available"

    store.register_mail_account(
        "imap", "imap-b", display_name="B", address="b@example.com", active=True
    )
    # Only the active account is polled, so A's last record no longer applies.
    assert store.sent_scope("imap", "imap-a") == "not_polled"
    assert store.sent_scope("imap", "imap-b") == "not_polled"


def test_coalescing_keeps_the_most_advanced_copy_as_the_canonical(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    pending = _imap_message(store, "1", "same@x")
    analyzed = _retained_duplicate(store, "2", "same@x")
    store.mark_analyzed(
        analyzed,
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
    _reset_to_schema_29(store)
    store.initialize()

    with store.connection() as db:
        logical_of = {
            str(r["message_id"]): r["logical_of"]
            for r in db.execute("SELECT message_id, logical_of FROM messages").fetchall()
        }
    # The analyzed copy is the logical message; the pending one points at it, so
    # the message is not analyzed again and its summary is the one listed.
    assert logical_of == {analyzed: None, pending: analyzed}
    assert [m.message_id for m in store.pending()] == []
    assert store.recent(1)[0]["summary"] == "Invoice received."


def test_a_skipped_message_is_queued_again_when_another_copy_is_recorded(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    first = _imap_message(store, "1", "same@x")
    store.mark_skipped(first)
    assert store.pending() == []

    assert store.add_message(
        message_id="imap-message-sent-copy",
        provider="imap",
        account_id="imap-account",
        provider_message_id="imap:sent:77:3",
        thread_id="<same@x>",
        sender="a@b.com",
        sender_name=None,
        subject="S",
        received_at="2026-08-29T12:00:00+00:00",
        rfc_message_id="same@x",
        locations=frozenset({"sent"}),
    ) is False

    assert [m.message_id for m in store.pending()] == [first]


def test_coalescing_ranks_a_silently_completed_copy_above_a_pending_one(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    pending = _imap_message(store, "1", "same@x")
    done = _retained_duplicate(store, "2", "same@x")
    store.mark_analyzed(
        done,
        {
            "category": "invoice", "priority": "normal", "summary": "Done.",
            "action_required": False, "suggested_action": None,
            "deadline_text": None, "deadline_iso": None, "confidence": 0.9,
        },
    )
    # Notifications off: completed without a notified_at.
    store.mark_delivery_complete(done, notified=False)
    _reset_to_schema_29(store)
    store.initialize()

    with store.connection() as db:
        logical_of = {
            str(r["message_id"]): r["logical_of"]
            for r in db.execute("SELECT message_id, logical_of FROM messages").fetchall()
        }
    assert logical_of == {done: None, pending: done}
    assert store.pending() == []


def test_coalescing_groups_legacy_rows_under_the_proven_identity(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    legacy_root = _imap_message(store, "1", "legacy-root@x")
    legacy_copy = _retained_duplicate(store, "2", "legacy-root@x")
    with store.connection() as db:
        identity = db.execute(
            "SELECT mailbox_identity_key FROM messages WHERE message_id = ?", (legacy_root,)
        ).fetchone()[0]
        db.execute("DROP TRIGGER messages_admission_provenance_immutable")
        db.execute(
            """UPDATE messages SET mailbox_identity_key = NULL, admission_kind = NULL,
                admission_selector_id = NULL, admission_display_name = NULL,
                admission_mailbox_identity_key = NULL, admitted_at = NULL"""
        )
        db.execute(
            """UPDATE mail_accounts SET legacy_identity_status = 'continuity_proven',
                legacy_identity_key = ? WHERE account_id = 'imap-account'""",
            (identity,),
        )
    _reset_to_schema_28(store)

    migrated = Store(store.path)
    migrated.initialize()

    with migrated.connection() as db:
        logical_of = {
            str(r["message_id"]): r["logical_of"]
            for r in db.execute("SELECT message_id, logical_of FROM messages").fetchall()
        }
    # Completed pre-identity rows keep a NULL identity, yet they are one message
    # under the account's proven identity, as their locations and threads are.
    assert logical_of == {legacy_root: None, legacy_copy: legacy_root}
    assert len(migrated.recent(10)) == 1


def test_automation_work_is_read_through_the_logical_message(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    first = admitted_scheduling_run(store, message_id="local-a", provider_message_id="m-a")
    second = admitted_scheduling_run(store, message_id="local-b", provider_message_id="m-b")
    assert first.run_id != second.run_id
    with store.connection() as db:
        # Two retained copies of one message, each with its own run from before.
        db.execute(
            "UPDATE messages SET rfc_message_id = 'same@x', thread_id = '<same@x>'"
            " WHERE message_id IN ('local-a', 'local-b')"
        )
    _reset_to_schema_29(store)
    store.initialize()

    with store.connection() as db:
        child = db.execute(
            "SELECT message_id FROM messages WHERE logical_of IS NOT NULL"
        ).fetchone()[0]
    # The child's run is not work: every automation queue reads the logical rows.
    assert store.automation_run_for_message(child) is None
    runs = {w.run.run_id for w in store.recoverable_automation_runs()}
    assert runs <= {first.run_id, second.run_id} and len(runs) <= 1


def test_logical_message_id_finds_the_stored_copy_a_source_duplicates(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    first = _imap_message(store, "1", "same@x")
    with store.connection() as db:
        identity = db.execute(
            "SELECT mailbox_identity_key FROM messages WHERE message_id = ?", (first,)
        ).fetchone()[0]
    assert store.logical_message_id(
        "imap", "imap-account", identity, "same@x", other_than="imap:sent:77:3"
    ) == first
    assert store.logical_message_id(
        "imap", "imap-account", identity, "same@x", other_than="imap:mailbox:44:1"
    ) is None


def _upgraded_completed_legacy_message(tmp_path: Path) -> tuple[Store, str, str]:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    root = _imap_message(store, "1", "same@x")
    identity = str(store.message_source(root).mailbox_identity_key)
    with store.connection() as db:
        db.execute("DROP TRIGGER messages_admission_provenance_immutable")
        db.execute(
            """UPDATE messages SET mailbox_identity_key = NULL, admission_kind = NULL,
                admission_selector_id = NULL, admission_display_name = NULL,
                admission_mailbox_identity_key = NULL, admitted_at = NULL,
                status = 'summarized', summary = 'Already completed'"""
        )
        db.execute(
            """UPDATE mail_accounts SET legacy_identity_status = 'continuity_proven',
                legacy_identity_key = ? WHERE provider = 'imap' AND account_id = 'imap-account'""",
            (identity,),
        )
    _reset_to_schema_28(store)
    store.initialize()
    return store, root, identity


def _capture_legacy_copy(
    store: Store, identity: str, suffix: str, *, rfc_id: str | None = "same@x",
) -> bool:
    return ProductionStore.add_message(
        store,
        message_id=f"copy-{suffix}", provider="imap", account_id="imap-account",
        provider_message_id=f"imap:sent:77:{suffix}", mailbox_identity_key=identity,
        thread_id="<same@x>", rfc_message_id=rfc_id, sender="a@b.com",
        sender_name=None, subject="S", received_at="2026-09-01T12:00:00+00:00",
        locations=frozenset({"sent"}),
        admission=db_module.AdmissionProvenance(
            kind="exact_sender", selector_id="sender:a@b.com", display_name=None,
            mailbox_identity_key=identity, admitted_at="2026-09-01T12:00:00+00:00",
        ),
    )


def test_upgrade_then_live_copy_does_not_recapture_completed_legacy_message(
    tmp_path: Path,
) -> None:
    store, root, identity = _upgraded_completed_legacy_message(tmp_path)
    assert _capture_legacy_copy(store, identity, "2") is False, "COPY_WAS_RECAPTURED"
    assert store.logical_message_id(
        "imap", "imap-account", identity, "same@x", other_than="imap:sent:77:2"
    ) == root
    assert [m["message_id"] for m in store.recent(10)] == [root]
    assert store.pending() == []
    assert store.message_locations(root) == ["inbox", "sent"]
    sources = store.message_sources(root)
    assert [(s.provider_message_id, s.mailbox_identity_key) for s in sources] == [
        ("imap:mailbox:44:1", None), ("imap:sent:77:2", identity),
    ]
    with store.connection() as db:
        row = db.execute("SELECT * FROM messages WHERE message_id = ?", (root,)).fetchone()
        assert row["mailbox_identity_key"] is None
        assert row["admission_kind"] is None
        assert row["admission_mailbox_identity_key"] is None
        assert row["summary"] == "Already completed"
        assert row["status"] == "summarized"


@pytest.mark.parametrize(
    ("status", "legacy_key", "matches"),
    [("continuity_proven", "same", True), ("continuity_proven", None, False),
     ("continuity_proven", "other", False), ("unresolved", "same", False),
     ("replacement", "same", False)],
)
def test_effective_legacy_identity_boundaries(
    tmp_path: Path, status: str, legacy_key: str | None, matches: bool,
) -> None:
    store, root, identity = _upgraded_completed_legacy_message(tmp_path)
    with store.connection() as db:
        # Remove assumed locations so seen lookup must evaluate the row itself.
        db.execute("DELETE FROM message_locations")
        db.execute(
            """UPDATE mail_accounts SET legacy_identity_status = ?, legacy_identity_key = ?
                WHERE provider = 'imap' AND account_id = 'imap-account'""",
            (status, identity if legacy_key == "same" else "b" * 64 if legacy_key else None),
        )
    assert store.logical_message_id(
        "imap", "imap-account", identity, "same@x", other_than="other-source"
    ) == (root if matches else None)
    assert store.has_seen_message(
        "imap:mailbox:44:1", provider="imap", account_id="imap-account",
        mailbox_identity_key=identity,
    ) is matches
    for provider, account in (("gmail", "imap-account"), ("imap", "different-account")):
        assert store.logical_message_id(
            provider, account, identity, "same@x", other_than="other-source"
        ) is None
    store.mark_skipped(root)
    with store.connection() as db:
        db_module._requeue_if_skipped(db, root)
    assert store.message_source(root).mailbox_identity_key == (identity if matches else None)


def test_explicit_identity_wins_over_legacy_proof(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    root = _imap_message(store, "1", "same@x")
    identity = str(store.message_source(root).mailbox_identity_key)
    with store.connection() as db:
        db.execute(
            """UPDATE mail_accounts SET legacy_identity_status = 'continuity_proven',
                legacy_identity_key = ? WHERE provider = 'imap' AND account_id = 'imap-account'""",
            ("b" * 64,),
        )
    assert store.logical_message_id(
        "imap", "imap-account", identity, "same@x", other_than="copy"
    ) == root
    assert store.logical_message_id(
        "imap", "imap-account", "b" * 64, "same@x", other_than="copy"
    ) is None


def test_replacement_mailbox_does_not_recapture_under_legacy_identity(tmp_path: Path) -> None:
    store, root, identity = _upgraded_completed_legacy_message(tmp_path)
    replacement = "b" * 64
    store.reconcile_mailbox_identity("imap", "imap-account", replacement)
    assert _capture_legacy_copy(store, replacement, "2") is True
    assert store.logical_message_id(
        "imap", "imap-account", replacement, "same@x", other_than="imap:sent:77:2"
    ) is None
    assert store.message_source(root).mailbox_identity_key is None
    with pytest.raises(db_module.MailboxIdentityChanged):
        _capture_legacy_copy(store, identity, "3")


@pytest.mark.parametrize("rfc_id", [None, "not-an-id", "different@x"])
def test_legacy_capture_does_not_join_missing_or_different_message_ids(
    tmp_path: Path, rfc_id: str | None,
) -> None:
    store, root, identity = _upgraded_completed_legacy_message(tmp_path)
    assert _capture_legacy_copy(store, identity, "2", rfc_id=rfc_id) is True
    assert len(store.recent(10)) == 2
    assert store.message_source(root).mailbox_identity_key is None


def test_legacy_lookup_prefers_completed_without_repairing_existing_rows(tmp_path: Path) -> None:
    store, root, identity = _upgraded_completed_legacy_message(tmp_path)
    pending = _retained_duplicate(store, "0", "same@x")
    assert store.logical_message_id(
        "imap", "imap-account", identity, "same@x", other_than="new-copy"
    ) == root
    assert _capture_legacy_copy(store, identity, "3") is False
    with store.connection() as db:
        rows = db.execute(
            "SELECT message_id, logical_of FROM messages ORDER BY message_id"
        ).fetchall()
    assert {r["message_id"]: r["logical_of"] for r in rows} == {root: None, pending: None}


def test_repeated_concurrent_copies_of_upgraded_legacy_message(tmp_path: Path) -> None:
    store, root, identity = _upgraded_completed_legacy_message(tmp_path)
    def capture(suffix: str) -> bool:
        return _capture_legacy_copy(Store(store.path), identity, suffix)
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(capture, ("2", "3", "4", "5"))) == [False] * 4
    assert _capture_legacy_copy(store, identity, "2") is False
    assert len(store.recent(10)) == 1
    assert len(store.message_sources(root)) == 5
    assert store.pending() == []


def test_seen_legacy_source_without_locations_records_its_observation(tmp_path: Path) -> None:
    store, root, identity = _upgraded_completed_legacy_message(tmp_path)
    with store.connection() as db:
        db.execute("DELETE FROM message_locations")
    assert store.has_seen_message(
        "imap:mailbox:44:1", provider="imap", account_id="imap-account",
        mailbox_identity_key=identity,
    ) is True
    recorded = store.record_message_location(
        provider="imap", account_id="imap-account", mailbox_identity_key=identity,
        provider_message_id="imap:mailbox:44:1", locations=frozenset({"sent"}),
    )
    assert recorded == 1, "SEEN_LEGACY_OBSERVATION_DROPPED"
    assert store.message_locations(root) == ["sent"]
    assert store.message_source(root).mailbox_identity_key is None


@pytest.mark.parametrize("source", ["capture", "observation"])
def test_skipped_legacy_message_requeues_with_proven_pending_identity(
    tmp_path: Path, source: str,
) -> None:
    store, root, identity = _upgraded_completed_legacy_message(tmp_path)
    store.mark_skipped(root)
    if source == "capture":
        assert _capture_legacy_copy(store, identity, "2") is False
    else:
        store.record_message_location(
            provider="imap", account_id="imap-account", mailbox_identity_key=identity,
            provider_message_id="imap:mailbox:44:1", locations=frozenset({"inbox"}),
        )
    assert store.message_source(root).mailbox_identity_key == identity, "LEGACY_REQUEUE_UNVERIFIED"
    assert [m.message_id for m in store.pending()] == [root]
    with store.connection() as db:
        assert db.execute(
            "SELECT admission_kind FROM messages WHERE message_id = ?", (root,),
        ).fetchone()[0] is None


def test_current_schema_startup_retains_preexisting_coalescing_behavior(tmp_path: Path) -> None:
    store, root, identity = _upgraded_completed_legacy_message(tmp_path)
    other = _retained_duplicate(store, "0", "same@x")
    assert len(store.recent(10)) == 2
    store.initialize()
    assert [r["message_id"] for r in store.recent(10)] == [root]
    with store.connection() as db:
        assert db.execute(
            "SELECT logical_of FROM messages WHERE message_id = ?", (other,),
        ).fetchone()[0] == root
        assert db.execute(
            "SELECT summary FROM messages WHERE message_id = ?", (root,),
        ).fetchone()[0] == "Already completed"
    assert store.message_source(root).mailbox_identity_key is None


def test_seen_lookup_work_does_not_grow_with_unrelated_account_mail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    root = _imap_message(store, "1", "same@x")
    identity = str(store.message_source(root).mailbox_identity_key)
    original_connection = store.connection
    work: list[int] = []

    @contextmanager
    def measured_connection():
        with original_connection() as db:
            steps = 0

            def step() -> int:
                nonlocal steps
                steps += 1
                return 0

            db.set_progress_handler(step, 1)
            try:
                yield db
            finally:
                db.set_progress_handler(None, 0)
                work.append(steps)

    monkeypatch.setattr(store, "connection", measured_connection)
    def lookup() -> bool:
        return ProductionStore.has_seen_message(
            store, "missing-source", provider="imap", account_id="imap-account",
            mailbox_identity_key=identity,
        )
    assert lookup() is False
    baseline = work[-1]
    with original_connection() as db:
        columns = [str(r["name"]) for r in db.execute("PRAGMA table_info(messages)")]
        replacements = {"message_id", "provider_message_id", "rfc_message_id"}
        expressions = ["?" if c in replacements else c for c in columns]
        db.executemany(
            f"INSERT INTO messages ({', '.join(columns)}) "
            f"SELECT {', '.join(expressions)} FROM messages WHERE message_id = ?",
            [(f"noise-{i}", f"noise-source-{i}", f"noise-{i}@x", root) for i in range(1000)],
        )
    assert lookup() is False
    assert work[-1] <= baseline + 500, "SEEN_LOOKUP_SCANNED_UNRELATED_MAIL"


def test_a_copy_captured_as_a_location_keeps_its_logical_message_in_retention(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    root = _imap_message(store, "1", "same@x")
    with store.connection() as db:
        db.execute(
            "UPDATE messages SET received_at = '2026-01-01T12:00:00+00:00' WHERE message_id = ?",
            (root,),
        )
    # The Sent copy arrives later and in window; it becomes a location, not a row.
    assert store.add_message(
        message_id="imap-message-sent-copy",
        provider="imap",
        account_id="imap-account",
        provider_message_id="imap:sent:77:3",
        thread_id="<same@x>",
        sender="a@b.com",
        sender_name=None,
        subject="S",
        received_at="2026-09-01T12:00:00+00:00",
        rfc_message_id="same@x",
        locations=frozenset({"sent"}),
    ) is False
    now = datetime(2026, 9, 10, tzinfo=UTC)

    assert store.purge_with_outcome(30, now=now).messages == 0
    assert store.has_message(root)
    assert store.purge_with_outcome(5, now=now).messages == 1
    assert not store.has_message(root)


def test_a_skipped_message_is_queued_again_when_a_known_folder_is_observed_again(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    store.add_message(
        message_id="gmail-1", thread_id="t", sender="a@b.com", sender_name=None,
        subject="S", received_at="2026-08-29T12:00:00+00:00", locations=frozenset({"inbox"}),
    )
    store.mark_skipped("gmail-1")
    with store.connection() as db:
        row = db.execute(
            "SELECT provider, account_id, mailbox_identity_key, provider_message_id FROM messages"
        ).fetchone()
    scope = {
        "provider": row[0], "account_id": row[1], "mailbox_identity_key": row[2],
        "provider_message_id": row[3],
    }
    # The message left and re-entered the Inbox: no new row, but a readable source.
    assert store.record_message_location(**scope, locations=frozenset({"inbox"})) == 0
    assert [m.message_id for m in store.pending()] == ["gmail-1"]


def test_a_polling_gap_clears_the_account_s_observation_stamps(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    store.add_message(
        message_id="gmail-1", thread_id="t", sender="a@b.com", sender_name=None,
        subject="S", received_at="2026-08-29T12:00:00+00:00", locations=frozenset({"inbox"}),
    )
    store.add_message(
        message_id="other-1", provider="gmail", account_id="gmail-other", thread_id="t",
        sender="a@b.com", sender_name=None, subject="S",
        received_at="2026-08-29T12:00:00+00:00", locations=frozenset({"inbox"}),
    )

    def stamps() -> dict[str, str | None]:
        with store.connection() as db:
            return {
                str(row["message_id"]): row["recorded_at"]
                for row in db.execute("SELECT message_id, recorded_at FROM message_locations")
            }

    assert None not in stamps().values()
    with store.connection() as db:
        row = db.execute(
            "SELECT provider, account_id, mailbox_identity_key FROM messages"
            " WHERE message_id = 'gmail-1'"
        ).fetchone()
    store.clear_location_stamps(row[0], row[1], row[2])
    # The gap is one account's: its stamps clear, another account's stay.
    assert stamps()["gmail-1"] is None
    assert stamps()["other-1"] is not None


def test_attachments_remember_the_copy_that_listed_them(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    first = _imap_message(store, "1", "same@x")
    store.replace_attachments(
        first,
        [
            AttachmentDescriptor(
                part_id="2", attachment_id=None, filename="quote.pdf",
                media_type="application/pdf", byte_size=10, position=0,
            )
        ],
        source_provider_message_id="imap:sent:77:3",
    )
    assert store.attachment(first, "2").source_provider_message_id == "imap:sent:77:3"


def test_a_deleted_location_copy_is_suppressed_for_its_own_retention(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    root = _imap_message(store, "1", "same@x")
    with store.connection() as db:
        db.execute(
            "UPDATE messages SET received_at = '2026-01-01T12:00:00+00:00' WHERE message_id = ?",
            (root,),
        )
    store.add_message(
        message_id="imap-message-sent-copy",
        provider="imap",
        account_id="imap-account",
        provider_message_id="imap:sent:77:3",
        thread_id="<same@x>",
        sender="a@b.com",
        sender_name=None,
        subject="S",
        received_at="2026-09-01T12:00:00+00:00",
        rfc_message_id="same@x",
        locations=frozenset({"sent"}),
    )

    assert store.delete_message(root, now=datetime(2026, 9, 10, tzinfo=UTC))
    with store.connection() as db:
        rows = db.execute(
            "SELECT message_key, expires_at FROM suppressed_messages ORDER BY expires_at"
        ).fetchall()
    # The newer copy's suppression outlives the older canonical's: each copy is
    # suppressed for its own retention horizon.
    assert len(rows) == 2
    assert rows[0]["expires_at"] < rows[1]["expires_at"]


def test_review_notifications_skip_a_run_owned_by_a_child_row(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    first = admitted_scheduling_run(store, message_id="local-a", provider_message_id="m-a")
    second = admitted_scheduling_run(store, message_id="local-b", provider_message_id="m-b")
    with store.connection() as db:
        db.execute(
            "UPDATE messages SET rfc_message_id = 'same@x', thread_id = '<same@x>'"
            " WHERE message_id IN ('local-a', 'local-b')"
        )
        db.execute(
            "UPDATE automation_runs SET state = 'manual_review', review_notified_at = NULL"
            " WHERE run_id IN (?, ?)",
            (first.run_id, second.run_id),
        )

    def review_intents() -> int:
        return sum(1 for i in store.notification_intents() if i.kind == "automation_review")

    before = review_intents()
    _reset_to_schema_29(store)
    store.initialize()

    # Both copies' runs wanted review; after coalescing only the canonical's does,
    # and the count agrees with the list.
    assert before == 2
    assert review_intents() == 1
    assert store.notification_intent_count() == len(store.notification_intents())


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
def test_retention_rule_boundaries(
    tmp_path: Path, received_at: str, discovered_at: str, expected: bool
) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    message_id = _imap_message(store, "1", None)
    with store.connection() as db:
        db.execute(
            "UPDATE messages SET received_at = ?, discovered_at = ? WHERE message_id = ?",
            (received_at, discovered_at, message_id),
        )
    observed_at = datetime(2026, 9, 9, 12, tzinfo=UTC)

    retained = store.logical_messages_within_cutoff(
        [message_id], cutoff=observed_at - timedelta(days=30), now=observed_at
    )

    assert (message_id in retained) is expected
    # The purge reads the same rule: it deletes exactly what is not retained.
    assert store.purge(30, now=observed_at) == (0 if expected else 1)


def test_a_newer_copy_keeps_a_logical_message_within_the_cutoff_for_every_reader(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    root = _imap_message(store, "1", "same@x")
    with store.connection() as db:
        db.execute(
            "UPDATE messages SET received_at = '2026-01-01T12:00:00+00:00' WHERE message_id = ?",
            (root,),
        )
    store.add_message(
        message_id="imap-message-sent-copy",
        provider="imap",
        account_id="imap-account",
        provider_message_id="imap:sent:77:3",
        thread_id="<same@x>",
        sender="a@b.com",
        sender_name=None,
        subject="S",
        received_at="2026-09-01T12:00:00+00:00",
        rfc_message_id="same@x",
        locations=frozenset({"sent"}),
    )
    observed_at = datetime(2026, 9, 9, 12, tzinfo=UTC)
    cutoff = observed_at - timedelta(days=30)

    # The canonical row is old, its Sent copy is not: kept, and processed.
    assert store.logical_messages_within_cutoff([root], cutoff=cutoff, now=observed_at) == {root}
    assert store.purge(30, now=observed_at) == 0
    # Once the copy is old too, both readers agree it has left retention.
    later = datetime(2026, 10, 9, 12, tzinfo=UTC)
    assert store.logical_messages_within_cutoff(
        [root], cutoff=later - timedelta(days=30), now=later
    ) == frozenset()
    assert store.purge(30, now=later) == 1
