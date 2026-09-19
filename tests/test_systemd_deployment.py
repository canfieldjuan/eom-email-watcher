import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from connect_automate import entitlement

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install-user-services.sh"
WATCHER_SERVICE = ROOT / "systemd" / "eom-email-watcher.service"
MONTHLY_SERVICE = ROOT / "systemd" / "eom-monthly-hours.service"


def test_installer_snapshots_cli_before_installing_units() -> None:
    script = INSTALLER.read_text()

    locked_export = 'uv export --project "$repo_dir" --locked --no-dev --no-emit-project'
    snapshot = (
        'uv tool install --force --reinstall --constraints "$constraints_file" "$repo_dir"'
    )
    unit_install = 'install -m 0644 "$repo_dir/systemd/eom-email-watcher.service"'
    assert locked_export in script
    assert snapshot in script
    assert script.index(locked_export) < script.index(snapshot)
    assert script.index(snapshot) < script.index(unit_install)
    assert 'test -x "$tool_bin_dir/eom-mail-watch"' in script
    assert '[[ "$release_keyring_source" != /* ]]' in script
    assert 'uv run --project "$repo_dir" --no-dev --locked python -c' in script
    assert "validate_entitlement_keyring" in script
    stage = 'install -m 0600 "$release_keyring_input" "$release_keyring_stage"'
    promote = 'mv -f "$release_keyring_stage" "$release_keyring_target"'
    assert stage in script
    assert promote in script
    assert script.index(stage) < script.index(promote)
    assert '$tool_bin_dir/eom-mail-watch setup' in script


def test_systemd_services_use_stable_cli_snapshot() -> None:
    watcher = WATCHER_SERVICE.read_text()
    monthly = MONTHLY_SERVICE.read_text()

    assert "ExecStart=%h/.local/bin/eom-mail-watch check" in watcher
    assert "ExecStart=%h/.local/bin/eom-mail-watch send-hours" in monthly
    assert "/eom-email-watcher/.venv/" not in watcher
    assert "/eom-email-watcher/.venv/" not in monthly
    working_directory = "WorkingDirectory=%h/Desktop/01 - Effingham Office Maids/eom-email-watcher"
    assert working_directory in watcher
    assert working_directory in monthly


LEGACY_RELEASE_KEYRING = Path(".local/share/eom-email-watcher/connect-entitlement-keyring.json")
FAKE_UV = """#!/usr/bin/env bash
set -euo pipefail
case "$1" in
  export)
    while [[ $# -gt 0 ]]; do
      if [[ "$1" == --output-file ]]; then
        : > "$2"
      fi
      shift
    done
    ;;
  tool)
    mkdir -p "$UV_TOOL_BIN_DIR"
    printf '#!/bin/sh\\n' > "$UV_TOOL_BIN_DIR/eom-mail-watch"
    chmod 0755 "$UV_TOOL_BIN_DIR/eom-mail-watch"
    ;;
  run)
    while [[ "$1" != python ]]; do
      shift
    done
    shift
    exec "$INSTALLER_TEST_PYTHON" "$@"
    ;;
  *)
    exit 97
    ;;
esac
"""
FAKE_SYSTEMCTL = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$INSTALLER_TEST_SYSTEMCTL_LOG"
"""
posix_installer = pytest.mark.skipif(
    os.name != "posix" or shutil.which("bash") is None,
    reason="the service installer is a POSIX shell script",
)


def _keyring_document(authorities: list[tuple[str, bytes]]) -> bytes:
    return json.dumps(
        {
            "keys": [
                {
                    "key_id": key_id,
                    "algorithm": "Ed25519",
                    "public_key_base64url": base64.urlsafe_b64encode(public_key)
                    .rstrip(b"=")
                    .decode(),
                }
                for key_id, public_key in authorities
            ]
        }
    ).encode()


def _approved_keyring() -> bytes:
    return _keyring_document(sorted(entitlement.APPROVED_RELEASE_AUTHORITIES))


def _unapproved_keyring() -> bytes:
    (key_id, _), *_ = sorted(entitlement.APPROVED_RELEASE_AUTHORITIES)
    public_key = (
        Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    )
    return _keyring_document([(key_id, public_key)])


def _write_private(path: Path, content: bytes) -> Path:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o600)
    return path


def _run_installer(
    tmp_path: Path, home: Path, keyring_source: Path | None = None
) -> subprocess.CompletedProcess[str]:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    for name, body in (("uv", FAKE_UV), ("systemctl", FAKE_SYSTEMCTL)):
        tool = fake_bin / name
        tool.write_text(body)
        tool.chmod(0o755)
    home.mkdir(exist_ok=True)
    environment = {
        "HOME": str(home),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "TMPDIR": str(tmp_path),
        "INSTALLER_TEST_PYTHON": sys.executable,
        "INSTALLER_TEST_SYSTEMCTL_LOG": str(tmp_path / "systemctl.log"),
    }
    if keyring_source is not None:
        environment["LOCAL_CONNECT_ENTITLEMENT_KEYRING_FILE"] = str(keyring_source)
    return subprocess.run(
        ["bash", str(INSTALLER)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _runtime_authority(home: Path, monkeypatch: pytest.MonkeyPatch) -> object:
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delattr(entitlement.sys, "_MEIPASS", raising=False)
    return entitlement._load_installed_release_keyring()


@posix_installer
def test_installer_places_release_authority_where_the_runtime_reads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    source = _write_private(tmp_path / "release" / "keyring.json", _approved_keyring())

    result = _run_installer(tmp_path, home, source)

    assert result.returncode == 0, result.stderr
    assert _runtime_authority(home, monkeypatch) == dict(entitlement.APPROVED_RELEASE_AUTHORITIES)
    installed = entitlement._installed_release_keyring_path(str(home))
    assert installed is not None
    assert installed.stat().st_mode & 0o777 == 0o600
    assert installed.parent.stat().st_mode & 0o777 == 0o700
    assert [child.name for child in installed.parent.iterdir()] == [installed.name]
    assert not (home / LEGACY_RELEASE_KEYRING).exists()
    assert "Installed the approved Connect release authority" in result.stdout
    assert "--user daemon-reload" in (tmp_path / "systemctl.log").read_text()


@posix_installer
def test_installer_migrates_a_legacy_release_authority_without_removing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    legacy = _write_private(home / LEGACY_RELEASE_KEYRING, _approved_keyring())
    sibling = legacy.parent / "backups"
    sibling.mkdir(mode=0o700)

    result = _run_installer(tmp_path, home)

    assert result.returncode == 0, result.stderr
    assert _runtime_authority(home, monkeypatch) == dict(entitlement.APPROVED_RELEASE_AUTHORITIES)
    assert legacy.read_bytes() == _approved_keyring()
    assert sibling.is_dir()
    assert "Migrated the Connect release authority" in result.stdout
    assert "Installed the approved Connect release authority" in result.stdout


@posix_installer
def test_installer_does_not_migrate_an_unapproved_legacy_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _write_private(home / LEGACY_RELEASE_KEYRING, _unapproved_keyring())

    result = _run_installer(tmp_path, home)

    assert result.returncode == 0, result.stderr
    assert _runtime_authority(home, monkeypatch) is None
    installed = entitlement._installed_release_keyring_path(str(home))
    assert installed is not None
    assert not installed.exists()
    assert "not the approved Connect release authority" in result.stderr
    assert "Connect remains unavailable" in result.stdout
    assert "--user daemon-reload" in (tmp_path / "systemctl.log").read_text()


@posix_installer
def test_installer_keeps_an_installed_authority_over_a_legacy_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    installed = entitlement._installed_release_keyring_path(str(home))
    assert installed is not None
    _write_private(installed, _approved_keyring())
    _write_private(home / LEGACY_RELEASE_KEYRING, _unapproved_keyring())

    result = _run_installer(tmp_path, home)

    assert result.returncode == 0, result.stderr
    assert _runtime_authority(home, monkeypatch) == dict(entitlement.APPROVED_RELEASE_AUTHORITIES)
    assert installed.read_bytes() == _approved_keyring()
    assert "Migrated" not in result.stdout
    assert result.stderr == ""


@posix_installer
def test_installer_rejects_an_unapproved_explicit_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    source = _write_private(tmp_path / "release" / "keyring.json", _unapproved_keyring())

    result = _run_installer(tmp_path, home, source)

    assert result.returncode != 0
    assert _runtime_authority(home, monkeypatch) is None
    assert not (tmp_path / "systemctl.log").exists()
