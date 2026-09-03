from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from eom_email_watcher import connect, entitlement
from eom_email_watcher.connect_windows import (
    WINDOWS_LOCK_LENGTH,
    WINDOWS_LOCK_OFFSET,
    WindowsFileLock,
    _protect_windows_directory,
    atomic_replace_bytes,
    ensure_private_directory,
    local_app_data_root,
    read_bounded_regular_file,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows contract")

TOKEN = "A" * 43
INSTANCE_ID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def private_root() -> Iterator[Path]:
    actual_local_app_data = os.environ.get("LOCALAPPDATA")
    if not actual_local_app_data:
        pytest.skip("LOCALAPPDATA is required for native Windows tests")
    with tempfile.TemporaryDirectory(dir=actual_local_app_data) as directory:
        root = Path(directory)
        _protect_windows_directory(root)
        assert local_app_data_root(str(root)) == root
        yield root


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


def _registration_v1() -> dict[str, object]:
    registration = _registration()
    registration["protocol_version"] = 1
    registration["app_id"] = "document-summarizer"
    registration["transport"] = {
        "kind": "http-loopback-v1",
        "base_url": "http://127.0.0.1:32124/",
    }
    return registration


def _manifest_v1() -> dict[str, object]:
    return {
        "protocol_version": 1,
        "instance_id": INSTANCE_ID,
        "app": {
            "id": "document-summarizer",
            "name": "Document Summarizer",
            "version": "1.0.0",
        },
        "capabilities": [
            {
                "id": "document.summarize",
                "version": "1.0",
                "accepts": [{"media_type": "application/pdf", "max_bytes": 1024}],
                "produces": ["application/vnd.local-connect.document-summary+json"],
            }
        ],
    }


def test_windows_default_connect_paths_use_local_app_data(
    private_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual_local_app_data = os.environ.get("LOCALAPPDATA")
    if actual_local_app_data:
        assert local_app_data_root(actual_local_app_data) == Path(actual_local_app_data)
    monkeypatch.setenv("LOCALAPPDATA", str(private_root))

    root, providers = connect._providers_directory(None, 2)
    installed = entitlement._entitlement_path(None, None, str(private_root))

    assert root == private_root
    assert providers == private_root / "LocalConnect/runtime/v2/providers"
    assert installed == private_root / "LocalConnect/entitlement-v1.json"


def test_windows_bounded_reader_rejects_oversized_file(private_root: Path) -> None:
    candidate = private_root / "oversized.json"
    candidate.write_bytes(b"1234")

    with pytest.raises(OSError):
        read_bounded_regular_file(candidate, 3)


def test_windows_private_reader_rejects_reparse_point_ancestor(
    private_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(private_root))
    target = ensure_private_directory(private_root / "target", root=private_root)
    candidate = target / "entitlement.json"
    candidate.write_text("{}", encoding="utf-8")
    junction = private_root / "LocalConnect"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        check=True,
        capture_output=True,
        text=True,
    )

    with pytest.raises(OSError):
        read_bounded_regular_file(junction / candidate.name, 16)


def test_windows_atomic_replacement_retries_a_short_lived_reader(
    private_root: Path,
) -> None:
    destination = private_root / "replaceable.json"
    atomic_replace_bytes(destination, b"old", 16)

    descriptor = os.open(destination, os.O_RDONLY)

    def release_reader() -> None:
        time.sleep(0.05)
        os.close(descriptor)

    release = threading.Thread(target=release_reader)
    release.start()
    try:
        atomic_replace_bytes(destination, b"new", 16)
    finally:
        release.join(timeout=1)

    assert not release.is_alive()
    assert destination.read_bytes() == b"new"


def test_windows_atomic_replacement_reclaims_only_safe_fixed_temporary(
    private_root: Path,
) -> None:
    destination = private_root / "entitlement-v1.json"
    temporary = private_root / ".entitlement-v1.json.tmp"
    temporary.write_bytes(b"crash residue")

    atomic_replace_bytes(destination, b"published", 32)

    assert destination.read_bytes() == b"published"
    assert not temporary.exists()

    temporary.mkdir()
    with pytest.raises(OSError):
        atomic_replace_bytes(destination, b"replacement", 32)
    assert destination.read_bytes() == b"published"
    assert temporary.is_dir()


def test_windows_registration_candidate_scan_is_bounded(private_root: Path) -> None:
    providers = private_root / "providers"
    providers.mkdir()
    for index in range(connect.MAX_WINDOWS_REGISTRATION_ENTRIES - 4):
        (providers / f"{index:03}.json").write_text("{}", encoding="utf-8")
    (providers / "upper.JSON").write_text("{}", encoding="utf-8")
    (providers / "counted-directory.json").mkdir()
    (providers / "ignored.tmp").write_text("not a registration", encoding="utf-8")
    (providers / "durable.lock").write_text("lock byte", encoding="utf-8")

    candidates = connect._registration_candidates(providers)

    assert candidates is not None
    assert len(candidates) == connect.MAX_WINDOWS_REGISTRATION_ENTRIES - 2
    assert providers / "upper.JSON" in candidates
    assert providers / "counted-directory.json" in candidates
    (providers / "overflow.tmp").write_text("not a registration", encoding="utf-8")
    assert connect._registration_candidates(providers) is None


def test_windows_default_discovery_authenticates_provider_manifest(
    private_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(private_root))
    monkeypatch.setattr(
        connect.entitlement,
        "connect_entitlement_decision",
        lambda: entitlement.EntitlementDecision.ACTIVE,
    )
    providers = private_root / "LocalConnect/runtime/v2/providers"
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


def test_windows_explicit_runtime_root_reaches_v1_and_v2_registrations(
    private_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    explicit_root = ensure_private_directory(private_root / "explicit", root=private_root)
    ambient_root = ensure_private_directory(private_root / "ambient", root=private_root)
    monkeypatch.setenv("LOCALAPPDATA", str(ambient_root))
    monkeypatch.setattr(
        connect.entitlement,
        "connect_entitlement_decision",
        lambda: entitlement.EntitlementDecision.ACTIVE,
    )
    providers_v1 = ensure_private_directory(
        explicit_root / "local-connect/v1/providers",
        root=explicit_root,
    )
    providers_v2 = ensure_private_directory(
        explicit_root / "local-connect/v2/providers",
        root=explicit_root,
    )
    (providers_v1 / "document-summarizer.json").write_text(
        json.dumps(_registration_v1()),
        encoding="utf-8",
    )
    (providers_v2 / "website-redesign.json").write_text(
        json.dumps(_registration()),
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.port == 32124:
            return httpx.Response(200, json=_manifest_v1())
        return httpx.Response(200, json=_manifest())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        v1 = connect.discover_summary_capability(explicit_root, client=client)
        v2 = connect.discover_capabilities(explicit_root, client=client)

    assert v1.provider is not None
    assert [item.capability_id for item in v2.items] == ["website.generate"]


def test_windows_entitlement_install_and_status(
    private_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(private_root))
    destination = private_root / "LocalConnect/entitlement-v1.json"
    source = _contracts_path("entitlements/v1/fixtures/valid/active.json")
    gate = _entitlement_gate(destination)

    status = gate.install(source)

    assert status == entitlement.EntitlementStatus(
        state=entitlement.EntitlementDecision.ACTIVE,
        active=True,
    )
    assert gate.decision() is entitlement.EntitlementDecision.ACTIVE
    assert destination.read_bytes() == source.read_bytes()
    subprocess.run(
        ["icacls", str(destination), "/grant", "*S-1-1-0:R"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert gate.decision() is entitlement.EntitlementDecision.MISSING


def test_windows_entitlement_lock_contention_is_busy(
    private_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(private_root))
    destination = private_root / "LocalConnect/entitlement-v1.json"
    source = _contracts_path("entitlements/v1/fixtures/valid/active.json")
    gate = _entitlement_gate(destination)
    destination.parent.mkdir(parents=True)
    lock = WindowsFileLock(destination.parent / entitlement.ENTITLEMENT_LOCK_FILE_NAME)
    try:
        assert WINDOWS_LOCK_OFFSET == 0
        assert WINDOWS_LOCK_LENGTH == 1
        with pytest.raises(entitlement.EntitlementInstallError) as failure:
            gate.install(source)
    finally:
        lock.close()

    assert lock.path.read_bytes() == b"\0"
    assert failure.value.code == entitlement.ACTIVATION_BUSY
    assert not destination.exists()


def test_windows_final_validation_failure_restores_previous_bytes(
    private_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(private_root))
    destination = private_root / "LocalConnect/entitlement-v1.json"
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


def test_windows_local_app_data_rejects_broad_read_acl(private_root: Path) -> None:
    subprocess.run(
        ["icacls", str(private_root), "/grant", "*S-1-1-0:(OI)(CI)R"],
        check=True,
        capture_output=True,
        text=True,
    )

    with pytest.raises(OSError):
        local_app_data_root(str(private_root))


def test_windows_owner_rights_ace_is_not_an_untrusted_principal(
    private_root: Path,
) -> None:
    subprocess.run(
        ["icacls", str(private_root), "/grant", "*S-1-3-4:(OI)(CI)F"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert local_app_data_root(str(private_root)) == private_root
