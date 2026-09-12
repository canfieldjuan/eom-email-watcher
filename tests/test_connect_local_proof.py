from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pytest

from eom_email_watcher import engine_api
from eom_email_watcher.mailbox import DEFAULT_MAIL_ACCOUNT_ID, DEFAULT_MAIL_PROVIDER
from eom_email_watcher.mime import AttachmentDescriptor
from eom_email_watcher.runtime import load_runtime

SCRIPTS_DIR = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
PROOF_SCRIPT = runpy.run_path(str(SCRIPTS_DIR / "connect-local-proof.py"))
FIXTURE_PART_ID = PROOF_SCRIPT["FIXTURE_PART_ID"]
install_fixture_mailbox = PROOF_SCRIPT["install_fixture_mailbox"]
require_proof_checks = PROOF_SCRIPT["require_proof_checks"]
privacy_projection = PROOF_SCRIPT["privacy_projection"]
request = PROOF_SCRIPT["request"]
write_config = PROOF_SCRIPT["write_config"]


def test_fixture_mailbox_satisfies_real_account_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.store.add_message(
        message_id="fixture-message",
        thread_id=None,
        sender="fixture@example.invalid",
        sender_name="Fixture Sender",
        subject="Fixture document",
        received_at="2026-08-29T12:00:00+00:00",
        provider=DEFAULT_MAIL_PROVIDER,
        account_id=DEFAULT_MAIL_ACCOUNT_ID,
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
