"""Application boundary proof for negotiated fixed parameters; no provider/model calls."""

import json
import os
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser

import httpx
import pytest
from connect_automate import connect
from test_connect_v2_engine_api import (
    INSTANCE_A,
    PDF,
    REQUEST_ID,
    SECOND_REQUEST_ID,
    TEST_MAILBOX_IDENTITY_KEY,
    TOKEN,
    SeededMailboxGateway,
    api_request,
    capability,
    contract_rule_definition,
    exact_sender_admission,
    make_connect_job_due,
    patch_runtime,
    seeded_runtime,
)

from eom_email_watcher import engine_api
from eom_email_watcher.db import Store
from eom_email_watcher.mime import AttachmentDescriptor

FEATURES = ("fixed-parameters-v1",)
HEADER = "Local-Connect-Manifest-Features"


@pytest.fixture
def provider(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    directory = root / "local-connect/v2/providers"
    directory.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    for parent in (root / "local-connect", root / "local-connect/v2", directory):
        parent.chmod(0o700)
    registration = {
        "protocol_version": 2,
        "instance_id": INSTANCE_A,
        "app_id": "generic-provider",
        "pid": os.getpid(),
        "started_at": datetime.now(UTC).isoformat(),
        "transport": {"kind": "http-loopback-v2", "base_url": "http://127.0.0.1:32123/"},
        "auth": {"scheme": "bearer", "token": TOKEN},
    }
    path = directory / "provider.json"
    path.write_text(json.dumps(registration))
    path.chmod(0o600)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(root))
    monkeypatch.setattr(
        connect.entitlement,
        "connect_entitlement_decision",
        lambda: connect.entitlement.EntitlementDecision.ACTIVE,
    )
    state = {
        "ack": True,
        "pin": "bundle-a",
        "posts": [],
        "headers": [],
        "gets": [],
        "retryable": False,
    }

    def handler(request):
        if request.url.path == "/v2/manifest":
            state["headers"].append(request.headers.get(HEADER))
            parameter = {
                "name": "pin",
                "value_type": (
                    "boolean"
                    if type(state["pin"]) is bool
                    else "integer"
                    if type(state["pin"]) is int
                    else "string"
                ),
                "required": True,
                "label": "Pin",
                "description": "Pinned compatibility",
            }
            headers = {}
            if state["ack"]:
                parameter["fixed_value"] = state["pin"]
                headers = {HEADER: FEATURES[0], "Cache-Control": "no-store", "Vary": HEADER}
            return httpx.Response(
                200,
                headers=headers,
                json={
                    "protocol_version": 2,
                    "instance_id": INSTANCE_A,
                    "app": {"id": "generic-provider", "name": "Fixture", "version": "1.2.3"},
                    "capabilities": [
                        {
                            "id": "document.translate",
                            "version": "1.0",
                            "action": {"label": "Translate", "description": "Translate"},
                            "accepts": [{"media_type": "application/pdf", "max_bytes": 1024}],
                            "produces": ["text/plain"],
                            "parameters": [parameter]
                            + (
                                [
                                    {
                                        "name": "editable",
                                        "value_type": "string",
                                        "required": False,
                                        "label": "Editable",
                                        "description": "Caller-owned value",
                                    }
                                ]
                                if state.get("editable")
                                else []
                            ),
                            "effects": {"external": False, "confirmation_required": False},
                        }
                    ],
                },
            )
        if request.method == "GET":
            state["gets"].append(str(request.url))
            return httpx.Response(
                404,
                json={
                    "protocol_version": 2,
                    "error": {
                        "code": "JOB_NOT_FOUND",
                        "message": "Not retained",
                        "retryable": False,
                    },
                },
            )
        raw = (
            f"Content-Type: {request.headers['content-type']}\r\nMIME-Version: 1.0\r\n\r\n".encode()
            + request.read()
        )
        parts = BytesParser(policy=policy.default).parsebytes(raw)
        body = next(
            part.get_payload(decode=True)
            for part in parts.iter_parts()
            if part.get_param("name", header="content-disposition") == "request"
        )
        state["posts"].append(body)
        return httpx.Response(
            409,
            json={
                "protocol_version": 2,
                "error": {
                    "code": "FIXED_PARAMETER_MISMATCH",
                    "message": "Fixture refusal",
                    "retryable": state["retryable"],
                },
            },
        )

    monkeypatch.setattr(
        connect,
        "_client",
        lambda *args, **kwargs: httpx.Client(transport=httpx.MockTransport(handler)),
    )
    yield state


def install_mailbox(monkeypatch, runtime):
    class Mailbox(SeededMailboxGateway):
        def set_operation_timeout(self, timeout):
            pass

        def attachment_bytes(self, *args):
            return PDF

    patch_runtime(monkeypatch, runtime)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: Mailbox())


def selected(provider):
    result = engine_api._connect_catalog({"payload": {}})
    assert provider["headers"][-1] == FEATURES[0], "catalog must opt in at discovery origin"
    assert result["items"], result
    item = result["items"][0]
    return {
        "app_id": item["provider"]["app_id"],
        "version": item["provider"]["version"],
        "instance_id": item["provider"]["instance_id"],
    }, item["capability"]


@pytest.mark.parametrize("ack", [True, False])
def test_catalog_to_restart_to_post(provider, tmp_path, monkeypatch, ack):
    provider["ack"] = ack
    cfg, runtime = seeded_runtime(tmp_path)
    install_mailbox(monkeypatch, runtime)
    ref, declaration = selected(provider)
    cap, values = engine_api._discover_selected_generic_capability(
        ref,
        {"id": declaration["id"], "version": declaration["version"]},
        {} if ack else {"pin": "legacy"},
    )
    assert values == {"pin": "bundle-a" if ack else "legacy"}
    job, saved, _, _ = engine_api._prepare_or_create_generic_connect_job(
        runtime,
        request_id=REQUEST_ID,
        message_id="message-1",
        part_id="2",
        capability=cap,
        parameters=values,
        confirmed=False,
        interactive_authorized=True,
    )
    assert saved is not None
    frozen = saved.request_json
    runtime = replace(runtime, store=Store(runtime.store.path))
    runtime.store.initialize()
    assert runtime.store.connect_dispatch(saved.job_id).required_manifest_features == (
        FEATURES if ack else ()
    )
    engine_api._pump_generic_connect_lane(runtime, runtime.store.connect_job(saved.job_id))
    assert provider["posts"] == [frozen]
    assert json.loads(provider["posts"][0])["parameters"] == values
    assert runtime.store.connect_job(saved.job_id).status == "failed"


def test_feature_requirement_prevents_downgraded_dispatch(provider, tmp_path, monkeypatch):
    _, runtime = seeded_runtime(tmp_path)
    install_mailbox(monkeypatch, runtime)
    ref, declaration = selected(provider)
    cap, values = engine_api._discover_selected_generic_capability(
        ref, {"id": declaration["id"], "version": declaration["version"]}, {}
    )
    _, saved, _, _ = engine_api._prepare_or_create_generic_connect_job(
        runtime,
        request_id=REQUEST_ID,
        message_id="message-1",
        part_id="2",
        capability=cap,
        parameters=values,
        confirmed=False,
    )
    provider["ack"] = False
    engine_api._pump_generic_connect_lane(runtime, saved)
    assert not provider["posts"]
    assert runtime.store.connect_dispatch(saved.job_id).state == "waiting"
    provider["ack"] = True
    provider["pin"] = "bundle-b"
    make_connect_job_due(runtime, saved.job_id)
    engine_api._pump_generic_connect_lane(runtime, runtime.store.connect_job(saved.job_id))
    assert provider["posts"] == [saved.request_json]
    assert json.loads(provider["posts"][0])["parameters"] == {"pin": "bundle-a"}


def test_join_identity_includes_features(tmp_path):
    _, runtime = seeded_runtime(tmp_path)
    cap = capability()
    job = connect.prepare_capability_job(
        cap, PDF, "application/pdf", "invoice.pdf", job_id=REQUEST_ID
    )
    fields = dict(
        message_id="message-1",
        part_id="2",
        protocol_version=2,
        capability_id=job.capability_id,
        capability_version=job.capability_version,
        provider_app_id=job.provider_app_id,
        provider_app_version=job.provider_app_version,
        provider_instance_id=job.provider_instance_id,
        request_json=job.request_json,
    )

    def create(job_id, features):
        return runtime.store.create_connect_job(
            **{
                **fields,
                "request_json": job.request_json.replace(REQUEST_ID.encode(), job_id.encode()),
            },
            job_id=job_id,
            input_artifact_id=job.artifact.artifact_id,
            input_media_type=job.artifact.media_type,
            input_byte_size=job.artifact.byte_size,
            input_sha256=job.artifact.sha256,
            input_display_name=job.display_name,
            source_app_id=connect.SOURCE_APP_ID,
            required_manifest_features=features,
            capability_produces=cap.produces,
        )

    legacy = create(REQUEST_ID, ())
    negotiated = create(SECOND_REQUEST_ID, FEATURES)
    assert negotiated.job_id != legacy.job_id
    assert (
        runtime.store.active_connect_job(**fields, required_manifest_features=FEATURES).job_id
        == SECOND_REQUEST_ID
    )
    assert runtime.store.active_connect_job(**fields).job_id == REQUEST_ID
    assert runtime.store.connect_job_parameters(negotiated) == {}
    rows, _ = runtime.store.query_inbox(limit=5)
    results = rows[0]["attachments"][0]["capability_results"]
    assert {item["job_id"]: item["required_manifest_features"] for item in results} == {
        REQUEST_ID: [],
        SECOND_REQUEST_ID: list(FEATURES),
    }


def test_malformed_fixed_refusal_keeps_possible_ownership(provider, tmp_path, monkeypatch):
    _, runtime = seeded_runtime(tmp_path)
    install_mailbox(monkeypatch, runtime)
    ref, declaration = selected(provider)
    cap, values = engine_api._discover_selected_generic_capability(
        ref, {"id": declaration["id"], "version": declaration["version"]}, {}
    )
    _, saved, _, _ = engine_api._prepare_or_create_generic_connect_job(
        runtime,
        request_id=REQUEST_ID,
        message_id="message-1",
        part_id="2",
        capability=cap,
        parameters=values,
        confirmed=False,
    )
    provider["retryable"] = True
    engine_api._pump_generic_connect_lane(runtime, saved)
    assert runtime.store.connect_job(saved.job_id).status == "requested"
    assert runtime.store.connect_dispatch(saved.job_id).state == "reconciling"
    assert len(provider["posts"]) == 1


def analyze(runtime, message_id="message-1"):
    runtime.store.mark_analyzed(
        message_id,
        {
            "category": "informational",
            "priority": "normal",
            "summary": "Fixture",
            "action_required": True,
            "suggested_action": "Review",
            "deadline_text": None,
            "deadline_iso": None,
            "confidence": 0.9,
        },
        mailbox_identity_key=TEST_MAILBOX_IDENTITY_KEY,
        body_chars=None,
        body_source_chars=None,
    )


def test_rule_save_freezes_omitted_fixed_intent(provider, tmp_path, monkeypatch):
    cfg, runtime = seeded_runtime(tmp_path)
    install_mailbox(monkeypatch, runtime)
    monkeypatch.setattr(engine_api, "_require_automation_entitlement", lambda: None)
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    ref, declaration = selected(provider)
    cap, _ = engine_api._discover_selected_generic_capability(
        ref, {"id": declaration["id"], "version": declaration["version"]}, {}
    )
    definition = contract_rule_definition(cap)
    definition["action"]["parameters"] = {}
    engine_api._automation_rules_put(
        api_request(cfg, "automation.rules.put", {"definition": definition})
    )
    analyze(runtime)
    fire = runtime.store.automation_fires_for_message("message-1")[0]
    engine_api._dispatch_automation_fire(runtime, fire.fire_id)
    first = runtime.store.automation_fire(fire.fire_id)
    assert first.state == "submitted"
    assert json.loads(runtime.store.connect_job(first.job_id).request_json)["parameters"] == {
        "pin": "bundle-a"
    }
    engine_api._pump_generic_connect_lane(runtime, runtime.store.connect_job(first.job_id))
    assert len(provider["posts"]) == 1
    runtime.store.add_message(
        message_id="message-2",
        thread_id=None,
        sender="private@example.com",
        sender_name="Private Sender",
        subject="Fixture",
        received_at="2026-08-30T12:01:00+00:00",
        mailbox_identity_key=TEST_MAILBOX_IDENTITY_KEY,
        admission=exact_sender_admission("private@example.com", "Private Sender"),
    )
    runtime.store.replace_attachments(
        "message-2",
        (
            AttachmentDescriptor(
                "2", "gmail-attachment-2", "invoice.pdf", "application/pdf", len(PDF), 0
            ),
        ),
    )
    analyze(runtime, "message-2")
    fire = runtime.store.automation_fires_for_message("message-2")[0]
    provider["pin"] = "bundle-b"
    engine_api._dispatch_automation_fire(runtime, fire.fire_id)
    observed = runtime.store.automation_fire(fire.fire_id)
    assert observed.state == "manual_review", observed
    assert observed.job_id is None
    assert len(provider["posts"]) == 1


@pytest.mark.parametrize("fixed", ["", 0, False])
@pytest.mark.parametrize("input_kind", ["omitted", "identical", "conflict", "wrong_type"])
def test_interactive_fixed_inputs_are_bound_at_shared_owner(
    provider, tmp_path, monkeypatch, fixed, input_kind
):
    cfg, runtime = seeded_runtime(tmp_path)
    install_mailbox(monkeypatch, runtime)
    provider["pin"] = fixed
    ref, declaration = selected(provider)
    supplied = {} if input_kind == "omitted" else {"pin": fixed}
    if input_kind == "conflict":
        supplied = {"pin": "other" if type(fixed) is str else 1 if type(fixed) is int else True}
    if input_kind == "wrong_type":
        supplied = {"pin": 0 if type(fixed) is str else False if type(fixed) is int else 0}
    payload = {
        "request_id": REQUEST_ID,
        "message_id": "message-1",
        "part_id": "2",
        "provider": ref,
        "capability": {"id": declaration["id"], "version": declaration["version"]},
        "parameters": supplied,
        "confirmed": False,
    }
    response = engine_api._response(api_request(cfg, "connect.attachment.invoke", payload))
    if input_kind in {"conflict", "wrong_type"}:
        assert response["ok"] is False
        assert not provider["posts"]
        assert runtime.store.connect_job(REQUEST_ID) is None
    else:
        assert len(provider["posts"]) == 1, response
        params = json.loads(provider["posts"][0])["parameters"]
        assert params == {"pin": fixed}
        assert type(params["pin"]) is type(fixed)


def test_interactive_omission_recovers_frozen_job_without_catalog(provider, tmp_path, monkeypatch):
    cfg, runtime = seeded_runtime(tmp_path)
    install_mailbox(monkeypatch, runtime)
    ref, declaration = selected(provider)
    cap, values = engine_api._discover_selected_generic_capability(
        ref, {"id": declaration["id"], "version": declaration["version"]}, {}
    )
    _, saved, _, _ = engine_api._prepare_or_create_generic_connect_job(
        runtime,
        request_id=REQUEST_ID,
        message_id="message-1",
        part_id="2",
        capability=cap,
        parameters=values,
        confirmed=False,
    )
    provider["ack"] = False
    payload = {
        "request_id": REQUEST_ID,
        "message_id": "message-1",
        "part_id": "2",
        "provider": ref,
        "capability": {"id": declaration["id"], "version": declaration["version"]},
        "parameters": {},
        "confirmed": False,
    }
    response = engine_api._response(api_request(cfg, "connect.attachment.invoke", payload))
    assert response["ok"] is True, response
    assert response["data"]["job_id"] == saved.job_id
    assert provider["gets"]
    assert not provider["posts"]
    assert runtime.store.connect_job(saved.job_id).request_json == saved.request_json


@pytest.mark.parametrize("toggle", [False, True])
def test_legacy_rule_fixed_selection_needs_manual_review(provider, tmp_path, monkeypatch, toggle):
    _, runtime = seeded_runtime(tmp_path)
    install_mailbox(monkeypatch, runtime)
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    ref, declaration = selected(provider)
    cap, _ = engine_api._discover_selected_generic_capability(
        ref, {"id": declaration["id"], "version": declaration["version"]}, {}
    )
    definition = contract_rule_definition(cap)
    definition["action"]["parameters"] = {"pin": "bundle-a"}
    legacy = runtime.store.put_automation_rule(definition)
    if toggle:
        runtime.store.set_automation_rule_enabled(legacy.summary.rule_id, 1, False)
        runtime.store.set_automation_rule_enabled(legacy.summary.rule_id, 2, True)
    analyze(runtime)
    fire = runtime.store.automation_fires_for_message("message-1")[0]
    engine_api._dispatch_automation_fire(runtime, fire.fire_id)
    observed = runtime.store.automation_fire(fire.fire_id)
    assert observed.state == "manual_review"
    assert observed.reason == "rule_selection_changed"
    assert not provider["posts"]


def test_rule_save_discovery_failure_is_atomic(provider, tmp_path, monkeypatch):
    cfg, runtime = seeded_runtime(tmp_path)
    install_mailbox(monkeypatch, runtime)
    monkeypatch.setattr(engine_api, "_require_automation_entitlement", lambda: None)
    ref, declaration = selected(provider)
    cap, _ = engine_api._discover_selected_generic_capability(
        ref, {"id": declaration["id"], "version": declaration["version"]}, {}
    )
    definition = contract_rule_definition(cap)
    definition["action"]["parameters"] = {}
    provider["ack"] = False
    response = engine_api._response(
        api_request(cfg, "automation.rules.put", {"definition": definition})
    )
    assert response["ok"] is False
    assert runtime.store.automation_rules_snapshot() == (0, [])


@pytest.mark.parametrize("fixed", ["", 0, False])
def test_rule_toggle_preserves_selection_and_definition_bytes(
    provider, tmp_path, monkeypatch, fixed
):
    provider["pin"] = fixed
    cfg, runtime = seeded_runtime(tmp_path)
    install_mailbox(monkeypatch, runtime)
    monkeypatch.setattr(engine_api, "_require_automation_entitlement", lambda: None)
    ref, declaration = selected(provider)
    cap, _ = engine_api._discover_selected_generic_capability(
        ref, {"id": declaration["id"], "version": declaration["version"]}, {}
    )
    definition = contract_rule_definition(cap)
    definition["action"]["parameters"] = {}
    created = engine_api._automation_rules_put(
        api_request(cfg, "automation.rules.put", {"definition": definition})
    )["rule"]
    rule_id = created["summary"]["rule_id"]
    patch_runtime(monkeypatch, runtime)
    for expected_version, enabled in [(1, False), (2, False), (2, True), (3, True)]:
        result = engine_api._automation_rules_set_enabled(
            api_request(
                cfg,
                "automation.rules.set_enabled",
                {"rule_id": rule_id, "expected_version": expected_version, "enabled": enabled},
            )
        )["rule"]
        observed = engine_api._automation_rules_get(
            api_request(cfg, "automation.rules.get", {"rule_id": rule_id})
        )["rule"]
        assert result["selection"] == observed["selection"] == created["selection"]
        assert result["definition"] == observed["definition"] == created["definition"]
    assert runtime.store.automation_rule_selection(rule_id, 1) == (
        runtime.store.automation_rule_selection(rule_id, 3)
    )
    with runtime.store.connection() as db:
        rows = db.execute(
            "SELECT definition_json, definition_sha256 FROM automation_rule_versions "
            "WHERE rule_id = ?",
            (rule_id,),
        ).fetchall()
    assert len({(row["definition_json"], row["definition_sha256"]) for row in rows}) == 1
    assert created["selection"]["acknowledged_manifest_features"] == list(FEATURES)
    assert "token" not in created["selection"]


def test_automation_exact_job_recovers_omitted_fixed_intent_without_catalog(
    provider, tmp_path, monkeypatch
):
    cfg, runtime = seeded_runtime(tmp_path)
    install_mailbox(monkeypatch, runtime)
    monkeypatch.setattr(engine_api, "_require_automation_entitlement", lambda: None)
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    ref, declaration = selected(provider)
    cap, parameters = engine_api._discover_selected_generic_capability(
        ref, {"id": declaration["id"], "version": declaration["version"]}, {}
    )
    definition = contract_rule_definition(cap)
    definition["action"]["parameters"] = {}
    engine_api._automation_rules_put(
        api_request(cfg, "automation.rules.put", {"definition": definition})
    )
    analyze(runtime)
    fire = runtime.store.automation_fires_for_message("message-1")[0]
    attempt = runtime.store.automation_fire_attempts(fire.fire_id)[0]
    _, saved, _, _ = engine_api._prepare_or_create_generic_connect_job(
        runtime,
        request_id=attempt.dispatch_request_id,
        message_id=fire.message_id,
        part_id=fire.part_id,
        capability=cap,
        parameters=parameters,
        confirmed=False,
        artifact_id=engine_api._automation_artifact_id(attempt.dispatch_request_id),
    )
    assert saved is not None
    runtime = replace(runtime, store=Store(runtime.store.path))
    runtime.store.initialize()
    before = list(provider["headers"])
    provider["ack"] = False
    provider["pin"] = "rotated"
    engine_api._dispatch_automation_fire(runtime, fire.fire_id)
    recovered = runtime.store.automation_fire(fire.fire_id)
    assert recovered.state == "submitted"
    assert recovered.job_id == saved.job_id
    assert provider["headers"] == before
    assert provider["posts"] == []
    assert runtime.store.connect_job(saved.job_id).request_json == saved.request_json


def test_legacy_dispatch_migration_preserves_existing_authority():
    from eom_email_watcher import db as database

    # The schema-31 dispatch shape, independent of the new table declaration.
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE connect_job_dispatch (
        job_id TEXT PRIMARY KEY, state TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
        reconciliation_failure_count INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT,
        admission_deadline TEXT NOT NULL, submission_possible INTEGER NOT NULL DEFAULT 0,
        source_available INTEGER NOT NULL DEFAULT 1,
        capability_external_effects INTEGER NOT NULL DEFAULT 0,
        capability_confirmation_required INTEGER NOT NULL DEFAULT 0,
        capability_authority_known INTEGER NOT NULL DEFAULT 0,
        capability_produces_json TEXT NOT NULL DEFAULT '[]', interactive_authorized_at TEXT,
        automation_paused_at TEXT, highest_provider_state TEXT NOT NULL DEFAULT 'requested',
        last_error_code TEXT, last_error_message TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")
    db.execute("CREATE TABLE messages (message_id TEXT PRIMARY KEY)")
    db.execute(database._CONNECT_JOBS_TABLE_SQL)
    db.execute("""INSERT INTO connect_job_dispatch(job_id, state, admission_deadline,
        submission_possible, capability_authority_known,
        capability_produces_json, created_at, updated_at)
        VALUES ('legacy', 'provider_owned', '2026-10-11T00:00:00+00:00',
            1, 1, '["text/plain"]', 'old', 'old')""")
    original = dict(db.execute("SELECT * FROM connect_job_dispatch").fetchone())
    database._ensure_connect_dispatch_schema(db)
    migrated = dict(db.execute("SELECT * FROM connect_job_dispatch").fetchone())
    assert {key: migrated[key] for key in original} == original
    assert migrated["required_manifest_features_json"] == "[]"
    assert migrated["selection_json"] is None
    database._ensure_connect_dispatch_schema(db)
    assert dict(db.execute("SELECT * FROM connect_job_dispatch").fetchone()) == migrated
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute(
            "UPDATE connect_job_dispatch SET required_manifest_features_json = ?",
            (json.dumps(list(FEATURES)),),
        )


def test_rule_prepare_preserves_omitted_fixed_definition(provider):
    ref, declaration = selected(provider)
    cap, _ = engine_api._discover_selected_generic_capability(
        ref, {"id": declaration["id"], "version": declaration["version"]}, {}
    )
    definition = contract_rule_definition(cap)
    definition["action"]["parameters"] = {}
    prepared = engine_api._automation_rules_prepare({"payload": {"definition": definition}})
    assert prepared["definition"]["action"]["parameters"] == {}


def test_recovery_does_not_fill_an_omitted_editable_value(provider, tmp_path, monkeypatch):
    cfg, runtime = seeded_runtime(tmp_path)
    install_mailbox(monkeypatch, runtime)
    provider["editable"] = True
    ref, declaration = selected(provider)
    cap, values = engine_api._discover_selected_generic_capability(
        ref, {"id": declaration["id"], "version": declaration["version"]}, {"editable": "kept"}
    )
    _, saved, _, _ = engine_api._prepare_or_create_generic_connect_job(
        runtime,
        request_id=REQUEST_ID,
        message_id="message-1",
        part_id="2",
        capability=cap,
        parameters=values,
        confirmed=False,
    )
    before = list(provider["headers"])
    response = engine_api._response(
        api_request(
            cfg,
            "connect.attachment.invoke",
            {
                "request_id": REQUEST_ID,
                "message_id": "message-1",
                "part_id": "2",
                "provider": ref,
                "capability": {"id": declaration["id"], "version": declaration["version"]},
                "parameters": {},
                "confirmed": False,
            },
        )
    )
    assert response["error"]["code"] == "request_id_conflict"
    assert provider["headers"] == before
    assert not provider["posts"]
    assert runtime.store.connect_job(saved.job_id).request_json == saved.request_json
