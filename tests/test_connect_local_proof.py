from __future__ import annotations

import inspect
import json
import runpy
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from connect_automate import connect, entitlement

from eom_email_watcher import engine_api
from eom_email_watcher.config import DEFAULT_RETENTION_DAYS, config_admission_snapshot
from eom_email_watcher.db import SCHEMA_VERSION, AdmissionProvenance, Store
from eom_email_watcher.mailbox import (
    DEFAULT_MAIL_ACCOUNT_ID,
    DEFAULT_MAIL_PROVIDER,
    MailboxGateway,
)
from eom_email_watcher.mime import AttachmentDescriptor
from eom_email_watcher.runtime import load_runtime

SCRIPTS_DIR = Path(__file__).parents[1] / "scripts"
PROOF_SCRIPT = runpy.run_path(str(SCRIPTS_DIR / "connect-local-proof.py"))
FIXTURE_PART_ID = PROOF_SCRIPT["FIXTURE_PART_ID"]
FIXTURE_MODEL_DIGEST = PROOF_SCRIPT["FIXTURE_MODEL_DIGEST"]
FIXTURE_MAILBOX_IDENTITY_KEY = PROOF_SCRIPT["FIXTURE_MAILBOX_IDENTITY_KEY"]
FixtureGmail = PROOF_SCRIPT["FixtureGmail"]
FixtureModelHandler = PROOF_SCRIPT["FixtureModelHandler"]
email_database_evidence = PROOF_SCRIPT["email_database_evidence"]
fixture_message_times = PROOF_SCRIPT["fixture_message_times"]
gmail_from_token = PROOF_SCRIPT["gmail_from_token"]
instrumented_client_factory = PROOF_SCRIPT["instrumented_client_factory"]
refuse_gmail = PROOF_SCRIPT["refuse_gmail"]
install_fixture_mailbox = PROOF_SCRIPT["install_fixture_mailbox"]
require_proof_checks = PROOF_SCRIPT["require_proof_checks"]
privacy_projection = PROOF_SCRIPT["privacy_projection"]
request = PROOF_SCRIPT["request"]
write_config = PROOF_SCRIPT["write_config"]
write_private_contract_summary = PROOF_SCRIPT["write_private_contract_summary"]


def test_fixture_model_serves_native_identity_and_stream() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureModelHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{server.server_port}", trust_env=False
        ) as client:
            expected_model = {
                "name": FixtureModelHandler.model_id,
                "digest": FIXTURE_MODEL_DIGEST,
                "size": 1,
                "details": {"family": "qwen3"},
            }
            assert client.get("/api/tags").json() == {"models": [expected_model]}
            assert client.get("/api/ps").json() == {"models": [expected_model]}
            assert client.post(
                "/api/show", json={"model": FixtureModelHandler.model_id, "verbose": False}
            ).json() == {
                "model_info": {
                    "general.architecture": "qwen3",
                    "qwen3.context_length": 131_072,
                }
            }
            assert client.post("/api/show", json={"model": "wrong-model"}).status_code == 400

            response = client.post(
                "/api/chat",
                json={
                    "model": FixtureModelHandler.model_id,
                    "messages": [
                        {"role": "system", "content": "Return JSON"},
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "quote_candidates": [
                                        {
                                            "quote_id": "q1",
                                            "exact_quote": "Bill total $27.",
                                        }
                                    ]
                                }
                            ),
                        },
                    ],
                    "stream": True,
                },
            )
            frames = [json.loads(frame) for frame in response.text.splitlines() if frame]
            assert response.status_code == 200
            assert len(frames) == 2
            assert frames[0] == {
                "model": FixtureModelHandler.model_id,
                "message": {"content": '{"selection":"q1"}'},
                "done": False,
            }
            # A real Ollama stream ends with done_reason; the PDF provider
            # requires "stop" before it accepts the generation (issue #267).
            assert frames[1] == {
                "model": FixtureModelHandler.model_id,
                "message": {"content": ""},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 1,
                "eval_count": 1,
            }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_fixture_mailbox_satisfies_real_account_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.store.reconcile_mailbox_identity(
        DEFAULT_MAIL_PROVIDER,
        DEFAULT_MAIL_ACCOUNT_ID,
        FIXTURE_MAILBOX_IDENTITY_KEY,
        legacy_status="replacement",
    )
    received_at, admitted_at = fixture_message_times()
    runtime.store.add_message(
        message_id="fixture-message",
        thread_id=None,
        sender="fixture@example.invalid",
        sender_name="Fixture Sender",
        subject="Fixture document",
        received_at=received_at,
        provider=DEFAULT_MAIL_PROVIDER,
        account_id=DEFAULT_MAIL_ACCOUNT_ID,
        mailbox_identity_key=FIXTURE_MAILBOX_IDENTITY_KEY,
        admission=AdmissionProvenance(
            kind="exact_sender",
            selector_id="sender:fixture@example.invalid",
            display_name="Fixture Sender",
            mailbox_identity_key=FIXTURE_MAILBOX_IDENTITY_KEY,
            admitted_at=admitted_at,
        ),
    )
    runtime.store.replace_attachments(
        "fixture-message",
        (
            AttachmentDescriptor(
                FIXTURE_PART_ID,
                "fixture-attachment",
                "fixture.pdf",
                "application/pdf",
                1,
                0,
            ),
        ),
    )
    capability_request = request(
        config_path,
        "connect.attachment.capabilities",
        {"message_id": "fixture-message", "part_id": FIXTURE_PART_ID},
    )
    discovery_calls = 0

    class EmptyCatalog:
        diagnostic_code = None

        def __init__(self, *, required_manifest_features):
            assert required_manifest_features == ("fixed-parameters-v1",)

        def compatible(self, _media_type: str, _byte_size: int) -> list[object]:
            nonlocal discovery_calls
            discovery_calls += 1
            return []

    monkeypatch.setattr(engine_api.connect, "discover_capabilities", EmptyCatalog)

    before = engine_api._response(capability_request)
    account = install_fixture_mailbox(runtime)
    after = engine_api._response(capability_request)

    assert before["error"]["code"] == "account_unavailable"
    assert account.provider == DEFAULT_MAIL_PROVIDER
    assert account.account_id == DEFAULT_MAIL_ACCOUNT_ID
    assert runtime.config.gmail_token_file.read_bytes() == b"fixture-only"
    assert runtime.config.gmail_token_file.stat().st_mode & 0o777 == 0o600
    assert discovery_calls == 1
    assert after == {
        "data": {"diagnostic": None, "items": []},
        "ok": True,
        "operation": "connect.attachment.capabilities",
        "protocol": 1,
    }


def test_proof_requests_carry_admission_for_admission_required_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    load_runtime(config_path)

    pump = request(config_path, "connect.queue.pump")

    # The Contract Watch pump is admission-required; the proof must bind it to
    # the engine's own configuration admission snapshot (issue #270).
    assert "connect.queue.pump" in engine_api.ADMISSION_REQUIRED_OPERATIONS
    assert pump["admission_token"] == config_admission_snapshot(config_path).token
    pumped = engine_api._response(pump)
    assert pumped["ok"] is True, pumped
    assert pumped["operation"] == "connect.queue.pump"
    for operation in (
        "inbox.recent",
        "connect.attachment.capabilities",
        "connect.attachment.invoke",
        "connect.output.present",
        "connect.output.export",
    ):
        assert operation not in engine_api.ADMISSION_REQUIRED_OPERATIONS
        assert "admission_token" not in request(config_path, operation)

    # The engine guard is unchanged: the same request without its token is refused.
    bare = {key: value for key, value in pump.items() if key != "admission_token"}
    assert engine_api._response(bare)["error"] == {
        "code": "invalid_request",
        "message": "admission_token is invalid",
    }


def _gateway_members() -> list[str]:
    return sorted(
        name
        for name, value in vars(MailboxGateway).items()
        if not name.startswith("_") and callable(value)
    )


def test_fixture_gmail_implements_every_mailbox_gateway_member() -> None:
    # A gateway member the double lacks fails here on every change, not first in
    # the nightly full proof (issue #275).
    members = _gateway_members()
    assert "set_operation_timeout" in members
    gateway = FixtureGmail(b"%PDF")
    for name in members:
        # The bound member of a real instance: an instance attribute with the same
        # name would shadow the method and fail here.
        member = getattr(gateway, name)
        assert inspect.ismethod(member), name
        expected = inspect.signature(getattr(MailboxGateway, name)).parameters
        actual = inspect.signature(member.__func__).parameters
        assert [(p.name, p.kind, p.default) for p in actual.values()] == [
            (p.name, p.kind, p.default) for p in expected.values()
        ], name
    with pytest.raises(AssertionError, match="does not read fixture message content"):
        gateway.content("fixture-message", 1, scope=frozenset())


def test_fixture_message_times_stay_inside_retention_at_any_clock() -> None:
    # Fixed calendar dates aged out of the retention window and would have made
    # the retained-source check refuse the fixture once time passed.
    for now in (
        datetime(2026, 10, 17, 2, 0, tzinfo=UTC),
        datetime(2027, 3, 1, tzinfo=UTC),
        datetime(2035, 1, 1, tzinfo=UTC),
    ):
        received_at, admitted_at = fixture_message_times(now)
        received = datetime.fromisoformat(received_at)
        admitted = datetime.fromisoformat(admitted_at)
        assert now - timedelta(days=DEFAULT_RETENTION_DAYS) < received < admitted < now


def test_fixture_gmail_operation_timeout_uses_production_validation() -> None:
    gateway = FixtureGmail(b"%PDF")
    for invalid in (0, -1, float("nan"), float("inf"), True):
        with pytest.raises(ValueError, match="positive finite"):
            gateway.set_operation_timeout(invalid)
    gateway.set_operation_timeout(2.5)

    assert gateway.operation_timeouts == [2.5]
    assert gateway.mailbox_address() == "fixture@example.invalid"
    with pytest.raises(AssertionError, match="does not poll"):
        gateway.initial_cursor()


def _fixture_attachment_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.store.reconcile_mailbox_identity(
        DEFAULT_MAIL_PROVIDER,
        DEFAULT_MAIL_ACCOUNT_ID,
        FIXTURE_MAILBOX_IDENTITY_KEY,
        legacy_status="replacement",
    )
    received_at, admitted_at = fixture_message_times()
    runtime.store.add_message(
        message_id="fixture-message",
        thread_id=None,
        sender="fixture@example.invalid",
        sender_name="Fixture Sender",
        subject="Fixture document",
        received_at=received_at,
        provider=DEFAULT_MAIL_PROVIDER,
        account_id=DEFAULT_MAIL_ACCOUNT_ID,
        mailbox_identity_key=FIXTURE_MAILBOX_IDENTITY_KEY,
        admission=AdmissionProvenance(
            kind="exact_sender",
            selector_id="sender:fixture@example.invalid",
            display_name="Fixture Sender",
            mailbox_identity_key=FIXTURE_MAILBOX_IDENTITY_KEY,
            admitted_at=admitted_at,
        ),
    )
    runtime.store.replace_attachments(
        "fixture-message",
        (
            AttachmentDescriptor(
                FIXTURE_PART_ID,
                "fixture-attachment",
                "fixture.pdf",
                "application/pdf",
                1,
                0,
            ),
        ),
    )
    install_fixture_mailbox(runtime)
    return runtime


def test_fixture_gmail_serves_the_real_deadline_bounded_attachment_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _fixture_attachment_runtime(tmp_path, monkeypatch)
    gateway = FixtureGmail(b"%PDF-fixture")
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", staticmethod(lambda *_args: gateway))

    content = engine_api._generic_attachment_content(
        runtime,
        "fixture-message",
        FIXTURE_PART_ID,
        operation_deadline=time.monotonic() + 30,
    )

    # The engine set the remaining deadline on the gateway before reading bytes.
    assert content == b"%PDF-fixture"
    assert len(gateway.operation_timeouts) == 1
    assert 0 < gateway.operation_timeouts[0] <= 30


def test_fixture_gmail_expired_deadline_reads_no_attachment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _fixture_attachment_runtime(tmp_path, monkeypatch)
    reads: list[tuple[str, str, str | None]] = []

    class RecordingFixtureGmail(FixtureGmail):
        def attachment_bytes(self, message_id, part_id, attachment_id):
            reads.append((message_id, part_id, attachment_id))
            return super().attachment_bytes(message_id, part_id, attachment_id)

    gateway = RecordingFixtureGmail(b"%PDF-fixture")
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", staticmethod(lambda *_args: gateway))

    with pytest.raises(engine_api._AutomationDispatchBudgetExhausted):
        engine_api._generic_attachment_content(
            runtime,
            "fixture-message",
            FIXTURE_PART_ID,
            operation_deadline=time.monotonic() - 1,
        )

    assert reads == []
    assert gateway.operation_timeouts == []


def _signature_call_shapes(callable_) -> list[tuple[tuple[object, ...], dict[str, object]]]:
    """Every-parameter positional and keyword calls admitted by a real signature."""
    parameters = [
        parameter
        for parameter in inspect.signature(callable_).parameters.values()
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    ]
    values = {parameter.name: object() for parameter in parameters}
    positional = tuple(
        values[parameter.name]
        for parameter in parameters
        if parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    )
    keyword_only = {
        parameter.name: values[parameter.name]
        for parameter in parameters
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY
    }
    return [((), {}), (positional, keyword_only), ((), values)]


def test_instrumented_client_factory_forwards_every_sdk_call_shape() -> None:
    # The proof replaces connect._client; the SDK calls it with a timeout, so a
    # zero-argument wrapper broke the interrupted-job phase (issue #281).
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    submissions: list[str] = []

    def original(*args: object, **kwargs: object) -> httpx.Client:
        calls.append((args, kwargs))
        return httpx.Client(
            base_url="http://provider.invalid",
            transport=httpx.MockTransport(lambda _request: httpx.Response(202)),
        )

    def record(request: httpx.Request) -> None:
        if request.method == "POST" and request.url.path == "/v2/jobs":
            submissions.append(request.url.path)

    factory = instrumented_client_factory(original, record)
    shapes = _signature_call_shapes(connect._client)
    assert ((), {"timeout_seconds": shapes[2][1]["timeout_seconds"]}) == shapes[2]
    for args, kwargs in shapes:
        with factory(*args, **kwargs) as client:
            client.post("/v2/jobs")
            client.get("/v2/manifest")
    assert calls == shapes
    assert submissions == ["/v2/jobs"] * len(shapes)


def test_gmail_from_token_double_accepts_every_real_factory_call_shape() -> None:
    gateway = FixtureGmail(b"%PDF")
    opened = gmail_from_token(lambda: gateway).__func__
    refused = gmail_from_token(refuse_gmail("must not open Gmail")).__func__
    for args, kwargs in _signature_call_shapes(engine_api.GmailGateway.from_token):
        assert opened(*args, **kwargs) is gateway
        with pytest.raises(AssertionError, match="must not open Gmail"):
            refused(*args, **kwargs)


def test_proof_entitlement_doubles_match_sdk_signatures() -> None:
    # The proof replaces these SDK callables with a bound zero-argument method and
    # a *feature_ids lambda; a changed SDK signature must fail here first.
    assert list(inspect.signature(entitlement.connect_entitlement_decision).parameters) == []
    features = list(inspect.signature(entitlement.feature_entitlements_active).parameters.values())
    assert [(parameter.name, parameter.kind) for parameter in features] == [
        ("feature_ids", inspect.Parameter.VAR_POSITIONAL)
    ]


def test_email_database_evidence_checks_schema_functions_through_store_owner(
    tmp_path: Path,
) -> None:
    # The current schema's CHECK constraints call application SQL functions that only
    # Store.connection registers; quick_check must run with them, not on a bare connection.
    store = Store(tmp_path / "watcher.sqlite3")
    store.initialize()

    quick_check, schema_version, job_row, interrupted_rows = email_database_evidence(
        store, "proof-job", "interrupted-proof-job"
    )

    assert quick_check == "ok"
    assert schema_version == SCHEMA_VERSION
    assert job_row is None
    assert interrupted_rows == []


def test_proof_check_gate_accepts_only_complete_success() -> None:
    require_proof_checks({"provider_removed": True, "provider_restored": True})

    with pytest.raises(
        RuntimeError,
        match="Connect proof failed checks: provider_removed, provider_restored",
    ):
        require_proof_checks({"provider_restored": False, "provider_removed": False})


def test_privacy_projection_allows_only_artifact_display_names() -> None:
    projected = privacy_projection(
        {
            "display_name": "top-level-name",
            "message_id": "private-message",
            "parameters": {"display_name": "parameter-name"},
            "inputs": [
                {
                    "display_name": "private-message.pdf",
                    "metadata": {"display_name": "nested-name"},
                }
            ],
        }
    )

    assert projected == {
        "display_name": "top-level-name",
        "message_id": "private-message",
        "parameters": {"display_name": "parameter-name"},
        "inputs": [
            {
                "display_name": "<allowed-artifact-display-name>",
                "metadata": {"display_name": "nested-name"},
            }
        ],
    }


def test_contract_summary_output_appears_complete_and_private(tmp_path: Path) -> None:
    output = tmp_path / "contract-summary.txt"

    write_private_contract_summary(output, "complete summary")

    assert output.read_text(encoding="utf-8") == "complete summary"
    assert output.stat().st_mode & 0o777 == 0o600


def test_contract_summary_output_refuses_existing_file_and_symlink(tmp_path: Path) -> None:
    existing = tmp_path / "existing.txt"
    existing.write_text("keep existing", encoding="utf-8")
    target = tmp_path / "target.txt"
    target.write_text("keep target", encoding="utf-8")
    linked = tmp_path / "linked.txt"
    linked.symlink_to(target)

    with pytest.raises(FileExistsError):
        write_private_contract_summary(existing, "replace existing")
    with pytest.raises(FileExistsError):
        write_private_contract_summary(linked, "follow link")

    assert existing.read_text(encoding="utf-8") == "keep existing"
    assert target.read_text(encoding="utf-8") == "keep target"
