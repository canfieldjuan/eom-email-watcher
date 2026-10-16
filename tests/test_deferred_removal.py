"""D-removal: real migration, frozen requests, and shared eligibility owners."""

import sqlite3
import stat
from datetime import timedelta

import pytest
from conftest import local_removal
from test_thread_bodies import followed, legacy_body_store
from test_thread_reconcile import NOW

from eom_email_watcher import body_state, thread_migration
from eom_email_watcher import db as database


def test_33_row_held_delete_accepts_durable_barrier(tmp_path, monkeypatch):
    from test_removal_refusal import held_runtime

    from eom_email_watcher import engine_api

    store = held_runtime(tmp_path, monkeypatch)
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 33
        assert db.execute("SELECT text FROM message_bodies").fetchone()[0] == "private body"
    result = engine_api._response(
        {
            "protocol": 1,
            "operation": "inbox.delete",
            "payload": {"message_id": "m"},
        }
    )
    assert result["ok"], result
    data = result["data"]
    assert data["outcome"] == "deferred"
    assert (data["requested"], data["completed"], data["pending"]) == (1, 0, 1)
    assert data["request_id"] and data["intent_ids"]
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 33
        assert db.execute("SELECT text FROM message_bodies").fetchone()[0] == "private body"
        assert not db.execute("SELECT 1 FROM logical_messages").fetchone()
        assert db.execute("SELECT COUNT(*) FROM message_removal_targets").fetchone()[0] == 33
    with pytest.raises(KeyError):
        store.message_source("m")


def test_source_gate_preserves_valid_alias_until_acceptance(tmp_path, monkeypatch):
    from test_removal_refusal import held_runtime

    from eom_email_watcher import engine_api

    store = held_runtime(tmp_path, monkeypatch)
    source = store.message_source("copy-00")
    assert source.message_id == "copy-00"
    reply = engine_api._response(
        {
            "protocol": 1,
            "operation": "inbox.delete",
            "payload": {"message_id": "m"},
        }
    )
    assert reply["ok"], reply
    with pytest.raises(KeyError):
        store.message_source("copy-00")


@pytest.mark.parametrize("owner", ["headers", "locations", "model_start", "capture"])
def test_accepted_target_refuses_real_pipeline_owners(tmp_path, monkeypatch, owner):
    from types import SimpleNamespace

    from test_removal_refusal import held_runtime
    from test_thread_reconcile import KEY

    from eom_email_watcher import engine_api

    store = held_runtime(tmp_path, monkeypatch)
    with store.connection() as db:
        db.execute("DELETE FROM message_recipients WHERE message_id='m'")
    reply = engine_api._response(
        {
            "protocol": 1,
            "operation": "inbox.delete",
            "payload": {"message_id": "m"},
        }
    )
    assert reply["ok"], reply
    if owner == "headers":
        store.record_reconcile_headers(
            "gmail",
            "mailbox",
            KEY,
            SimpleNamespace(
                message_id="m", to=("late@example.com",), cc=(), received_at=NOW.isoformat()
            ),
            locations=frozenset({"inbox"}),
            observed_at=NOW,
            scope_complete=True,
        )
    elif owner == "locations":
        store.record_message_location(
            provider="gmail",
            account_id="mailbox",
            mailbox_identity_key=KEY,
            provider_message_id="m",
            locations=frozenset({"inbox"}),
            to=("late@example.com",),
            headers_observed=True,
            now=NOW,
        )
    elif owner == "model_start":
        with pytest.raises(KeyError):
            store.reserve_analysis_request("m", 100, now=NOW)
    else:
        # The provider source remains targeted even if its physical alias has
        # disappeared. The ordinary has-seen lookup alone cannot enforce that.
        with store.connection() as db:
            db.execute("DELETE FROM messages WHERE message_id='copy-00'")
        watcher = engine_api._runtime({})

        def forbidden(source):
            pytest.fail("target reached provider metadata fetch")

        monkeypatch.setattr(watcher.gateway, "metadata", forbidden)
        assert (
            watcher._capture_ids(
                ["copy-00"],
                mailbox_identity_key=KEY,
                label_selectors=(),
                gated_allowed=True,
                vendor_snapshot=store.vendor_index(),
                checked_at=NOW,
                retention_cutoff=NOW - timedelta(days=30),
                folders=frozenset({"inbox"}),
                known_locations={},
                dry_run=False,
                dry_run_messages=[],
            )
            == 0
        )
    with store.connection() as db:
        assert not db.execute("SELECT 1 FROM message_recipients WHERE message_id='m'").fetchone()
        assert db.execute("SELECT text FROM message_bodies").fetchone()[0] == "private body"


def prior_store(tmp_path, monkeypatch, version):
    if version == 34:
        return legacy_body_store(tmp_path, monkeypatch)
    with monkeypatch.context() as prior:
        prior.setattr(database, "SCHEMA_VERSION", 35)
        store = followed(tmp_path)[0]
        body_state.record(store, "m", ("retained body", 13), fetched_at=NOW)
    return store


@pytest.mark.parametrize("owner", ["job_enqueue", "model_adopt", "source_return", "copy_join"])
def test_late_source_adoption_cannot_repopulate_held_target(tmp_path, monkeypatch, owner):
    import hashlib
    import uuid

    from test_removal_refusal import held_runtime
    from test_service import FakeModel
    from test_thread_reconcile import KEY, seed

    from eom_email_watcher import engine_api
    from eom_email_watcher.mime import AttachmentDescriptor

    store = held_runtime(tmp_path, monkeypatch)
    job_values = dict(
        message_id="m",
        part_id="2",
        capability_id="document.summarize",
        capability_version="1.0",
        provider_app_id="document-summarizer",
        provider_instance_id=str(uuid.uuid4()),
        input_artifact_id=str(uuid.uuid4()),
        input_media_type="application/pdf",
        input_byte_size=1,
        input_sha256=hashlib.sha256(b"x").hexdigest(),
    )
    if owner == "job_enqueue":
        store.replace_attachments(
            "m",
            (
                AttachmentDescriptor(
                    "2",
                    "public-attachment",
                    "public.pdf",
                    "application/pdf",
                    1,
                    0,
                ),
            ),
        )
        assert store.create_connect_job(job_id=str(uuid.uuid4()), **job_values)
    elif owner == "model_adopt":
        assert store.reserve_analysis_request("m", 100, now=NOW)
    elif owner == "copy_join":
        seed(store, "before-copy", rfc_message_id="same@vendor")
        with store.connection() as db:
            assert db.execute(
                "SELECT 1 FROM message_locations WHERE provider_message_id='before-copy'"
            ).fetchone()
    accepted = engine_api._response(
        {
            "protocol": 1,
            "operation": "inbox.delete",
            "payload": {"message_id": "m"},
        }
    )
    assert accepted["ok"] and accepted["data"]["outcome"] == "deferred", accepted
    if owner == "job_enqueue":
        with pytest.raises(KeyError):
            store.create_connect_job(job_id=str(uuid.uuid4()), **job_values)
    elif owner == "model_adopt":
        with pytest.raises(KeyError):
            database.Store(store.path).mark_analyzed(
                "m",
                FakeModel().analyze().model_dump(),
                mailbox_identity_key=KEY,
                body_chars=12,
                body_source_chars=12,
                now=NOW,
            )
    elif owner == "source_return":
        with store.connection() as db:
            db.execute("DELETE FROM messages WHERE message_id='copy-00'")
        seed(store, "copy-00")
        with store.connection() as db:
            assert not db.execute("SELECT 1 FROM messages WHERE message_id='copy-00'").fetchone()
            assert not db.execute(
                "SELECT 1 FROM message_locations WHERE provider_message_id='copy-00'"
            ).fetchone()
    else:
        seed(store, "after-copy", rfc_message_id="same@vendor")
        with store.connection() as db:
            assert not db.execute(
                "SELECT 1 FROM message_locations WHERE provider_message_id='after-copy'"
            ).fetchone()
    with store.connection() as db:
        assert not db.execute("SELECT 1 FROM logical_messages").fetchone()
        assert db.execute("SELECT text FROM message_bodies").fetchone()[0] == "private body"


@pytest.mark.parametrize("owner", ["v1_fetch", "v1_enqueue", "v1_submit", "v2_enqueue"])
def test_connect_api_acceptance_between_source_use_boundaries(tmp_path, monkeypatch, owner):
    import uuid
    from concurrent.futures import ThreadPoolExecutor

    from connect_automate import connect
    from test_connect_engine_api import provider
    from test_connect_v2_engine_api import PDF, capability, seeded_runtime

    from eom_email_watcher import engine_api, source_locks

    _, runtime = seeded_runtime(tmp_path)
    monkeypatch.setattr(engine_api, "_runtime", lambda _: runtime)
    monkeypatch.setattr(source_locks, "SOURCE_LOCK_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(connect, "require_connect_entitlement", lambda: None)
    with runtime.store.connection() as db:
        db.execute("UPDATE messages SET received_at=?", (NOW.isoformat(),))
    accepted = []

    def accept():
        # Use the real other-thread operation lock and acceptance transaction.
        # Any held source context stays in this thread through the late result.
        with ThreadPoolExecutor(max_workers=1) as pool:
            reply = pool.submit(
                engine_api._response,
                {
                    "protocol": 1,
                    "operation": "inbox.delete",
                    "payload": {"message_id": "message-1"},
                },
            ).result(timeout=15)
        assert reply["ok"], reply
        accepted.append(reply)

    def fetch(*args, **kwargs):
        if owner == "v1_fetch":
            pytest.fail("accepted target reached provider fetch")
        accept()
        return PDF

    def discover(**kwargs):
        if owner == "v1_fetch":
            accept()
        return connect.CapabilityDiscovery(provider())

    monkeypatch.setattr(engine_api, "_verified_mailbox_attachment_bytes", fetch)
    monkeypatch.setattr(connect, "discover_summary_capability", discover)
    if owner == "v2_enqueue":
        with pytest.raises(engine_api.ApiError) as refused:
            engine_api._prepare_or_create_generic_connect_job(
                runtime,
                request_id=str(uuid.uuid4()),
                message_id="message-1",
                part_id="2",
                capability=capability(),
                parameters={},
                confirmed=False,
            )
        assert refused.value.code == "connect_source_unavailable"
        assert accepted[0]["data"]["outcome"] == "deferred"
    elif owner == "v1_submit":
        job = connect.prepare_summary_job(PDF, "invoice.pdf")
        runtime.store.create_connect_job(
            job_id=job.job_id,
            message_id="message-1",
            part_id="2",
            capability_id=connect.CAPABILITY_ID,
            capability_version=connect.CAPABILITY_VERSION,
            provider_app_id=provider().app_id,
            provider_instance_id=provider().instance_id,
            input_artifact_id=job.artifact.artifact_id,
            input_media_type=job.artifact.media_type,
            input_byte_size=job.artifact.byte_size,
            input_sha256=job.artifact.sha256,
        )
        accept()

        class ForbiddenClient:
            def __init__(self, selected):
                pass

            def submit(self, job, content):
                pytest.fail("accepted target reached provider submit")

        monkeypatch.setattr(connect, "ConnectClient", ForbiddenClient)
        with pytest.raises(engine_api.ApiError) as refused:
            engine_api._run_connect_job(runtime, provider(), job, PDF)
        assert refused.value.code == "connect_source_unavailable"
    else:
        response = engine_api._response(
            {
                "protocol": 1,
                "operation": "connect.attachment.summarize",
                "payload": {"message_id": "message-1", "part_id": "2"},
            }
        )
        assert not response["ok"], response
        assert response["error"]["code"] == "connect_source_unavailable", response
    assert len(accepted) == 1
    with runtime.store.connection() as db:
        assert not db.execute("SELECT 1 FROM connect_attachment_jobs").fetchone()


def retain_legacy_aliases(store, message_id):
    # Reproduce the existing pre-coalescing physical unit without another
    # identity algorithm or a fake source-availability owner.
    with store.connection() as db:
        values = dict(
            db.execute(
                "SELECT * FROM messages WHERE message_id=?",
                (message_id,),
            ).fetchone()
        )
        columns = ",".join(values)
        placeholders = ",".join("?" for _ in values)
        for index in range(32):
            alias = dict(
                values,
                message_id=f"legacy-{index}",
                logical_of=message_id,
                provider_message_id=f"legacy-{index}",
            )
            db.execute(
                f"INSERT INTO messages({columns}) VALUES({placeholders})", tuple(alias.values())
            )
        db.execute("DELETE FROM maintenance_state WHERE key='source_lock_protocol'")


@pytest.mark.parametrize("owner", ["completion", "completion_missing", "ledger", "ledger_missing"])
def test_pending_certificate_has_no_content_adoption_or_reader(tmp_path, monkeypatch, owner):
    from test_certificate_expiry_ledger import (
        INSTANCE_A,
        _certificate_fire_job,
        _record,
        _result,
    )
    from test_connect_v2_engine_api import seeded_runtime

    from eom_email_watcher import engine_api

    _, runtime = seeded_runtime(tmp_path)
    monkeypatch.setattr(engine_api, "_runtime", lambda _: runtime)
    fire, job_id = _certificate_fire_job(runtime.store)
    runtime.store.transition_connect_job(
        job_id=job_id,
        expected_state="requested",
        next_state="accepted",
        provider_app_id="invoice-processor",
        provider_instance_id=INSTANCE_A,
    )
    if owner.startswith("ledger"):
        runtime.store.transition_connect_job(
            job_id=job_id,
            expected_state="accepted",
            next_state="completed",
            provider_app_id="invoice-processor",
            provider_instance_id=INSTANCE_A,
            result=_result(_record()),
        )
        assert runtime.store.list_certificate_expiry_ledger(today="2026-09-20")
    retain_legacy_aliases(runtime.store, "message-1")
    accepted = engine_api._response(
        {
            "protocol": 1,
            "operation": "inbox.delete",
            "payload": {"message_id": "message-1"},
        }
    )
    assert accepted["ok"] and accepted["data"]["pending"] == 1, accepted
    if owner.startswith("completion"):
        if owner == "completion_missing":
            with runtime.store.connection() as db:
                db.execute("DELETE FROM messages")
        terminal = runtime.store.transition_connect_job(
            job_id=job_id,
            expected_state="accepted",
            next_state="completed",
            provider_app_id="invoice-processor",
            provider_instance_id=INSTANCE_A,
            result=_result(_record()),
        )
        assert terminal.status == "completed"
        settled = runtime.store.automation_fire(fire.fire_id)
        assert settled.state == "completed" and settled.reason == "connect_completed"
        assert runtime.store.connect_job(job_id) is None
        with runtime.store.connection() as db:
            assert not db.execute("SELECT 1 FROM certificate_records").fetchone()
    if owner == "ledger_missing":
        with runtime.store.connection() as db:
            db.execute("DELETE FROM messages")
    assert runtime.store.list_certificate_expiry_ledger(today="2026-09-20") == []
    with runtime.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == (
            0 if owner.endswith("_missing") else 33
        )


def connect_source_job(store):
    import uuid

    from connect_automate import connect
    from test_connect_v2_engine_api import capability

    from eom_email_watcher.mime import AttachmentDescriptor

    selected = capability()
    content = b"public attachment"
    store.replace_attachments(
        "m",
        (
            AttachmentDescriptor(
                "2",
                "public-attachment",
                "public.pdf",
                "application/pdf",
                len(content),
                0,
            ),
        ),
    )
    job = connect.prepare_capability_job(
        selected,
        content,
        "application/pdf",
        "public.pdf",
        job_id=str(uuid.uuid4()),
    )
    created = store.create_connect_job(
        job_id=job.job_id,
        message_id="m",
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
        selection=selected,
        capability_produces=selected.produces,
    )
    return selected, created


def test_accepted_target_refuses_connect_resubmission(tmp_path, monkeypatch):
    from test_removal_refusal import held_runtime

    from eom_email_watcher import engine_api

    store = held_runtime(tmp_path, monkeypatch)
    selected, job = connect_source_job(store)
    assert store.claim_connect_lane_head(
        provider_app_id=selected.app_id,
        provider_instance_id=selected.instance_id,
    )
    reply = engine_api._response(
        {
            "protocol": 1,
            "operation": "inbox.delete",
            "payload": {"message_id": "m"},
        }
    )
    assert reply["ok"] and reply["data"]["pending"] == 1, reply
    before = store.connect_dispatch(job.job_id)
    assert before is not None and not before.source_available
    with pytest.raises(KeyError):
        store.reset_connect_job_for_resubmission(
            job_id=job.job_id,
            expected_state="requested",
            provider_app_id=selected.app_id,
            provider_instance_id=selected.instance_id,
        )
    after = store.connect_dispatch(job.job_id)
    assert after == before
    # A possibly submitted source-less job still owns the same GET identity.
    claimed = store.claim_connect_lane_head(
        provider_app_id=selected.app_id,
        provider_instance_id=selected.instance_id,
    )
    assert claimed is not None and claimed[0].job_id == job.job_id
    assert claimed[1].state == "reconciling" and not claimed[1].source_available


@pytest.mark.parametrize(
    "owner",
    [
        "source_start",
        "message_source",
        "add_message",
        "create_connect_job",
        "mark_analyzed",
        "connect_dispatch",
        "claim_connect_lane_head",
        "reset_connect_job_for_resubmission",
        "transition_connect_job",
        "reconcile_source",
        "connect_job",
        "notification_source_start",
        "notification_review_start",
        "notification_intents",
        "notification_intent_count",
        "mark_fallback_notified",
        "mark_delivery_complete",
        "acknowledge_analysis",
        "acknowledge_fallback",
        "acknowledge_review",
        "reserve_automation_extraction",
        "record_automation_extraction",
        "record_automation_extraction_failure",
        "record_automation_proposal",
        "automation_extraction_payloads",
        "automation_proposal",
        "automation_proposal_for_message",
    ],
)
def test_source_decision_serializes_with_real_acceptance(tmp_path, monkeypatch, owner):
    import hashlib
    import threading
    import uuid
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager

    from test_removal_refusal import held_runtime
    from test_service import FakeModel
    from test_thread_reconcile import KEY

    from eom_email_watcher import removal
    from eom_email_watcher.config import exact_sender_selector_id
    from eom_email_watcher.mime import AttachmentDescriptor

    scheduling_owners = {
        "reserve_automation_extraction",
        "record_automation_extraction",
        "record_automation_extraction_failure",
        "record_automation_proposal",
        "automation_extraction_payloads",
        "automation_proposal",
        "automation_proposal_for_message",
    }
    if owner in scheduling_owners:
        from test_service import (
            admit_scheduling_run,
            awaiting_confirmation_run,
            config,
            proposing_automation_run,
            valid_scheduling_output,
        )

        cfg = config(tmp_path)
        seeded = database.Store(cfg.database_file)
        seeded.initialize()
        if owner in {"automation_proposal", "automation_proposal_for_message"}:
            _, run, _ = awaiting_confirmation_run(cfg, seeded, monkeypatch)
        elif owner == "record_automation_proposal":
            _, run = proposing_automation_run(cfg, seeded, monkeypatch)
        else:
            _, run = admit_scheduling_run(seeded, monkeypatch)
        with seeded.connection() as db:
            target_id = db.execute("SELECT message_id FROM messages").fetchone()[0]
        retain_legacy_aliases(seeded, target_id)
    elif owner in {
        "notification_review_start",
        "notification_intents",
        "notification_intent_count",
        "acknowledge_review",
    }:
        _, seeded, notification, target_id = review_notification_fixture(tmp_path, monkeypatch)
    else:
        seeded = held_runtime(tmp_path, monkeypatch)
        target_id = "m"
    main_thread = threading.get_ident()
    reached, release = threading.Event(), threading.Event()
    observation_timeouts = []

    class ProbeStore(database.Store):
        @contextmanager
        def connection(self):
            with super().connection() as db:
                # Observe the actual SQLite write exclusion without waiting the
                # production busy timeout; production timeout is unchanged.
                db.execute("PRAGMA busy_timeout=0")

                execute = db.execute

                def observe(sql, parameters=()):
                    cursor = execute(sql, parameters)
                    if (
                        threading.get_ident() != main_thread
                        and not reached.is_set()
                        and "FROM message_removal_targets" in sql
                    ):

                        class DecisionCursor:
                            def _decision(self, fetch):
                                # Pause after SQLite made the real decision, not
                                # before the SELECT could observe acceptance.
                                result = fetch()
                                reached.set()
                                if not release.wait(10):
                                    observation_timeouts.append(sql)
                                return result

                            def fetchone(self):
                                return self._decision(cursor.fetchone)

                            def fetchall(self):
                                return self._decision(cursor.fetchall)

                        return DecisionCursor()
                    return cursor

                db.execute = observe
                try:
                    yield db
                finally:
                    db.execute = execute

    store = ProbeStore(seeded.path)
    if owner in scheduling_owners:
        reservation_args = dict(
            source_content_sha256="b" * 64,
            context_at="2026-09-07T08:00:00-05:00",
            timezone="America/Chicago",
            body_char_limit=20_000,
            organizer_address="owner@example.com",
        )
        if owner in {
            "record_automation_extraction",
            "record_automation_extraction_failure",
            "automation_extraction_payloads",
        }:
            payload = store.reserve_automation_extraction(
                run.run_id, run.state_version, **reservation_args
            )
            run = store.automation_run(run.run_id)
            assert run is not None
        if owner == "record_automation_proposal":
            payload = store.automation_extraction_payloads(run.run_id)[0]
    if owner in {"mark_delivery_complete", "acknowledge_analysis"}:
        store.mark_analyzed(
            target_id,
            FakeModel().analyze().model_dump(),
            mailbox_identity_key=KEY,
            body_chars=12,
            body_source_chars=12,
            now=NOW,
        )
    if owner in {"mark_fallback_notified", "acknowledge_fallback"}:
        store.record_failure(target_id, "public failure", 0)
    if owner in {"acknowledge_analysis", "acknowledge_fallback"}:
        kind = "analysis" if owner == "acknowledge_analysis" else "fallback"
        notification = next(
            item for item in store.notification_intents(kind=kind) if item.subject_id == target_id
        )
    if owner == "create_connect_job":
        store.replace_attachments(
            target_id,
            (
                AttachmentDescriptor(
                    "2",
                    "public-attachment",
                    "public.pdf",
                    "application/pdf",
                    1,
                    0,
                ),
            ),
        )
    if owner == "mark_analyzed":
        store.reserve_analysis_request(target_id, 100, now=NOW)
    if owner in {
        "connect_dispatch",
        "claim_connect_lane_head",
        "reset_connect_job_for_resubmission",
        "transition_connect_job",
        "connect_job",
    }:
        selected, source_job = connect_source_job(store)
        if owner == "connect_job":
            from connect_automate import connect

            content = b"public output"
            source_output = connect.CapabilityOutput(
                artifact_id=str(uuid.uuid4()),
                media_type="text/plain",
                display_name="public.txt",
                byte_size=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                payload=content,
            )
            store.transition_connect_job(
                job_id=source_job.job_id,
                expected_state="requested",
                next_state="completed",
                provider_app_id=selected.app_id,
                provider_instance_id=selected.instance_id,
                result=connect.CapabilityResult((source_output,)).store_dict(),
            )
        if owner == "reset_connect_job_for_resubmission":
            assert store.claim_connect_lane_head(
                provider_app_id=selected.app_id,
                provider_instance_id=selected.instance_id,
            )
    key = removal.issue_key(store, "delete", {"message_id": target_id}, retention_days=30, now=NOW)

    def start():
        if owner == "reserve_automation_extraction":
            return store.reserve_automation_extraction(
                run.run_id, run.state_version, **reservation_args
            )
        if owner == "record_automation_extraction":
            result = valid_scheduling_output().encode()
            return store.record_automation_extraction(
                run.run_id,
                run.state_version,
                payload_id=payload.payload_id,
                result_json=result,
                result_sha256=hashlib.sha256(result).hexdigest(),
                violations=[],
                accepted_state="proposing",
            )
        if owner == "record_automation_extraction_failure":
            return store.record_automation_extraction_failure(
                run.run_id,
                run.state_version,
                payload_id=payload.payload_id,
                error_code="model_unavailable",
                retryable=True,
            )
        if owner == "record_automation_proposal":
            from test_service import SCHEDULING_FIXTURE_NOW

            return store.record_automation_proposal(
                run.run_id,
                run.state_version,
                extraction_payload_id=payload.payload_id,
                request_sha256="c" * 64,
                subject="Public meeting",
                attendees=("jane@example.com",),
                start="2026-09-08T10:00:00-05:00",
                end="2026-09-08T10:30:00-05:00",
                timezone="America/Chicago",
                suggestion_reason="Everyone is available.",
                empty_reason=None,
                observed_at=SCHEDULING_FIXTURE_NOW,
            )
        if owner == "automation_proposal_for_message":
            return store.automation_proposal_for_message(target_id)
        if owner in {"automation_extraction_payloads", "automation_proposal"}:
            return getattr(store, owner)(run.run_id)
        if owner in {"notification_intents", "notification_intent_count"}:
            return getattr(store, owner)()
        if owner == "notification_source_start":
            return store.source_start(target_id)
        if owner == "notification_review_start":
            return store.source_start(automation_run_id=notification.subject_id)
        if owner == "mark_fallback_notified":
            return store.mark_fallback_notified(target_id)
        if owner == "mark_delivery_complete":
            return store.mark_delivery_complete(target_id, True)
        if owner in {"acknowledge_analysis", "acknowledge_fallback", "acknowledge_review"}:
            return store.acknowledge_notification(
                **{
                    name: getattr(notification, name)
                    for name in (
                        "message_id",
                        "kind",
                        "analysis_at",
                        "subject_type",
                        "subject_id",
                        "revision",
                    )
                }
            )
        if owner == "connect_job":
            from types import SimpleNamespace

            from eom_email_watcher import engine_api

            return engine_api._selected_connect_output(
                dict(
                    message_id=target_id,
                    part_id="2",
                    job_id=source_job.job_id,
                    artifact_id=source_output.artifact_id,
                ),
                SimpleNamespace(store=store),
            )
        if owner == "reconcile_source":
            return store.reconcile_source("gmail", "mailbox", KEY, target_id)
        if owner == "transition_connect_job":
            return store.transition_connect_job(
                job_id=source_job.job_id,
                expected_state="requested",
                next_state="accepted",
                provider_app_id=selected.app_id,
                provider_instance_id=selected.instance_id,
            )
        if owner == "connect_dispatch":
            return store.connect_dispatch(source_job.job_id)
        if owner == "claim_connect_lane_head":
            return store.claim_connect_lane_head(
                provider_app_id=selected.app_id,
                provider_instance_id=selected.instance_id,
            )
        if owner == "reset_connect_job_for_resubmission":
            return store.reset_connect_job_for_resubmission(
                job_id=source_job.job_id,
                expected_state="requested",
                provider_app_id=selected.app_id,
                provider_instance_id=selected.instance_id,
            )
        if owner == "source_start":
            return store.source_start(target_id)
        if owner == "message_source":
            return store.message_source(target_id)
        if owner == "mark_analyzed":
            return store.mark_analyzed(
                target_id,
                FakeModel().analyze().model_dump(),
                mailbox_identity_key=KEY,
                body_chars=12,
                body_source_chars=12,
                now=NOW,
            )
        if owner == "create_connect_job":
            return store.create_connect_job(
                job_id=str(uuid.uuid4()),
                message_id=target_id,
                part_id="2",
                capability_id="document.summarize",
                capability_version="1.0",
                provider_app_id="document-summarizer",
                provider_instance_id=str(uuid.uuid4()),
                input_artifact_id=str(uuid.uuid4()),
                input_media_type="application/pdf",
                input_byte_size=1,
                input_sha256=hashlib.sha256(b"x").hexdigest(),
            )
        return store.add_message(
            message_id="before-race-copy",
            provider="gmail",
            account_id="mailbox",
            provider_message_id="before-race-copy",
            mailbox_identity_key=KEY,
            thread_id="native",
            sender="vendor@example.com",
            sender_name=None,
            subject="Public copy",
            received_at=NOW.isoformat(),
            rfc_message_id="same@vendor",
            admission=database.AdmissionProvenance(
                "exact_sender",
                exact_sender_selector_id("vendor@example.com"),
                None,
                KEY,
                NOW.isoformat(),
            ),
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(start)
        try:
            assert reached.wait(5), "owner never consulted persisted target eligibility"
            with (
                removal.acceptance_scope(store) as acceptance,
                pytest.raises(sqlite3.OperationalError, match="locked"),
            ):
                store.request_removal(
                    "delete",
                    {"message_id": target_id},
                    request_id=key,
                    now=NOW, acceptance=acceptance,
                )
        finally:
            release.set()
        future.result(timeout=10)
    assert not observation_timeouts
    with removal.acceptance_scope(store) as acceptance:
        result = store.request_removal(
            "delete", {"message_id": target_id}, request_id=key, now=NOW, acceptance=acceptance,
        )
    assert result.pending == 1 and result.completed == 0
    with store.connection() as db:
        assert not db.execute("SELECT 1 FROM logical_messages").fetchone()


def test_restart_preserves_request_and_qualification_completes_once(tmp_path, monkeypatch):
    from test_removal_refusal import held_runtime

    from eom_email_watcher import engine_api, removal, source_locks

    store = held_runtime(tmp_path, monkeypatch)
    reply = engine_api._response(
        {
            "protocol": 1,
            "operation": "inbox.delete",
            "payload": {"message_id": "m"},
        }
    )
    assert reply["ok"], reply
    key = reply["data"]["request_id"]
    reopened = database.Store(store.path)

    def not_qualified(*args, **kwargs):
        raise source_locks.SourceProtocolUnavailable("public restart before lift")

    with monkeypatch.context() as blocked:
        blocked.setattr(source_locks, "qualify_protocol", not_qualified)
        reopened.initialize()
    with reopened.connection() as db:
        assert removal.outcome(db, key).pending == 1
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 33
        assert db.execute("SELECT text FROM message_bodies").fetchone()[0] == "private body"
        assert not db.execute("SELECT 1 FROM logical_messages").fetchone()
    source_locks.qualify_protocol(reopened, fresh=False)
    with reopened.connection() as db:
        assert not db.execute("SELECT 1 FROM messages").fetchone()
        assert not db.execute("SELECT 1 FROM message_bodies").fetchone()
        done = removal.outcome(db, key).data()
        assert (done["completed"], done["pending"], done["outcome"]) == (1, 0, "completed")
    reopened.initialize()
    source_locks.qualify_protocol(reopened, fresh=False)
    with reopened.connection() as db:
        assert removal.outcome(db, key).data() == done


def test_retention_does_not_bypass_pending_completion_owner(tmp_path, monkeypatch):
    from test_removal_refusal import held_runtime

    from eom_email_watcher import engine_api, removal, source_locks

    store = held_runtime(tmp_path, monkeypatch)
    reply = engine_api._response(
        {
            "protocol": 1,
            "operation": "inbox.delete",
            "payload": {"message_id": "m"},
        }
    )
    assert reply["ok"], reply
    # Qualification may publish while completion remains pending (for example,
    # another operation owns the check lock). Retention has no completion claim.
    with monkeypatch.context() as delayed:
        delayed.setattr(removal, "resume", lambda *a, **kw: None)
        source_locks.qualify_protocol(store, fresh=False)
    store.purge(1, now=NOW + timedelta(days=31))
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 33
        assert db.execute("SELECT text FROM message_bodies").fetchone()[0] == "private body"
        assert removal.outcome(db, reply["data"]["request_id"]).pending == 1
    removal.resume(store)
    with store.connection() as db:
        assert not db.execute("SELECT 1 FROM messages").fetchone()
        assert removal.outcome(db, reply["data"]["request_id"]).completed == 1


@pytest.mark.parametrize("retire", [False, True])
def test_zero_target_retry_and_retired_expiry_never_select_new_arrivals(
    tmp_path,
    retire,
):
    from test_thread_reconcile import KEY, seed

    from eom_email_watcher import removal
    from eom_email_watcher.config import exact_sender_selector_id

    store = database.Store(tmp_path / "empty.db")
    store.initialize()
    key = removal.issue_key(store, "clear", {}, retention_days=1, now=NOW)
    empty = local_removal(
        store, "request_removal", "clear", {}, request_id=key, retention_days=1, now=NOW,
    )
    assert empty.requested == empty.completed == 0
    later = NOW + timedelta(days=2)
    if retire:
        store.purge(1, now=later)
        with store.connection() as db:
            assert removal.outcome(db, key) is None
    store.register_mail_account("gmail", "mailbox", display_name="Mailbox")
    store.reconcile_mailbox_identity("gmail", "mailbox", KEY)
    seed(
        store,
        "later-arrival",
        admission=database.AdmissionProvenance(
            "exact_sender",
            exact_sender_selector_id("vendor@example.com"),
            None,
            KEY,
            NOW.isoformat(),
        ),
    )
    with store.connection() as db:
        before = tuple(db.iterdump())
    if retire:
        with pytest.raises(removal.RemovalRequestInvalid, match="expired"):
            # A changed retention setting cannot extend the key's fixed expiry.
            local_removal(
                store, "request_removal", "clear", {}, request_id=key,
                retention_days=100, now=later,
            )
    else:
        assert (
            local_removal(store, "request_removal",
                "clear",
                {},
                request_id=key,
                retention_days=100,
                now=later,
            ).data()
            == empty.data()
        )
    with store.connection() as db:
        assert tuple(db.iterdump()) == before
        assert db.execute("SELECT message_id FROM messages").fetchone()[0] == "later-arrival"


def test_schema36_request_shape_has_real_foreign_keys(tmp_path):
    store = database.Store(tmp_path / "mail.db")
    store.initialize()
    with store.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 36
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert {r[1] for r in db.execute("PRAGMA table_info(message_removal_requests)")} == {
            "request_id",
            "kind",
            "parameters_sha256",
            "accepted_at",
        }
        assert db.execute("PRAGMA foreign_key_list(message_removal_intents)").fetchall()
        assert db.execute("PRAGMA foreign_key_list(message_removal_targets)").fetchall()
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("version", [34, 35])
def test_schema36_upgrade_backups_precede_any_write_and_preserve_rows(
    tmp_path,
    monkeypatch,
    version,
):
    store = prior_store(tmp_path, monkeypatch, version)
    with sqlite3.connect(store.path) as db:
        before = db.execute("SELECT * FROM messages ORDER BY message_id").fetchall()
        assert db.execute("PRAGMA user_version").fetchone()[0] == version
    original = thread_migration.write_backup
    seen = []

    def backup(path, db, *, target_schema=33):
        assert db.execute("PRAGMA user_version").fetchone()[0] == version
        assert target_schema == 36
        assert not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='message_removal_requests'"
        ).fetchone()
        result = original(path, db, target_schema=target_schema)
        assert stat.S_IMODE(result.stat().st_mode) == 0o600
        with sqlite3.connect(result) as snapshot:
            assert snapshot.execute("PRAGMA user_version").fetchone()[0] == version
            assert (
                snapshot.execute("SELECT * FROM messages ORDER BY message_id").fetchall() == before
            )
            if version == 35:
                assert snapshot.execute("SELECT text FROM message_bodies").fetchone()[0] == (
                    "retained body"
                )
        seen.append(result)
        return result

    monkeypatch.setattr(thread_migration, "write_backup", backup)
    store.initialize()
    assert len(seen) == 1
    assert not seen[0].exists()
    with store.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 36
        assert [
            tuple(r) for r in db.execute("SELECT * FROM messages ORDER BY message_id")
        ] == before
        assert db.execute("SELECT COUNT(*) FROM message_removal_requests").fetchone()[0] == 0
    store.initialize()
    assert len(seen) == 1, "Reopening the same schema must not repeat backup/migration"


def test_schema35_binary_refuses36_before_any_ddl(tmp_path, monkeypatch):
    store = database.Store(tmp_path / "mail.db")
    store.initialize()
    with sqlite3.connect(store.path) as db:
        before = db.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall()
    monkeypatch.setattr(database, "SCHEMA_VERSION", 35)
    with pytest.raises(RuntimeError, match="newer than supported"):
        store.initialize()
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall() == before


@pytest.mark.parametrize("step", range(1, 10))
def test_schema36_ddl_fault_rolls_back_everything_and_keeps_private_backup(
    tmp_path,
    monkeypatch,
    step,
):
    from eom_email_watcher import removal

    store = prior_store(tmp_path, monkeypatch, 35)
    with sqlite3.connect(store.path) as db:
        before = tuple(db.iterdump())
    original = removal.ensure_schema

    def fail(db):
        class Fault:
            count = 0

            def execute(self, statement, *parameters):
                result = db.execute(statement, *parameters)
                self.count += 1
                if self.count == step:
                    raise RuntimeError("injected v36 DDL fault")
                return result

        original(Fault())

    monkeypatch.setattr(removal, "ensure_schema", fail)
    with pytest.raises(RuntimeError, match="injected v36 DDL fault"):
        store.initialize()
    with sqlite3.connect(store.path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 35
        assert tuple(db.iterdump()) == before
    backups = list(tmp_path.glob("*.pre-v36.*.bak"))
    assert len(backups) == 1
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    with sqlite3.connect(backups[0]) as db:
        assert tuple(db.iterdump()) == before
    monkeypatch.setattr(removal, "ensure_schema", original)
    store.initialize()
    assert not backups[0].exists()


def test_schema36_backup_full_disk_does_not_start_migration(tmp_path, monkeypatch):
    import errno

    store = prior_store(tmp_path, monkeypatch, 35)
    with sqlite3.connect(store.path) as db:
        before = tuple(db.iterdump())

    def full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "public full-disk fixture")

    monkeypatch.setattr(thread_migration, "write_backup", full)
    with pytest.raises(OSError, match="public full-disk fixture"):
        store.initialize()
    with sqlite3.connect(store.path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 35
        assert tuple(db.iterdump()) == before


def test_schema36_foreign_keys_prevent_unbound_intents_and_cascade_only_metadata(tmp_path):
    store = followed(tmp_path)[0]
    body_state.record(store, "m", ("retained body", 13), fetched_at=NOW)
    intent = "00000000-0000-4000-8000-000000000000"
    with store.connection() as db:
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            db.execute(
                "INSERT INTO message_removal_intents VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    intent,
                    "absent",
                    "clear",
                    "gmail",
                    "account",
                    None,
                    NOW.isoformat(),
                    "pending",
                    1,
                    0,
                    None,
                ),
            )
        db.execute(
            "INSERT INTO message_removal_requests VALUES(?,?,?,?)",
            ("request", "clear", "a" * 64, NOW.isoformat()),
        )
        db.execute(
            "INSERT INTO message_removal_intents VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                intent,
                "request",
                "clear",
                "gmail",
                "account",
                None,
                NOW.isoformat(),
                "pending",
                1,
                0,
                None,
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            db.execute(
                "INSERT INTO message_removal_targets VALUES(?,?,?,?,?,?,?,?,?,?)",
                (intent, "m", "m", "gmail", "other-account", None, "source", "[]", "pending", None),
            )
        db.execute(
            "INSERT INTO message_removal_targets VALUES(?,?,?,?,?,?,?,?,?,?)",
            (intent, "m", "m", "gmail", "account", None, "source", "[]", "pending", None),
        )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            db.execute("DELETE FROM message_removal_requests WHERE request_id='request'")
        db.execute("DELETE FROM message_removal_intents WHERE request_id='request'")
        db.execute("DELETE FROM message_removal_requests WHERE request_id='request'")
        assert not db.execute("SELECT 1 FROM message_removal_intents").fetchone()
        assert not db.execute("SELECT 1 FROM message_removal_targets").fetchone()
        assert db.execute("SELECT 1 FROM messages WHERE message_id='m'").fetchone()
        assert db.execute("SELECT text FROM message_bodies").fetchone()[0] == "retained body"


def test_request_key_is_signed_and_expiry_does_not_replace_retained_receipt(tmp_path):
    from eom_email_watcher import removal

    store = database.Store(tmp_path / "mail.db")
    store.initialize()
    key = removal.issue_key(store, "clear", {}, retention_days=30, now=NOW)
    fingerprint = removal.request_hash("clear", {})
    with store.connection() as db:
        removal.validate_key(db, key, "clear", fingerprint, now=NOW)
        assert removal.outcome(db, key) is None
        with pytest.raises(removal.RemovalRequestInvalid, match="expired"):
            removal.validate_key(db, key, "clear", fingerprint, now=NOW + timedelta(days=31))
        forged = key[:-1] + ("0" if key[-1] != "0" else "1")
        with pytest.raises(removal.RemovalRequestInvalid, match="does not match"):
            removal.validate_key(db, forged, "clear", fingerprint, now=NOW)
        with pytest.raises(removal.RemovalRequestInvalid, match="does not match"):
            removal.validate_key(
                db, key, "clear", removal.request_hash("clear", {"scope": "other"}), now=NOW
            )
        db.execute(
            "INSERT INTO message_removal_requests VALUES(?,?,?,?)",
            (key, "clear", fingerprint, NOW.isoformat()),
        )
        # Lookup is independent of expiry and retains the accepted empty selection.
        assert removal.outcome(db, key, kind="clear", fingerprint=fingerprint).data() == {
            "request_id": key,
            "intent_ids": [],
            "outcome": "completed",
            "requested": 0,
            "completed": 0,
            "pending": 0,
            "message": None,
        }
        with pytest.raises(removal.RemovalRequestInvalid, match="different parameters"):
            removal.outcome(db, key, kind="delete")


@pytest.mark.parametrize("key", [None, False, 0, "", "rm1.0.bad.bad", "x" * 1000])
def test_malformed_request_keys_are_invalid_without_mutation(tmp_path, key):
    from eom_email_watcher import removal

    store = database.Store(tmp_path / "mail.db")
    store.initialize()
    with store.connection() as db:
        before = tuple(db.iterdump())
        with pytest.raises(removal.RemovalRequestInvalid, match="Malformed"):
            removal.validate_key(db, key, "clear", removal.request_hash("clear", {}), now=NOW)
        assert tuple(db.iterdump()) == before


@pytest.mark.parametrize("days", [0, False, -1, "30", 10**30])
def test_invalid_request_lifetimes_do_not_default(tmp_path, days):
    from eom_email_watcher import removal

    store = database.Store(tmp_path / "mail.db")
    store.initialize()
    with pytest.raises(removal.RemovalRequestInvalid, match="retention interval"):
        removal.issue_key(store, "clear", {}, retention_days=days, now=NOW)


@pytest.mark.parametrize(
    "kind,parameters",
    [(None, {}), ([], {}), ("other", {}), ("clear", []), ("clear", {"bad": float("nan")})],
)
def test_request_parameter_hash_rejects_invalid_input(kind, parameters):
    from eom_email_watcher import removal

    with pytest.raises(removal.RemovalRequestInvalid):
        removal.request_hash(kind, parameters)


@pytest.mark.parametrize("context", ["selected", "raw"])
def test_qualification_inside_source_context_defers_physical_completion(
    tmp_path, monkeypatch, context
):
    from test_removal_refusal import held_runtime

    from eom_email_watcher import engine_api, removal, source_locks

    store = held_runtime(tmp_path, monkeypatch)
    reply = engine_api._response(
        {
            "protocol": 1,
            "operation": "inbox.delete",
            "payload": {"message_id": "m"},
        }
    )
    assert reply["ok"] and reply["data"]["pending"] == 1, reply
    # Simulate the completed qualification marker while an existing handoff
    # still owns a subset. Completion must enter later in operation→source order.
    with store.connection() as db:
        db.execute(
            "INSERT INTO maintenance_state VALUES('source_lock_protocol',?,?)",
            (source_locks.SOURCE_LOCK_PROTOCOL, NOW.isoformat()),
        )
    owned = (
        store.source_locks(("m",))
        if context == "selected"
        else source_locks.source_locks(store.path, ("m",))
    )
    with owned:
        source_locks.qualify_protocol(store, fresh=False)
        with store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 33
            assert removal.outcome(db, reply["data"]["request_id"]).pending == 1
    source_locks.qualify_protocol(store, fresh=False)
    with store.connection() as db:
        assert not db.execute("SELECT 1 FROM messages").fetchone()
        assert removal.outcome(db, reply["data"]["request_id"]).completed == 1


@pytest.mark.parametrize("changed", [None, "provider", "account_id", "mailbox_identity_key"])
def test_missing_source_eligibility_preserves_retained_binding(tmp_path, monkeypatch, changed):
    from test_removal_refusal import held_runtime
    from test_thread_reconcile import KEY

    from eom_email_watcher import engine_api, removal, source_locks

    store = held_runtime(tmp_path, monkeypatch)
    accepted = engine_api._response(
        {
            "protocol": 1,
            "operation": "inbox.delete",
            "payload": {"message_id": "m"},
        }
    )
    assert accepted["ok"] and accepted["data"]["pending"] == 1, accepted
    binding = dict(provider="gmail", account_id="mailbox", mailbox_identity_key=KEY)
    if changed:
        binding[changed] = "e" * 64 if changed == "mailbox_identity_key" else "different"
        assert binding != dict(provider="gmail", account_id="mailbox", mailbox_identity_key=KEY)
    with store.connection() as db:
        db.execute("DELETE FROM messages")
        assert removal.message_available(
            db,
            "m",
            require_source=False,
            retained_bindings=(binding,),
        ) is (changed is not None)
        assert not removal.message_available(db, "m")
        db.execute(
            "INSERT INTO maintenance_state VALUES('source_lock_protocol',?,?)",
            (source_locks.SOURCE_LOCK_PROTOCOL, NOW.isoformat()),
        )
    source_locks.qualify_protocol(store, fresh=False)
    with store.connection() as db:
        assert removal.outcome(db, accepted["data"]["request_id"]).completed == 1
        assert removal.message_available(
            db,
            "m",
            require_source=False,
            retained_bindings=(binding,),
        )


@pytest.mark.parametrize("stage", ["headers", "body", "body_fallback"])
def test_reconcile_queued_source_refuses_fetch_after_acceptance(tmp_path, monkeypatch, stage):
    from test_removal_refusal import held_runtime
    from test_thread_bodies import prepare_derived
    from test_thread_reconcile import KEY

    from eom_email_watcher import engine_api, reconcile
    from eom_email_watcher.mailbox import MailboxMessageUnavailable
    from eom_email_watcher.reconcile_state import ReconcileState

    store = held_runtime(tmp_path, monkeypatch)
    watcher = engine_api._runtime({})
    state = ReconcileState(store, ("gmail", "mailbox", KEY))
    accepted = []
    calls = []

    def accept():
        reply = engine_api._response(
            {
                "protocol": 1,
                "operation": "inbox.delete",
                "payload": {"message_id": "m"},
            }
        )
        assert reply["ok"] and reply["data"]["pending"] == 1, reply
        accepted.append(reply)

    if stage == "headers":
        unit = reconcile._unit("public")
        unit["listed"] = True  # Its final page was already persisted with this queued item.
        progress = state.load_or_start(
            NOW,
            30,
            dict(
                stage="discovery",
                units=[unit],
                folders=["inbox"],
                replay_discovery=True,
            ),
        )
        state.put_items(
            progress, "discovery", "public", (reconcile.ReconcileItem("copy-00"),), "inbox"
        )
        original = ReconcileState.pending

        def pending(self, *args):
            item = original(self, *args)
            if item is not None and not accepted:
                accept()
                with store.connection() as db:
                    db.execute("DELETE FROM messages WHERE message_id='copy-00'")
            return item

        monkeypatch.setattr(ReconcileState, "pending", pending)

        def forbidden(source):
            calls.append(source)
            pytest.fail(f"accepted queued target reached metadata/preflight: {source}")

        monkeypatch.setattr(watcher.gateway, "metadata", forbidden)
        monkeypatch.setattr(
            watcher.gateway,
            "reconcile_preflight",
            lambda source, **kwargs: forbidden(source),
            raising=False,
        )
    else:
        if stage == "body_fallback":
            from test_thread_reconcile import seed

            seed(store, "z-copy", rfc_message_id="same@vendor")
        with store.connection() as db:
            db.execute("DELETE FROM message_bodies")
        prepare_derived(state)
        original = body_state.sources

        def sources(*args):
            copies = original(*args)
            if stage == "body" and not accepted:
                accept()
            return copies

        monkeypatch.setattr(body_state, "sources", sources)

        def body(source, **kwargs):
            calls.append(source)
            if stage == "body_fallback" and not accepted:
                accept()
                raise MailboxMessageUnavailable("public source disappeared")
            pytest.fail("accepted queued target reached stored-body fetch")

        monkeypatch.setattr(watcher.gateway, "stored_body", body)
    reconcile.run(watcher, KEY, "unavailable", label_selectors=(), now=NOW)
    assert len(accepted) == 1
    assert len(calls) == int(stage == "body_fallback")
    with store.connection() as db:
        if stage == "headers":
            assert db.execute("SELECT text FROM message_bodies").fetchone()[0] == "private body"
        else:
            assert not db.execute("SELECT 1 FROM message_bodies").fetchone()
            assert not db.execute("SELECT 1 FROM message_body_unavailable").fetchone()


@pytest.mark.parametrize("legacy_null", [False, True])
@pytest.mark.parametrize(
    "changed", [None, "provider", "account_id", "mailbox_identity_key", "provider_message_id"]
)
def test_frozen_alias_source_membership_survives_missing_row(
    tmp_path, monkeypatch, legacy_null, changed
):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import nullcontext

    from test_db import _upgraded_completed_legacy_message
    from test_removal_refusal import held_runtime
    from test_thread_reconcile import KEY

    from eom_email_watcher import removal, source_locks

    if legacy_null:
        store, root, identity = _upgraded_completed_legacy_message(tmp_path)
        legacy_source = store.message_source(root)
        assert legacy_source.mailbox_identity_key is None
        native = legacy_source.provider_message_id
        with store.connection() as db:
            db.execute("DELETE FROM message_locations WHERE message_id=?", (root,))
        target_id = root
        source = dict(
            provider="imap",
            account_id="imap-account",
            mailbox_identity_key=identity,
            provider_message_id=native,
        )
        held = store.source_locks((root,))
        monkeypatch.setattr(source_locks, "SOURCE_LOCK_WAIT_SECONDS", 0.05)
    else:
        store = held_runtime(tmp_path, monkeypatch)
        root, target_id = "m", "copy-00"
        source = dict(
            provider="gmail",
            account_id="mailbox",
            mailbox_identity_key=KEY,
            provider_message_id=target_id,
        )
        held = nullcontext()
    if changed:
        before = dict(source)
        source[changed] = "e" * 64 if changed == "mailbox_identity_key" else "unrelated"
        assert source != before
    assert store.source_start(**source)

    def accept():
        with removal.acceptance_scope(store) as acceptance:
            return store.request_removal(
                "delete", {"message_id": root}, now=NOW, acceptance=acceptance,
            )

    with held:
        with ThreadPoolExecutor(max_workers=1) as pool:
            reply = pool.submit(accept).result(timeout=10)
        assert reply.pending == 1
        assert store.source_start(**source) is (changed is not None)
        with store.connection() as db:
            assert db.execute(
                "SELECT 1 FROM message_removal_targets WHERE message_id=?",
                (target_id,),
            ).fetchone()
            db.execute("DELETE FROM messages WHERE message_id=?", (target_id,))
        assert store.source_start(**source) is (changed is not None)


def test_reconcile_source_does_not_expose_accepted_physical_target(tmp_path, monkeypatch):
    from test_removal_refusal import held_runtime
    from test_thread_reconcile import KEY

    from eom_email_watcher import engine_api

    store = held_runtime(tmp_path, monkeypatch)
    assert store.reconcile_source("gmail", "mailbox", KEY, "m") is not None
    reply = engine_api._response(
        {
            "protocol": 1,
            "operation": "inbox.delete",
            "payload": {"message_id": "m"},
        }
    )
    assert reply["ok"] and reply["data"]["pending"] == 1, reply
    assert store.reconcile_source("gmail", "mailbox", KEY, "m") is None


@pytest.mark.parametrize("owner", ["attachment_fetch", "output_present", "output_export"])
def test_content_reader_refuses_start_after_acceptance(tmp_path, monkeypatch, owner):
    from test_removal_refusal import held_runtime

    from eom_email_watcher import engine_api

    if owner == "attachment_fetch":
        store = held_runtime(tmp_path, monkeypatch)
        runtime = engine_api._runtime({})
        source = store.message_source("m")
        accepted = engine_api._response(
            {
                "protocol": 1,
                "operation": "inbox.delete",
                "payload": {"message_id": "m"},
            }
        )
        assert accepted["ok"] and accepted["data"]["pending"] == 1, accepted

        def forbidden(*args, **kwargs):
            pytest.fail("accepted attachment source reached provider open")

        monkeypatch.setattr(engine_api, "load_mailbox_account", forbidden)
        with pytest.raises(engine_api.ApiError) as refused:
            engine_api._verified_mailbox_attachment_bytes(runtime, source, "2", "public")
        assert refused.value.code == "connect_source_unavailable"
        return

    from test_certificate_expiry_ledger import (
        INSTANCE_A,
        OUTPUT_ID,
        _certificate_job,
        _certificate_pending_fires,
        _record,
        _result,
    )
    from test_connect_v2_engine_api import seeded_runtime

    _, runtime = seeded_runtime(tmp_path)
    monkeypatch.setattr(engine_api, "_runtime", lambda _: runtime)
    store = runtime.store
    fires = _certificate_pending_fires(store, count=2)
    job_id = store.automation_fire_attempts(fires[0].fire_id)[0].dispatch_request_id
    _certificate_job(store, job_id)
    for index, fire in enumerate(fires):
        store.transition_automation_fire(
            fire_id=fire.fire_id,
            expected_state=fire.state,
            expected_version=fire.state_version,
            next_state="submitted",
            reason="connect_admitted",
            job_id=job_id,
        )
        if index == 0:
            store.transition_connect_job(
                job_id=job_id,
                expected_state="requested",
                next_state="completed",
                provider_app_id="invoice-processor",
                provider_instance_id=INSTANCE_A,
                result=_result(_record()),
            )
    payload = dict(message_id="message-1", part_id="2", job_id=job_id, artifact_id=OUTPUT_ID)
    before = engine_api._response(
        {
            "protocol": 1,
            "operation": "connect.output.present",
            "payload": payload,
        }
    )
    assert before["ok"], before
    retain_legacy_aliases(store, "message-1")
    accepted = engine_api._response(
        {
            "protocol": 1,
            "operation": "inbox.delete",
            "payload": {"message_id": "message-1"},
        }
    )
    assert accepted["ok"] and accepted["data"]["pending"] == 1, accepted
    assert store.connect_job(job_id) is not None  # Existing reconciliation receipt survives.
    if owner == "output_export":
        destination = tmp_path / "public-export"
        destination.mkdir(mode=0o700)
        payload["destination_dir"] = str(destination)
    response = engine_api._response(
        {
            "protocol": 1,
            "operation": "connect.output." + owner.removeprefix("output_"),
            "payload": payload,
        }
    )
    assert not response["ok"] and response["error"]["code"] == "not_found", response
    if owner == "output_export":
        assert not list(destination.iterdir())


def review_notification_fixture(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from test_service import admit_scheduling_run, config

    from eom_email_watcher import engine_api

    cfg = config(tmp_path)
    store = database.Store(cfg.database_file)
    store.initialize()
    _, run = admit_scheduling_run(store, monkeypatch)
    reviewed = store.transition_automation_to_review(
        run.run_id,
        run.state_version,
        next_state="manual_review",
        failure_code="source_invalid",
    )
    intent = next(
        item
        for item in store.notification_intents(kind="automation_review")
        if item.subject_id == reviewed.run_id
    )
    with store.connection() as db:
        source_id = db.execute("SELECT message_id FROM messages").fetchone()[0]
    retain_legacy_aliases(store, source_id)
    monkeypatch.setattr(engine_api, "_runtime", lambda _: SimpleNamespace(store=store, config=cfg))
    return cfg, store, intent, source_id


@pytest.mark.parametrize("kind", ["fallback", "analysis", "automation_review"])
@pytest.mark.parametrize("accept_at", ["never", "before", "desktop", "desktop_failure"])
def test_each_notification_channel_rechecks_accepted_source(tmp_path, monkeypatch, kind, accept_at):
    from dataclasses import replace
    from types import SimpleNamespace

    from test_removal_refusal import held_runtime
    from test_service import FakeModel
    from test_thread_reconcile import KEY

    from eom_email_watcher import engine_api, notifications, service

    model_result = FakeModel().analyze()
    if kind == "automation_review":
        cfg, store, intent, source_id = review_notification_fixture(tmp_path, monkeypatch)
        cfg = replace(
            cfg,
            notifications_enabled=True,
            ntfy_topic="public-fixture-topic",
            ntfy_content_disclosure_acknowledged=True,
        )
        monkeypatch.setattr(
            engine_api, "_runtime", lambda _: SimpleNamespace(store=store, config=cfg)
        )
    else:
        store = held_runtime(tmp_path, monkeypatch)
        watcher = engine_api._runtime({})
        cfg = replace(
            watcher.config,
            notifications_enabled=True,
            ntfy_topic="public-fixture-topic",
            ntfy_content_disclosure_acknowledged=True,
        )
        watcher.config = cfg
        message = store.pending()[0]
        source_id = "m"
        if kind == "analysis":
            database.Store(store.path).mark_analyzed(
                "m",
                model_result.model_dump(),
                mailbox_identity_key=KEY,
                body_chars=12,
                body_source_chars=12,
                now=NOW,
            )
    delivered = []
    accepted = []
    acknowledgements = []
    fallback_retries = []
    ack_name = {
        "fallback": "mark_fallback_notified",
        "analysis": "mark_delivery_complete",
        "automation_review": "acknowledge_notification",
    }[kind]
    original_ack = getattr(store, ack_name)

    def acknowledge(*args, **kwargs):
        acknowledgements.append(True)
        return original_ack(*args, **kwargs)

    monkeypatch.setattr(store, ack_name, acknowledge)
    if kind == "analysis":
        original_fallback = watcher._send_fallback

        def fallback(*args, **kwargs):
            fallback_retries.append(True)
            return original_fallback(*args, **kwargs)

        monkeypatch.setattr(watcher, "_send_fallback", fallback)

    def accept():
        reply = engine_api._response(
            {
                "protocol": 1,
                "operation": "inbox.delete",
                "payload": {"message_id": source_id},
            }
        )
        assert reply["ok"] and reply["data"]["pending"] == 1, reply
        accepted.append(reply)

    def desktop(*args, **kwargs):
        delivered.append("desktop")
        if accept_at in {"desktop", "desktop_failure"}:
            accept()
        if accept_at == "desktop_failure":
            raise notifications.NotificationError("public desktop transport failure")

    def ntfy(*args, **kwargs):
        delivered.append("ntfy")

    monkeypatch.setattr(notifications, "_send_desktop", desktop)
    monkeypatch.setattr(notifications, "_send_ntfy", ntfy)
    if accept_at == "before":
        accept()
    if kind == "automation_review":
        outcome = service._deliver_automation_review_intent(cfg, store, intent, {}, dry_run=False)
    elif kind == "fallback":
        outcome = watcher._send_fallback(message, False)
    else:
        outcome = watcher._deliver_analysis(message, model_result, False, partial_summary=False)
    expected = (
        ["desktop", "ntfy"]
        if accept_at == "never"
        else ([] if accept_at == "before" else ["desktop"])
    )
    assert delivered == expected
    actual_delivery = accept_at in {"never", "desktop"}
    assert len(acknowledgements) == int(actual_delivery)
    assert not fallback_retries
    if kind != "analysis":
        assert outcome == actual_delivery
    assert len(accepted) == int(accept_at != "never")
    with store.connection() as db:
        if kind == "automation_review":
            acknowledged = db.execute(
                "SELECT review_notified_at FROM automation_runs WHERE run_id=?",
                (intent.subject_id,),
            ).fetchone()[0]
        else:
            row = db.execute(
                "SELECT notified_at,fallback_notified_at FROM messages WHERE message_id=?",
                (source_id,),
            ).fetchone()
            acknowledged = row[0] if kind == "analysis" else row[1]
        assert (acknowledged is not None) is (accept_at == "never")


@pytest.mark.parametrize("missing", [False, True])
def test_native_notification_queue_excludes_only_accepted_review_source(
    tmp_path, monkeypatch, missing
):
    from dataclasses import replace
    from types import SimpleNamespace

    from test_service import admit_scheduling_run, config

    from eom_email_watcher import engine_api

    cfg = replace(config(tmp_path), notifications_enabled=True, ntfy_topic=None)
    store = database.Store(cfg.database_file)
    store.initialize()
    _, first = admit_scheduling_run(store, monkeypatch)
    store.transition_automation_to_review(
        first.run_id,
        first.state_version,
        next_state="manual_review",
        failure_code="source_invalid",
    )
    with store.connection() as db:
        first_message = db.execute("SELECT message_id FROM messages").fetchone()[0]
    _, second = admit_scheduling_run(store, monkeypatch, provider_message_id="other-schedule")
    store.transition_automation_to_review(
        second.run_id,
        second.state_version,
        next_state="manual_review",
        failure_code="source_invalid",
    )
    with store.connection() as db:
        second_message = db.execute(
            "SELECT message_id FROM messages WHERE provider_message_id=?", ("other-schedule",)
        ).fetchone()[0]
    initial = store.notification_intents()
    assert {item.subject_id for item in initial} == {
        first_message,
        first.run_id,
        second_message,
        second.run_id,
    }
    assert store.notification_intent_count() == len(initial)
    retain_legacy_aliases(store, first_message)
    monkeypatch.setattr(engine_api, "_runtime", lambda _: SimpleNamespace(store=store, config=cfg))
    accepted = engine_api._response(
        {
            "protocol": 1,
            "operation": "inbox.delete",
            "payload": {"message_id": first_message},
        }
    )
    assert accepted["ok"] and accepted["data"]["pending"] == 1, accepted
    if missing:
        with store.connection() as db:
            db.execute(
                "DELETE FROM messages WHERE message_id=? OR logical_of=?",
                (first_message, first_message),
            )
    response = engine_api._response(
        {"protocol": 1, "operation": "notifications.pending", "payload": {}}
    )
    assert response["ok"], response
    assert {item["subject_id"] for item in response["data"]["items"]} == {
        second_message,
        second.run_id,
    }
    assert store.notification_intent_count() == len(response["data"]["items"])
    assert {item.subject_id for item in store.notification_intents(kind="automation_review")} == {
        second.run_id
    }


@pytest.mark.parametrize("kind", ["fallback", "analysis", "automation_review"])
@pytest.mark.parametrize("accept", [False, True])
def test_public_notification_ack_uses_accepted_source(tmp_path, monkeypatch, kind, accept):
    from dataclasses import replace

    from test_removal_refusal import held_runtime
    from test_service import FakeModel
    from test_thread_reconcile import KEY

    from eom_email_watcher import engine_api

    if kind == "automation_review":
        _, store, intent, source_id = review_notification_fixture(tmp_path, monkeypatch)
    else:
        store = held_runtime(tmp_path, monkeypatch)
        watcher = engine_api._runtime({})
        watcher.config = replace(watcher.config, notifications_enabled=True, ntfy_topic=None)
        source_id = "m"
        if kind == "analysis":
            database.Store(store.path).mark_analyzed(
                source_id,
                FakeModel().analyze().model_dump(),
                mailbox_identity_key=KEY,
                body_chars=12,
                body_source_chars=12,
                now=NOW,
            )
        else:
            store.record_failure(source_id, "public failure", 0)
        intent = next(
            item for item in store.notification_intents(kind=kind) if item.subject_id == source_id
        )
    if accept:
        accepted = engine_api._response(
            {
                "protocol": 1,
                "operation": "inbox.delete",
                "payload": {"message_id": source_id},
            }
        )
        assert accepted["ok"] and accepted["data"]["pending"] == 1, accepted
    response = engine_api._response(
        {
            "protocol": 1,
            "operation": "notifications.ack",
            "payload": {
                name: getattr(intent, name)
                for name in (
                    "message_id",
                    "kind",
                    "analysis_at",
                    "subject_type",
                    "subject_id",
                    "revision",
                )
            },
        }
    )
    if accept:
        assert not response["ok"] and response["error"]["code"] == "not_found", response
    else:
        assert response["ok"] and response["data"]["status"] == "acknowledged", response
    with store.connection() as db:
        if kind == "automation_review":
            acknowledged = db.execute(
                "SELECT review_notified_at FROM automation_runs WHERE run_id=?",
                (intent.subject_id,),
            ).fetchone()[0]
        else:
            row = db.execute(
                "SELECT notified_at,fallback_notified_at FROM messages WHERE message_id=?",
                (source_id,),
            ).fetchone()
            acknowledged = row[0] if kind == "analysis" else row[1]
        assert (acknowledged is not None) is (not accept)


@pytest.mark.parametrize(
    "accept_at,model_error",
    [
        ("never", False),
        ("before", False),
        ("metadata", False),
        ("body", False),
        ("reservation", False),
        ("model", False),
        ("model", True),
        ("model", "gateway"),
    ],
)
def test_scheduling_extraction_rechecks_accepted_source(
    tmp_path, monkeypatch, accept_at, model_error
):
    from types import SimpleNamespace

    from test_service import (
        SCHEDULING_FIXTURE_NOW,
        AnyAutomationGateway,
        ExtractionModel,
        admit_scheduling_run,
        allow_automation_processing,
        config,
        valid_scheduling_output,
    )

    from eom_email_watcher import engine_api, service
    from eom_email_watcher.model import GatewayModel, GatewayModelError, ModelError

    cfg = config(tmp_path)
    store = database.Store(cfg.database_file)
    store.initialize()
    _, run = admit_scheduling_run(store, monkeypatch)
    with store.connection() as db:
        source_id = db.execute("SELECT message_id FROM messages").fetchone()[0]
    retain_legacy_aliases(store, source_id)
    monkeypatch.setattr(engine_api, "_runtime", lambda _: SimpleNamespace(store=store, config=cfg))
    calls = []
    accepted = []
    acknowledgements = []

    def accept():
        reply = engine_api._response(
            {
                "protocol": 1,
                "operation": "inbox.delete",
                "payload": {"message_id": source_id},
            }
        )
        assert reply["ok"] and reply["data"]["pending"] == 1, reply
        accepted.append(reply)

    class Gateway(AnyAutomationGateway):
        def metadata(self, *args, **kwargs):
            calls.append("metadata")
            result = super().metadata(*args, **kwargs)
            if accept_at == "metadata":
                accept()
            return result

        def content(self, *args, **kwargs):
            calls.append("body")
            result = super().content(*args, **kwargs)
            if accept_at == "body":
                accept()
            return result

    class Model(ExtractionModel, GatewayModel):
        def acknowledge(self, request_id, disposition):
            with store.connection() as db:
                rows = db.execute(
                    "SELECT status FROM automation_extraction_payloads WHERE run_id=?",
                    (run.run_id,),
                ).fetchall()
            acknowledgements.append((disposition, [row[0] for row in rows]))

        def extract_scheduling(self, **kwargs):
            calls.append("model")
            if accept_at == "model":
                accept()
            return super().extract_scheduling(**kwargs)

    allow_automation_processing(monkeypatch, Gateway())
    original_page = store.recoverable_automation_runs

    def page(*args, **kwargs):
        rows = original_page(*args, **kwargs)
        if rows and accept_at == "before" and not accepted:
            accept()
        return rows

    monkeypatch.setattr(store, "recoverable_automation_runs", page)
    original_reserve = store.reserve_automation_extraction

    def reserve(*args, **kwargs):
        result = original_reserve(*args, **kwargs)
        if accept_at == "reservation":
            accept()
        return result

    monkeypatch.setattr(store, "reserve_automation_extraction", reserve)
    failure = None
    try:
        outcome = service.process_scheduling_automations(
            cfg,
            store,
            Model(
                [
                    GatewayModelError("forbidden", retryable=False)
                    if model_error == "gateway"
                    else ModelError("public model unavailable")
                    if model_error
                    else valid_scheduling_output()
                ]
            ),
            now=SCHEDULING_FIXTURE_NOW,
        )
    except (KeyError, RuntimeError) as exc:
        failure = exc
    assert failure is None, repr(failure)
    expected = {
        "before": [],
        "metadata": ["metadata"],
        "body": ["metadata", "body"],
        "reservation": ["metadata", "body"],
    }.get(accept_at, ["metadata", "body", "model"])
    assert calls == expected
    current = store.automation_run(run.run_id)
    assert current is not None
    if accept_at == "never":
        assert outcome.processed == 1 and current.state == "proposing"
        assert store.automation_extraction_payloads(run.run_id)[0].status == "accepted"
    else:
        assert len(accepted) == 1 and current.state == "source_unavailable"
        assert not store.automation_extraction_payloads(run.run_id)

    if accept_at == "never":
        assert acknowledgements == [("persisted", ["accepted"])]
    elif accept_at == "model" and not model_error:
        assert acknowledgements == [("application_rejected", [])]
    else:
        assert not acknowledgements


@pytest.mark.parametrize("accept_at", ["never", "before", "proposal"])
def test_scheduling_proposal_rechecks_accepted_source(tmp_path, monkeypatch, accept_at):
    from types import SimpleNamespace

    from test_service import (
        SCHEDULING_FIXTURE_NOW,
        ProposalAuthorization,
        config,
        proposing_automation_run,
    )

    from eom_email_watcher import engine_api, service
    from eom_email_watcher.microsoft_calendar import CalendarMeetingProposal, CalendarProposalResult

    cfg = config(tmp_path)
    store = database.Store(cfg.database_file)
    store.initialize()
    _, run = proposing_automation_run(cfg, store, monkeypatch)
    with store.connection() as db:
        source_id = db.execute("SELECT message_id FROM messages").fetchone()[0]
    retain_legacy_aliases(store, source_id)
    monkeypatch.setattr(engine_api, "_runtime", lambda _: SimpleNamespace(store=store, config=cfg))
    monkeypatch.setattr(
        service,
        "_scheduling_proposal_authorization",
        lambda *args, **kwargs: ProposalAuthorization(),
    )
    accepted = []
    calls = []

    def accept():
        reply = engine_api._response(
            {
                "protocol": 1,
                "operation": "inbox.delete",
                "payload": {"message_id": source_id},
            }
        )
        assert reply["ok"] and reply["data"]["pending"] == 1, reply
        accepted.append(reply)

    original_page = store.proposable_automation_runs

    def page(*args, **kwargs):
        rows = original_page(*args, **kwargs)
        if rows and accept_at == "before" and not accepted:
            accept()
        return rows

    def find(*args, **kwargs):
        calls.append(True)
        if accept_at == "proposal":
            accept()
        return CalendarProposalResult(
            CalendarMeetingProposal(
                start="2026-09-08T10:00:00-05:00",
                end="2026-09-08T10:30:00-05:00",
                timezone="America/Chicago",
                suggestion_reason="Everyone is available.",
            ),
            None,
        )

    monkeypatch.setattr(store, "proposable_automation_runs", page)
    monkeypatch.setattr(service, "find_meeting_time", find)
    failure = None
    try:
        outcome = service.process_scheduling_proposals(cfg, store, now=SCHEDULING_FIXTURE_NOW)
    except (KeyError, RuntimeError) as exc:
        failure = exc
    assert failure is None, repr(failure)
    assert len(calls) == int(accept_at != "before")
    current = store.automation_run(run.run_id)
    assert current is not None
    if accept_at == "never":
        assert outcome.processed == 1 and current.state == "awaiting_confirmation"
        assert store.automation_proposal(run.run_id) is not None
    else:
        assert len(accepted) == 1 and current.state == "source_unavailable"
        assert store.automation_proposal(run.run_id) is None


@pytest.mark.parametrize("kind", ["delete", "clear"])
def test_deferred_completion_purges_expired_automation_at_physical_owner(
    tmp_path, monkeypatch, kind
):
    from test_service import admit_scheduling_run, config

    from eom_email_watcher import removal, source_locks

    monkeypatch.setattr(database, "MAX_RETENTION_DAYS", 30)
    cfg = config(tmp_path)
    store = database.Store(cfg.database_file)
    store.initialize()
    _, run = admit_scheduling_run(store, monkeypatch)
    with store.connection() as db:
        source_id = db.execute("SELECT message_id FROM messages").fetchone()[0]
    retain_legacy_aliases(store, source_id)
    cleanup_at = database.datetime.fromisoformat(run.expires_at) + timedelta(days=1)
    receipt = local_removal(store, "request_removal",
        kind, {"message_id": source_id} if kind == "delete" else {}, now=cleanup_at
    )
    assert receipt.completed == 0 and receipt.pending == 1
    assert store.automation_run(run.run_id) is not None
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 33

    class CompletionTime(database.datetime):
        @classmethod
        def now(cls, tz=None):
            return cleanup_at.astimezone(tz) if tz is not None else cleanup_at.replace(tzinfo=None)

    monkeypatch.setattr(removal, "datetime", CompletionTime)
    reopened = database.Store(store.path)
    source_locks.qualify_protocol(reopened, fresh=False)
    assert reopened.automation_run(run.run_id) is None
    assert reopened.automation_events(run.run_id) == []
    with reopened.connection() as db:
        assert not db.execute("SELECT 1 FROM messages").fetchone()
        result = removal.outcome(db, receipt.request_id)
        assert result.completed == 1 and result.pending == 0
    source_locks.qualify_protocol(reopened, fresh=False)
    assert reopened.automation_run(run.run_id) is None


@pytest.mark.parametrize("missing", [False, True, "replaced"])
def test_completed_missing_target_preserves_frozen_source_suppression(
    tmp_path, monkeypatch, missing
):
    from test_removal_refusal import held_runtime
    from test_thread_reconcile import KEY

    from eom_email_watcher import engine_api, removal, source_locks
    from eom_email_watcher.config import exact_sender_selector_id

    store = held_runtime(tmp_path, monkeypatch)
    accepted = engine_api._response(
        {
            "protocol": 1,
            "operation": "inbox.delete",
            "payload": {"message_id": "m"},
        }
    )
    assert accepted["ok"] and accepted["data"]["pending"] == 1, accepted
    if missing:
        with store.connection() as db:
            db.execute("DELETE FROM messages")
            db.execute("DELETE FROM message_locations")
    if missing == "replaced":
        store.register_mail_account(
            "imap",
            "imap-new",
            display_name="Public fixture",
            address="new@example.com",
            active=False,
        )
        store.reconcile_mailbox_identity("imap", "imap-new", "e" * 64, legacy_status="replacement")
        assert store.add_message(
            message_id="m",
            provider="imap",
            account_id="imap-new",
            mailbox_identity_key="e" * 64,
            provider_message_id="new-native",
            thread_id=None,
            sender="new@example.com",
            sender_name=None,
            subject="Public replacement",
            received_at=NOW.isoformat(),
            admission=database.AdmissionProvenance(
                "exact_sender",
                exact_sender_selector_id("new@example.com"),
                None,
                "e" * 64,
                NOW.isoformat(),
            ),
        )
    source_locks.qualify_protocol(store, fresh=False)
    with store.connection() as db:
        receipt = removal.outcome(db, accepted["data"]["request_id"])
        assert receipt.completed == 1 and receipt.pending == 0
        if missing == "replaced":
            assert tuple(
                db.execute(
                    "SELECT message_id,provider,account_id,mailbox_identity_key,"
                    "provider_message_id FROM messages"
                ).fetchone()
            ) == (
                "m",
                "imap",
                "imap-new",
                "e" * 64,
                "new-native",
            )
        else:
            assert not db.execute("SELECT 1 FROM messages").fetchone()
    for native in ("m", "copy-00"):
        assert store.has_seen_message(
            native, provider="gmail", account_id="mailbox", mailbox_identity_key=KEY
        )
    for provider, account, key, native in (
        ("imap", "mailbox", KEY, "m"),
        ("gmail", "other", KEY, "m"),
        ("gmail", "mailbox", "e" * 64, "m"),
        ("gmail", "mailbox", KEY, "other"),
    ):
        assert not store.has_seen_message(
            native, provider=provider, account_id=account, mailbox_identity_key=key
        )


@pytest.mark.parametrize("kind", ["delete", "clear"])
def test_public_removal_request_lifecycle_survives_lost_reply_and_restart(
    tmp_path, monkeypatch, kind
):
    from test_removal_refusal import held_runtime, snapshot
    from test_thread_reconcile import seed

    from eom_email_watcher import engine_api, source_locks

    store = held_runtime(tmp_path, monkeypatch)
    watcher = engine_api._runtime({})
    parameters = {"message_id": "m"} if kind == "delete" else {}
    before = snapshot(store)
    prepared = engine_api._response(
        {
            "protocol": 1,
            "operation": "inbox.removal.prepare",
            "payload": {"kind": kind, **parameters},
        }
    )
    assert prepared["ok"], prepared
    key = prepared["data"]["request_id"]
    assert set(prepared["data"]) == {"request_id"}
    assert isinstance(key, str) and key
    assert snapshot(store) == before
    status_request = {
        "protocol": 1,
        "operation": "inbox.removal.status",
        "payload": {"kind": kind, "request_id": key, **parameters},
    }
    unaccepted = engine_api._response(status_request)
    assert not unaccepted["ok"] and unaccepted["error"]["code"] == "not_found", unaccepted
    assert snapshot(store) == before
    mutation = {
        "protocol": 1,
        "operation": "inbox." + kind,
        "payload": {"request_id": key, **parameters},
    }
    # Execute once, then discard its response as if delivery was lost.
    accepted = engine_api._response(mutation)
    assert accepted["ok"] and accepted["data"]["pending"] == 1, accepted
    assert accepted["data"]["deleted"] == (False if kind == "delete" else 0)
    assert type(accepted["data"]["deleted"]) is (bool if kind == "delete" else int)
    persisted = engine_api._response(status_request)
    assert persisted["ok"], persisted
    assert persisted["data"] == {
        name: accepted["data"][name]
        for name in (
            "request_id",
            "intent_ids",
            "outcome",
            "requested",
            "completed",
            "pending",
            "message",
        )
    }
    assert persisted["data"]["outcome"] == "deferred"
    bare_status = engine_api._response(
        {"protocol": 1, "operation": "inbox.removal.status", "payload": {"request_id": key}}
    )
    assert bare_status["ok"] and bare_status["data"] == persisted["data"], bare_status
    seed(store, "later", thread_id="later", rfc_message_id="later@vendor")
    reopened = database.Store(store.path)
    with monkeypatch.context() as blocked:

        def unavailable(*args, **kwargs):
            raise source_locks.SourceProtocolUnavailable("public restart before lift")

        blocked.setattr(source_locks, "qualify_protocol", unavailable)
        reopened.initialize()
    watcher.store = reopened
    assert engine_api._response(status_request)["data"] == persisted["data"]
    source_locks.qualify_protocol(reopened, fresh=False)
    complete = engine_api._response(status_request)
    assert complete["ok"], complete
    assert complete["data"] == {
        **persisted["data"],
        "outcome": "completed",
        "completed": 1,
        "pending": 0,
        "message": None,
    }
    retried = engine_api._response(mutation)
    assert retried["ok"] and retried["data"]["completed"] == 1, retried
    assert retried["data"]["request_id"] == key
    with reopened.connection() as db:
        assert [r[0] for r in db.execute("SELECT message_id FROM logical_messages")] == ["later"]
        assert db.execute("SELECT COUNT(*) FROM message_removal_requests").fetchone()[0] == 1


@pytest.mark.parametrize(
    "operation,payload",
    [
        *[
            ("inbox.removal.prepare", {"kind": kind})
            for kind in (None, False, 0, "", [], {}, "unfollow")
        ],
        *[
            ("inbox.removal.prepare", {"kind": "delete", "message_id": value})
            for value in (None, False, 0, "", " ", [], "x" * 513)
        ],
        ("inbox.removal.prepare", {}),
        ("inbox.removal.prepare", {"kind": "delete"}),
        ("inbox.removal.prepare", {"kind": "clear", "message_id": "m"}),
        ("inbox.removal.prepare", {"kind": "clear", "request_id": "extra"}),
        *[("inbox.removal.status", {"request_id": value}) for value in (None, False, 0, [])],
        ("inbox.removal.status", {}),
        ("inbox.removal.status", {"request_id": "key", "kind": False}),
        ("inbox.removal.status", {"request_id": "key", "message_id": "m"}),
    ],
)
def test_removal_lifecycle_invalid_parameters_refuse_before_runtime(
    monkeypatch, operation, payload
):
    from eom_email_watcher import engine_api

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid removal parameters reached runtime access")

    monkeypatch.setattr(engine_api, "_runtime", forbidden)
    result = engine_api._response({"protocol": 1, "operation": operation, "payload": payload})
    assert not result["ok"] and result["error"]["code"] == "invalid_request", result


def test_removal_prepare_boundary_and_status_authentication_are_read_only(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from test_removal_refusal import snapshot
    from test_service import config

    from eom_email_watcher import engine_api, removal

    cfg = config(tmp_path)
    store = database.Store(cfg.database_file)
    store.initialize()
    monkeypatch.setattr(engine_api, "_runtime", lambda _: SimpleNamespace(store=store, config=cfg))
    before = snapshot(store)
    for kind, parameters in (
        ("delete", {"message_id": "m"}),
        ("delete", {"message_id": "x" * 512}),
        ("clear", {}),
    ):
        result = engine_api._response(
            {
                "protocol": 1,
                "operation": "inbox.removal.prepare",
                "payload": {"kind": kind, **parameters},
            }
        )
        assert result["ok"] and set(result["data"]) == {"request_id"}, result
        key = result["data"]["request_id"]
        with store.connection() as db:
            removal.validate_key(db, key, kind, removal.request_hash(kind, parameters), now=NOW)
        assert snapshot(store) == before
    key = removal.issue_key(store, "clear", {}, retention_days=1, now=NOW)
    accepted = local_removal(store, "request_removal", "clear", {}, request_id=key, now=NOW)
    assert accepted.requested == accepted.completed == 0
    retained = snapshot(store)
    future = NOW + timedelta(days=3)

    class FutureTime(database.datetime):
        @classmethod
        def now(cls, tz=None):
            return future.astimezone(tz) if tz is not None else future.replace(tzinfo=None)

    monkeypatch.setattr(removal, "datetime", FutureTime)
    known = engine_api._response(
        {"protocol": 1, "operation": "inbox.removal.status", "payload": {"request_id": key}}
    )
    assert known["ok"], known
    assert known["data"] == {
        "request_id": key,
        "intent_ids": [],
        "outcome": "completed",
        "requested": 0,
        "completed": 0,
        "pending": 0,
        "message": None,
    }
    expired = removal.issue_key(store, "delete", {"message_id": "other"}, retention_days=1, now=NOW)
    fresh = removal.issue_key(
        store, "delete", {"message_id": "other"}, retention_days=1, now=future
    )
    forged = fresh[:-1] + ("0" if fresh[-1] != "0" else "1")
    for payload in (
        {"request_id": expired, "kind": "delete", "message_id": "other"},
        {"request_id": expired},
        {"request_id": fresh},
        {"request_id": forged, "kind": "delete", "message_id": "other"},
        {"request_id": fresh, "kind": "delete", "message_id": "different"},
        {"request_id": key, "kind": "delete", "message_id": "other"},
        {"request_id": "", "kind": "clear"},
        {"request_id": "malformed", "kind": "clear"},
    ):
        result = engine_api._response(
            {"protocol": 1, "operation": "inbox.removal.status", "payload": payload}
        )
        assert not result["ok"] and result["error"]["code"] == "invalid_request", result
        assert snapshot(store) == retained
    unaccepted = engine_api._response(
        {
            "protocol": 1,
            "operation": "inbox.removal.status",
            "payload": {"request_id": fresh, "kind": "delete", "message_id": "other"},
        }
    )
    assert not unaccepted["ok"] and unaccepted["error"]["code"] == "not_found", unaccepted
    assert snapshot(store) == retained


def test_removal_status_reads_one_snapshot_during_receipt_retirement(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    from types import SimpleNamespace

    from connect_automate.locking import operation_lock

    from eom_email_watcher import engine_api, removal, source_locks

    store, _, _ = followed(tmp_path)
    key = removal.issue_key(store, "delete", {"message_id": "m"}, retention_days=1, now=NOW)
    with removal.acceptance_scope(store) as acceptance:
        result = store.request_removal(
            "delete", {"message_id": "m"}, request_id=key, now=NOW, acceptance=acceptance,
        )
    assert result.completed == 1 and result.pending == 0
    reached, release = threading.Event(), threading.Event()
    main_thread = threading.get_ident()
    timeouts = []

    class ProbeStore(database.Store):
        @contextmanager
        def connection(self):
            with super().connection() as db:
                execute = db.execute

                def observe(sql, parameters=()):
                    cursor = execute(sql, parameters)
                    if (
                        threading.get_ident() != main_thread
                        and not reached.is_set()
                        and sql.startswith(
                            "SELECT kind,parameters_sha256 FROM message_removal_requests"
                        )
                    ):

                        class ReceiptCursor:
                            def fetchone(self):
                                row = cursor.fetchone()
                                reached.set()
                                if not release.wait(10):
                                    timeouts.append(True)
                                return row

                        return ReceiptCursor()
                    return cursor

                db.execute = observe
                try:
                    yield db
                finally:
                    db.execute = execute

    monkeypatch.setattr(
        engine_api, "_runtime", lambda _: SimpleNamespace(store=ProbeStore(store.path))
    )
    request = {"protocol": 1, "operation": "inbox.removal.status", "payload": {"request_id": key}}
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(engine_api._response, request)
        try:
            assert reached.wait(5), "Status did not read the persisted receipt"
            with operation_lock(source_locks.operation_lock_path(store.path), "public operation"):
                removal.retire(store, 30, now=NOW + timedelta(days=365))
            with store.connection() as db:
                assert not db.execute("SELECT 1 FROM message_removal_requests").fetchone()
        finally:
            release.set()
        failure = None
        try:
            response = future.result(timeout=10)
        except (KeyError, RuntimeError, TypeError) as exc:
            failure = exc
    assert not timeouts
    assert failure is None, repr(failure)
    assert response["ok"], response
    assert response["data"]["requested"] == response["data"]["completed"] == 1
    assert response["data"]["pending"] == 0
    assert response["data"]["request_id"] == key


def saved_unfollow_v35(tmp_path, monkeypatch):
    from test_removal_refusal import held_runtime

    with monkeypatch.context() as legacy:
        legacy.setattr(database, "SCHEMA_VERSION", 35)
        store = held_runtime(tmp_path, legacy)
        with store.connection() as db:
            vendor_id = db.execute("SELECT vendor_id FROM vendor_addresses").fetchone()[0]
        store.remove_vendor_address(vendor_id, "vendor@example.com", now=NOW)
        with store.connection() as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 35
            assert db.execute("SELECT text FROM message_bodies").fetchone()[0] == "private body"
            assert not db.execute("SELECT 1 FROM thread_follow WHERE followed=1").fetchone()
            assert not db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='message_removal_requests'"
            ).fetchone()
    return store


def test_saved_unfollow_conversion_is_atomic_and_body_only(tmp_path, monkeypatch):
    from eom_email_watcher import removal, source_locks

    store = saved_unfollow_v35(tmp_path, monkeypatch)

    def unavailable(*args, **kwargs):
        raise source_locks.SourceProtocolUnavailable("migration conversion before native lift")

    with monkeypatch.context() as held:
        held.setattr(source_locks, "qualify_protocol", unavailable)
        store.initialize()
        with store.connection() as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 36
            requests = db.execute("SELECT * FROM message_removal_requests").fetchall()
            assert len(requests) == 1, "Saved-unfollow upgrade must create its receipt atomically"
            receipt = requests[0]
            assert receipt["kind"] == "unfollow"
            removal._parse_key(db, receipt["request_id"], "unfollow", receipt["parameters_sha256"])
            result = removal.outcome(db, receipt["request_id"])
            assert (result.requested, result.completed, result.pending) == (1, 0, 1)
            assert db.execute("SELECT COUNT(*) FROM message_removal_targets").fetchone()[0] == 33
            assert db.execute("SELECT COUNT(*) FROM logical_messages").fetchone()[0] == 1
            assert db.execute("SELECT text FROM message_bodies").fetchone()[0] == "private body"
            assert not removal.message_available(db, "m", include_unfollow=True)
            assert removal.message_available(db, "m")
        store.initialize()
        with store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM message_removal_requests").fetchone()[0] == 1
    source_locks.qualify_protocol(store, fresh=False)
    with store.connection() as db:
        assert not db.execute("SELECT 1 FROM message_bodies").fetchone()
        assert not db.execute("SELECT 1 FROM message_body_unavailable").fetchone()
        assert db.execute("SELECT COUNT(*) FROM logical_messages").fetchone()[0] == 1
        assert not db.execute("SELECT 1 FROM suppressed_messages").fetchone()
        result = removal.outcome(db, receipt["request_id"])
        assert (result.requested, result.completed, result.pending) == (1, 1, 0)


def test_saved_unfollow_conversion_failure_rolls_back_original_store(tmp_path, monkeypatch):
    from test_removal_refusal import snapshot

    from eom_email_watcher import source_locks

    store = saved_unfollow_v35(tmp_path, monkeypatch)
    before = snapshot(store)

    def fail_after_conversion(db):
        assert db.in_transaction
        assert db.execute("SELECT COUNT(*) FROM message_removal_requests").fetchone()[0] == 1
        assert db.execute("PRAGMA user_version").fetchone()[0] == 35
        raise RuntimeError("injected failure after migration conversion")

    with monkeypatch.context() as failing:
        failing.setattr(thread_migration, "check_database", fail_after_conversion)
        with pytest.raises(RuntimeError, match="injected failure after migration conversion"):
            store.initialize()
    assert snapshot(store) == before
    with store.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 35
        assert not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='message_removal_requests'"
        ).fetchone()

    def unavailable(*args, **kwargs):
        raise source_locks.SourceProtocolUnavailable("rollback retry before native lift")

    monkeypatch.setattr(source_locks, "qualify_protocol", unavailable)
    store.initialize()
    store.initialize()
    with store.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 36
        assert db.execute("SELECT COUNT(*) FROM message_removal_requests").fetchone()[0] == 1
        assert db.execute("SELECT text FROM message_bodies").fetchone()[0] == "private body"


def test_saved_unfollow_conversion_rejects_unowned_capabilities_before_writes(
    tmp_path, monkeypatch
):
    import copy
    from concurrent.futures import ThreadPoolExecutor

    from eom_email_watcher import removal, source_locks

    store = saved_unfollow_v35(tmp_path, monkeypatch)
    original = removal.convert_saved_unfollow
    captured = []

    def check_authority(db, owner, *, now):
        assert db.in_transaction
        captured.append(owner)
        parameters = {"message_ids": ["m"]}
        key = removal._issue_key(db, "unfollow", parameters, retention_days=30, now=now)
        for invalid in (None, False, True, object(), copy.copy(owner)):
            with pytest.raises(RuntimeError, match="live migration owner"):
                removal._accept_in_transaction(
                    db, key, "unfollow", parameters, ["m"], now=now, authority=invalid,
                )
            assert not db.execute("SELECT 1 FROM message_removal_requests").fetchone()
        with (
            store.connection() as other,
            pytest.raises(RuntimeError, match="live migration owner"),
        ):
            thread_migration.require_migration_owner(owner, other)
        with ThreadPoolExecutor(max_workers=1) as pool:
            error = pool.submit(thread_migration.require_migration_owner, owner, db).exception(5)
        assert isinstance(error, RuntimeError), repr(error)
        assert "live migration owner" in str(error)
        assert not db.execute("SELECT 1 FROM message_removal_requests").fetchone()
        original(db, owner, now=now)

    def unavailable(*args, **kwargs):
        raise source_locks.SourceProtocolUnavailable("authority fixture before native lift")

    monkeypatch.setattr(removal, "convert_saved_unfollow", check_authority)
    monkeypatch.setattr(source_locks, "qualify_protocol", unavailable)
    store.initialize()
    with store.connection() as db:
        with pytest.raises(RuntimeError, match="live migration owner"):
            thread_migration.require_migration_owner(captured[0], db)
        assert db.execute("SELECT COUNT(*) FROM message_removal_requests").fetchone()[0] == 1


def test_migration_capability_expires_on_native_commit_before_scope_exit(tmp_path, monkeypatch):
    store = saved_unfollow_v35(tmp_path, monkeypatch)
    with thread_migration.upgrade_owner(store.path) as owner, owner.transaction(store) as db:
        thread_migration.require_migration_owner(owner, db)
        db.commit()
        db.execute("BEGIN IMMEDIATE")
        with pytest.raises(RuntimeError, match="live migration owner"):
            thread_migration.require_migration_owner(owner, db)
    with store.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 35


@pytest.mark.parametrize("upgrade", [False, True])
def test_migration_without_saved_unfollow_creates_no_receipt(tmp_path, monkeypatch, upgrade):
    from test_removal_refusal import held_runtime

    if upgrade:
        with monkeypatch.context() as legacy:
            legacy.setattr(database, "SCHEMA_VERSION", 35)
            store = held_runtime(tmp_path, legacy)
        store.initialize()
    else:
        store = database.Store(tmp_path / "empty.db")
        store.initialize()
    store.initialize()
    with store.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 36
        assert not db.execute("SELECT 1 FROM message_removal_requests").fetchone()
        if upgrade:
            assert db.execute("SELECT text FROM message_bodies").fetchone()[0] == "private body"


def test_converted_unfollow_refollow_cannot_adopt_a_late_body(tmp_path, monkeypatch):
    from eom_email_watcher import removal, source_locks

    store = saved_unfollow_v35(tmp_path, monkeypatch)

    def unavailable(*args, **kwargs):
        raise source_locks.SourceProtocolUnavailable("refollow before native lift")

    with monkeypatch.context() as held:
        held.setattr(source_locks, "qualify_protocol", unavailable)
        store.initialize()
    with store.connection() as db:
        key = db.execute("SELECT request_id FROM message_removal_requests").fetchone()[0]
        vendor_id = db.execute("SELECT vendor_id FROM vendors").fetchone()[0]
    store.add_vendor_address(vendor_id, "vendor@example.com", now=NOW)
    body_state.record(store, "m", ("late refollow body", 18), fetched_at=NOW)
    with store.connection() as db:
        assert db.execute("SELECT 1 FROM thread_follow WHERE followed=1").fetchone()
        assert db.execute("SELECT text FROM message_bodies").fetchone()[0] == "private body"
        assert removal.outcome(db, key).pending == 1
    source_locks.qualify_protocol(store, fresh=False)
    source_locks.qualify_protocol(store, fresh=False)
    with store.connection() as db:
        assert not db.execute("SELECT 1 FROM message_bodies").fetchone()
        assert db.execute("SELECT COUNT(*) FROM logical_messages").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM message_removal_requests").fetchone()[0] == 1
        assert removal.outcome(db, key).completed == 1
        assert not db.execute("SELECT 1 FROM suppressed_messages").fetchone()


def test_saved_unfollow_upgrade_excludes_old_writer_and_second_runtime(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from dataclasses import replace

    from connect_automate.locking import OperationLockBusy, connect_operation_lock
    from test_service import config

    from eom_email_watcher import removal, runtime, source_locks

    store = saved_unfollow_v35(tmp_path, monkeypatch)
    reached, release, second_started = (threading.Event() for _ in range(3))
    original_conversion = removal.convert_saved_unfollow
    original_initialize = database.Store.initialize
    timeouts = []

    def pause_after_conversion(db, owner, *, now):
        original_conversion(db, owner, now=now)
        assert db.in_transaction
        assert db.execute("PRAGMA user_version").fetchone()[0] == 35
        assert db.execute("SELECT COUNT(*) FROM message_removal_requests").fetchone()[0] == 1
        reached.set()
        if not release.wait(10):
            timeouts.append(True)

    def observed_initialize(self):
        if self is not store:
            second_started.set()
        return original_initialize(self)

    def unavailable(*args, **kwargs):
        raise source_locks.SourceProtocolUnavailable("concurrent migration before native lift")

    monkeypatch.setattr(removal, "convert_saved_unfollow", pause_after_conversion)
    monkeypatch.setattr(database.Store, "initialize", observed_initialize)
    monkeypatch.setattr(source_locks, "qualify_protocol", unavailable)
    cfg = replace(config(tmp_path), database_file=store.path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        migrating = pool.submit(store.initialize)
        try:
            assert reached.wait(5), "Migration did not reach uncommitted conversion"
            with sqlite3.connect(store.path, timeout=0) as old:
                assert old.execute("PRAGMA user_version").fetchone()[0] == 35
                assert not old.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='message_removal_requests'"
                ).fetchone()
                with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                    old.execute("BEGIN IMMEDIATE")
            with (
                pytest.raises(OperationLockBusy),
                connect_operation_lock(
                    store.path.with_name(store.path.name + ".upgrade.lock"), "upgrade busy",
                    timeout_seconds=0,
                ),
            ):
                pytest.fail("Upgrade lock admitted another initializer")
            waiting = pool.submit(runtime.runtime_from_config, cfg, config_path_already_safe=True)
            assert second_started.wait(5), "Second runtime did not enter Store initialization"
            assert not waiting.done(), "Runtime returned before conversion committed"
        finally:
            release.set()
        migrating.result(timeout=10)
        reopened = waiting.result(timeout=10)
    assert not timeouts
    with reopened.store.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 36
        assert db.execute("SELECT COUNT(*) FROM message_removal_requests").fetchone()[0] == 1
        assert db.execute("SELECT text FROM message_bodies").fetchone()[0] == "private body"


@pytest.mark.parametrize("retained", ["copy_body", "unavailable"])
def test_saved_unfollow_conversion_freezes_complete_unit_from_retained_alias_or_marker(
    tmp_path, monkeypatch, retained
):
    from eom_email_watcher import removal, source_locks

    store = saved_unfollow_v35(tmp_path, monkeypatch)
    with store.connection() as db:
        if retained == "copy_body":
            db.execute("UPDATE message_bodies SET message_id='copy-00'")
        else:
            db.execute("DELETE FROM message_bodies")
            db.execute("INSERT INTO message_body_unavailable VALUES('copy-00','source_gone')")

    def unavailable(*args, **kwargs):
        raise source_locks.SourceProtocolUnavailable("alias conversion before native lift")

    with monkeypatch.context() as held:
        held.setattr(source_locks, "qualify_protocol", unavailable)
        store.initialize()
    with store.connection() as db:
        key = db.execute("SELECT request_id FROM message_removal_requests").fetchone()[0]
        assert db.execute("SELECT COUNT(*) FROM message_removal_requests").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM message_removal_targets").fetchone()[0] == 33
        assert removal.outcome(db, key).requested == 1
        assert not removal.message_available(db, "m", include_unfollow=True)
        assert not removal.message_available(db, "copy-00", include_unfollow=True)
        assert db.execute("SELECT COUNT(*) FROM logical_messages").fetchone()[0] == 1
    source_locks.qualify_protocol(store, fresh=False)
    with store.connection() as db:
        assert removal.outcome(db, key).completed == 1
        assert not db.execute("SELECT 1 FROM message_bodies").fetchone()
        assert not db.execute("SELECT 1 FROM message_body_unavailable").fetchone()
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 33
        assert not db.execute("SELECT 1 FROM suppressed_messages").fetchone()


def test_direct_acceptance_requires_runtime_capability_before_writes(tmp_path):
    from test_removal_refusal import snapshot

    store, _, _ = followed(tmp_path)
    before = snapshot(store)
    with pytest.raises(RuntimeError, match="runtime acceptance"):
        store.request_removal("clear", {}, now=NOW)
    assert snapshot(store) == before


def test_runtime_acceptance_capability_rejects_forged_stale_cross_execution_and_path(
    tmp_path, monkeypatch
):
    import copy
    from concurrent.futures import ThreadPoolExecutor

    from test_removal_refusal import snapshot

    from eom_email_watcher import removal

    store, _, _ = followed(tmp_path)
    before = snapshot(store)
    for invalid in (None, False, True, object(), object.__new__(removal._RuntimeAcceptance)):
        with pytest.raises(RuntimeError, match="runtime acceptance"):
            store.request_removal("clear", {}, acceptance=invalid, now=NOW)
        assert snapshot(store) == before
    with pytest.raises(TypeError, match="acquiring scope"):
        removal._RuntimeAcceptance()
    with removal.acceptance_scope(store) as capability:
        with pytest.raises(RuntimeError, match="runtime acceptance"):
            store.request_removal("clear", {}, acceptance=copy.copy(capability), now=NOW)
        with pytest.raises(RuntimeError, match="runtime acceptance"):
            removal.require_runtime_acceptance(capability, tmp_path / "another.db")
        with ThreadPoolExecutor(max_workers=1) as pool:
            error = pool.submit(
                store.request_removal, "clear", {}, acceptance=capability, now=NOW,
            ).exception(5)
        assert isinstance(error, RuntimeError), repr(error)
        assert "runtime acceptance" in str(error)
        with monkeypatch.context() as other_process:
            other_process.setattr(removal.os, "getpid", lambda: capability.execution[0] + 1)
            with pytest.raises(RuntimeError, match="runtime acceptance"):
                removal.require_runtime_acceptance(capability, store.path)
        assert snapshot(store) == before
    with pytest.raises(RuntimeError, match="runtime acceptance"):
        store.request_removal("clear", {}, acceptance=capability, now=NOW)
    assert snapshot(store) == before


def test_runtime_acceptance_refuses_outer_connection_and_native_committed_transaction(tmp_path):
    from eom_email_watcher import removal

    store, _, _ = followed(tmp_path)
    # This connection and transaction predate the acquiring guard scope.
    with store.connection() as outer:
        outer.execute("BEGIN")
        with removal.acceptance_scope(store) as capability, capability.transaction(store) as db:
            authority = db._removal_transaction
            authority.require(db)
            with pytest.raises(RuntimeError, match="guarded write transaction"):
                authority.require(outer)
            with pytest.raises(RuntimeError, match="live migration owner"):
                thread_migration.require_migration_owner(capability, db)
            key = removal._issue_key(db, "clear", {}, retention_days=30, now=NOW)
            with pytest.raises(RuntimeError, match="guarded write transaction"):
                removal._accept_in_transaction(
                    outer, key, "clear", {}, None, now=NOW, authority=authority,
                )
            assert not db.execute("SELECT 1 FROM message_removal_requests").fetchone()
            db.commit()
            db.execute("BEGIN IMMEDIATE")
            with pytest.raises(RuntimeError, match="guarded write transaction"):
                authority.require(db)
    with store.connection() as db:
        assert not db.execute("SELECT 1 FROM message_removal_requests").fetchone()
        assert db.execute("SELECT COUNT(*) FROM logical_messages").fetchone()[0] == 1


def test_delete_api_acquires_once_before_transaction_and_releases_guard_before_completion(
    tmp_path, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    from dataclasses import replace
    from types import SimpleNamespace

    from test_service import config

    from eom_email_watcher import engine_api, removal, source_locks

    store, _, _ = followed(tmp_path)
    cfg = replace(config(tmp_path), database_file=store.path)
    monkeypatch.setattr(engine_api, "_runtime", lambda _: SimpleNamespace(store=store, config=cfg))
    monkeypatch.setattr(source_locks, "SOURCE_LOCK_WAIT_SECONDS", 0.05)
    operation_acquisitions, guard_transactions, active_connections = [], [], []
    original_operation = removal.connect_operation_lock
    original_guard = source_locks.protocol_guard
    original_connection = database.Store.connection

    @contextmanager
    def observe_operation(path, *args, **kwargs):
        operation_acquisitions.append(path)
        with original_operation(path, *args, **kwargs):
            yield

    @contextmanager
    def observe_connection(self):
        with original_connection(self) as db:
            active_connections.append(db)
            try:
                yield db
            finally:
                active_connections.remove(db)

    @contextmanager
    def observe_guard(*args, **kwargs):
        guard_transactions.append(any(db.in_transaction for db in active_connections))
        with original_guard(*args, **kwargs):
            yield

    monkeypatch.setattr(removal, "connect_operation_lock", observe_operation)
    monkeypatch.setattr(database.Store, "connection", observe_connection)
    monkeypatch.setattr(source_locks, "protocol_guard", observe_guard)
    with ThreadPoolExecutor(max_workers=1) as pool:
        response = pool.submit(engine_api._response, {
            "protocol": 1, "operation": "inbox.delete", "payload": {"message_id": "m"},
        }).result(timeout=5)
    assert response["ok"], response
    assert response["data"]["outcome"] == "completed"
    assert response["data"]["completed"] == 1 and response["data"]["pending"] == 0
    assert operation_acquisitions == [source_locks.operation_lock_path(store.path)]
    assert len(guard_transactions) >= 2
    assert not any(guard_transactions), "Guard acquired inside an existing transaction"
    assert not active_connections
    with store.connection() as db:
        assert not db.execute("SELECT 1 FROM messages").fetchone()


@pytest.mark.parametrize("qualified", [False, True])
def test_completion_batches_frozen_units_without_splitting_oversized_legacy_unit(
    tmp_path, monkeypatch, qualified
):
    from contextlib import contextmanager

    from test_removal_refusal import held_runtime
    from test_thread_reconcile import seed

    from eom_email_watcher import engine_api, source_locks

    store = held_runtime(tmp_path, monkeypatch)
    for index in range(3):
        seed(store, f"small-{index}", rfc_message_id=f"small-{index}@vendor")
    if qualified:
        source_locks.qualify_protocol(store, fresh=False)
    with store.connection() as db:
        units = {r[0]: r[1] for r in db.execute(
            "SELECT message_id,COALESCE(logical_of,message_id) FROM messages"
        )}
        assert len(set(units.values())) == 4
    original = store._source_cleanup_locks
    acquired_groups, refused_groups = [], []

    @contextmanager
    def observed(message_ids):
        ids = tuple(message_ids)
        with original(ids) as acquired:
            (acquired_groups if acquired else refused_groups).append(ids)
            yield acquired

    monkeypatch.setattr(database, "SOURCE_CLEANUP_LOCK_BATCH_SIZE", 2)
    monkeypatch.setattr(store, "_source_cleanup_locks", observed)
    response = engine_api._response({"protocol": 1, "operation": "inbox.clear", "payload": {}})
    assert response["ok"], response
    assert response["data"]["requested"] == 4
    assert response["data"]["completed"] == (4 if qualified else 3)
    assert response["data"]["pending"] == (0 if qualified else 1)
    logical_widths = [len({units[row] for row in group}) for group in acquired_groups]
    assert max(logical_widths) == 2, "Completion bypassed the existing bounded batch owner"
    assert all(width <= 2 for width in logical_widths)
    if qualified:
        assert not refused_groups
    else:
        assert len(refused_groups) == 1
        assert len(refused_groups[0]) == 33
        assert {units[row] for row in refused_groups[0]} == {"m"}
        assert max(len(group) for group in acquired_groups) == 2
    with store.connection() as db:
        assert not db.execute("SELECT 1 FROM logical_messages").fetchone()
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == (0 if qualified else 33)
        if not qualified:
            assert db.execute("SELECT text FROM message_bodies").fetchone()[0] == "private body"


@pytest.mark.parametrize("message_id", ["m", "copy-00", "copy-31"])
def test_delete_request_resolves_any_retained_alias_through_frozen_unit(
    tmp_path, monkeypatch, message_id
):
    from test_removal_refusal import held_runtime
    from test_thread_reconcile import seed

    from eom_email_watcher import engine_api, removal, source_locks

    store = held_runtime(tmp_path, monkeypatch)
    parameters = {"message_id": message_id}
    with store.connection() as db:
        expected_targets = [tuple(r) for r in db.execute(
            "SELECT 'm',message_id,provider,account_id,mailbox_identity_key,provider_message_id "
            "FROM messages ORDER BY message_id"
        )]
    key = removal.issue_key(store, "delete", parameters, retention_days=30, now=NOW)
    request = {
        "protocol": 1, "operation": "inbox.delete",
        "payload": {"request_id": key, **parameters},
    }
    accepted = engine_api._response(request)
    assert accepted["ok"], accepted
    assert accepted["data"]["request_id"] == key
    assert accepted["data"]["requested"] == accepted["data"]["pending"] == 1
    assert accepted["data"]["completed"] == 0 and accepted["data"]["deleted"] is False
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM message_removal_requests").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM message_removal_targets").fetchone()[0] == 33
        assert [tuple(r) for r in db.execute(
            "SELECT logical_target_id,message_id,provider,account_id,stored_identity_key,"
            "provider_message_id FROM message_removal_targets ORDER BY message_id"
        )] == expected_targets
        assert [r[0] for r in db.execute(
            "SELECT DISTINCT logical_target_id FROM message_removal_targets"
        )] == ["m"]
        assert db.execute("SELECT text FROM message_bodies").fetchone()[0] == "private body"
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 33
        assert not db.execute("SELECT 1 FROM logical_messages").fetchone()
    seed(store, "later", rfc_message_id="later@vendor")
    replayed = engine_api._response(request)
    assert replayed == accepted
    source_locks.qualify_protocol(store, fresh=False)
    complete = engine_api._response(request)
    assert complete["ok"], complete
    assert complete["data"]["completed"] == 1 and complete["data"]["pending"] == 0
    assert complete["data"]["deleted"] is True
    with store.connection() as db:
        assert [r[0] for r in db.execute("SELECT message_id FROM logical_messages")] == ["later"]
        assert db.execute("SELECT COUNT(*) FROM message_removal_requests").fetchone()[0] == 1


def test_freeze_resolves_selected_alias_without_a_physical_canonical_row(tmp_path, monkeypatch):
    from test_removal_refusal import held_runtime

    from eom_email_watcher import source_locks

    store = held_runtime(tmp_path, monkeypatch, copies=33)
    with store.connection() as db:
        db.execute("UPDATE messages SET logical_of='missing-root' WHERE logical_of='m'")
        unit = database._logical_unit(db, "copy-00")
        assert len(unit.rows) == 33
        assert not db.execute("SELECT 1 FROM messages WHERE message_id='missing-root'").fetchone()
    result = local_removal(
        store, "request_removal", "delete", {"message_id": "copy-00"}, now=NOW,
    )
    assert (result.requested, result.completed, result.pending) == (1, 0, 1)
    with store.connection() as db:
        assert [r[0] for r in db.execute(
            "SELECT DISTINCT logical_target_id FROM message_removal_targets"
        )] == ["missing-root"]
        assert db.execute("SELECT COUNT(*) FROM message_removal_targets").fetchone()[0] == 33
        assert db.execute("SELECT text FROM message_bodies WHERE message_id='m'").fetchone()[0] == (
            "private body"
        )
    source_locks.qualify_protocol(store, fresh=False)
    with store.connection() as db:
        assert [r[0] for r in db.execute("SELECT message_id FROM messages")] == ["m"]
        assert db.execute("SELECT text FROM message_bodies WHERE message_id='m'").fetchone()[0] == (
            "private body"
        )


@pytest.mark.parametrize(
    "field,new_value",
    [
        ("provider", "microsoft365"),
        ("account_id", "new-account"),
        ("mailbox_identity_key", "e" * 64),
        ("provider_message_id", "new-native-id"),
    ],
)
def test_old_alias_request_preserves_reused_binding_and_new_request_freezes_its_own_unit(
    tmp_path, monkeypatch, field, new_value
):
    from test_removal_refusal import held_runtime

    from eom_email_watcher import removal, source_locks
    from eom_email_watcher.config import exact_sender_selector_id

    store = held_runtime(tmp_path, monkeypatch)
    parameters = {"message_id": "copy-00"}
    old_key = removal.issue_key(store, "delete", parameters, retention_days=30, now=NOW)
    pending = local_removal(
        store, "request_removal", "delete", parameters, request_id=old_key, now=NOW,
    )
    assert pending.requested == pending.pending == 1
    with store.connection() as db:
        coordinates = dict(db.execute(
            "SELECT provider,account_id,mailbox_identity_key,provider_message_id "
            "FROM messages WHERE message_id='copy-00'"
        ).fetchone())
        assert coordinates[field] != new_value
        coordinates[field] = new_value
        db.execute("DELETE FROM messages WHERE message_id='copy-00'")
    with store.connection() as db:
        registered = db.execute(
            "SELECT 1 FROM mail_accounts WHERE provider=? AND account_id=?",
            (coordinates["provider"], coordinates["account_id"]),
        ).fetchone()
    if registered is None:
        store.register_mail_account(
            coordinates["provider"], coordinates["account_id"], display_name="Reused scope",
        )
    store.reconcile_mailbox_identity(
        coordinates["provider"], coordinates["account_id"], coordinates["mailbox_identity_key"],
    )
    assert store.add_message(
        message_id="copy-00", **coordinates,
        thread_id="new-thread", sender="vendor@example.com", sender_name=None,
        subject="Reused source", received_at=NOW.isoformat(), rfc_message_id="reused@new",
        admission=database.AdmissionProvenance(
            "exact_sender", exact_sender_selector_id("vendor@example.com"), None,
            coordinates["mailbox_identity_key"], NOW.isoformat(),
        ),
    )
    new_binding = tuple(coordinates[name] for name in (
        "provider", "account_id", "mailbox_identity_key", "provider_message_id",
    ))
    with store.connection() as db:
        assert tuple(db.execute(
            "SELECT provider,account_id,mailbox_identity_key,provider_message_id "
            "FROM messages WHERE message_id='copy-00'"
        ).fetchone()) == new_binding
        assert removal.message_available(db, "copy-00")
    assert local_removal(
        store, "request_removal", "delete", parameters, request_id=old_key, now=NOW,
    ) == pending
    source_locks.qualify_protocol(store, fresh=False)
    old_result = local_removal(
        store, "request_removal", "delete", parameters, request_id=old_key, now=NOW,
    )
    assert old_result.requested == old_result.completed == 1
    assert old_result.pending == 0
    with store.connection() as db:
        assert [r[0] for r in db.execute("SELECT message_id FROM messages")] == ["copy-00"]
        assert tuple(db.execute(
            "SELECT provider,account_id,mailbox_identity_key,provider_message_id "
            "FROM messages WHERE message_id='copy-00'"
        ).fetchone()) == new_binding
        assert removal.message_available(db, "copy-00")
    new_key = removal.issue_key(store, "delete", parameters, retention_days=30, now=NOW)
    new_result = local_removal(
        store, "request_removal", "delete", parameters, request_id=new_key, now=NOW,
    )
    assert new_result.requested == new_result.completed == 1 and new_result.pending == 0
    assert new_result.request_id != old_result.request_id
    with store.connection() as db:
        assert not db.execute("SELECT 1 FROM messages").fetchone()
        assert [r[0] for r in db.execute(
            "SELECT t.logical_target_id FROM message_removal_targets t "
            "JOIN message_removal_intents i USING(intent_id) WHERE i.request_id=?",
            (new_key,),
        )] == ["copy-00"]
        assert tuple(db.execute(
            "SELECT t.provider,t.account_id,t.stored_identity_key,t.provider_message_id "
            "FROM message_removal_targets t JOIN message_removal_intents i USING(intent_id) "
            "WHERE i.request_id=?", (new_key,),
        ).fetchone()) == new_binding
        assert removal.outcome(db, old_key) == old_result


def test_alias_delete_missing_pending_and_retained_key_controls(tmp_path, monkeypatch):
    from test_removal_refusal import held_runtime, snapshot

    from eom_email_watcher import removal, source_locks

    store = held_runtime(tmp_path, monkeypatch)
    parameters = {"message_id": "copy-00"}
    key = removal.issue_key(store, "delete", parameters, retention_days=30, now=NOW)
    pending = local_removal(
        store, "request_removal", "delete", parameters, request_id=key, now=NOW,
    )
    before = snapshot(store)
    assert local_removal(
        store, "request_removal", "delete", parameters, request_id=key, now=NOW,
    ) == pending
    assert snapshot(store) == before
    for message_id in ("copy-00", "copy-31", "missing"):
        assert local_removal(store, "delete_message", message_id, now=NOW) is False
        assert snapshot(store) == before
    source_locks.qualify_protocol(store, fresh=False)
    settled = snapshot(store)
    assert local_removal(store, "delete_message", "copy-00", now=NOW) is False
    assert snapshot(store) == settled
    complete = local_removal(
        store, "request_removal", "delete", parameters, request_id=key, now=NOW,
    )
    assert complete.requested == complete.completed == 1 and complete.pending == 0
