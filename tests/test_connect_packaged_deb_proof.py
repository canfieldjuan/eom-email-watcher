from __future__ import annotations

import argparse
import runpy
import stat
import sys
from pathlib import Path

import pytest

from eom_email_watcher.config import load_config
from eom_email_watcher.db import Store

SCRIPTS_DIR = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
PROOF_SCRIPT = runpy.run_path(str(SCRIPTS_DIR / "connect-packaged-deb-proof.py"))

PackagedConnectProofError = PROOF_SCRIPT["PackagedConnectProofError"]
capability_state_satisfied = PROOF_SCRIPT["capability_state_satisfied"]
isolated_environment = PROOF_SCRIPT["isolated_environment"]
positive_seconds = PROOF_SCRIPT["positive_seconds"]
prepare_private_directories = PROOF_SCRIPT["prepare_private_directories"]
require_checks = PROOF_SCRIPT["require_checks"]
seed_attachment = PROOF_SCRIPT["seed_attachment"]
summary_capabilities = PROOF_SCRIPT["summary_capabilities"]
write_isolated_config = PROOF_SCRIPT["write_isolated_config"]


def capability_item(
    *,
    app_id: str = "document-summarizer",
    available: bool = True,
    protocol_version: int = 2,
    capability_id: str = "document.summarize",
    capability_version: str = "1.0",
) -> dict[str, object]:
    return {
        "protocol_version": protocol_version,
        "provider": {
            "app_id": app_id,
            "available": available,
            "instance_id": "11111111-1111-4111-8111-111111111111",
            "name": "Document Summarizer",
            "version": "0.1.0",
        },
        "capability": {
            "id": capability_id,
            "version": capability_version,
        },
    }


def test_isolated_config_pins_every_private_state_path(tmp_path: Path) -> None:
    prepare_private_directories(tmp_path)
    config_path = tmp_path / "config" / "email-watcher" / "config.toml"
    database_path = write_isolated_config(config_path, tmp_path / "state")
    seed_attachment(database_path)

    config = load_config(config_path)

    assert config.gmail_credentials_file == tmp_path / "state" / "credentials.json"
    assert config.gmail_token_file == tmp_path / "state" / "token.json"
    assert config.gmail_send_token_file == tmp_path / "state" / "send-token.json"
    assert config.database_file == database_path
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(database_path.stat().st_mode) == 0o600
    assert Store(database_path).attachment(
        "packaged-connect-proof-message", "packaged-connect-proof-pdf"
    ).media_type == "application/pdf"


def test_isolated_environment_does_not_forward_home_or_secret_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-cross")
    prepare_private_directories(tmp_path)

    environment = isolated_environment(tmp_path)

    assert "HOME" not in environment
    assert "GITHUB_TOKEN" not in environment
    assert environment["XDG_RUNTIME_DIR"] == str(tmp_path / "runtime")
    assert environment["DOC_SUM_MODEL_BASE_URL"] == "http://127.0.0.1:9/v1"


def test_capability_filter_accepts_only_the_expected_live_v2_provider() -> None:
    expected = capability_item()
    unrelated = capability_item(capability_id="document.translate")

    assert summary_capabilities({"items": [unrelated, expected]}) == [expected]

    with pytest.raises(PackagedConnectProofError, match="protocol-v2 declaration"):
        summary_capabilities({"items": [capability_item(available=False)]})
    with pytest.raises(PackagedConnectProofError, match="protocol-v2 declaration"):
        summary_capabilities({"items": [capability_item(app_id="lookalike-provider")]})
    with pytest.raises(PackagedConnectProofError, match="protocol-v2 declaration"):
        summary_capabilities({"items": [capability_item(protocol_version=1)]})


def test_availability_boundary_rejects_zero_or_multiple_live_providers() -> None:
    one = [capability_item()]
    two = [capability_item(), capability_item()]

    assert capability_state_satisfied([], available=False) is True
    assert capability_state_satisfied(one, available=True) is True
    assert capability_state_satisfied([], available=True) is False
    assert capability_state_satisfied(one, available=False) is False
    assert capability_state_satisfied(two, available=True) is False
    assert capability_state_satisfied(two, available=False) is False


def test_proof_check_gate_and_timeout_parser_fail_closed() -> None:
    require_checks({"removed": True, "restored": True})

    with pytest.raises(PackagedConnectProofError, match="removed, restored"):
        require_checks({"restored": False, "removed": False})
    assert positive_seconds("1") == 1
    for value in ("0", "-1", "1.5", "not-a-number"):
        with pytest.raises(argparse.ArgumentTypeError):
            positive_seconds(value)
