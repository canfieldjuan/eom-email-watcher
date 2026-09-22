from __future__ import annotations

import json
import runpy
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from eom_email_watcher import engine_api
from eom_email_watcher.db import AdmissionProvenance
from eom_email_watcher.mailbox import DEFAULT_MAIL_ACCOUNT_ID, DEFAULT_MAIL_PROVIDER
from eom_email_watcher.mime import AttachmentDescriptor
from eom_email_watcher.runtime import load_runtime

SCRIPTS_DIR = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
PROOF_SCRIPT = runpy.run_path(str(SCRIPTS_DIR / "connect-local-proof.py"))
FIXTURE_PART_ID = PROOF_SCRIPT["FIXTURE_PART_ID"]
FIXTURE_MODEL_DIGEST = PROOF_SCRIPT["FIXTURE_MODEL_DIGEST"]
FIXTURE_MAILBOX_IDENTITY_KEY = PROOF_SCRIPT["FIXTURE_MAILBOX_IDENTITY_KEY"]
FixtureModelHandler = PROOF_SCRIPT["FixtureModelHandler"]
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
            assert frames[1] == {
                "model": FixtureModelHandler.model_id,
                "message": {"content": ""},
                "done": True,
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
    runtime.store.add_message(
        message_id="fixture-message",
        thread_id=None,
        sender="fixture@example.invalid",
        sender_name="Fixture Sender",
        subject="Fixture document",
        received_at="2026-08-29T12:00:00+00:00",
        provider=DEFAULT_MAIL_PROVIDER,
        account_id=DEFAULT_MAIL_ACCOUNT_ID,
        mailbox_identity_key=FIXTURE_MAILBOX_IDENTITY_KEY,
        admission=AdmissionProvenance(
            kind="exact_sender",
            selector_id="sender:fixture@example.invalid",
            display_name="Fixture Sender",
            mailbox_identity_key=FIXTURE_MAILBOX_IDENTITY_KEY,
            admitted_at="2026-09-19T12:00:00+00:00",
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
