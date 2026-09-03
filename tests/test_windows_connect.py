from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from eom_email_watcher import connect, entitlement
from eom_email_watcher.connect_windows import (
    WINDOWS_LOCK_LENGTH,
    WINDOWS_LOCK_OFFSET,
    WindowsFileLock,
    _current_user_sid,
    local_app_data_root,
    read_bounded_regular_file,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows contract")

TOKEN = "A" * 43
INSTANCE_ID = "11111111-1111-4111-8111-111111111111"


def _make_private_root(path: Path) -> None:
    subprocess.run(
        [
            "icacls",
            str(path),
            "/grant:r",
            f"*{_current_user_sid()}:(OI)(CI)F",
            "*S-1-5-18:(OI)(CI)F",
            "*S-1-5-32-544:(OI)(CI)F",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["icacls", str(path), "/inheritance:r"],
        check=True,
        capture_output=True,
        text=True,
    )


def _contracts_path(relative: str) -> Path:
    root_value = os.environ.get("CONNECT_CONTRACTS_DIR")
    if not root_value:
        raise RuntimeError("CONNECT_CONTRACTS_DIR is required for Windows Connect tests")
    return Path(root_value) / relative


def _entitlement_gate(destination: Path) -> entitlement.EntitlementGate:
    return entitlement.EntitlementGate.for_test(
        destination,
        _contracts_path("entitlements/v1/fixtures/test-keyring.json").read_bytes(),
        datetime(2026, 9, 1, tzinfo=UTC),
    )


def _registration() -> dict[str, object]:
    return {
        "protocol_version": 2,
        "instance_id": INSTANCE_ID,
        "app_id": "website-redesign",
        "pid": os.getpid(),
        "started_at": "2026-09-01T12:00:00+00:00",
        "transport": {
            "kind": "http-loopback-v2",
            "base_url": "http://127.0.0.1:32123/",
        },
        "auth": {"scheme": "bearer", "token": TOKEN},
    }


def _manifest() -> dict[str, object]:
    return {
        "protocol_version": 2,
        "instance_id": INSTANCE_ID,
        "app": {
            "id": "website-redesign",
            "name": "Website Redesign",
            "version": "1.0.0",
        },
        "capabilities": [
            {
                "id": "website.generate",
                "version": "1.0",
                "action": {
                    "label": "Generate website",
                    "description": "Generate a website from a structured specification.",
                },
                "accepts": [
                    {
                        "media_type": "application/vnd.local-connect.website-spec+json",
                        "max_bytes": 1_048_576,
                    }
                ],
                "produces": ["text/html"],
                "parameters": [],
                "effects": {"external": False, "confirmation_required": False},
            }
        ],
    }


def test_windows_default_connect_paths_use_local_app_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual_local_app_data = os.environ.get("LOCALAPPDATA")
    if actual_local_app_data:
        assert local_app_data_root(actual_local_app_data) == Path(actual_local_app_data)
    _make_private_root(tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    root, providers = connect._providers_directory(None, 2)
    installed = entitlement._entitlement_path(None, None, str(tmp_path))

    assert root == tmp_path
    assert providers == tmp_path / "LocalConnect/runtime/v2/providers"
    assert installed == tmp_path / "LocalConnect/entitlement-v1.json"


def test_windows_bounded_reader_rejects_oversized_file(tmp_path: Path) -> None:
    _make_private_root(tmp_path)
    candidate = tmp_path / "oversized.json"
    candidate.write_bytes(b"1234")

    with pytest.raises(OSError):
        read_bounded_regular_file(candidate, 3)


def test_windows_default_discovery_authenticates_provider_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_private_root(tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(
        connect.entitlement,
        "connect_entitlement_decision",
        lambda: entitlement.EntitlementDecision.ACTIVE,
    )
    providers = tmp_path / "LocalConnect/runtime/v2/providers"
    providers.mkdir(parents=True)
    (providers / "website-redesign.json").write_text(
        json.dumps(_registration()),
        encoding="utf-8",
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_manifest())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        catalog = connect.discover_capabilities(client=client)

    assert [item.capability_id for item in catalog.items] == ["website.generate"]
    assert catalog.diagnostic_code is None
    assert len(requests) == 1
    assert requests[0].url == "http://127.0.0.1:32123/v2/manifest"
    assert requests[0].headers["authorization"] == f"Bearer {TOKEN}"


def test_windows_entitlement_install_and_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_private_root(tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    destination = tmp_path / "LocalConnect/entitlement-v1.json"
    source = _contracts_path("entitlements/v1/fixtures/valid/active.json")
    gate = _entitlement_gate(destination)

    status = gate.install(source)

    assert status == entitlement.EntitlementStatus(
        state=entitlement.EntitlementDecision.ACTIVE,
        active=True,
    )
    assert gate.decision() is entitlement.EntitlementDecision.ACTIVE
    assert destination.read_bytes() == source.read_bytes()


def test_windows_entitlement_lock_contention_is_busy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_private_root(tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    destination = tmp_path / "LocalConnect/entitlement-v1.json"
    source = _contracts_path("entitlements/v1/fixtures/valid/active.json")
    gate = _entitlement_gate(destination)
    destination.parent.mkdir(parents=True)
    lock = WindowsFileLock(destination.parent / entitlement.ENTITLEMENT_LOCK_FILE_NAME)
    try:
        assert WINDOWS_LOCK_OFFSET == 0
        assert WINDOWS_LOCK_LENGTH == 1
        assert lock.path.read_bytes() == b"\0"
        with pytest.raises(entitlement.EntitlementInstallError) as failure:
            gate.install(source)
    finally:
        lock.close()

    assert failure.value.code == entitlement.ACTIVATION_BUSY
    assert not destination.exists()


def test_windows_final_validation_failure_restores_previous_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_private_root(tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    destination = tmp_path / "LocalConnect/entitlement-v1.json"
    destination.parent.mkdir(parents=True)
    previous = b"previous-entitlement-bytes"
    destination.write_bytes(previous)
    source = _contracts_path("entitlements/v1/fixtures/valid/active.json")
    gate = _entitlement_gate(destination)
    times = iter(
        [
            datetime(2026, 9, 1, tzinfo=UTC),
            datetime(2026, 9, 1, tzinfo=UTC),
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
    assert destination.read_bytes() == previous


def test_windows_local_app_data_rejects_directory_junction(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    junction = tmp_path / "junction"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        check=True,
        capture_output=True,
        text=True,
    )

    with pytest.raises(OSError):
        local_app_data_root(str(junction))


def test_windows_local_app_data_rejects_broad_read_acl(tmp_path: Path) -> None:
    _make_private_root(tmp_path)
    assert local_app_data_root(str(tmp_path)) == tmp_path
    subprocess.run(
        ["icacls", str(tmp_path), "/grant", "*S-1-1-0:(OI)(CI)R"],
        check=True,
        capture_output=True,
        text=True,
    )

    with pytest.raises(OSError):
        local_app_data_root(str(tmp_path))
