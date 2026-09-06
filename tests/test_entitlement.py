from __future__ import annotations

import base64
import json
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from eom_email_watcher import entitlement

CONTRACTS_REVISION = "c5405935bd1354cf6a4c8539425a53dfd7f52949"


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
    return signed_payload(key, payload, key_id)


def signed_payload(
    key: Ed25519PrivateKey,
    payload: bytes,
    key_id: str = "test-key",
) -> bytes:
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

    path.write_bytes(signed_license(key, claims(features=["document.local_processing"])))
    path.chmod(0o600)
    assert decision("2026-08-31T00:00:00Z") is entitlement.EntitlementDecision.FEATURE_MISSING

    tampered = json.loads(license_bytes)
    tampered["payload_base64url"] = encoded(b"{}")
    path.write_text(json.dumps(tampered), encoding="utf-8")
    path.chmod(0o600)
    assert decision("2026-08-31T00:00:00Z") is entitlement.EntitlementDecision.INVALID


def test_gate_evaluates_each_requested_feature_without_changing_connect_default(
    tmp_path: Path,
) -> None:
    key = Ed25519PrivateKey.generate()
    path = private_entitlement_path(
        tmp_path,
        signed_license(
            key,
            claims(
                features=[
                    entitlement.FEATURE_ID,
                    entitlement.AUTOMATIONS_FEATURE_ID,
                ]
            ),
        ),
    )
    gate = entitlement.EntitlementGate.for_test(
        path,
        keyring(key),
        datetime(2026, 8, 31, tzinfo=UTC),
    )

    assert gate.decision() is entitlement.EntitlementDecision.ACTIVE
    assert (
        gate.decision(entitlement.AUTOMATIONS_FEATURE_ID) is entitlement.EntitlementDecision.ACTIVE
    )
    assert gate.status(entitlement.AUTOMATIONS_FEATURE_ID).public_dict() == {
        "state": "active",
        "active": True,
    }

    path.write_bytes(signed_license(key, claims()))
    path.chmod(0o600)
    assert gate.decision() is entitlement.EntitlementDecision.ACTIVE
    assert (
        gate.decision(entitlement.AUTOMATIONS_FEATURE_ID)
        is entitlement.EntitlementDecision.FEATURE_MISSING
    )


@pytest.mark.parametrize(
    "feature_id",
    ["", "Connect.Automations", "connect.", "a" * 101, 0, False, None],
)
def test_requested_feature_id_is_strictly_validated(
    tmp_path: Path,
    feature_id: object,
) -> None:
    key = Ed25519PrivateKey.generate()
    path = private_entitlement_path(tmp_path, signed_license(key, claims()))
    gate = entitlement.EntitlementGate.for_test(
        path,
        keyring(key),
        datetime(2026, 8, 31, tzinfo=UTC),
    )

    with pytest.raises(ValueError, match="feature ID is invalid"):
        gate.decision(feature_id)  # type: ignore[arg-type]


@pytest.mark.parametrize("feature_id", ["a", "a" * 100])
def test_requested_feature_id_accepts_length_boundaries(
    tmp_path: Path,
    feature_id: str,
) -> None:
    key = Ed25519PrivateKey.generate()
    path = private_entitlement_path(
        tmp_path,
        signed_license(key, claims(features=[feature_id])),
    )
    gate = entitlement.EntitlementGate.for_test(
        path,
        keyring(key),
        datetime(2026, 8, 31, tzinfo=UTC),
    )

    assert gate.decision(feature_id) is entitlement.EntitlementDecision.ACTIVE


def test_duplicate_claim_members_are_rejected_before_authorization(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    payload = (
        b'{"format_version":1,'
        b'"entitlement_id":"11111111-1111-4111-8111-111111111111",'
        b'"subject":"test-customer",'
        b'"features":["document.local_processing"],'
        b'"features":["connect.capability_exchange"],'
        b'"issued_at":"2026-01-01T00:00:00Z",'
        b'"not_before":"2026-01-01T00:00:00Z",'
        b'"expires_at":"2027-01-01T00:00:00Z"}'
    )
    path = private_entitlement_path(tmp_path, signed_payload(key, payload))
    gate = entitlement.EntitlementGate.for_test(
        path,
        keyring(key),
        datetime(2026, 8, 31, tzinfo=UTC),
    )

    assert gate.decision() is entitlement.EntitlementDecision.INVALID


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
    installed = private_entitlement_path(configured, signed_license(issuer, claims()))
    attacker_keyring = tmp_path / "attacker-keyring.json"
    attacker_keyring.write_bytes(keyring(attacker))

    monkeypatch.setattr(entitlement.sys, "_MEIPASS", str(bundle_root), raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(configured))
    monkeypatch.setenv("LOCAL_CONNECT_ENTITLEMENT_KEYRING_FILE", str(attacker_keyring))
    assert entitlement.connect_entitlement_decision() is entitlement.EntitlementDecision.ACTIVE
    assert (
        entitlement.feature_entitlement_decision(entitlement.AUTOMATIONS_FEATURE_ID)
        is entitlement.EntitlementDecision.FEATURE_MISSING
    )

    installed.write_bytes(
        signed_license(
            issuer,
            claims(
                features=[
                    entitlement.CONNECT_FEATURE_ID,
                    entitlement.AUTOMATIONS_FEATURE_ID,
                ]
            ),
        )
    )
    installed.chmod(0o600)
    assert entitlement.feature_entitlement_status(
        entitlement.AUTOMATIONS_FEATURE_ID
    ).public_dict() == {"state": "active", "active": True}

    monkeypatch.setattr(entitlement.sys, "_MEIPASS", str(tmp_path / "missing-bundle"))
    assert (
        entitlement.connect_entitlement_decision()
        is entitlement.EntitlementDecision.AUTHORITY_UNAVAILABLE
    )


def test_empty_xdg_config_home_uses_home_fallback() -> None:
    assert entitlement._entitlement_path("", "/home/test-user") == Path(
        "/home/test-user/.config/local-connect/entitlement-v1.json"
    )


@pytest.mark.skipif(os.name != "posix", reason="Unix activation boundary")
def test_active_install_is_exact_private_and_visible_after_reopen(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    license_bytes = signed_license(key, claims())
    source = tmp_path / "purchased-license.json"
    source.write_bytes(license_bytes)
    source_before = source.read_bytes()
    destination = tmp_path / "config" / "local-connect" / entitlement.ENTITLEMENT_FILE_NAME
    gate = entitlement.EntitlementGate.for_test(
        destination,
        keyring(key),
        datetime(2026, 8, 31, tzinfo=UTC),
    )

    assert gate.status().public_dict() == {"state": "missing", "active": False}
    assert gate.install(source).public_dict() == {"state": "active", "active": True}
    assert source.read_bytes() == source_before
    assert destination.read_bytes() == license_bytes
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    lock = destination.parent / entitlement.ENTITLEMENT_LOCK_FILE_NAME
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    assert stat.S_IMODE(destination.parent.stat().st_mode) == 0o700
    assert not list(destination.parent.glob(f".{entitlement.ENTITLEMENT_FILE_NAME}.tmp.*"))

    reopened = entitlement.EntitlementGate.for_test(
        destination,
        keyring(key),
        datetime(2026, 8, 31, tzinfo=UTC),
    )
    assert reopened.status().public_dict() == {"state": "active", "active": True}


@pytest.mark.skipif(os.name != "posix", reason="Unix activation boundary")
def test_invalid_and_inactive_sources_preserve_existing_entitlement(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    existing = signed_license(
        key,
        claims(expires_at="2028-01-01T00:00:00Z"),
    )
    destination = private_entitlement_path(tmp_path / "config", existing)
    gate = entitlement.EntitlementGate.for_test(
        destination,
        keyring(key),
        datetime(2026, 8, 31, tzinfo=UTC),
    )

    candidates = [
        (b"{}", entitlement.SOURCE_INVALID),
        (
            signed_license(
                key,
                claims(
                    not_before="2025-01-01T00:00:00Z",
                    expires_at="2026-01-01T00:00:00Z",
                ),
            ),
            entitlement.NOT_ACTIVE,
        ),
        (
            signed_license(
                key,
                claims(
                    not_before="2026-09-01T00:00:00Z",
                    expires_at="2027-01-01T00:00:00Z",
                ),
            ),
            entitlement.NOT_ACTIVE,
        ),
        (
            signed_license(
                key,
                claims(features=["document.local_processing"]),
            ),
            entitlement.NOT_ACTIVE,
        ),
    ]
    for index, (candidate, code) in enumerate(candidates):
        source = tmp_path / f"candidate-{index}.json"
        source.write_bytes(candidate)
        with pytest.raises(entitlement.EntitlementInstallError) as failure:
            gate.install(source)
        assert failure.value.code == code
        assert destination.read_bytes() == existing


@pytest.mark.skipif(os.name != "posix", reason="Unix activation boundary")
def test_source_destination_and_authority_boundaries_fail_closed(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    license_bytes = signed_license(key, claims())
    source = tmp_path / "candidate.json"
    source.write_bytes(license_bytes)
    destination = tmp_path / "config" / "local-connect" / entitlement.ENTITLEMENT_FILE_NAME
    gate = entitlement.EntitlementGate.for_test(
        destination,
        keyring(key),
        datetime(2026, 8, 31, tzinfo=UTC),
    )

    for unsafe in [tmp_path / "missing.json", tmp_path / "empty.json"]:
        if unsafe.name == "empty.json":
            unsafe.write_bytes(b"")
        with pytest.raises(entitlement.EntitlementInstallError) as failure:
            gate.install(unsafe)
        assert failure.value.code == entitlement.SOURCE_INVALID

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"x" * (entitlement.MAX_ENTITLEMENT_BYTES + 1))
    with pytest.raises(entitlement.EntitlementInstallError) as failure:
        gate.install(oversized)
    assert failure.value.code == entitlement.SOURCE_INVALID

    linked = tmp_path / "linked.json"
    linked.symlink_to(source)
    with pytest.raises(entitlement.EntitlementInstallError) as failure:
        gate.install(linked)
    assert failure.value.code == entitlement.SOURCE_INVALID

    destination.parent.mkdir(parents=True, mode=0o700)
    destination.write_bytes(b"existing")
    destination.chmod(0o640)
    with pytest.raises(entitlement.EntitlementInstallError) as failure:
        gate.install(source)
    assert failure.value.code == entitlement.STORAGE_UNAVAILABLE
    assert destination.read_bytes() == b"existing"

    no_authority = entitlement.EntitlementGate(
        path=destination,
        keys=None,
        now=datetime(2026, 8, 31, tzinfo=UTC),
    )
    with pytest.raises(entitlement.EntitlementInstallError) as failure:
        no_authority.install(source)
    assert failure.value.code == entitlement.AUTHORITY_UNAVAILABLE


@pytest.mark.skipif(os.name != "posix", reason="Unix activation boundary")
def test_candidate_swap_to_fifo_is_rejected_without_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = Ed25519PrivateKey.generate()
    source = tmp_path / "candidate.json"
    source.write_bytes(signed_license(key, claims()))
    destination = tmp_path / "config" / "local-connect" / entitlement.ENTITLEMENT_FILE_NAME
    gate = entitlement.EntitlementGate.for_test(
        destination,
        keyring(key),
        datetime(2026, 8, 31, tzinfo=UTC),
    )
    original_open = os.open
    swapped = False

    def swap_before_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        *args: object,
        **kwargs: object,
    ) -> int:
        nonlocal swapped
        if Path(path) == source and not swapped:
            source.unlink()
            os.mkfifo(source)
            swapped = True
            assert flags & os.O_NONBLOCK
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_before_open)

    with pytest.raises(entitlement.EntitlementInstallError) as failure:
        gate.install(source)

    assert swapped is True
    assert failure.value.code == entitlement.SOURCE_INVALID
    assert not destination.exists()


@pytest.mark.skipif(os.name != "posix", reason="Unix activation boundary")
def test_unusable_private_directory_mode_is_rejected_before_replacement(
    tmp_path: Path,
) -> None:
    key = Ed25519PrivateKey.generate()
    existing = signed_license(key, claims(expires_at="2028-01-01T00:00:00Z"))
    destination = private_entitlement_path(tmp_path / "config", existing)
    source = tmp_path / "replacement.json"
    source.write_bytes(signed_license(key, claims()))
    gate = entitlement.EntitlementGate.for_test(
        destination,
        keyring(key),
        datetime(2026, 8, 31, tzinfo=UTC),
    )

    destination.parent.chmod(0o300)
    try:
        with pytest.raises(entitlement.EntitlementInstallError) as failure:
            gate.install(source)
        assert failure.value.code == entitlement.STORAGE_UNAVAILABLE
        assert destination.read_bytes() == existing
    finally:
        destination.parent.chmod(0o700)

    assert not list(destination.parent.glob(f".{entitlement.ENTITLEMENT_FILE_NAME}.tmp.*"))


@pytest.mark.skipif(os.name != "posix", reason="Unix activation boundary")
def test_commit_time_revalidation_preserves_existing_entitlement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = Ed25519PrivateKey.generate()
    existing = signed_license(key, claims(expires_at="2028-01-01T00:00:00Z"))
    destination = private_entitlement_path(tmp_path / "config", existing)
    source = tmp_path / "replacement.json"
    source.write_bytes(signed_license(key, claims()))
    gate = entitlement.EntitlementGate.for_test(
        destination,
        keyring(key),
        datetime(2026, 8, 31, tzinfo=UTC),
    )
    times = iter(
        [
            datetime(2026, 8, 31, tzinfo=UTC),
            datetime(2027, 1, 1, tzinfo=UTC),
        ]
    )
    monkeypatch.setattr(
        entitlement.EntitlementGate,
        "_current_time",
        lambda _gate: next(times),
    )

    with pytest.raises(entitlement.EntitlementInstallError) as failure:
        gate.install(source)
    assert failure.value.code == entitlement.NOT_ACTIVE
    assert destination.read_bytes() == existing


@pytest.mark.skipif(os.name != "posix", reason="Unix activation boundary")
def test_final_validation_failure_restores_existing_entitlement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = Ed25519PrivateKey.generate()
    existing = signed_license(key, claims(expires_at="2028-01-01T00:00:00Z"))
    destination = private_entitlement_path(tmp_path / "config", existing)
    source = tmp_path / "replacement.json"
    source.write_bytes(signed_license(key, claims()))
    gate = entitlement.EntitlementGate.for_test(
        destination,
        keyring(key),
        datetime(2026, 8, 31, tzinfo=UTC),
    )
    times = iter(
        [
            datetime(2026, 8, 31, tzinfo=UTC),
            datetime(2026, 8, 31, tzinfo=UTC),
            datetime(2027, 1, 1, tzinfo=UTC),
        ]
    )
    monkeypatch.setattr(
        entitlement.EntitlementGate,
        "_current_time",
        lambda _gate: next(times),
    )

    with pytest.raises(entitlement.EntitlementInstallError) as failure:
        gate.install(source)

    assert failure.value.code == entitlement.INSTALL_FAILED
    assert destination.read_bytes() == existing
    assert not list(destination.parent.glob(f".{entitlement.ENTITLEMENT_FILE_NAME}.tmp.*"))


@pytest.mark.skipif(os.name != "posix", reason="Unix activation boundary")
def test_final_validation_failure_removes_candidate_without_previous_entitlement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = Ed25519PrivateKey.generate()
    destination = tmp_path / "config" / "local-connect" / entitlement.ENTITLEMENT_FILE_NAME
    source = tmp_path / "candidate.json"
    source.write_bytes(signed_license(key, claims()))
    gate = entitlement.EntitlementGate.for_test(
        destination,
        keyring(key),
        datetime(2026, 8, 31, tzinfo=UTC),
    )
    times = iter(
        [
            datetime(2026, 8, 31, tzinfo=UTC),
            datetime(2026, 8, 31, tzinfo=UTC),
            datetime(2027, 1, 1, tzinfo=UTC),
        ]
    )
    monkeypatch.setattr(
        entitlement.EntitlementGate,
        "_current_time",
        lambda _gate: next(times),
    )

    with pytest.raises(entitlement.EntitlementInstallError) as failure:
        gate.install(source)

    assert failure.value.code == entitlement.INSTALL_FAILED
    assert not destination.exists()
    assert not list(destination.parent.glob(f".{entitlement.ENTITLEMENT_FILE_NAME}.tmp.*"))


@pytest.mark.skipif(os.name != "posix", reason="Unix activation boundary")
def test_post_promotion_directory_sync_failure_restores_existing_entitlement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = Ed25519PrivateKey.generate()
    existing = signed_license(key, claims(expires_at="2028-01-01T00:00:00Z"))
    destination = private_entitlement_path(tmp_path / "config", existing)
    source = tmp_path / "replacement.json"
    source.write_bytes(signed_license(key, claims()))
    gate = entitlement.EntitlementGate.for_test(
        destination,
        keyring(key),
        datetime(2026, 8, 31, tzinfo=UTC),
    )
    original_sync = entitlement._sync_directory
    sync_calls = 0

    def fail_first_sync(path: Path) -> None:
        nonlocal sync_calls
        sync_calls += 1
        if sync_calls == 1:
            raise entitlement._install_error(entitlement.STORAGE_UNAVAILABLE)
        original_sync(path)

    monkeypatch.setattr(entitlement, "_sync_directory", fail_first_sync)

    with pytest.raises(entitlement.EntitlementInstallError) as failure:
        gate.install(source)

    assert failure.value.code == entitlement.INSTALL_FAILED
    assert sync_calls == 2
    assert destination.read_bytes() == existing
    assert not list(destination.parent.glob(f".{entitlement.ENTITLEMENT_FILE_NAME}.tmp.*"))


@pytest.mark.skipif(os.name != "posix", reason="Unix activation boundary")
def test_post_promotion_directory_sync_failure_removes_candidate_without_previous_entitlement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = Ed25519PrivateKey.generate()
    source = tmp_path / "candidate.json"
    source.write_bytes(signed_license(key, claims()))
    destination = tmp_path / "config" / "local-connect" / entitlement.ENTITLEMENT_FILE_NAME
    destination.parent.mkdir(parents=True, mode=0o700)
    destination.parent.chmod(0o700)
    gate = entitlement.EntitlementGate.for_test(
        destination,
        keyring(key),
        datetime(2026, 8, 31, tzinfo=UTC),
    )
    original_sync = entitlement._sync_directory
    sync_calls = 0

    def fail_first_sync(path: Path) -> None:
        nonlocal sync_calls
        sync_calls += 1
        if sync_calls == 1:
            raise entitlement._install_error(entitlement.STORAGE_UNAVAILABLE)
        original_sync(path)

    monkeypatch.setattr(entitlement, "_sync_directory", fail_first_sync)

    with pytest.raises(entitlement.EntitlementInstallError) as failure:
        gate.install(source)

    assert failure.value.code == entitlement.INSTALL_FAILED
    assert sync_calls == 2
    assert not destination.exists()
    assert not list(destination.parent.glob(f".{entitlement.ENTITLEMENT_FILE_NAME}.tmp.*"))


@pytest.mark.skipif(os.name != "posix", reason="Unix activation boundary")
def test_new_private_directories_sync_their_parent_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = Ed25519PrivateKey.generate()
    source = tmp_path / "candidate.json"
    source.write_bytes(signed_license(key, claims()))
    destination = tmp_path / "config" / "local-connect" / entitlement.ENTITLEMENT_FILE_NAME
    gate = entitlement.EntitlementGate.for_test(
        destination,
        keyring(key),
        datetime(2026, 8, 31, tzinfo=UTC),
    )
    synced: list[Path] = []
    original_sync = entitlement._sync_directory

    def record_sync(path: Path) -> None:
        synced.append(path)
        original_sync(path)

    monkeypatch.setattr(entitlement, "_sync_directory", record_sync)

    assert gate.install(source).active is True
    assert synced[:4] == [
        destination.parent.parent,
        tmp_path,
        destination.parent,
        destination.parent.parent,
    ]


@pytest.mark.skipif(os.name != "posix", reason="Unix activation boundary")
def test_held_lock_blocks_installer_and_child_process(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    source = tmp_path / "candidate.json"
    source.write_bytes(signed_license(key, claims()))
    destination = tmp_path / "config" / "local-connect" / entitlement.ENTITLEMENT_FILE_NAME
    gate = entitlement.EntitlementGate.for_test(
        destination,
        keyring(key),
        datetime(2026, 8, 31, tzinfo=UTC),
    )
    entitlement._ensure_private_directory(destination.parent)

    with entitlement._activation_lock(destination.parent):
        with pytest.raises(entitlement.EntitlementInstallError) as failure:
            gate.install(source)
        assert failure.value.code == entitlement.ACTIVATION_BUSY
        assert not destination.exists()
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; from pathlib import Path; "
                    "from eom_email_watcher import entitlement as e; "
                    "\ntry:\n"
                    "    with e._activation_lock(Path(sys.argv[1])):\n"
                    "        raise SystemExit(4)\n"
                    "except e.EntitlementInstallError as exc:\n"
                    "    raise SystemExit(0 if exc.code == e.ACTIVATION_BUSY else 3)\n"
                ),
                str(destination.parent),
            ],
            check=False,
        )
        assert child.returncode == 0

    assert gate.install(source).active is True


@pytest.mark.skipif(os.name != "posix", reason="Unix activation boundary")
def test_write_and_temp_preparation_failures_roll_back_and_clean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = Ed25519PrivateKey.generate()
    existing = signed_license(key, claims(expires_at="2028-01-01T00:00:00Z"))
    destination = private_entitlement_path(tmp_path / "config", existing)
    source = tmp_path / "replacement.json"
    source.write_bytes(signed_license(key, claims()))
    gate = entitlement.EntitlementGate.for_test(
        destination,
        keyring(key),
        datetime(2026, 8, 31, tzinfo=UTC),
    )

    monkeypatch.setattr(entitlement.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError()))
    with pytest.raises(entitlement.EntitlementInstallError) as failure:
        gate.install(source)
    assert failure.value.code == entitlement.INSTALL_FAILED
    assert destination.read_bytes() == existing
    assert not list(destination.parent.glob(f".{entitlement.ENTITLEMENT_FILE_NAME}.tmp.*"))

    monkeypatch.undo()
    original_fchmod = entitlement.os.fchmod
    calls = 0

    def fail_temp_fchmod(descriptor: int, mode: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected temp permission failure")
        original_fchmod(descriptor, mode)

    monkeypatch.setattr(entitlement.os, "fchmod", fail_temp_fchmod)
    with pytest.raises(entitlement.EntitlementInstallError) as failure:
        gate.install(source)
    assert failure.value.code == entitlement.INSTALL_FAILED
    assert destination.read_bytes() == existing
    assert not list(destination.parent.glob(f".{entitlement.ENTITLEMENT_FILE_NAME}.tmp.*"))


@pytest.mark.skipif(os.name != "posix", reason="Unix activation boundary")
def test_installer_sets_exact_modes_under_restrictive_umask(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    source = tmp_path / "candidate.json"
    source.write_bytes(signed_license(key, claims()))
    destination = tmp_path / "config" / "local-connect" / entitlement.ENTITLEMENT_FILE_NAME
    gate = entitlement.EntitlementGate.for_test(
        destination,
        keyring(key),
        datetime(2026, 8, 31, tzinfo=UTC),
    )

    previous = os.umask(0o777)
    try:
        assert gate.install(source).active is True
    finally:
        os.umask(previous)

    assert stat.S_IMODE(destination.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert (
        stat.S_IMODE((destination.parent / entitlement.ENTITLEMENT_LOCK_FILE_NAME).stat().st_mode)
        == 0o600
    )


def test_public_status_and_install_failure_codes_are_stable_and_claim_free() -> None:
    status = entitlement.EntitlementStatus.from_decision(entitlement.EntitlementDecision.EXPIRED)
    assert status.public_dict() == {"state": "expired", "active": False}
    assert set(status.public_dict()) == {"state", "active"}
    assert set(entitlement._INSTALL_ERROR_MESSAGES) == {
        "CONNECT_ENTITLEMENT_AUTHORITY_UNAVAILABLE",
        "CONNECT_ENTITLEMENT_SOURCE_INVALID",
        "CONNECT_ENTITLEMENT_NOT_ACTIVE",
        "CONNECT_ENTITLEMENT_STORAGE_UNAVAILABLE",
        "CONNECT_ENTITLEMENT_ACTIVATION_BUSY",
        "CONNECT_ENTITLEMENT_INSTALL_FAILED",
    }


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
