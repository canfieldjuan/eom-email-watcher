from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from connect_automate import entitlement
from connect_automate.automate import (
    AdapterRegistry,
    AutomateHost,
    AutomateLicenseError,
    PackError,
    PackOwnershipError,
    PackRuntime,
    PackVersionError,
    load_pack,
)
from connect_automate.automate.store import WorkflowStore

NOW = datetime(2026, 6, 1, tzinfo=UTC)
PACK_ID = "11111111-1111-4111-8111-111111111111"
SUBJECT = "pc-0001"
KEY_ID = "pack-key"

NOTIFY_ACTION = {"action": "notify.local", "request": {"title": "New lead", "body": "Call back"}}
MAIL_ACTION = {"action": "mail.send", "request": {"to": "ops", "subject": "lead"}}


def _encoded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


# --- entitlement / licensed host (mirrors the other automate suites) ----------------------


def _ent_keyring(key: Ed25519PrivateKey) -> bytes:
    return json.dumps(
        {
            "keys": [
                {
                    "key_id": "test-key",
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


def _host(tmp_path: Path, features: list[str]) -> AutomateHost:
    key = Ed25519PrivateKey.generate()
    directory = tmp_path / "local-connect"
    directory.mkdir(parents=True, mode=0o700)
    path = directory / entitlement.ENTITLEMENT_FILE_NAME
    path.write_bytes(_signed_license(key, features))
    path.chmod(0o600)
    gate = entitlement.EntitlementGate.for_test(
        path=path, keyring_document=_ent_keyring(key), now=NOW
    )
    return AutomateHost(entitlement=gate)


def _licensed_host(tmp_path: Path) -> AutomateHost:
    return _host(tmp_path, [entitlement.CONNECT_FEATURE_ID, entitlement.AUTOMATIONS_FEATURE_ID])


# --- pack / grant signing -----------------------------------------------------------------


def _pack_keys(key: Ed25519PrivateKey) -> dict[str, bytes]:
    return {KEY_ID: key.public_key().public_bytes_raw()}


def _envelope(key: Ed25519PrivateKey, payload: bytes) -> bytes:
    return json.dumps(
        {
            "format_version": 1,
            "key_id": KEY_ID,
            "payload_base64url": _encoded(payload),
            "signature_base64url": _encoded(key.sign(payload)),
        },
        separators=(",", ":"),
    ).encode()


def _workflow(actions: list[dict]) -> dict:
    return {
        "name": "lead-funnel",
        "stages": ["captured", "reviewing"],
        "initial_stage": "captured",
        "definitions": [
            {
                "name": "review",
                "trigger": {"source_kind": "operator.decision", "decision": "review"},
                "conditions": [{"field": "record.stage", "op": "equals", "value": "captured"}],
                "effects": [{"kind": "record.transition", "to_stage": "reviewing"}],
                "actions": actions,
            }
        ],
    }


def _pack_payload(workflow: dict, *, pack_version: int = 1) -> bytes:
    return json.dumps(
        {
            "format_version": 1,
            "pack_id": PACK_ID,
            "pack_version": pack_version,
            "workflow": workflow,
        },
        separators=(",", ":"),
    ).encode()


def _pack_bytes(key: Ed25519PrivateKey, workflow: dict, *, pack_version: int = 1) -> bytes:
    return _envelope(key, _pack_payload(workflow, pack_version=pack_version))


def _grant_bytes(key: Ed25519PrivateKey, *, subject: str = SUBJECT) -> bytes:
    payload = json.dumps(
        {
            "format_version": 1,
            "grant_id": "33333333-3333-4333-8333-333333333333",
            "pack_id": PACK_ID,
            "subject": subject,
            "issued_at": "2026-01-01T00:00:00Z",
            "not_before": "2026-01-01T00:00:00Z",
            "expires_at": "2027-01-01T00:00:00Z",
        },
        separators=(",", ":"),
    ).encode()
    return _envelope(key, payload)


def _store(tmp_path: Path) -> WorkflowStore:
    store = WorkflowStore(tmp_path / "automate" / "workflow.db")
    store.initialize()
    return store


def _runtime(
    tmp_path: Path,
    key: Ed25519PrivateKey,
    *,
    actions: list[dict],
    registry: AdapterRegistry,
    store: WorkflowStore,
    host: AutomateHost | None = None,
    grant: bytes | None = None,
) -> PackRuntime:
    return PackRuntime.load(
        _pack_bytes(key, _workflow(actions)),
        grant if grant is not None else _grant_bytes(key),
        store=store,
        host=host or _licensed_host(tmp_path),
        registry=registry,
        publisher_keys=_pack_keys(key),
        grant_keys=_pack_keys(key),
        subject=SUBJECT,
        now=NOW,
    )


# --- acceptance ---------------------------------------------------------------------------


def test_pack_runs_end_to_end_on_notify_local_alone(tmp_path: Path) -> None:
    # Zero external accounts: the default registry configures only the host-guaranteed
    # notify.local. A signed, granted pack drives a record from captured to reviewing and
    # settles a durable local notification.
    key = Ed25519PrivateKey.generate()
    store = _store(tmp_path)
    runtime = _runtime(
        tmp_path,
        key,
        actions=[NOTIFY_ACTION],
        registry=AdapterRegistry.with_defaults(),
        store=store,
    )
    record = runtime.create_record(now=NOW)
    assert record.stage == "captured"
    run = runtime.submit_decision(
        record.record_id,
        decision="review",
        operation_key="op-1",
        request={},
        expected_version=record.state_version,
        now=NOW,
    )
    assert run.outcome.matched is True
    assert run.outcome.applied is True
    assert run.outcome.record.stage == "reviewing"
    assert len(run.actions) == 1
    assert run.actions[0].status == "settled"
    assert run.actions[0].delivered is True
    feed = store.list_actions(record.record_id)
    assert [action.kind for action in feed] == ["notify.local"]
    assert feed[0].status == "settled"
    assert feed[0].request == {"title": "New lead", "body": "Call back"}


def test_pack_resolves_a_configured_mail_send_adapter(tmp_path: Path) -> None:
    # The pack names the abstract kind mail.send; the host resolves it to whatever adapter is
    # configured. No vendor is named in the pack or the workflow.
    delivered: list[Mapping[str, object]] = []

    class StubMail:
        def deliver(self, request: Mapping[str, object]) -> Mapping[str, object]:
            delivered.append(request)
            return {"transport": "stub", "message_id": "m1"}

    key = Ed25519PrivateKey.generate()
    store = _store(tmp_path)
    registry = AdapterRegistry.with_defaults()
    registry.register("mail.send", StubMail())
    runtime = _runtime(tmp_path, key, actions=[MAIL_ACTION], registry=registry, store=store)
    record = runtime.create_record(now=NOW)
    run = runtime.submit_decision(
        record.record_id,
        decision="review",
        operation_key="op-1",
        request={},
        expected_version=record.state_version,
        now=NOW,
    )
    assert len(run.actions) == 1
    assert run.actions[0].status == "settled"
    assert delivered == [{"to": "ops", "subject": "lead"}]  # abstract kind resolved to the stub


def test_replaying_a_decision_dispatches_the_action_once(tmp_path: Path) -> None:
    deliveries: list[Mapping[str, object]] = []

    class CountingMail:
        def deliver(self, request: Mapping[str, object]) -> Mapping[str, object]:
            deliveries.append(request)
            return {"ok": True}

    key = Ed25519PrivateKey.generate()
    store = _store(tmp_path)
    registry = AdapterRegistry.with_defaults()
    registry.register("mail.send", CountingMail())
    runtime = _runtime(tmp_path, key, actions=[MAIL_ACTION], registry=registry, store=store)
    record = runtime.create_record(now=NOW)
    first = runtime.submit_decision(
        record.record_id,
        decision="review",
        operation_key="op-1",
        request={},
        expected_version=record.state_version,
        now=NOW,
    )
    # Replay the same operation key: the engine short-circuits (applied=False) and re-admits
    # nothing, and the runtime re-dispatches the decision's action rows -- but the row is
    # already settled, so it is replayed without a new delivery.
    replay = runtime.submit_decision(
        record.record_id,
        decision="review",
        operation_key="op-1",
        request={},
        expected_version=record.state_version,  # stale, but replay precedes the version check
        now=NOW,
    )
    assert first.outcome.applied is True
    assert len(first.actions) == 1
    assert first.actions[0].delivered is True
    assert replay.outcome.applied is False
    # The replay reports the decision's action, replayed from its settled row (no re-delivery).
    assert len(replay.actions) == 1
    assert replay.actions[0].delivered is False
    assert replay.actions[0].status == "settled"
    assert len(deliveries) == 1  # the side effect ran exactly once
    assert len(store.list_actions(record.record_id)) == 1


def test_unconfigured_action_stays_pending_and_resumes_later(tmp_path: Path) -> None:
    # Durable admission: a decision's action intent is admitted in the ledger transaction, so
    # even with no adapter configured the record advances and the intent survives as a pending
    # row (not lost). A later dispatch, once the adapter is configured, resumes it.
    key = Ed25519PrivateKey.generate()
    store = _store(tmp_path)
    registry = AdapterRegistry.with_defaults()  # no mail.send adapter
    runtime = _runtime(tmp_path, key, actions=[MAIL_ACTION], registry=registry, store=store)
    record = runtime.create_record(now=NOW)
    run = runtime.submit_decision(
        record.record_id,
        decision="review",
        operation_key="op-1",
        request={},
        expected_version=record.state_version,
        now=NOW,
    )
    assert run.outcome.applied is True
    assert run.outcome.record.stage == "reviewing"
    assert [action.status for action in run.actions] == ["pending"]
    assert run.actions[0].delivered is False
    assert [action.status for action in store.list_actions(record.record_id)] == ["pending"]

    delivered: list[Mapping[str, object]] = []

    class StubMail:
        def deliver(self, request: Mapping[str, object]) -> Mapping[str, object]:
            delivered.append(request)
            return {"ok": True}

    registry.register("mail.send", StubMail())
    replay = runtime.submit_decision(
        record.record_id,
        decision="review",
        operation_key="op-1",
        request={},
        expected_version=record.state_version,
        now=NOW,
    )
    assert replay.outcome.applied is False  # the decision itself replays
    assert [action.status for action in replay.actions] == ["settled"]
    assert replay.actions[0].delivered is True  # the resumed action delivered now
    assert delivered == [{"to": "ops", "subject": "lead"}]
    assert [action.status for action in store.list_actions(record.record_id)] == ["settled"]


def test_partial_dispatch_leaves_only_the_unconfigured_action_pending(tmp_path: Path) -> None:
    # Mid-list failure: with one adapter configured and one not, the record advances, the
    # deliverable action settles, and the undeliverable one stays pending and resumable -- no
    # action is lost and the settled one is never re-delivered on resume.
    key = Ed25519PrivateKey.generate()
    store = _store(tmp_path)
    registry = AdapterRegistry.with_defaults()  # notify.local yes, mail.send no
    runtime = _runtime(
        tmp_path, key, actions=[NOTIFY_ACTION, MAIL_ACTION], registry=registry, store=store
    )
    record = runtime.create_record(now=NOW)
    run = runtime.submit_decision(
        record.record_id,
        decision="review",
        operation_key="op-1",
        request={},
        expected_version=record.state_version,
        now=NOW,
    )
    assert run.outcome.applied is True
    assert [action.status for action in run.actions] == ["settled", "pending"]
    feed = store.list_actions(record.record_id)
    assert [action.kind for action in feed] == ["notify.local", "mail.send"]
    assert [action.status for action in feed] == ["settled", "pending"]

    deliveries: list[Mapping[str, object]] = []

    class StubMail:
        def deliver(self, request: Mapping[str, object]) -> Mapping[str, object]:
            deliveries.append(request)
            return {"ok": True}

    registry.register("mail.send", StubMail())
    replay = runtime.submit_decision(
        record.record_id,
        decision="review",
        operation_key="op-1",
        request={},
        expected_version=record.state_version,
        now=NOW,
    )
    assert [action.status for action in replay.actions] == ["settled", "settled"]
    # notify.local replayed (no re-delivery); mail.send delivered now.
    assert [action.delivered for action in replay.actions] == [False, True]
    assert len(deliveries) == 1
    assert [action.status for action in store.list_actions(record.record_id)] == [
        "settled",
        "settled",
    ]


def test_a_failed_action_is_not_reported_as_delivered(tmp_path: Path) -> None:
    # delivered means this call performed the side effect. A failed adapter attempt must not
    # be reported delivered=True, or a consumer would treat an undelivered action as sent.
    class BrokenMail:
        def deliver(self, request: Mapping[str, object]) -> Mapping[str, object]:
            raise RuntimeError("smtp down")

    key = Ed25519PrivateKey.generate()
    store = _store(tmp_path)
    registry = AdapterRegistry.with_defaults()
    registry.register("mail.send", BrokenMail())
    runtime = _runtime(tmp_path, key, actions=[MAIL_ACTION], registry=registry, store=store)
    record = runtime.create_record(now=NOW)
    run = runtime.submit_decision(
        record.record_id,
        decision="review",
        operation_key="op-1",
        request={},
        expected_version=record.state_version,
        now=NOW,
    )
    assert run.actions[0].status == "failed"
    assert run.actions[0].delivered is False
    assert store.list_actions(record.record_id)[0].status == "failed"


def test_tampered_pack_is_rejected_before_any_record(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    store = _store(tmp_path)
    signed = key.sign(_pack_payload(_workflow([NOTIFY_ACTION]), pack_version=1))
    tampered = _pack_payload(_workflow([NOTIFY_ACTION]), pack_version=999)
    envelope = json.dumps(
        {
            "format_version": 1,
            "key_id": KEY_ID,
            "payload_base64url": _encoded(tampered),
            "signature_base64url": _encoded(signed),
        },
        separators=(",", ":"),
    ).encode()
    with pytest.raises(PackError):
        PackRuntime.load(
            envelope,
            _grant_bytes(key),
            store=store,
            host=_licensed_host(tmp_path),
            registry=AdapterRegistry.with_defaults(),
            publisher_keys=_pack_keys(key),
            grant_keys=_pack_keys(key),
            subject=SUBJECT,
            now=NOW,
        )


def test_unlicensed_host_refuses_to_load_a_pack(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    store = _store(tmp_path)
    host = _host(tmp_path, [entitlement.CONNECT_FEATURE_ID])  # missing automations feature
    with pytest.raises(AutomateLicenseError):
        PackRuntime.load(
            _pack_bytes(key, _workflow([NOTIFY_ACTION])),
            _grant_bytes(key),
            store=store,
            host=host,
            registry=AdapterRegistry.with_defaults(),
            publisher_keys=_pack_keys(key),
            grant_keys=_pack_keys(key),
            subject=SUBJECT,
            now=NOW,
        )


def test_publisher_cannot_self_sign_a_grant_with_separate_keyrings(tmp_path: Path) -> None:
    # When publishers and the grant issuer use distinct keys, a grant signed by the publisher
    # must not authorize a pack: the grant keyring does not trust the publisher key.
    publisher = Ed25519PrivateKey.generate()
    grant_authority = Ed25519PrivateKey.generate()
    with pytest.raises(PackError):
        PackRuntime.load(
            _pack_bytes(publisher, _workflow([NOTIFY_ACTION])),
            _grant_bytes(publisher),  # signed by the publisher, not the grant authority
            store=_store(tmp_path),
            host=_licensed_host(tmp_path),
            registry=AdapterRegistry.with_defaults(),
            publisher_keys=_pack_keys(publisher),
            grant_keys=_pack_keys(grant_authority),
            subject=SUBJECT,
            now=NOW,
        )


def test_separate_publisher_and_grant_keyrings_accept_correct_signatures(tmp_path: Path) -> None:
    # The pack signed by the publisher and the grant signed by the distinct grant authority
    # is the correctly split case, and it runs.
    publisher = Ed25519PrivateKey.generate()
    grant_authority = Ed25519PrivateKey.generate()
    store = _store(tmp_path)
    runtime = PackRuntime.load(
        _pack_bytes(publisher, _workflow([NOTIFY_ACTION])),
        _grant_bytes(grant_authority),
        store=store,
        host=_licensed_host(tmp_path),
        registry=AdapterRegistry.with_defaults(),
        publisher_keys=_pack_keys(publisher),
        grant_keys=_pack_keys(grant_authority),
        subject=SUBJECT,
        now=NOW,
    )
    record = runtime.create_record(now=NOW)
    run = runtime.submit_decision(
        record.record_id,
        decision="review",
        operation_key="op-1",
        request={},
        expected_version=record.state_version,
        now=NOW,
    )
    assert run.actions[0].status == "settled"


def test_action_dedupe_key_is_namespaced_by_committed_event(tmp_path: Path) -> None:
    # The runtime owns its outbox keys: a reserved namespace over the committed event id, so
    # they cannot collide with a caller-minted key such as "op-1:0".
    key = Ed25519PrivateKey.generate()
    store = _store(tmp_path)
    runtime = _runtime(
        tmp_path,
        key,
        actions=[NOTIFY_ACTION],
        registry=AdapterRegistry.with_defaults(),
        store=store,
    )
    record = runtime.create_record(now=NOW)
    run = runtime.submit_decision(
        record.record_id,
        decision="review",
        operation_key="op-1",
        request={},
        expected_version=record.state_version,
        now=NOW,
    )
    dedupe_key = store.list_actions(record.record_id)[0].dedupe_key
    assert dedupe_key == f"pack.action:{run.outcome.event_id}:0"
    assert not dedupe_key.startswith("op-1:")  # not the raw caller key


def test_exposed_workflow_is_a_defensive_copy(tmp_path: Path) -> None:
    # Mutating the exposed workflow graph must not change the verified semantics the engine
    # executes, or the publisher signature would be defeatable after load().
    key = Ed25519PrivateKey.generate()
    store = _store(tmp_path)
    runtime = _runtime(
        tmp_path,
        key,
        actions=[NOTIFY_ACTION],
        registry=AdapterRegistry.with_defaults(),
        store=store,
    )
    exposed = runtime.workflow
    assert exposed is not runtime.workflow  # a fresh copy each access
    assert runtime.pack.workflow is not runtime.workflow
    # Neuter the transition on the exposed copy; the executed semantics must be unaffected.
    exposed.definitions[0].effects[0].to_stage = "captured"
    record = runtime.create_record(now=NOW)
    run = runtime.submit_decision(
        record.record_id,
        decision="review",
        operation_key="op-1",
        request={},
        expected_version=record.state_version,
        now=NOW,
    )
    assert run.outcome.record.stage == "reviewing"  # the original, signed transition ran


def test_direct_construction_is_rejected(tmp_path: Path) -> None:
    # A caller cannot build a runtime around an unverified pack: construction must go through
    # load(), which performs signature and grant verification.
    key = Ed25519PrivateKey.generate()
    loaded = load_pack(_pack_bytes(key, _workflow([NOTIFY_ACTION])), keys=_pack_keys(key))
    with pytest.raises(TypeError):
        PackRuntime(
            pack=loaded,
            store=_store(tmp_path),
            host=_licensed_host(tmp_path),
            registry=AdapterRegistry.with_defaults(),
        )


def test_grant_for_a_different_subject_is_rejected(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    store = _store(tmp_path)
    with pytest.raises(PackError):
        PackRuntime.load(
            _pack_bytes(key, _workflow([NOTIFY_ACTION])),
            _grant_bytes(key, subject="pc-9999"),
            store=store,
            host=_licensed_host(tmp_path),
            registry=AdapterRegistry.with_defaults(),
            publisher_keys=_pack_keys(key),
            grant_keys=_pack_keys(key),
            subject=SUBJECT,
            now=NOW,
        )


def test_a_pack_cannot_drive_another_packs_record_with_the_same_workflow_name(
    tmp_path: Path,
) -> None:
    # Two independently signed packs whose workflows share the name "lead-funnel" but carry
    # different pack ids, over one shared store. Pack ownership, not the workflow name, is the
    # boundary: pack B must not drive a record pack A created.
    key = Ed25519PrivateKey.generate()
    store = _store(tmp_path)
    host = _licensed_host(tmp_path)
    registry = AdapterRegistry.with_defaults()
    pack_b_id = "22222222-2222-4222-8222-222222222222"

    def pack_bytes(pack_id: str) -> bytes:
        payload = json.dumps(
            {
                "format_version": 1,
                "pack_id": pack_id,
                "pack_version": 1,
                "workflow": _workflow([NOTIFY_ACTION]),
            },
            separators=(",", ":"),
        ).encode()
        return _envelope(key, payload)

    def grant_bytes(pack_id: str) -> bytes:
        payload = json.dumps(
            {
                "format_version": 1,
                "grant_id": "33333333-3333-4333-8333-333333333333",
                "pack_id": pack_id,
                "subject": SUBJECT,
                "issued_at": "2026-01-01T00:00:00Z",
                "not_before": "2026-01-01T00:00:00Z",
                "expires_at": "2027-01-01T00:00:00Z",
            },
            separators=(",", ":"),
        ).encode()
        return _envelope(key, payload)

    def make_runtime(pack_id: str) -> PackRuntime:
        return PackRuntime.load(
            pack_bytes(pack_id),
            grant_bytes(pack_id),
            store=store,
            host=host,
            registry=registry,
            publisher_keys=_pack_keys(key),
            grant_keys=_pack_keys(key),
            subject=SUBJECT,
            now=NOW,
        )

    runtime_a = make_runtime(PACK_ID)
    runtime_b = make_runtime(pack_b_id)

    record = runtime_a.create_record(now=NOW)
    assert record.pack_id == PACK_ID

    with pytest.raises(PackOwnershipError):
        runtime_b.submit_decision(
            record.record_id,
            decision="review",
            operation_key="op-1",
            request={},
            expected_version=record.state_version,
            now=NOW,
        )
    # The record is untouched: pack B was refused before any mutation.
    assert store.get_record(record.record_id).stage == "captured"

    # Pack A drives its own record normally.
    run = runtime_a.submit_decision(
        record.record_id,
        decision="review",
        operation_key="op-1",
        request={},
        expected_version=record.state_version,
        now=NOW,
    )
    assert run.outcome.applied is True
    assert run.outcome.record.stage == "reviewing"


def test_an_upgraded_pack_cannot_drive_a_record_started_under_an_earlier_version(
    tmp_path: Path,
) -> None:
    # Same signed pack id, two versions (an upgrade), over one shared store. A record is frozen
    # to the version it started under: the upgraded runtime must not finish in-flight work.
    key = Ed25519PrivateKey.generate()
    store = _store(tmp_path)
    host = _licensed_host(tmp_path)
    registry = AdapterRegistry.with_defaults()

    def pack_bytes(pack_version: int) -> bytes:
        payload = json.dumps(
            {
                "format_version": 1,
                "pack_id": PACK_ID,
                "pack_version": pack_version,
                "workflow": _workflow([NOTIFY_ACTION]),
            },
            separators=(",", ":"),
        ).encode()
        return _envelope(key, payload)

    def make_runtime(pack_version: int) -> PackRuntime:
        return PackRuntime.load(
            pack_bytes(pack_version),
            _grant_bytes(key),  # the grant is bound to pack_id, not version, so it serves both
            store=store,
            host=host,
            registry=registry,
            publisher_keys=_pack_keys(key),
            grant_keys=_pack_keys(key),
            subject=SUBJECT,
            now=NOW,
        )

    runtime_v1 = make_runtime(1)
    runtime_v2 = make_runtime(2)

    record = runtime_v1.create_record(now=NOW)
    assert record.pack_version == 1

    with pytest.raises(PackVersionError):
        runtime_v2.submit_decision(
            record.record_id,
            decision="review",
            operation_key="op-1",
            request={},
            expected_version=record.state_version,
            now=NOW,
        )
    assert store.get_record(record.record_id).stage == "captured"

    # The originating version drives its own record normally.
    run = runtime_v1.submit_decision(
        record.record_id,
        decision="review",
        operation_key="op-1",
        request={},
        expected_version=record.state_version,
        now=NOW,
    )
    assert run.outcome.applied is True
    assert run.outcome.record.stage == "reviewing"
