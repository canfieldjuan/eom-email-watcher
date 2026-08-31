from __future__ import annotations

import runpy
from pathlib import Path

import pytest

PROOF_SCRIPT = runpy.run_path(str(Path(__file__).parents[1] / "scripts/connect-local-proof.py"))
require_proof_checks = PROOF_SCRIPT["require_proof_checks"]
privacy_projection = PROOF_SCRIPT["privacy_projection"]


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
