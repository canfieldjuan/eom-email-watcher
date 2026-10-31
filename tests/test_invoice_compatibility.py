"""EW278 backend proof: synthetic stores and loopback HTTP; no real mailbox or model."""

import base64
import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from connect_automate import connect
from connect_reference_provider import ReferenceProvider, _Handler, _now
from test_connect_v2_engine_api import (
    INSTANCE_B,
    OUTPUT_ID,
    PDF,
    REQUEST_ID,
    SeededMailboxGateway,
    api_request,
    capability,
    contract_rule_definition,
    invocation_payload,
    patch_runtime,
    seeded_runtime,
)

from eom_email_watcher import engine_api
from eom_email_watcher.connect_selection import decode_selection, encode_selection

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
    state = {"version": "1.0", "outcome": "completed", "requests": []}
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
                "effects": {"external": False, "confirmation_required": False},
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


def prepare_http_job(invoice_runtime, monkeypatch):
    _, runtime = invoice_runtime

    class Mailbox(SeededMailboxGateway):
        def set_operation_timeout(self, timeout):
            pass

        def attachment_bytes(self, *args):
            return PDF

    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: Mailbox())
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


@pytest.mark.parametrize("outcome", ["refused", "interrupted"])
def test_legacy_invoice_http_failures_are_terminal_without_resubmission(
    invoice_runtime,
    invoice_http_provider,
    monkeypatch,
    outcome,
):
    _, runtime = invoice_runtime
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
    for _ in range(2):
        engine_api._pump_generic_connect_lane(runtime, runtime.store.connect_job(REQUEST_ID))
    assert provider.post_attempt_count() == 1
    assert len(state["requests"]) == 1


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
