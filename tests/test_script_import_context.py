from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "filename",
    (
        "test_desktop_packaging.py",
        "test_connect_packaged_deb_proof.py",
        "test_connect_local_proof.py",
        "test_connect_entitlement_runtime.py",
        "test_connect_reference_provider.py",
        "test_coi_local_proof.py",
    ),
)
def test_script_tests_collect_without_other_modules(tmp_path, filename):
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(root / "src")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", str(root / "tests" / filename)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
