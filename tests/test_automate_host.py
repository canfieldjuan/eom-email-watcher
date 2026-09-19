from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from connect_automate import entitlement
from connect_automate.automate import (
    REQUIRED_FEATURES,
    AutomateHost,
    AutomateLicenseError,
)

# A clock inside the validity window used by the licenses below.
NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _encoded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _keyring(key: Ed25519PrivateKey, key_id: str = "test-key") -> bytes:
    return json.dumps(
        {
            "keys": [
                {
                    "key_id": key_id,
                    "algorithm": "Ed25519",
                    "public_key_base64url": _encoded(key.public_key().public_bytes_raw()),
                }
            ]
        },
        separators=(",", ":"),
    ).encode()


def _signed_license(
    key: Ed25519PrivateKey,
    features: list[str],
    *,
    not_before: str = "2026-01-01T00:00:00Z",
    expires_at: str = "2027-01-01T00:00:00Z",
    key_id: str = "test-key",
) -> bytes:
    payload = json.dumps(
        {
            "format_version": 1,
            "entitlement_id": "11111111-1111-4111-8111-111111111111",
            "subject": "test-customer",
            "features": features,
            "issued_at": not_before,
            "not_before": not_before,
            "expires_at": expires_at,
        },
        separators=(",", ":"),
    ).encode()
    return json.dumps(
        {
            "format_version": 1,
            "key_id": key_id,
            "payload_base64url": _encoded(payload),
            "signature_base64url": _encoded(key.sign(payload)),
        },
        separators=(",", ":"),
    ).encode()


def _entitlement_path(tmp_path: Path, content: bytes | None) -> Path:
    directory = tmp_path / "local-connect"
    directory.mkdir(parents=True, mode=0o700)
    directory.chmod(0o700)
    path = directory / entitlement.ENTITLEMENT_FILE_NAME
    if content is not None:
        path.write_bytes(content)
        path.chmod(0o600)
    return path


def _host(tmp_path: Path, features: list[str] | None, **license_kwargs: str) -> AutomateHost:
    key = Ed25519PrivateKey.generate()
    content = None if features is None else _signed_license(key, features, **license_kwargs)
    gate = entitlement.EntitlementGate.for_test(
        path=_entitlement_path(tmp_path, content),
        keyring_document=_keyring(key),
        now=NOW,
    )
    return AutomateHost(entitlement=gate)


def test_required_features_are_capability_exchange_and_automations() -> None:
    assert REQUIRED_FEATURES == (
        entitlement.CONNECT_FEATURE_ID,
        entitlement.AUTOMATIONS_FEATURE_ID,
    )


def test_host_licensed_with_both_features(tmp_path: Path) -> None:
    host = _host(
        tmp_path,
        [entitlement.CONNECT_FEATURE_ID, entitlement.AUTOMATIONS_FEATURE_ID],
    )
    assert host.licensed() is True
    host.require_license()
    assert host.start() is host


def test_host_refuses_without_automations_feature(tmp_path: Path) -> None:
    host = _host(tmp_path, [entitlement.CONNECT_FEATURE_ID])
    assert host.licensed() is False
    with pytest.raises(AutomateLicenseError):
        host.require_license()
    with pytest.raises(AutomateLicenseError):
        host.start()


def test_host_refuses_with_only_automations_feature(tmp_path: Path) -> None:
    host = _host(tmp_path, [entitlement.AUTOMATIONS_FEATURE_ID])
    assert host.licensed() is False
    with pytest.raises(AutomateLicenseError):
        host.require_license()


def test_host_refuses_when_entitlement_missing(tmp_path: Path) -> None:
    host = _host(tmp_path, None)
    assert host.licensed() is False
    with pytest.raises(AutomateLicenseError):
        host.require_license()


def test_host_refuses_when_entitlement_expired(tmp_path: Path) -> None:
    host = _host(
        tmp_path,
        [entitlement.CONNECT_FEATURE_ID, entitlement.AUTOMATIONS_FEATURE_ID],
        not_before="2025-01-01T00:00:00Z",
        expires_at="2025-06-01T00:00:00Z",
    )
    assert host.licensed() is False
    with pytest.raises(AutomateLicenseError):
        host.require_license()
