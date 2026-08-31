from __future__ import annotations

import base64
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from eom_email_watcher import entitlement

CONTRACTS_REVISION = "3aef9c78186dca29949c10e4fc129d12ab932cf6"


def encoded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def keyring(key: Ed25519PrivateKey, key_id: str = "test-key") -> bytes:
    return json.dumps(
        {
            "keys": [
                {
                    "key_id": key_id,
                    "algorithm": "Ed25519",
                    "public_key_base64url": encoded(key.public_key().public_bytes_raw()),
                }
            ]
        },
        separators=(",", ":"),
    ).encode()


def claims(
    *,
    not_before: str = "2026-01-01T00:00:00Z",
    expires_at: str = "2027-01-01T00:00:00Z",
    features: list[str] | None = None,
) -> dict[str, object]:
    return {
        "format_version": 1,
        "entitlement_id": "11111111-1111-4111-8111-111111111111",
        "subject": "test-customer",
        "features": features or [entitlement.FEATURE_ID],
        "issued_at": not_before,
        "not_before": not_before,
        "expires_at": expires_at,
    }


def signed_license(
    key: Ed25519PrivateKey,
    payload_claims: dict[str, object],
    key_id: str = "test-key",
) -> bytes:
    payload = json.dumps(payload_claims, separators=(",", ":")).encode()
    return json.dumps(
        {
            "format_version": 1,
            "key_id": key_id,
            "payload_base64url": encoded(payload),
            "signature_base64url": encoded(key.sign(payload)),
        },
        separators=(",", ":"),
    ).encode()


def private_entitlement_path(tmp_path: Path, content: bytes) -> Path:
    directory = tmp_path / "local-connect"
    directory.mkdir(parents=True, mode=0o700)
    directory.chmod(0o700)
    path = directory / entitlement.ENTITLEMENT_FILE_NAME
    path.write_bytes(content)
    path.chmod(0o600)
    return path


def test_signature_feature_and_exact_time_boundaries(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    license_bytes = signed_license(key, claims())
    path = private_entitlement_path(tmp_path, license_bytes)

    def decision(at: str) -> entitlement.EntitlementDecision:
        return entitlement.EntitlementGate.for_test(
            path,
            keyring(key),
            datetime.fromisoformat(at.replace("Z", "+00:00")),
        ).decision()

    assert decision("2026-01-01T00:00:00Z") is entitlement.EntitlementDecision.ACTIVE
    assert decision("2025-12-31T23:59:59Z") is entitlement.EntitlementDecision.NOT_YET_VALID
    assert decision("2027-01-01T00:00:00Z") is entitlement.EntitlementDecision.EXPIRED

    path.write_bytes(
        signed_license(key, claims(features=["document.local_processing"]))
    )
    path.chmod(0o600)
    assert decision("2026-08-31T00:00:00Z") is entitlement.EntitlementDecision.FEATURE_MISSING

    tampered = json.loads(license_bytes)
    tampered["payload_base64url"] = encoded(b"{}")
    path.write_text(json.dumps(tampered), encoding="utf-8")
    path.chmod(0o600)
    assert decision("2026-08-31T00:00:00Z") is entitlement.EntitlementDecision.INVALID


@pytest.mark.skipif(os.name != "posix", reason="Unix permission boundary")
def test_entitlement_file_requires_private_owner_regular_path(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    license_bytes = signed_license(key, claims())
    path = private_entitlement_path(tmp_path, license_bytes)
    gate = entitlement.EntitlementGate.for_test(
        path,
        keyring(key),
        datetime(2026, 8, 31, tzinfo=UTC),
    )
    assert gate.decision() is entitlement.EntitlementDecision.ACTIVE

    path.chmod(0o640)
    assert gate.decision() is entitlement.EntitlementDecision.MISSING

    path.unlink()
    target = tmp_path / "target.json"
    target.write_bytes(license_bytes)
    target.chmod(0o600)
    path.symlink_to(target)
    assert gate.decision() is entitlement.EntitlementDecision.MISSING


def test_installation_uses_bundled_authority_and_ignores_runtime_key_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issuer = Ed25519PrivateKey.generate()
    attacker = Ed25519PrivateKey.generate()
    bundle_root = tmp_path / "bundle"
    bundled_keyring = bundle_root / entitlement.BUNDLED_KEYRING
    bundled_keyring.parent.mkdir(parents=True)
    bundled_keyring.write_bytes(keyring(issuer))
    configured = tmp_path / "config"
    private_entitlement_path(configured, signed_license(issuer, claims()))
    attacker_keyring = tmp_path / "attacker-keyring.json"
    attacker_keyring.write_bytes(keyring(attacker))

    monkeypatch.setattr(entitlement.sys, "_MEIPASS", str(bundle_root), raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(configured))
    monkeypatch.setenv("LOCAL_CONNECT_ENTITLEMENT_KEYRING_FILE", str(attacker_keyring))
    assert entitlement.connect_entitlement_decision() is entitlement.EntitlementDecision.ACTIVE

    monkeypatch.setattr(entitlement.sys, "_MEIPASS", str(tmp_path / "missing-bundle"))
    assert (
        entitlement.connect_entitlement_decision()
        is entitlement.EntitlementDecision.AUTHORITY_UNAVAILABLE
    )


def git_fixture(repository: Path, relative_path: str) -> bytes:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "show",
            f"{CONTRACTS_REVISION}:entitlements/v1/{relative_path}",
        ],
        check=True,
        capture_output=True,
    )
    return result.stdout


@pytest.mark.skipif(
    "CONNECT_CONTRACTS_DIR" not in os.environ,
    reason="requires CONNECT_CONTRACTS_DIR",
)
def test_canonical_entitlement_v1_fixtures() -> None:
    repository = Path(os.environ["CONNECT_CONTRACTS_DIR"])
    keys = entitlement._parse_keyring(git_fixture(repository, "fixtures/test-keyring.json"))
    index = json.loads(git_fixture(repository, "fixtures/index.json"))
    now = datetime.fromisoformat(index["evaluated_at"].replace("Z", "+00:00"))
    for case in index["cases"]:
        decision = entitlement._evaluate_entitlement(
            git_fixture(repository, f"fixtures/{case['fixture']}"),
            keys,
            now,
        )
        assert decision.is_active is case["entitled"], case["fixture"]
