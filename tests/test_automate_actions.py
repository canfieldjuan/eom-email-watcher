from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from eom_email_watcher import entitlement
from eom_email_watcher.automate import (
    ActionDeliveryError,
    ActionRunner,
    AdapterNotConfigured,
    AdapterRegistry,
    AutomateHost,
    AutomateLicenseError,
    LocalNotifyAdapter,
)
from eom_email_watcher.automate.store import WorkflowStore

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
    assert outcome.result == {
        "channel": "local",
        "title": "New lead",
        "body": "Call back",
        "delivered": True,
    }
    # The settled outbox row is the durable local notification.
    actions = store.list_actions(record_id)
    assert [action.kind for action in actions] == ["notify.local"]
    assert actions[0].status == "settled"


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
