from __future__ import annotations

import runpy
from pathlib import Path

import pytest

PROOF_SCRIPT = runpy.run_path(str(Path(__file__).parents[1] / "scripts/connect-local-proof.py"))
require_proof_checks = PROOF_SCRIPT["require_proof_checks"]


def test_proof_check_gate_accepts_only_complete_success() -> None:
    require_proof_checks({"provider_removed": True, "provider_restored": True})

    with pytest.raises(
        RuntimeError,
        match="Connect proof failed checks: provider_removed, provider_restored",
    ):
        require_proof_checks({"provider_restored": False, "provider_removed": False})
