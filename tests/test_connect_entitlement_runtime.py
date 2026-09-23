"""Exercise the installed shared verifier through Email Watcher's Connect dependency."""

from __future__ import annotations

import base64
import json
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from connect_automate import connect, entitlement
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from connect_reference_provider import REFERENCE_APP_ID, ReferenceProvider  # noqa: E402


def _encoded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _stamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _license(
    key: Ed25519PrivateKey,
    *,
    not_before: datetime,
    expires_at: datetime,
    features: list[str] | None = None,
) -> bytes:
    payload = json.dumps(
        {
            "format_version": 1,
            "entitlement_id": "11111111-1111-4111-8111-111111111111",
            "subject": "test-customer",
            "features": features if features is not None else [entitlement.CONNECT_FEATURE_ID],
            "issued_at": _stamp(not_before),
            "not_before": _stamp(not_before),
            "expires_at": _stamp(expires_at),
        },
        separators=(",", ":"),
    ).encode()
    return json.dumps(
        {
            "format_version": 1,
            "key_id": "test-key",
            "payload_base64url": _encoded(payload),
            "signature_base64url": _encoded(key.sign(payload)),
        },
        separators=(",", ":"),
    ).encode()


def _install_bundle_keyring(root: Path, key: Ed25519PrivateKey) -> None:
    path = root / entitlement.BUNDLED_KEYRING
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "keys": [
                    {
                        "key_id": "test-key",
                        "algorithm": "Ed25519",
                        "public_key_base64url": _encoded(key.public_key().public_bytes_raw()),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX entitlement and runtime fixture")
def test_signed_install_controls_discovery_without_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = Ed25519PrivateKey.generate()
    bundle = tmp_path / "bundle"
    _install_bundle_keyring(bundle, key)
    home = tmp_path / "home"
    config = home / ".config"
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(entitlement.sys, "_MEIPASS", str(bundle), raising=False)

    now = datetime.now(UTC).replace(microsecond=0)
    active = _license(key, not_before=now - timedelta(days=1), expires_at=now + timedelta(days=1))
    source = tmp_path / "selected-license.json"
    source.write_bytes(active)
    source.chmod(0o600)
    assert entitlement.install_connect_entitlement(source).active
    installed = config / "local-connect" / entitlement.ENTITLEMENT_FILE_NAME
    assert installed.read_bytes() == active
    assert stat.S_IMODE(installed.stat().st_mode) == 0o600
    assert stat.S_IMODE(installed.parent.stat().st_mode) == 0o700

    provider = ReferenceProvider.start(runtime)
    try:
        def visible() -> bool:
            return any(
                item.app_id == REFERENCE_APP_ID
                for item in connect.discover_capabilities(runtime).items
            )

        assert entitlement.connect_entitlement_decision() is entitlement.EntitlementDecision.ACTIVE
        assert visible()

        cases = [
            (
                _license(
                    key,
                    not_before=now - timedelta(days=3),
                    expires_at=now - timedelta(days=1),
                ),
                entitlement.EntitlementDecision.EXPIRED,
            ),
            (
                _license(
                    key,
                    not_before=now - timedelta(days=1),
                    expires_at=now + timedelta(days=1),
                    features=["document.local_processing"],
                ),
                entitlement.EntitlementDecision.FEATURE_MISSING,
            ),
            (
                _license(
                    Ed25519PrivateKey.generate(),
                    not_before=now - timedelta(days=1),
                    expires_at=now + timedelta(days=1),
                ),
                entitlement.EntitlementDecision.INVALID,
            ),
        ]
        for content, expected in cases:
            installed.write_bytes(content)
            installed.chmod(0o600)
            assert entitlement.connect_entitlement_decision() is expected
            assert not visible()

        installed.write_bytes(active)
        installed.chmod(0o600)
        assert entitlement.connect_entitlement_decision() is entitlement.EntitlementDecision.ACTIVE
        assert visible()
    finally:
        provider.stop()


def test_signed_verifier_enforces_exact_time_boundaries(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    path = tmp_path / entitlement.ENTITLEMENT_FILE_NAME
    path.write_bytes(
        _license(
            key,
            not_before=datetime(2026, 1, 1, tzinfo=UTC),
            expires_at=datetime(2027, 1, 1, tzinfo=UTC),
        )
    )
    path.chmod(0o600)
    keyring = tmp_path / "bundle"
    _install_bundle_keyring(keyring, key)
    keyring_bytes = (keyring / entitlement.BUNDLED_KEYRING).read_bytes()

    for at, expected in (
        (
            datetime(2025, 12, 31, 23, 59, 59, tzinfo=UTC),
            entitlement.EntitlementDecision.NOT_YET_VALID,
        ),
        (datetime(2026, 1, 1, tzinfo=UTC), entitlement.EntitlementDecision.ACTIVE),
        (datetime(2027, 1, 1, tzinfo=UTC), entitlement.EntitlementDecision.EXPIRED),
    ):
        assert entitlement.EntitlementGate.for_test(path, keyring_bytes, at).decision() is expected
