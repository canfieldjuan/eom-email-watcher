"""The executable proof must not report success on missing or false evidence."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "coi_local_proof", Path(__file__).resolve().parents[1] / "scripts" / "coi_local_proof.py"
)
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)


def complete_checks():
    return dict.fromkeys(
        (
            "one_fire",
            "one_attempt",
            "one_job",
            "expected_terminal",
            "expected_projection",
            "replay_unchanged",
            "restart_unchanged",
        ),
        True,
    )


def test_complete_proof_is_accepted():
    proof.require_checks(complete_checks())


@pytest.mark.parametrize("value", [False, None, 0, 1, "", "true"])
def test_non_boolean_or_false_proof_is_rejected(value):
    checks = complete_checks()
    checks["expected_projection"] = value
    with pytest.raises(RuntimeError, match="Incomplete or failed"):
        proof.require_checks(checks)


@pytest.mark.parametrize("key", list(complete_checks()))
def test_missing_proof_is_rejected(key):
    checks = complete_checks()
    del checks[key]
    with pytest.raises(RuntimeError, match="Incomplete or failed"):
        proof.require_checks(checks)


def test_staged_authority_uses_shared_package_namespace(tmp_path, monkeypatch):
    from connect_automate import entitlement
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from test_connect_entitlement_runtime import _install_bundle_keyring

    key = Ed25519PrivateKey.generate()
    source = tmp_path / "source"
    _install_bundle_keyring(source, key)
    monkeypatch.setattr(entitlement.sys, "_MEIPASS", "", raising=False)
    proof.stage_authority(tmp_path / "bundle", source / entitlement.BUNDLED_KEYRING)
    gate = entitlement.EntitlementGate.from_installation()
    assert dict(gate.keys) == {"test-key": key.public_key().public_bytes_raw()}
