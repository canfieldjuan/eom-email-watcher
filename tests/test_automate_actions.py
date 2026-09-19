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
    ActionContext,
    ActionDeliveryError,
    ActionRunner,
    AdapterNotConfigured,
    AdapterRegistry,
    AutomateHost,
    AutomateLicenseError,
    ConnectInvokeAdapter,
    LocalNotifyAdapter,
)
from connect_automate.automate.store import WorkflowStore

NOW = datetime(2026, 6, 1, tzinfo=UTC)
STAGES = frozenset({"captured", "reviewing"})


def _encoded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _keyring(key: Ed25519PrivateKey) -> bytes:
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


def _runner(tmp_path: Path, host: AutomateHost | None = None) -> tuple[ActionRunner, WorkflowStore]:
    store = WorkflowStore(tmp_path / "automate" / "workflow.db")
    store.initialize()
    registry = AdapterRegistry.with_defaults()
    runner = ActionRunner(store=store, registry=registry, host=host or _licensed_host(tmp_path))
    return runner, store


def _record(store: WorkflowStore) -> str:
    return store.create_record("lead-funnel", "captured", now=NOW, allowed_stages=STAGES).record_id


def test_registry_resolves_notify_local_by_default() -> None:
    registry = AdapterRegistry.with_defaults()
    assert isinstance(registry.resolve("notify.local"), LocalNotifyAdapter)


def test_registry_rejects_an_unknown_kind_on_register() -> None:
    registry = AdapterRegistry()
    with pytest.raises(ValueError):
        registry.register("mail.blast", LocalNotifyAdapter())


def test_registry_raises_for_an_unconfigured_kind() -> None:
    registry = AdapterRegistry.with_defaults()
    with pytest.raises(AdapterNotConfigured):
        registry.resolve("mail.send")


def test_notify_local_delivers_and_records_durably(tmp_path: Path) -> None:
    runner, store = _runner(tmp_path)
    record_id = _record(store)
    outcome = runner.run(
        record_id,
        kind="notify.local",
        dedupe_key="k1",
        request={"title": "New lead", "body": "Call back"},
        now=NOW,
    )
    assert outcome.delivered is True
    assert outcome.status == "settled"
    # The result is delivery metadata only; the title/body live on the durable request.
    assert outcome.result == {"channel": "local", "delivered": True}
    # The settled outbox row is the durable local notification: request carries the content.
    actions = store.list_actions(record_id)
    assert [action.kind for action in actions] == ["notify.local"]
    assert actions[0].status == "settled"
    assert actions[0].request == {"title": "New lead", "body": "Call back"}


def test_notify_local_settles_a_request_near_the_size_bound(tmp_path: Path) -> None:
    # A large-but-valid notification must settle: the result is fixed-size metadata, so it
    # never grows with the request and cannot push a just-admitted action over the bound.
    runner, store = _runner(tmp_path)
    record_id = _record(store)
    big_body = "x" * 16_000
    outcome = runner.run(
        record_id,
        kind="notify.local",
        dedupe_key="k1",
        request={"title": "New lead", "body": big_body},
        now=NOW,
    )
    assert outcome.status == "settled"
    assert store.list_actions(record_id)[0].request["body"] == big_body


def test_dispatch_uses_the_persisted_action_not_a_mutated_view(tmp_path: Path) -> None:
    received: list[Mapping[str, object]] = []

    class RecordingAdapter:
        def deliver(self, request: Mapping[str, object]) -> Mapping[str, object]:
            received.append(dict(request))
            return {"ok": True}

    runner, store = _runner(tmp_path)
    runner._registry.register("mail.send", RecordingAdapter())
    record_id = _record(store)
    admission = store.admit_action(
        record_id, kind="mail.send", dedupe_key="k1", request={"to": "a"}, now=NOW
    )
    view = admission.view
    view.request["to"] = "attacker"  # mutate the in-memory view after admission
    outcome = runner.dispatch(view, now=NOW)
    assert outcome.status == "settled"
    # dispatch reloaded the persisted row, so the adapter saw the committed request, not the
    # mutated view.
    assert received == [{"to": "a"}]


def test_adapter_result_with_a_non_json_value_marks_the_action_failed(tmp_path: Path) -> None:
    from datetime import datetime as _dt

    class DatetimeAdapter:
        def deliver(self, request: Mapping[str, object]) -> Mapping[str, object]:
            return {"sent_at": _dt(2026, 6, 1)}  # a TypeError for json.dumps, not a ValueError

    runner, store = _runner(tmp_path)
    runner._registry.register("mail.send", DatetimeAdapter())
    record_id = _record(store)
    with pytest.raises(ActionDeliveryError):
        runner.run(record_id, kind="mail.send", dedupe_key="k1", request={"to": "a"}, now=NOW)
    actions = store.list_actions(record_id)
    # Terminalized as failed, not left stuck pending (which would raise UnresolvedAction).
    assert len(actions) == 1
    assert actions[0].status == "failed"


def test_adapter_result_with_a_circular_reference_marks_the_action_failed(tmp_path: Path) -> None:
    class CyclicAdapter:
        def deliver(self, request: Mapping[str, object]) -> Mapping[str, object]:
            result: dict[str, object] = {"ok": True}
            result["self"] = result  # a circular reference json.dumps cannot persist
            return result

    runner, store = _runner(tmp_path)
    runner._registry.register("mail.send", CyclicAdapter())
    record_id = _record(store)
    with pytest.raises(ActionDeliveryError):
        runner.run(record_id, kind="mail.send", dedupe_key="k1", request={"to": "a"}, now=NOW)
    actions = store.list_actions(record_id)
    # Terminalized as failed, not left stuck pending (which would raise UnresolvedAction).
    assert len(actions) == 1
    assert actions[0].status == "failed"


def test_adapter_result_that_raises_while_traversed_marks_the_action_failed(
    tmp_path: Path,
) -> None:
    class ExplodingMapping(Mapping):
        # A mapping that raises while being iterated (e.g. one mutated by another thread mid
        # traversal). json.dumps and the key walk both iterate it, so canonicalization must
        # still fail closed rather than let the exception strand the action pending.
        def __getitem__(self, key: object) -> object:
            raise KeyError(key)

        def __iter__(self):
            raise RuntimeError("changed size during iteration")

        def __len__(self) -> int:
            return 1

    class ExplodingAdapter:
        def deliver(self, request: Mapping[str, object]) -> Mapping[str, object]:
            return ExplodingMapping()

    runner, store = _runner(tmp_path)
    runner._registry.register("mail.send", ExplodingAdapter())
    record_id = _record(store)
    with pytest.raises(ActionDeliveryError):
        runner.run(record_id, kind="mail.send", dedupe_key="k1", request={"to": "a"}, now=NOW)
    actions = store.list_actions(record_id)
    assert len(actions) == 1
    assert actions[0].status == "failed"


def test_adapter_receives_the_committed_snapshot_not_the_caller_object(tmp_path: Path) -> None:
    received: list[Mapping[str, object]] = []

    class RecordingAdapter:
        def deliver(self, request: Mapping[str, object]) -> Mapping[str, object]:
            received.append(request)
            return {"ok": True}

    runner, store = _runner(tmp_path)
    runner._registry.register("mail.send", RecordingAdapter())
    record_id = _record(store)
    # A tuple value is stored as a JSON array and read back as a list; the adapter must see
    # the committed snapshot (list), proving it was not handed the caller's original object.
    runner.run(record_id, kind="mail.send", dedupe_key="k1", request={"tags": ("a", "b")}, now=NOW)
    assert received == [{"tags": ["a", "b"]}]


def test_replaying_a_settled_action_does_not_redeliver(tmp_path: Path) -> None:
    deliveries: list[Mapping[str, object]] = []

    class CountingAdapter:
        def deliver(self, request: Mapping[str, object]) -> Mapping[str, object]:
            deliveries.append(request)
            return {"ok": True}

    runner, store = _runner(tmp_path)
    runner._registry.register("mail.send", CountingAdapter())
    record_id = _record(store)
    first = runner.run(record_id, kind="mail.send", dedupe_key="k1", request={"to": "a"}, now=NOW)
    replay = runner.run(record_id, kind="mail.send", dedupe_key="k1", request={"to": "a"}, now=NOW)
    assert first.delivered is True
    assert replay.delivered is False
    assert replay.status == "settled"
    assert replay.result == {"ok": True}
    assert len(deliveries) == 1  # the side effect ran exactly once


def test_unconfigured_kind_does_not_create_an_outbox_row(tmp_path: Path) -> None:
    runner, store = _runner(tmp_path)
    record_id = _record(store)
    with pytest.raises(AdapterNotConfigured):
        runner.run(record_id, kind="mail.send", dedupe_key="k1", request={"to": "a"}, now=NOW)
    assert store.list_actions(record_id) == []


def test_adapter_failure_marks_the_action_failed(tmp_path: Path) -> None:
    class BrokenAdapter:
        def deliver(self, request: Mapping[str, object]) -> Mapping[str, object]:
            raise RuntimeError("smtp down")

    runner, store = _runner(tmp_path)
    runner._registry.register("mail.send", BrokenAdapter())
    record_id = _record(store)
    with pytest.raises(ActionDeliveryError):
        runner.run(record_id, kind="mail.send", dedupe_key="k1", request={"to": "a"}, now=NOW)
    actions = store.list_actions(record_id)
    assert len(actions) == 1
    assert actions[0].status == "failed"
    assert actions[0].last_error is not None


def test_notify_local_requires_a_title(tmp_path: Path) -> None:
    runner, store = _runner(tmp_path)
    record_id = _record(store)
    with pytest.raises(ActionDeliveryError):
        runner.run(record_id, kind="notify.local", dedupe_key="k1", request={"body": "x"}, now=NOW)


def test_unknown_action_kind_is_rejected(tmp_path: Path) -> None:
    runner, store = _runner(tmp_path)
    record_id = _record(store)
    with pytest.raises(ValueError):
        runner.run(record_id, kind="mail.blast", dedupe_key="k1", request={}, now=NOW)


def test_unlicensed_host_refuses_to_run_an_action(tmp_path: Path) -> None:
    host = _host(tmp_path, [entitlement.CONNECT_FEATURE_ID])  # missing automations feature
    runner, store = _runner(tmp_path, host=host)
    record_id = _record(store)
    with pytest.raises(AutomateLicenseError):
        runner.run(
            record_id,
            kind="notify.local",
            dedupe_key="k1",
            request={"title": "t", "body": "b"},
            now=NOW,
        )
    assert store.list_actions(record_id) == []


def test_settled_action_replays_after_its_adapter_is_removed(tmp_path: Path) -> None:
    class OkAdapter:
        def deliver(self, request: Mapping[str, object]) -> Mapping[str, object]:
            return {"ok": True}

    host = _licensed_host(tmp_path)
    store = WorkflowStore(tmp_path / "automate" / "workflow.db")
    store.initialize()
    record_id = _record(store)

    with_adapter = AdapterRegistry.with_defaults()
    with_adapter.register("mail.send", OkAdapter())
    first = ActionRunner(store=store, registry=with_adapter, host=host).run(
        record_id, kind="mail.send", dedupe_key="k1", request={"to": "a"}, now=NOW
    )
    assert first.delivered is True

    # A later run whose registry no longer configures mail.send must still replay the
    # recorded outcome instead of raising AdapterNotConfigured.
    without_adapter = AdapterRegistry.with_defaults()
    replay = ActionRunner(store=store, registry=without_adapter, host=host).run(
        record_id, kind="mail.send", dedupe_key="k1", request={"to": "a"}, now=NOW
    )
    assert replay.delivered is False
    assert replay.status == "settled"
    assert replay.result == {"ok": True}


def test_unpersistable_result_marks_the_action_failed(tmp_path: Path) -> None:
    class NanAdapter:
        def deliver(self, request: Mapping[str, object]) -> Mapping[str, object]:
            return {"value": float("nan")}  # not canonical JSON

    runner, store = _runner(tmp_path)
    runner._registry.register("mail.send", NanAdapter())
    record_id = _record(store)
    with pytest.raises(ActionDeliveryError):
        runner.run(record_id, kind="mail.send", dedupe_key="k1", request={"to": "a"}, now=NOW)
    actions = store.list_actions(record_id)
    assert len(actions) == 1
    # Terminalized as failed, not left stuck pending.
    assert actions[0].status == "failed"


def test_unconfigured_kind_releases_and_leaves_the_key_reusable(tmp_path: Path) -> None:
    class OkAdapter:
        def deliver(self, request: Mapping[str, object]) -> Mapping[str, object]:
            return {"ok": True}

    runner, store = _runner(tmp_path)
    record_id = _record(store)
    with pytest.raises(AdapterNotConfigured):
        runner.run(record_id, kind="mail.send", dedupe_key="k1", request={"to": "a"}, now=NOW)
    assert store.list_actions(record_id) == []
    # The released dedupe key is reusable once the adapter is configured.
    runner._registry.register("mail.send", OkAdapter())
    outcome = runner.run(record_id, kind="mail.send", dedupe_key="k1", request={"to": "a"}, now=NOW)
    assert outcome.delivered is True


def test_recover_pending_dispatches_a_stranded_pending_action(tmp_path: Path) -> None:
    runner, store = _runner(tmp_path)
    record_id = _record(store)
    # A crash between admission and dispatch leaves a pending row with no dispatcher.
    store.admit_action(
        record_id,
        kind="notify.local",
        dedupe_key="k1",
        request={"title": "New lead", "body": "Call back"},
        now=NOW,
    )
    assert store.list_actions(record_id)[0].status == "pending"
    outcomes = runner.recover_pending(now=NOW)
    assert len(outcomes) == 1
    assert outcomes[0].delivered is True
    assert outcomes[0].status == "settled"
    assert store.list_actions(record_id)[0].status == "settled"


def test_recover_pending_sweeps_multiple_records_in_admission_order(tmp_path: Path) -> None:
    delivered: list[str] = []

    class RecordingAdapter:
        idempotent = True

        def deliver(self, request: Mapping[str, object]) -> Mapping[str, object]:
            delivered.append(str(request["tag"]))
            return {"ok": True}

    runner, store = _runner(tmp_path)
    runner._registry.register("mail.send", RecordingAdapter())
    r1 = _record(store)
    r2 = _record(store)
    store.admit_action(r1, kind="mail.send", dedupe_key="a", request={"tag": "first"}, now=NOW)
    store.admit_action(r2, kind="mail.send", dedupe_key="b", request={"tag": "second"}, now=NOW)
    store.admit_action(r1, kind="mail.send", dedupe_key="c", request={"tag": "third"}, now=NOW)
    outcomes = runner.recover_pending(now=NOW)
    assert [outcome.status for outcome in outcomes] == ["settled", "settled", "settled"]
    # rowid admission order across all records, not grouped by record.
    assert delivered == ["first", "second", "third"]


def test_recover_pending_leaves_an_unconfigured_action_pending(tmp_path: Path) -> None:
    runner, store = _runner(tmp_path)
    record_id = _record(store)
    # No adapter is registered for mail.send.
    store.admit_action(record_id, kind="mail.send", dedupe_key="k1", request={"to": "a"}, now=NOW)
    outcomes = runner.recover_pending(now=NOW)
    assert len(outcomes) == 1
    assert outcomes[0].delivered is False
    # Left pending (durable intent), resumable once the adapter is configured.
    assert store.list_actions(record_id)[0].status == "pending"


def test_recover_pending_does_not_abort_on_one_failing_action(tmp_path: Path) -> None:
    class FlakyAdapter:
        idempotent = True

        def deliver(self, request: Mapping[str, object]) -> Mapping[str, object]:
            if request.get("boom"):
                raise RuntimeError("provider down")
            return {"ok": True}

    runner, store = _runner(tmp_path)
    runner._registry.register("mail.send", FlakyAdapter())
    bad = _record(store)
    good = _record(store)
    store.admit_action(bad, kind="mail.send", dedupe_key="x", request={"boom": True}, now=NOW)
    store.admit_action(good, kind="mail.send", dedupe_key="y", request={"to": "a"}, now=NOW)
    outcomes = runner.recover_pending(now=NOW)
    assert len(outcomes) == 2
    # The failing row terminalizes as failed; the sweep still reaches and settles the good one.
    assert store.list_actions(bad)[0].status == "failed"
    assert store.list_actions(good)[0].status == "settled"


def test_recover_pending_is_safe_to_repeat_without_re_delivery(tmp_path: Path) -> None:
    delivered: list[str] = []

    class RecordingAdapter:
        idempotent = True

        def deliver(self, request: Mapping[str, object]) -> Mapping[str, object]:
            delivered.append(str(request["to"]))
            return {"ok": True}

    runner, store = _runner(tmp_path)
    runner._registry.register("mail.send", RecordingAdapter())
    record_id = _record(store)
    store.admit_action(record_id, kind="mail.send", dedupe_key="k1", request={"to": "a"}, now=NOW)
    runner.recover_pending(now=NOW)
    second = runner.recover_pending(now=NOW)
    # The settled row is no longer in the pending feed, so the second sweep does nothing and
    # the side effect never runs twice.
    assert second == []
    assert delivered == ["a"]


def test_recover_pending_requires_the_license(tmp_path: Path) -> None:
    runner, store = _runner(tmp_path, host=_host(tmp_path, [entitlement.CONNECT_FEATURE_ID]))
    record_id = _record(store)
    store.admit_action(
        record_id,
        kind="notify.local",
        dedupe_key="k1",
        request={"title": "a", "body": "b"},
        now=NOW,
    )
    with pytest.raises(AutomateLicenseError):
        runner.recover_pending(now=NOW)
    # Nothing was dispatched: the row is still pending.
    assert store.list_actions(record_id)[0].status == "pending"


def test_recover_pending_requires_the_license_even_with_an_empty_feed(tmp_path: Path) -> None:
    # No pending rows at all: recover_pending must still refuse on an unlicensed host rather
    # than short-circuit to [] before the license check.
    runner, _ = _runner(tmp_path, host=_host(tmp_path, [entitlement.CONNECT_FEATURE_ID]))
    with pytest.raises(AutomateLicenseError):
        runner.recover_pending(now=NOW)


def test_recover_pending_leaves_a_non_idempotent_action_pending(tmp_path: Path) -> None:
    delivered: list[Mapping[str, object]] = []

    class NonIdempotentAdapter:
        # Declares no idempotent marker: an external send that must not be auto-redelivered
        # after an ambiguous crash (delivered but not settled).
        def deliver(self, request: Mapping[str, object]) -> Mapping[str, object]:
            delivered.append(dict(request))
            return {"ok": True}

    runner, store = _runner(tmp_path)
    runner._registry.register("mail.send", NonIdempotentAdapter())
    record_id = _record(store)
    store.admit_action(record_id, kind="mail.send", dedupe_key="k1", request={"to": "a"}, now=NOW)
    outcomes = runner.recover_pending(now=NOW)
    assert len(outcomes) == 1
    assert outcomes[0].delivered is False
    # The adapter was never invoked and the row is left pending for the deferred
    # ambiguous-state reconciliation, not auto-redelivered.
    assert delivered == []
    assert store.list_actions(record_id)[0].status == "pending"


def test_dispatch_terminalizes_an_adapter_error_with_surrogate_text(tmp_path: Path) -> None:
    class SurrogateAdapter:
        def deliver(self, request: Mapping[str, object]) -> Mapping[str, object]:
            # An exception whose message carries a lone surrogate, as a real transport error
            # decoded with errors="surrogateescape" could.
            raise RuntimeError("provider said \udce9")

    runner, store = _runner(tmp_path)
    runner._registry.register("mail.send", SurrogateAdapter())
    record_id = _record(store)
    admission = store.admit_action(
        record_id, kind="mail.send", dedupe_key="k1", request={"to": "a"}, now=NOW
    )
    # dispatch calls fail_action with str(exc); the surrogate must not make fail_action raise
    # and leave the row pending.
    outcome = runner.dispatch(admission.view, now=NOW)
    assert outcome.delivered is False
    assert store.list_actions(record_id)[0].status == "failed"


class _RecordingInvoker:
    """A fake CapabilityInvoker: records (request, job_id) and returns a fixed result."""

    def __init__(self, *, result: object = None, error: Exception | None = None) -> None:
        self.calls: list[tuple[dict, str]] = []
        self._result = result if result is not None else {"job": "done"}
        self._error = error

    def invoke(self, request: Mapping[str, object], *, job_id: str) -> object:
        self.calls.append((dict(request), job_id))
        if self._error is not None:
            raise self._error
        return self._result


def test_connect_invoke_kind_is_registrable() -> None:
    registry = AdapterRegistry.with_defaults()
    registry.register("connect.invoke", ConnectInvokeAdapter(_RecordingInvoker()))
    assert isinstance(registry.resolve("connect.invoke"), ConnectInvokeAdapter)


def test_connect_invoke_adapter_declares_idempotent() -> None:
    assert ConnectInvokeAdapter(_RecordingInvoker()).idempotent is True


def test_connect_invoke_dispatch_uses_action_id_as_stable_job_id(tmp_path: Path) -> None:
    invoker = _RecordingInvoker(result={"receipt": "ok"})
    runner, store = _runner(tmp_path)
    runner._registry.register("connect.invoke", ConnectInvokeAdapter(invoker))
    record_id = _record(store)
    admission = store.admit_action(
        record_id,
        kind="connect.invoke",
        dedupe_key="k1",
        request={"capability_id": "onboarding.public-link.list"},
        now=NOW,
    )
    outcome = runner.dispatch(admission.view, now=NOW)
    assert outcome.status == "settled"
    assert outcome.result == {"receipt": "ok"}
    # Exactly one invocation, and the job id is the durable action id -- stable across a retry
    # or crash-recovery re-dispatch, so the provider replays the same job (ADR-0002).
    assert len(invoker.calls) == 1
    request, job_id = invoker.calls[0]
    assert job_id == admission.view.action_id
    assert request == {"capability_id": "onboarding.public-link.list"}


def test_connect_invoke_is_recovered_by_the_sweep(tmp_path: Path) -> None:
    invoker = _RecordingInvoker(result={"ok": True})
    runner, store = _runner(tmp_path)
    runner._registry.register("connect.invoke", ConnectInvokeAdapter(invoker))
    record_id = _record(store)
    store.admit_action(
        record_id, kind="connect.invoke", dedupe_key="k1", request={"capability_id": "x"}, now=NOW
    )
    # A stranded pending connect.invoke row is auto-recovered: its stable job id makes a
    # re-POST idempotent, so the sweep may re-drive it (unlike a raw external send).
    outcomes = runner.recover_pending(now=NOW)
    assert len(outcomes) == 1
    assert outcomes[0].status == "settled"
    assert store.list_actions(record_id)[0].status == "settled"
    assert invoker.calls[0][1] == store.list_actions(record_id)[0].action_id


def test_connect_invoke_failure_marks_the_action_failed(tmp_path: Path) -> None:
    invoker = _RecordingInvoker(error=RuntimeError("provider down"))
    runner, store = _runner(tmp_path)
    runner._registry.register("connect.invoke", ConnectInvokeAdapter(invoker))
    record_id = _record(store)
    admission = store.admit_action(
        record_id, kind="connect.invoke", dedupe_key="k1", request={"capability_id": "x"}, now=NOW
    )
    outcome = runner.dispatch(admission.view, now=NOW)
    assert outcome.delivered is False
    assert store.list_actions(record_id)[0].status == "failed"


def test_connect_invoke_non_mapping_result_marks_the_action_failed(tmp_path: Path) -> None:
    invoker = _RecordingInvoker(result="not-a-mapping")
    runner, store = _runner(tmp_path)
    runner._registry.register("connect.invoke", ConnectInvokeAdapter(invoker))
    record_id = _record(store)
    admission = store.admit_action(
        record_id, kind="connect.invoke", dedupe_key="k1", request={"capability_id": "x"}, now=NOW
    )
    outcome = runner.dispatch(admission.view, now=NOW)
    assert outcome.delivered is False
    assert store.list_actions(record_id)[0].status == "failed"


def test_context_aware_adapter_receives_the_durable_action_identity(tmp_path: Path) -> None:
    seen: list[ActionContext] = []

    class ContextAdapter:
        def deliver_with_context(
            self, request: Mapping[str, object], context: ActionContext
        ) -> Mapping[str, object]:
            seen.append(context)
            return {"ok": True}

    runner, store = _runner(tmp_path)
    runner._registry.register("mail.send", ContextAdapter())
    record_id = _record(store)
    admission = store.admit_action(
        record_id, kind="mail.send", dedupe_key="k1", request={"to": "a"}, now=NOW
    )
    runner.dispatch(admission.view, now=NOW)
    assert len(seen) == 1
    context = seen[0]
    assert context.action_id == admission.view.action_id
    assert context.dedupe_key == "k1"
    assert context.record_id == record_id
    assert context.kind == "mail.send"
