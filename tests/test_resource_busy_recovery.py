"""Retry authority, confirmation and the bounded generic recovery transaction."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from connect_automate import connect
from test_connect_v2_engine_api import (
    active_connect_entitlement as active_connect_entitlement,
)
from test_connect_v2_engine_api import (
    api_request,
    install_automation_dispatch_fakes,
    seed_contract_fire,
    seeded_runtime,
)

from eom_email_watcher import engine_api
from eom_email_watcher.db import Store


def prepare(tmp_path, monkeypatch, *, confirm_each=False):
    config, runtime = seeded_runtime(tmp_path)
    selected, fire, attempt = seed_contract_fire(runtime, confirm_each=confirm_each)
    install_automation_dispatch_fakes(
        monkeypatch, runtime, lambda **kwargs: connect.CapabilityCatalog((selected,))
    )
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    engine_api._dispatch_automation_fire(runtime, fire.fire_id)
    return config, runtime, selected, fire, attempt


def fail(runtime, selected, job_id, *, code="PROVIDER_RESOURCE_BUSY", retryable=True):
    runtime.store.transition_connect_job(
        job_id=job_id,
        expected_state="requested",
        next_state="failed",
        provider_app_id=selected.app_id,
        provider_instance_id=selected.instance_id,
        error={
            "code": code,
            "message": "GPU_BUSY is text, never authority",
            "retryable": retryable,
        },
    )


def confirm(config, runtime, fire_id):
    awaiting = runtime.store.automation_fire(fire_id)
    assert awaiting.state == "awaiting_confirmation"
    response = engine_api._response(
        api_request(
            config,
            "automation.fire.decide",
            {
                "fire_id": fire_id,
                "expected_version": awaiting.state_version,
                "prepared_identity_sha256": awaiting.prepared_identity_sha256,
                "decision": "confirmed",
            },
        )
    )
    assert response["ok"]
    engine_api._dispatch_automation_fire(runtime, fire_id)


@pytest.mark.parametrize(
    "direction", ["generic_fault", "nonretryable", "unknown", "external", "active"]
)
def test_only_authoritative_resource_refusal_can_replace_work(tmp_path, monkeypatch, direction):
    _, runtime, selected, fire, attempt = prepare(tmp_path, monkeypatch)
    if direction != "active":
        fail(
            runtime,
            selected,
            attempt.dispatch_request_id,
            code="PROVIDER_FAULT" if direction == "generic_fault" else "PROVIDER_RESOURCE_BUSY",
            retryable=direction != "nonretryable",
        )
    if direction in {"unknown", "external"}:
        with runtime.store.connection() as db:
            db.execute(
                """UPDATE connect_job_dispatch SET capability_authority_known = ?,
                    capability_external_effects = ? WHERE job_id = ?""",
                (
                    int(direction != "unknown"),
                    int(direction == "external"),
                    attempt.dispatch_request_id,
                ),
            )
    current = runtime.store.automation_fire(fire.fire_id)
    assert runtime.store.automation_fire_retry_kind(fire.fire_id) is None
    with pytest.raises(RuntimeError, match="recovery proof"):
        runtime.store.retry_automation_fire_after_failure(
            fire_id=fire.fire_id,
            expected_version=current.state_version,
        )
    engine_api._settle_submitted_automation_fires(runtime, limit=25)
    assert len(runtime.store.automation_fire_attempts(fire.fire_id)) == 1


def test_retry_requires_fresh_confirmation_and_stops_after_second_busy(tmp_path, monkeypatch):
    config, runtime, selected, fire, first = prepare(tmp_path, monkeypatch, confirm_each=True)
    confirm(config, runtime, fire.fire_id)
    fail(runtime, selected, first.dispatch_request_id)
    engine_api._settle_submitted_automation_fires(runtime, limit=25)
    retry = runtime.store.automation_fire(fire.fire_id)
    assert retry.current_attempt_no == 2
    assert not retry.confirmed
    assert retry.prepared_identity_sha256 is None
    assert retry.prepared_identity_json is None
    with runtime.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM automation_fire_confirmations").fetchone()[0] == 0
    engine_api._dispatch_automation_fire(runtime, fire.fire_id)
    second = runtime.store.automation_fire_attempts(fire.fire_id)[1]
    assert runtime.store.connect_job(second.dispatch_request_id) is None
    confirm(config, runtime, fire.fire_id)
    fail(runtime, selected, second.dispatch_request_id)
    engine_api._settle_submitted_automation_fires(runtime, limit=25)
    stopped = runtime.store.automation_fire(fire.fire_id)
    assert stopped.state == "manual_review"
    assert stopped.reason == "second_resource_busy"
    engine_api._settle_submitted_automation_fires(runtime, limit=25)
    assert len(runtime.store.automation_fire_attempts(fire.fire_id)) == 2


def test_retry_revalidates_authority_under_transaction_and_survives_restart(tmp_path, monkeypatch):
    _, runtime, selected, fire, first = prepare(tmp_path, monkeypatch)
    fail(runtime, selected, first.dispatch_request_id)
    current = runtime.store.automation_fire(fire.fire_id)
    assert runtime.store.automation_fire_retry_kind(fire.fire_id) == "resource_busy"
    with runtime.store.connection() as db:
        db.execute(
            "UPDATE connect_job_dispatch SET capability_external_effects=1 WHERE job_id=?",
            (first.dispatch_request_id,),
        )
    with pytest.raises(RuntimeError, match="recovery proof"):
        runtime.store.retry_automation_fire_after_failure(
            fire_id=fire.fire_id,
            expected_version=current.state_version,
        )
    with runtime.store.connection() as db:
        db.execute(
            "UPDATE connect_job_dispatch SET capability_external_effects=0 WHERE job_id=?",
            (first.dispatch_request_id,),
        )
    reopened = Store(runtime.store.path)
    reopened.retry_automation_fire_after_failure(
        fire_id=fire.fire_id,
        expected_version=current.state_version,
    )
    with pytest.raises((RuntimeError, KeyError)):
        runtime.store.retry_automation_fire_after_failure(
            fire_id=fire.fire_id,
            expected_version=current.state_version,
        )
    assert len(reopened.automation_fire_attempts(fire.fire_id)) == 2


def test_busy_recovery_pauses_without_spinning_then_reuses_attempt(tmp_path, monkeypatch):
    _, runtime, selected, fire, first = prepare(tmp_path, monkeypatch)
    fail(runtime, selected, first.dispatch_request_id)
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: False)
    engine_api._settle_submitted_automation_fires(runtime, limit=25)
    assert runtime.store.automation_fire(fire.fire_id).state == "entitlement_paused"
    assert runtime.store.automation_fire_settlement_due() is False
    assert len(runtime.store.automation_fire_attempts(fire.fire_id)) == 1
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    engine_api._settle_submitted_automation_fires(runtime, limit=25)
    # Generic entitlement resume must precede the recovery transition.
    paused = runtime.store.automation_fire(fire.fire_id)
    assert paused.state == "entitlement_paused"
    runtime.store.resume_automation_fire_job(
        fire_id=fire.fire_id, expected_version=paused.state_version
    )
    engine_api._settle_submitted_automation_fires(runtime, limit=25)
    assert runtime.store.automation_fire(fire.fire_id).current_attempt_no == 2


def test_two_retry_transactions_create_only_one_replacement(tmp_path, monkeypatch):
    _, runtime, selected, fire, first = prepare(tmp_path, monkeypatch)
    fail(runtime, selected, first.dispatch_request_id)
    current = runtime.store.automation_fire(fire.fire_id)
    stores = [Store(runtime.store.path), Store(runtime.store.path)]
    barrier = Barrier(2)

    def retry(store):
        barrier.wait(timeout=5)
        try:
            store.retry_automation_fire_after_failure(
                fire_id=fire.fire_id, expected_version=current.state_version,
            )
            return "created"
        except (RuntimeError, KeyError):
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(retry, stores))
    assert sorted(results) == ["created", "rejected"]
    assert len(runtime.store.automation_fire_attempts(fire.fire_id)) == 2
