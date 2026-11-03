"""EW278 backend proof: synthetic stores and loopback HTTP; no real mailbox or model."""

import base64
import hashlib
import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from threading import Event

import pytest
from connect_automate import connect
from connect_reference_provider import ReferenceProvider, _Handler, _now
from test_connect_v2_engine_api import (
    INSTANCE_B,
    OUTPUT_ID,
    PDF,
    REQUEST_ID,
    TEST_MAILBOX_IDENTITY_KEY,
    SeededMailboxGateway,
    api_request,
    capability,
    contract_rule_definition,
    invocation_payload,
    patch_runtime,
    seed_second_attachment,
    seeded_runtime,
)

from eom_email_watcher import engine_api
from eom_email_watcher.connect_selection import decode_selection, encode_selection
from eom_email_watcher.db import Store

INVOICE_MEDIA_TYPE = "application/vnd.local-connect.invoice+json"
REOPEN_SELECTION = """
import json
import sys
from pathlib import Path
from eom_email_watcher.connect_selection import encode_selection
from eom_email_watcher.db import Store
store = Store(Path(sys.argv[1]))
selection = store.automation_rule_selection(sys.argv[2], int(sys.argv[3]))
print(encode_selection(selection))
"""


def invoice_capability(version="1.0"):
    return replace(
        capability(
            app_id="invoice-processor",
            capability_id="invoice.extract",
            produces=(INVOICE_MEDIA_TYPE,),
        ),
        capability_version=version,
    )


def invoice_rule(selected):
    definition = contract_rule_definition(selected, name="Synthetic invoice rule")
    definition["action"]["parameters"] = {}
    return definition


@pytest.fixture
def invoice_runtime(tmp_path, monkeypatch):
    config_path, runtime = seeded_runtime(tmp_path)
    patch_runtime(monkeypatch, runtime)
    monkeypatch.setattr(engine_api, "_require_automation_entitlement", lambda: None)
    monkeypatch.setattr(
        connect.entitlement,
        "connect_entitlement_decision",
        lambda: connect.entitlement.EntitlementDecision.ACTIVE,
    )
    return config_path, runtime


@pytest.mark.parametrize("version", ["1.0", "2.0"])
def test_invoice_selection_roundtrip_preserves_full_snapshot(version):
    selected = invoice_capability(version)
    encoded = encode_selection(selected)
    decoded = decode_selection(encoded)
    assert decoded == replace(selected, base_url="", token="")
    assert encode_selection(decoded) == encoded
    assert json.loads(encoded)["capability_version"] == version
    assert "base_url" not in json.loads(encoded)
    assert "token" not in json.loads(encoded)


@pytest.mark.parametrize(
    ("change", "expected_code"),
    [
        ({}, "capability_unavailable"),
        (
            {"capability_id": "document.translate", "capability_version": "1.0"},
            "capability_unavailable",
        ),
        ({"app_version": "2.0.0"}, "provider_unavailable"),
        ({"instance_id": INSTANCE_B}, "provider_unavailable"),
    ],
)
def test_saved_legacy_invoice_selection_refuses_before_handoff(
    invoice_runtime,
    monkeypatch,
    change,
    expected_code,
):
    config_path, runtime = invoice_runtime
    saved = invoice_capability()
    current = replace(invoice_capability("2.0"), **change)
    monkeypatch.setattr(
        connect, "discover_capabilities", lambda **kwargs: connect.CapabilityCatalog((current,))
    )

    def forbidden(*args, **kwargs):
        pytest.fail("stale invoice selection reached mailbox or POST")

    monkeypatch.setattr(engine_api.GmailGateway, "from_token", forbidden)
    monkeypatch.setattr(connect, "ConnectV2Client", forbidden)
    frozen = encode_selection(saved)
    for _ in range(2):
        response = engine_api._response(
            api_request(
                config_path,
                "connect.attachment.invoke",
                invocation_payload(saved),
            )
        )
        assert response["ok"] is False
        assert response["error"]["code"] == expected_code
        assert runtime.store.connect_job(REQUEST_ID) is None
        assert encode_selection(saved) == frozen


@pytest.mark.parametrize(
    ("change", "diagnostic", "retryable"),
    [
        (None, None, True),
        (None, "provider_unavailable", True),
        ({"app_id": "other-app", "instance_id": INSTANCE_B}, None, True),
        ({"app_version": "2.0.0"}, None, False),
        ({"instance_id": INSTANCE_B}, None, True),
        ({"instance_id": INSTANCE_B, "app_version": "2.0.0"}, None, True),
        ({"app_id": "other-app"}, None, False),
        ({"app_version": "2.0.0"}, "provider_unavailable", True),
    ],
)
def test_invoice_provider_unavailability_preserves_structured_retryability(
    invoice_runtime, monkeypatch, change, diagnostic, retryable,
):
    config_path, runtime = invoice_runtime
    saved = invoice_capability()
    offered = () if change is None else (replace(saved, **change),)

    def discover(**kwargs):
        # Match SDK filtering: an old instance filter cannot see its replacement.
        instance_id = kwargs.get("provider_instance_id")
        items = tuple(item for item in offered if instance_id in (None, item.instance_id))
        return connect.CapabilityCatalog(items, diagnostic)

    monkeypatch.setattr(connect, "discover_capabilities", discover)
    monkeypatch.setattr(
        engine_api.GmailGateway, "from_token",
        lambda *args: pytest.fail("unavailable selection reached mailbox handoff"),
    )
    response = engine_api._response(
        api_request(config_path, "connect.attachment.invoke", invocation_payload(saved))
    )
    assert response["error"]["code"] == "provider_unavailable"
    assert response["error"].get("retryable") is retryable
    assert runtime.store.connect_job(REQUEST_ID) is None


@pytest.mark.parametrize("diagnostic", [None, "provider_unavailable"])
def test_invoice_exact_identity_still_matches_in_a_mixed_catalog(monkeypatch, diagnostic):
    saved = invoice_capability()
    def deadline():
        return 7.0
    calls = []

    def discover(**kwargs):
        calls.append(kwargs)
        return connect.CapabilityCatalog(
            (replace(saved, instance_id=INSTANCE_B), saved, invoice_capability("2.0")),
            diagnostic,
        )

    monkeypatch.setattr(connect, "discover_capabilities", discover)
    payload = invocation_payload(saved)
    selected, parameters = engine_api._discover_selected_generic_capability(
        payload["provider"], payload["capability"], {}, remaining_timeout=deadline,
    )
    assert selected == saved
    assert parameters == {}
    assert len(calls) == 1
    assert calls[0]["remaining_timeout"] is deadline
    assert calls[0]["required_manifest_features"] == ("fixed-parameters-v1",)


def test_invoice_real_catalog_distinguishes_changed_identity_from_failed_discovery(
    invoice_runtime, invoice_http_provider, monkeypatch,
):
    provider, _state = invoice_http_provider
    selected = connect.discover_capabilities().items[0]
    payload = invocation_payload(selected)
    original_manifest = provider.server.manifest

    def upgraded_manifest():
        value = original_manifest()
        value["app"]["version"] = "9.0.0"
        return value

    monkeypatch.setattr(provider.server, "manifest", upgraded_manifest)
    with pytest.raises(engine_api.ApiError) as mismatch:
        engine_api._discover_selected_generic_capability(
            payload["provider"], payload["capability"], {},
        )
    assert mismatch.value.code == "provider_unavailable"
    assert mismatch.value.retryable is False

    monkeypatch.setattr(provider.server, "manifest", lambda: {})
    with pytest.raises(engine_api.ApiError) as unavailable:
        engine_api._discover_selected_generic_capability(
            payload["provider"], payload["capability"], {},
        )
    assert unavailable.value.code == "provider_unavailable"
    assert unavailable.value.retryable is True
    assert provider.post_attempt_count() == 0


def test_invoice_selected_instance_down_with_same_app_peer_stays_retryable(
    invoice_runtime, invoice_http_provider, tmp_path, monkeypatch,
):
    config_path, runtime = invoice_runtime
    selected_provider, _state = invoice_http_provider
    selected = connect.discover_capabilities().items[0]
    selected_manifest = selected_provider.server.manifest
    peer = ReferenceProvider.start(tmp_path / "provider-runtime")
    peer_manifest = peer.server.manifest

    def peer_invoice_manifest():
        value = peer_manifest()
        value["capabilities"] = selected_manifest()["capabilities"]
        return value

    monkeypatch.setattr(peer.server, "manifest", peer_invoice_manifest)
    try:
        # Keep the selected registration but stop only this disposable listener.
        # SDK discovery must skip it while the same-app peer remains reachable.
        selected_provider.server.shutdown()
        selected_provider.server.server_close()
        selected_provider.thread.join(timeout=5)
        catalog = connect.discover_capabilities()
        assert catalog.diagnostic_code is None
        assert catalog.items
        assert all(item.app_id == selected.app_id for item in catalog.items)
        assert all(item.instance_id == peer.instance_id for item in catalog.items)
        request = api_request(
            config_path, "connect.attachment.invoke", invocation_payload(selected),
        )
        responses = [engine_api._response(request) for _ in range(2)]
        for response in responses:
            assert response["error"]["code"] == "provider_unavailable"
            assert response["error"]["retryable"] is True
        assert runtime.store.connect_job(REQUEST_ID) is None
        assert selected_provider.post_attempt_count() == 0
        assert peer.post_attempt_count() == 0
        print("INVOICE_RESTART_ERROR=" + json.dumps(responses[0]["error"]))
    finally:
        peer.stop()


def test_mixed_catalog_matches_exact_saved_invoice_version(monkeypatch):
    legacy, current = invoice_capability(), invoice_capability("2.0")
    monkeypatch.setattr(
        connect,
        "discover_capabilities",
        lambda **kwargs: connect.CapabilityCatalog((current, legacy)),
    )
    for chosen in (legacy, current):
        payload = invocation_payload(chosen)
        selected, parameters = engine_api._discover_selected_generic_capability(
            payload["provider"],
            payload["capability"],
            {},
        )
        assert selected == chosen
        assert parameters == {}


@pytest.mark.parametrize("version", ["1.0", "2.0"])
def test_duplicate_invoice_identity_refuses_before_handoff(
    invoice_runtime,
    monkeypatch,
    version,
):
    config_path, runtime = invoice_runtime
    selected = invoice_capability(version)
    duplicate = replace(selected, action_label="Another label for the same identity")
    monkeypatch.setattr(
        connect,
        "discover_capabilities",
        lambda **kwargs: connect.CapabilityCatalog((selected, duplicate)),
    )

    def forbidden(*args, **kwargs):
        pytest.fail("ambiguous invoice selection reached mailbox or POST")

    monkeypatch.setattr(engine_api.GmailGateway, "from_token", forbidden)
    monkeypatch.setattr(connect, "ConnectV2Client", forbidden)
    response = engine_api._response(
        api_request(
            config_path,
            "connect.attachment.invoke",
            invocation_payload(selected),
        )
    )
    assert response["ok"] is False
    assert response["error"]["code"] == "capability_unavailable"
    assert runtime.store.connect_job(REQUEST_ID) is None


def test_api_invoice_reselection_preserves_old_version_after_process_reopen(
    invoice_runtime,
    monkeypatch,
):
    config_path, runtime = invoice_runtime
    legacy, current = invoice_capability(), invoice_capability("2.0")
    offered = [legacy]
    monkeypatch.setattr(
        connect, "discover_capabilities", lambda **kwargs: connect.CapabilityCatalog(tuple(offered))
    )
    created = engine_api._automation_rules_put(
        api_request(
            config_path,
            "automation.rules.put",
            {"definition": invoice_rule(legacy)},
        )
    )["rule"]
    rule_id = created["summary"]["rule_id"]
    frozen = encode_selection(runtime.store.automation_rule_selection(rule_id, 1))

    # Fresh interpreter: no in-process Store or decoder cache can supply the snapshot.
    reopened = subprocess.run(
        [sys.executable, "-c", REOPEN_SELECTION, str(runtime.store.path), rule_id, "1"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert reopened.stdout.strip() == frozen
    assert decode_selection(reopened.stdout).capability_version == "1.0"

    offered[:] = [current]
    stale = engine_api._response(
        api_request(
            config_path,
            "automation.rules.put",
            {
                "rule_id": rule_id,
                "expected_version": 1,
                "definition": invoice_rule(legacy),
            },
        )
    )
    assert stale["error"]["code"] == "capability_unavailable"
    assert runtime.store.automation_rule(rule_id).summary.version == 1

    # Explicit API reselection creates a new rule version, not a legacy identity rewrite.
    replacement = engine_api._automation_rules_put(
        api_request(
            config_path,
            "automation.rules.put",
            {
                "rule_id": rule_id,
                "expected_version": 1,
                "definition": invoice_rule(current),
            },
        )
    )["rule"]
    assert replacement["summary"]["version"] == 2
    assert replacement["definition"]["action"]["capability"] == {
        "id": "invoice.extract",
        "version": "2.0",
    }
    assert encode_selection(runtime.store.automation_rule_selection(rule_id, 1)) == frozen
    assert runtime.store.automation_rule_selection(rule_id, 2).capability_version == "2.0"
    reopened_again = subprocess.run(
        [sys.executable, "-c", REOPEN_SELECTION, str(runtime.store.path), rule_id, "1"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert reopened_again.stdout.strip() == frozen

    conflict = engine_api._response(
        api_request(
            config_path,
            "automation.rules.put",
            {
                "rule_id": rule_id,
                "expected_version": 1,
                "definition": invoice_rule(current),
            },
        )
    )
    assert conflict["error"]["code"] == "stale_rule"
    assert runtime.store.automation_rule(rule_id).summary.version == 2
    assert runtime.store.connect_job(REQUEST_ID) is None


@pytest.fixture
def invoice_http_provider(tmp_path, monkeypatch):
    """Reuse the real reference listener, auth, multipart parser and GET framing."""
    runtime_dir = tmp_path / "provider-runtime"
    runtime_dir.mkdir(mode=0o700)
    provider = ReferenceProvider.start(runtime_dir)
    state = {
        "version": "1.0",
        "outcome": "completed",
        "requests": [],
        "confirmation_required": False,
    }
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    original_manifest = provider.server.manifest

    def manifest():
        value = original_manifest()
        value["capabilities"] = [
            {
                "id": "invoice.extract",
                "version": state["version"],
                "action": {"label": "Extract invoice", "description": "Synthetic invoice"},
                "accepts": [{"media_type": "application/pdf", "max_bytes": 1024}],
                "produces": [INVOICE_MEDIA_TYPE],
                "parameters": [],
                "effects": {
                    "external": False,
                    "confirmation_required": state["confirmation_required"],
                },
            }
        ]
        return value

    class Handler(_Handler):
        def do_POST(self):
            if not self._authorized():
                self._error(401, "UNAUTHORIZED", "Synthetic authorization refused")
                return
            request, artifact = self._request_parts()
            with self.server.lock:
                self.server.post_attempts += 1
                state["requests"].append(request)
            if state["outcome"] == "refused":
                self._error(400, "UNSUPPORTED_CAPABILITY", "Selected version is unsupported")
                return
            assert artifact == PDF
            source = request["inputs"][0]
            payload = legacy_invoice_payload(source)
            status = {
                "protocol_version": 2,
                "job_id": request["job_id"],
                "capability": request["capability"],
                "provider": {
                    "app_id": manifest()["app"]["id"],
                    "instance_id": self.server.instance_id,
                },
                "status": state["outcome"],
                "created_at": _now(),
                "updated_at": _now(),
                "input_artifacts": [
                    {
                        key: source[key]
                        for key in ("artifact_id", "media_type", "byte_size", "sha256")
                    }
                ],
            }
            if state["outcome"] == "completed":
                status["result"] = {
                    "outputs": [
                        {
                            "artifact_id": OUTPUT_ID,
                            "media_type": INVOICE_MEDIA_TYPE,
                            "display_name": "synthetic-invoice.json",
                            "byte_size": len(payload),
                            "sha256": hashlib.sha256(payload).hexdigest(),
                            "payload_base64": base64.b64encode(payload).decode(),
                        }
                    ]
                }
            with self.server.lock:
                self.server.jobs[request["job_id"]] = ("fixture", status, request)
            self._json(200, status)

    monkeypatch.setattr(provider.server, "manifest", manifest)
    monkeypatch.setattr(provider.server, "RequestHandlerClass", Handler)
    try:
        yield provider, state
    finally:
        provider.stop()


def legacy_invoice_payload(source):
    # Synthetic null-field record in InvoiceRecord 1.1 shape, from the provider's
    # src/invoice_processor/schema.py at 8e953b2. Consumer treats it as opaque bytes.
    return (
        json.dumps(
            {
                "record_version": "1.1",
                "source": {
                    "kind": "native_text",
                    "media_type": "application/pdf",
                    "artifact_id": source["artifact_id"],
                    "sha256": source["sha256"],
                    "byte_size": source["byte_size"],
                    "page_count": 1,
                    "display_name": "synthetic.pdf",
                    "parser": {"id": "fixture", "version": "1", "settings": "synthetic"},
                },
                "batches": 1,
                "coverage": {
                    "state": "no_known_gaps",
                    "capped_batches": [],
                    "unreferenced_amounts": 0,
                    "line_items_residual_minor": None,
                },
                "vendor": None,
                "invoice_number": None,
                "issue_date": None,
                "due_date": None,
                "currency": None,
                "number_format": None,
                "components": dict.fromkeys(
                    (
                        "subtotal",
                        "discount",
                        "shipping",
                        "fees",
                        "credits",
                        "deposit",
                        "tax",
                        "rounding",
                        "total",
                    )
                ),
                "line_items": [],
                "arithmetic": {
                    "checked": False,
                    "unchecked_reason": "missing_total",
                    "expected_total_minor": None,
                    "total_residual_minor": None,
                    "line_items_sum_minor": None,
                    "line_items_residual_minor": None,
                },
                "withheld": [],
                "warnings": [],
                "duplicate_keys": {"exact": source["sha256"], "candidate": None},
                "extracted_at": "2026-10-01T00:00:00Z",
            },
            indent=2,
        ).encode()
        + b"\n"
    )


def install_invoice_mailbox(monkeypatch):
    class Mailbox(SeededMailboxGateway):
        def set_operation_timeout(self, timeout):
            pass

        def attachment_bytes(self, *args):
            return PDF

    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: Mailbox())


def prepare_http_job(invoice_runtime, monkeypatch):
    _, runtime = invoice_runtime
    install_invoice_mailbox(monkeypatch)
    catalog = connect.discover_capabilities()
    assert len(catalog.items) == 1
    selected = catalog.items[0]
    prepared, stored, _, content = engine_api._prepare_or_create_generic_connect_job(
        runtime,
        request_id=REQUEST_ID,
        message_id="message-1",
        part_id="2",
        capability=selected,
        parameters={},
        confirmed=False,
        interactive_authorized=True,
    )
    assert stored is not None
    return selected, prepared, content()


def analyze_invoice_attachment(store, message_id):
    store.mark_analyzed(
        message_id,
        {
            "category": "informational",
            "priority": "normal",
            "summary": "A synthetic invoice arrived.",
            "action_required": True,
            "suggested_action": "Review the invoice.",
            "deadline_text": None,
            "deadline_iso": None,
            "confidence": 0.9,
        },
        mailbox_identity_key=TEST_MAILBOX_IDENTITY_KEY,
        body_chars=None,
        body_source_chars=None,
    )


def invoice_confirmation_decision(fire):
    return {
        "fire_id": fire.fire_id,
        "expected_version": fire.state_version,
        "prepared_identity_sha256": fire.prepared_identity_sha256,
        "decision": "confirmed",
    }


def test_saved_invoice_confirmation_cannot_authorize_replacement(
    invoice_runtime, invoice_http_provider, monkeypatch
):
    config_path, runtime = invoice_runtime
    store = runtime.store
    provider, state = invoice_http_provider
    install_invoice_mailbox(monkeypatch)
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    state.update(confirmation_required=True, outcome="accepted")

    def call(operation, payload=None):
        return engine_api._response(api_request(config_path, operation, payload))

    def prepare_fire(message_id, rule_version, capability_version):
        analyze_invoice_attachment(store, message_id)
        assert call("connect.queue.pump")["ok"] is True
        (fire,) = store.automation_fires_for_message(message_id)
        assert fire.rule_id == rule_id
        assert fire.rule_version == rule_version
        assert fire.state == "awaiting_confirmation"
        assert fire.job_id is None
        assert fire.confirmed is False
        assert fire.prepared_identity_sha256 is not None
        assert json.loads(fire.prepared_identity_json)["capability"] == {
            "id": "invoice.extract",
            "version": capability_version,
        }
        (attempt,) = store.automation_fire_attempts(fire.fire_id)
        assert store.connect_job(attempt.dispatch_request_id) is None
        assert provider.post_attempt_count() == 0
        return fire, attempt.dispatch_request_id

    (legacy,) = connect.discover_capabilities().items
    created = call("automation.rules.put", {"definition": invoice_rule(legacy)})
    assert created["ok"] is True
    rule_id = created["data"]["rule"]["summary"]["rule_id"]
    old_fire, old_job_id = prepare_fire("message-1", 1, "1.0")
    old_decision = invoice_confirmation_decision(old_fire)
    assert call("automation.fire.decide", old_decision)["data"]["state"] == "pending_dispatch"
    assert store.automation_confirmation_matches(store.automation_fire(old_fire.fire_id))
    assert provider.post_attempt_count() == 0

    state["version"] = "2.0"
    (current,) = connect.discover_capabilities().items
    replaced = call(
        "automation.rules.put",
        {"rule_id": rule_id, "expected_version": 1, "definition": invoice_rule(current)},
    )
    assert replaced["ok"] is True
    assert replaced["data"]["rule"]["summary"]["version"] == 2
    seed_second_attachment(runtime)
    new_fire, new_job_id = prepare_fire("message-2", 2, "2.0")
    assert new_job_id != old_job_id
    assert new_fire.prepared_identity_sha256 != old_fire.prepared_identity_sha256

    refused = store.automation_fire(old_fire.fire_id)
    assert refused.state == "manual_review"
    assert refused.reason == "capability_unavailable"
    assert refused.rule_version == old_fire.rule_version
    assert refused.prepared_identity_sha256 == old_fire.prepared_identity_sha256
    assert store.automation_confirmation_matches(refused)
    assert store.connect_job(old_job_id) is None

    replayed = call("automation.fire.decide", old_decision)
    assert replayed["ok"] is False
    assert replayed["error"]["code"] == "stale_automation_fire"
    new_decision = invoice_confirmation_decision(new_fire)
    substituted = call(
        "automation.fire.decide",
        {**new_decision, "prepared_identity_sha256": old_fire.prepared_identity_sha256},
    )
    assert substituted["ok"] is False
    assert substituted["error"]["code"] == "stale_automation_fire"
    assert call("connect.queue.pump")["ok"] is True
    assert store.automation_fire(new_fire.fire_id) == new_fire
    assert store.connect_job(new_job_id) is None
    assert provider.post_attempt_count() == 0

    approved = call("automation.fire.decide", new_decision)
    assert approved["data"]["state"] == "pending_dispatch"
    assert call("connect.queue.pump")["ok"] is True
    assert call("connect.queue.pump")["ok"] is True
    submitted = store.automation_fire(new_fire.fire_id)
    assert submitted.state == "submitted"
    assert submitted.job_id == new_job_id
    assert store.automation_confirmation_matches(submitted)
    job = store.connect_job(new_job_id)
    assert job.status == "accepted"
    assert job.capability_version == "2.0"
    assert provider.post_attempt_count() == 1
    (request,) = state["requests"]
    assert request["job_id"] == new_job_id
    assert request["capability"] == {"id": "invoice.extract", "version": "2.0"}
    assert store.automation_fire(old_fire.fire_id) == refused
    assert store.connect_job(old_job_id) is None


def test_concurrent_invoice_rule_update_rejects_stale_api_write(
    invoice_runtime, invoice_http_provider, monkeypatch
):
    config_path, runtime = invoice_runtime
    store = runtime.store
    provider, state = invoice_http_provider
    install_invoice_mailbox(monkeypatch)
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    state["confirmation_required"] = True

    def call(operation, payload=None):
        return engine_api._response(api_request(config_path, operation, payload))

    (legacy,) = connect.discover_capabilities().items
    created = call("automation.rules.put", {"definition": invoice_rule(legacy)})
    assert created["ok"] is True
    summary = created["data"]["rule"]["summary"]
    rule_id = summary["rule_id"]
    assert summary["version"] == 1
    analyze_invoice_attachment(store, "message-1")
    assert call("connect.queue.pump")["ok"] is True
    (awaiting,) = store.automation_fires_for_message("message-1")
    decision = call("automation.fire.decide", invoice_confirmation_decision(awaiting))
    assert decision["ok"] is True
    old_fire = store.automation_fire(awaiting.fire_id)
    assert old_fire is not None and old_fire.rule_version == 1
    assert store.automation_confirmation_matches(old_fire)
    old_definition = store.automation_fire_definition(old_fire)
    old_selection = store.automation_rule_selection(rule_id, 1)
    assert old_selection is not None and old_selection.capability_version == "1.0"
    revision, _ = store.automation_rules_snapshot()

    state["version"] = "2.0"
    (current,) = connect.discover_capabilities().items
    winning_definition = {**invoice_rule(current), "name": "Winning invoice update"}
    stale_definition = {**invoice_rule(current), "name": "Stale invoice update"}
    stale_request = {"rule_id": rule_id, "expected_version": 1, "definition": stale_definition}
    at_store_write = Event()
    release_store_write = Event()
    put = store.put_automation_rule

    def pause_before_write(definition, **kwargs):
        assert definition == stale_definition
        assert kwargs["rule_id"] == rule_id and kwargs["expected_version"] == 1
        at_store_write.set()
        assert release_store_write.wait(timeout=15), "timed out waiting for competing commit"
        return put(definition, **kwargs)

    monkeypatch.setattr(store, "put_automation_rule", pause_before_write)
    competing_store = Store(store.path)
    with ThreadPoolExecutor(max_workers=1) as executor:
        stale_write = executor.submit(call, "automation.rules.put", stale_request)
        try:
            assert at_store_write.wait(timeout=15), "API did not reach the store write"
            assert competing_store.automation_rule(rule_id).summary.version == 1

            # The normal API lock is held. Use an independent store connection
            # to commit the competing write without disabling that lock.
            busy = call("automation.rules.put", stale_request)
            assert busy["error"]["code"] == "mailbox_busy"
            winner = competing_store.put_automation_rule(
                winning_definition,
                rule_id=rule_id,
                expected_version=1,
                selection=current,
            )
            assert winner.summary.version == 2
            assert winner.summary.name == winning_definition["name"]
            assert not stale_write.done()
        finally:
            release_store_write.set()
        rejected = stale_write.result(timeout=15)

    assert rejected["ok"] is False, rejected
    assert rejected["error"]["code"] == "stale_rule"
    assert store.automation_rule(rule_id) == winner
    assert store.automation_rules_snapshot()[0] == revision + 1
    assert store.automation_rule_selection(rule_id, 2) == replace(current, base_url="", token="")
    assert store.automation_rule_selection(rule_id, 3) is None
    assert store.automation_rule_selection(rule_id, 1) == old_selection
    assert store.automation_fire(old_fire.fire_id) == old_fire
    assert store.automation_fire_definition(old_fire) == old_definition
    assert store.automation_confirmation_matches(old_fire)
    assert provider.post_attempt_count() == 0


@pytest.mark.parametrize("outcome", ["refused", "interrupted"])
def test_legacy_invoice_http_failures_are_terminal_without_resubmission(
    invoice_runtime,
    invoice_http_provider,
    monkeypatch,
    outcome,
):
    config_path, runtime = invoice_runtime
    provider, state = invoice_http_provider
    selected, prepared, content = prepare_http_job(invoice_runtime, monkeypatch)
    if outcome == "interrupted":
        state["outcome"] = "accepted"
        initial = connect.ConnectV2Client(selected).submit(prepared, content)
        engine_api._apply_connect_update(runtime.store, initial)
        with provider.server.lock:
            signature, status, request = provider.server.jobs[REQUEST_ID]
            status = dict(
                status,
                status="failed",
                error={
                    "code": "INTERRUPTED",
                    "message": "Legacy work was interrupted",
                    "retryable": False,
                },
            )
            provider.server.jobs[REQUEST_ID] = (signature, status, request)
        state["version"] = "2.0"
        with pytest.raises(engine_api.ApiError) as failure:
            engine_api._run_generic_connect_job(runtime, selected, prepared, None)
        assert failure.value.code == "interrupted"
    else:
        # Provider cutover between discovery and POST: a real nonretryable HTTP envelope.
        state.update(version="2.0", outcome="refused")
        with pytest.raises(connect.ConnectError) as failure:
            engine_api._run_generic_connect_job(runtime, selected, prepared, content)
        assert failure.value.code == "UNSUPPORTED_CAPABILITY"
        assert failure.value.retryable is False
    stored = runtime.store.connect_job(REQUEST_ID)
    assert stored.status == "failed"
    assert stored.capability_version == "1.0"
    assert stored.request_json == prepared.request_json
    assert stored.error_code == (
        "INTERRUPTED" if outcome == "interrupted" else "UNSUPPORTED_CAPABILITY"
    )
    assert stored.error_message == (
        "Legacy work was interrupted"
        if outcome == "interrupted"
        else "Selected version is unsupported"
    )
    for _ in range(2):
        engine_api._pump_generic_connect_lane(runtime, runtime.store.connect_job(REQUEST_ID))
    assert provider.post_attempt_count() == 1
    assert len(state["requests"]) == 1
    assert stored.error_retryable == 0
    dispatch_before = runtime.store.connect_dispatch(REQUEST_ID)
    original_request = api_request(
        config_path, "connect.attachment.invoke", invocation_payload(selected)
    )
    # Real interpreter restart: no parent Runtime, mocks or decoded objects are inherited.
    script = """
import json, sys
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch
from connect_automate import connect
from eom_email_watcher import engine_api
from eom_email_watcher.runtime import load_runtime
request = json.loads(sys.stdin.read())
runtime = load_runtime(Path(request["config_path"]))
job_id = request["payload"]["request_id"]
access_attempts = []
def forbidden(name):
    def refuse(*args, **kwargs):
        access_attempts.append(name)
        raise AssertionError("terminal work attempted " + name)
    return refuse
with (
    patch.object(engine_api, "_automation_entitlement_active", return_value=True),
    patch.object(connect, "require_connect_entitlement", return_value=None),
    patch.object(connect, "discover_capabilities", side_effect=forbidden("catalog discovery")),
    patch.object(connect, "registered_capability_for_reconciliation",
                 side_effect=forbidden("registration discovery")),
    patch.object(connect, "ConnectV2Client", side_effect=forbidden("provider client")),
    patch.object(engine_api.GmailGateway, "from_token", side_effect=forbidden("mailbox")),
):
    for _ in range(2):
        engine_api._pump_generic_connect_lane(runtime, runtime.store.connect_job(job_id))
    responses = [engine_api._response(request) for _ in range(2)]
print(json.dumps({"job": asdict(runtime.store.connect_job(job_id)),
                  "dispatch": asdict(runtime.store.connect_dispatch(job_id)),
                  "responses": responses, "access_attempts": access_attempts},
                 default=lambda value: value.hex()))
"""
    reopened = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps(original_request),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    observation = json.loads(reopened.stdout)
    assert observation["access_attempts"] == []
    assert observation["job"] == json.loads(json.dumps(asdict(stored), default=lambda v: v.hex()))
    assert observation["dispatch"] == json.loads(
        json.dumps(asdict(dispatch_before), default=lambda v: v.hex())
    )
    for response in observation["responses"]:
        assert response["ok"] is False
        assert response["error"]["code"] == stored.error_code.casefold()
        assert response["error"]["message"] == stored.error_message
    assert provider.post_attempt_count() == 1
    assert len(state["requests"]) == 1

    # Deliberate current selection is a separate admission, never a relabelled old request.
    state.update(version="2.0", outcome="completed", confirmation_required=True)
    current = connect.discover_capabilities().items[0]
    assert current.capability_version == "2.0"
    assert current.confirmation_required is True
    replacement_id = "99999999-9999-4999-8999-999999999999"

    def forbidden(*args, **kwargs):
        pytest.fail("unconfirmed invoice replacement reached mailbox")

    with monkeypatch.context() as guard:
        guard.setattr(engine_api.GmailGateway, "from_token", forbidden)
        reused = engine_api._response(
            api_request(
                config_path,
                "connect.attachment.invoke",
                invocation_payload(current, confirmed=True),
            )
        )
        assert reused["ok"] is False
        assert reused["error"]["code"] == "request_id_conflict"
        unconfirmed = engine_api._response(
            api_request(
                config_path,
                "connect.attachment.invoke",
                invocation_payload(current, request_id=replacement_id),
            )
        )
        assert unconfirmed["ok"] is False
        assert unconfirmed["error"]["code"] == "confirmation_required"
    assert runtime.store.connect_job(replacement_id) is None
    assert runtime.store.connect_job(REQUEST_ID) == stored
    assert provider.post_attempt_count() == 1

    replacement = engine_api._response(
        api_request(
            config_path,
            "connect.attachment.invoke",
            invocation_payload(current, request_id=replacement_id, confirmed=True),
        )
    )
    assert replacement["ok"] is True
    assert replacement["data"]["status"] == "completed"
    assert replacement["data"]["job_id"] == replacement_id
    assert replacement["data"]["capability"] == {"id": "invoice.extract", "version": "2.0"}
    new_job = runtime.store.connect_job(replacement_id)
    assert new_job.request_json != stored.request_json
    assert new_job.capability_version == "2.0"
    assert [request["job_id"] for request in state["requests"]] == [REQUEST_ID, replacement_id]
    replay = engine_api._response(original_request)
    assert replay["ok"] is False
    assert replay["error"]["code"] == stored.error_code.casefold()
    assert replay["error"]["message"] == stored.error_message
    assert runtime.store.connect_job(REQUEST_ID) == stored
    assert runtime.store.connect_dispatch(REQUEST_ID) == dispatch_before
    assert provider.post_attempt_count() == 2


def test_completed_legacy_invoice_replays_unchanged_after_rediscovery(
    invoice_runtime,
    invoice_http_provider,
    monkeypatch,
    tmp_path,
):
    config_path, runtime = invoice_runtime
    provider, state = invoice_http_provider
    selected, prepared, content = prepare_http_job(invoice_runtime, monkeypatch)
    result = engine_api._run_generic_connect_job(runtime, selected, prepared, content)
    before = runtime.store.connect_job(REQUEST_ID)
    output = runtime.store.completed_connect_outputs(before)[0]
    assert output.payload == legacy_invoice_payload(json.loads(prepared.request_json)["inputs"][0])
    assert result["capability"] == {"id": "invoice.extract", "version": "1.0"}
    state["version"] = "2.0"
    current = connect.discover_capabilities().items[0]
    assert current.capability_version == "2.0"
    with pytest.raises(RuntimeError, match="durable provider request"):
        engine_api._tracked_generic_job(before, current)
    # Reopening in a fresh process verifies retained full result/request, not a Python object cache.
    script = """
import json, sys
from pathlib import Path
from eom_email_watcher.db import Store
store = Store(Path(sys.argv[1]))
job = store.connect_job(sys.argv[2])
print(json.dumps({"request": job.request_json.decode(), "result": job.result_json.decode(),
                  "capability_version": job.capability_version}))
"""
    reopened = subprocess.run(
        [sys.executable, "-c", script, str(runtime.store.path), REQUEST_ID],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert json.loads(reopened.stdout) == {
        "request": before.request_json.decode(),
        "result": before.result_json.decode(),
        "capability_version": "1.0",
    }

    def no_discovery(*args, **kwargs):
        pytest.fail("retained output presentation/export attempted live rediscovery")

    monkeypatch.setattr(connect, "discover_capabilities", no_discovery)
    payload = {
        "message_id": "message-1",
        "part_id": "2",
        "job_id": REQUEST_ID,
        "artifact_id": OUTPUT_ID,
    }
    presentation = engine_api._connect_output_present(
        api_request(
            config_path,
            "connect.output.present",
            payload,
        )
    )
    assert presentation["presentation"] == {"kind": "opaque"}
    destination = tmp_path / "export"
    destination.mkdir()
    exported = engine_api._connect_output_export(
        api_request(
            config_path,
            "connect.output.export",
            dict(payload, destination_dir=str(destination)),
        )
    )
    assert Path(exported["path"]).read_bytes() == output.payload
    assert runtime.store.connect_job(REQUEST_ID) == before
    assert provider.post_attempt_count() == 1


@pytest.fixture
def completed_invoice(invoice_runtime, invoice_http_provider, monkeypatch):
    config_path, runtime = invoice_runtime
    provider, _ = invoice_http_provider
    payload_factory = legacy_invoice_payload

    def padded_payload(source):
        payload = payload_factory(source)
        # JSON trailing whitespace preserves the record while guaranteeing base64 padding.
        return payload + b" " * ((1 - len(payload)) % 3)

    monkeypatch.setattr(sys.modules[__name__], "legacy_invoice_payload", padded_payload)
    selected, prepared, content = prepare_http_job(invoice_runtime, monkeypatch)
    engine_api._run_generic_connect_job(runtime, selected, prepared, content)
    job = runtime.store.connect_job(REQUEST_ID)
    assert job.status == "completed"
    assert provider.post_attempt_count() == 1

    def forbidden(*args, **kwargs):
        pytest.fail("retained invoice read attempted discovery, mailbox or provider access")

    monkeypatch.setattr(connect, "discover_capabilities", forbidden)
    monkeypatch.setattr(connect, "ConnectV2Client", forbidden)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", forbidden)
    yield config_path, runtime, job
    assert provider.post_attempt_count() == 1


def reopened_invoice_outputs(store_path):
    script = """
import base64, json, sys
from pathlib import Path
from eom_email_watcher.db import Store
store = Store(Path(sys.argv[1]))
job = store.connect_job(sys.argv[2])
try:
    outputs = store.completed_connect_outputs(job)
except RuntimeError:
    result = {"accepted": False}
else:
    result = {"accepted": True, "outputs": [
        {**item.metadata(), "payload_base64": base64.b64encode(item.payload).decode()}
        for item in outputs]}
print(json.dumps(result))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(store_path), REQUEST_ID],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    ("corruption", "api_code"),
    [
        ("payload", "output_invalid"),
        ("hash", "output_invalid"),
        ("size", "output_invalid"),
        ("encoding", "output_invalid"),
        ("nonalphabet_encoding", "output_invalid"),
        ("padding_bits", "output_invalid"),
    ],
)
def test_retained_invoice_integrity_refuses_after_reopen(
    completed_invoice, tmp_path, corruption, api_code
):
    config_path, runtime, original = completed_invoice
    # Permissive-side control uses the same actual stored record in a fresh interpreter.
    assert reopened_invoice_outputs(runtime.store.path) == {
        "accepted": True,
        "outputs": json.loads(original.result_json)["outputs"],
    }
    result = json.loads(original.result_json)
    output = result["outputs"][0]
    if corruption == "payload":
        payload = base64.b64decode(output["payload_base64"])
        altered = bytes([payload[0] ^ 1]) + payload[1:]
        output["payload_base64"] = base64.b64encode(altered).decode()
    elif corruption == "hash":
        output["sha256"] = "0" * 64
    elif corruption == "size":
        output["byte_size"] += 1
    elif corruption == "encoding":
        output["payload_base64"] = "!invalid-base64!"
    elif corruption in ("nonalphabet_encoding", "padding_bits"):
        encoded = output["payload_base64"]
        original_payload = base64.b64decode(encoded, validate=True)
        if corruption == "nonalphabet_encoding":
            output["payload_base64"] = encoded[:4] + "!" + encoded[4:]
        else:
            assert encoded.endswith("==")
            alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
            noncanonical = alphabet[alphabet.index(encoded[-3]) + 1]
            output["payload_base64"] = encoded[:-3] + noncanonical + "=="
        # Both corruptions retain exact bytes; padding-bit corruption also uses a valid alphabet.
        assert (
            base64.b64decode(output["payload_base64"], validate=corruption == "padding_bits")
            == original_payload
        )
        assert len(original_payload) == output["byte_size"]
        assert hashlib.sha256(original_payload).hexdigest() == output["sha256"]
    value = json.dumps(result).encode()
    # Corrupt only the disposable persisted record; application writes cannot create this state.
    with runtime.store.connection() as db:
        db.execute(
            "UPDATE connect_attachment_jobs SET result_json = ? WHERE job_id = ?",
            (value, REQUEST_ID),
        )
    corrupted = runtime.store.connect_job(REQUEST_ID)
    assert corrupted.request_json == original.request_json
    with pytest.raises(RuntimeError, match="Completed Connect v2"):
        runtime.store.completed_connect_outputs(corrupted)
    assert reopened_invoice_outputs(runtime.store.path) == {"accepted": False}
    destination = tmp_path / "refused-export"
    destination.mkdir()
    payload = {
        "message_id": "message-1",
        "part_id": "2",
        "job_id": REQUEST_ID,
        "artifact_id": OUTPUT_ID,
    }
    for operation in ("connect.output.present", "connect.output.export"):
        request_payload = dict(payload)
        if operation.endswith("export"):
            request_payload["destination_dir"] = str(destination)
        response = engine_api._response(api_request(config_path, operation, request_payload))
        assert response["ok"] is False
        assert response["error"]["code"] == api_code
        assert list(destination.iterdir()) == []
        assert runtime.store.connect_job(REQUEST_ID) == corrupted


@pytest.mark.parametrize("field", ["message_id", "part_id", "job_id", "artifact_id"])
def test_retained_invoice_output_selection_refuses_mismatched_identity(
    completed_invoice, tmp_path, field
):
    config_path, runtime, original = completed_invoice
    payload = {
        "message_id": "message-1",
        "part_id": "2",
        "job_id": REQUEST_ID,
        "artifact_id": OUTPUT_ID,
    }
    payload[field] = "8dd9fa73-98db-412e-8329-21a4e95b7d27"
    destination = tmp_path / "wrong-identity-export"
    destination.mkdir()
    for operation in ("connect.output.present", "connect.output.export"):
        request_payload = dict(payload)
        if operation.endswith("export"):
            request_payload["destination_dir"] = str(destination)
        response = engine_api._response(api_request(config_path, operation, request_payload))
        assert response["ok"] is False
        assert response["error"]["code"] == "not_found"
        assert list(destination.iterdir()) == []
        assert runtime.store.connect_job(REQUEST_ID) == original


@pytest.mark.parametrize(
    ("field", "value"),
    [("protocol_version", 1), ("status", "failed"), ("result_json", None)],
)
def test_retained_invoice_record_shape_refuses_at_store_owner(completed_invoice, field, value):
    _, runtime, original = completed_invoice
    # Persisted states have a stronger schema guard; prove it without disabling constraints.
    with (
        pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"),
        runtime.store.connection() as db,
    ):
        db.execute(
            f"UPDATE connect_attachment_jobs SET {field} = ? WHERE job_id = ?",
            (value, REQUEST_ID),
        )
    assert runtime.store.connect_job(REQUEST_ID) == original
    assert reopened_invoice_outputs(runtime.store.path) == {
        "accepted": True,
        "outputs": json.loads(original.result_json)["outputs"],
    }
    # Direct owner boundary: isolate the reader guard from both SQL and API guards.
    invalid_record = replace(original, **{field: value})
    with pytest.raises(RuntimeError, match="missing its durable result"):
        runtime.store.completed_connect_outputs(invalid_record)
