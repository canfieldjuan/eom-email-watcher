import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from connect_automate import entitlement
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

import eom_email_watcher.config as config_module

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


def test_systemd_config_lock_resolves_inside_only_writable_state_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv("STATE_DIRECTORY", raising=False)
    watcher = WATCHER_SERVICE.read_text()

    assert config_module._config_serialization_lock_path() == (
        home / ".local/state/eom-email-watcher/config-serialization.lock"
    )
    assert "ProtectHome=read-only" in watcher
    assert "StateDirectory=eom-email-watcher" in watcher
    assert "StateDirectoryMode=0700" in watcher
    assert "StateDirectory=eom-email-watcher" in MONTHLY_SERVICE.read_text()
    assert "StateDirectoryMode=0700" in MONTHLY_SERVICE.read_text()


def _read_write_paths(service: Path) -> set[str]:
    return {
        line.removeprefix("ReadWritePaths=")
        for line in service.read_text().splitlines()
        if line.startswith("ReadWritePaths=")
    }


def test_systemd_services_grant_only_runtime_config_and_legacy_state_parents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    legacy_state = home / ".local/state/eom-email-watcher"
    config_path = home / ".config/eom-email-watcher/config.toml"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path / "service-state"))

    loaded = config_module._load_config_bytes(
        (
            b'model_base_url = "http://127.0.0.1:1234/v1"\n'
            b'model_name = "local-model"\n'
            + f'database_file = "{legacy_state / "watcher.sqlite3"}"\n'.encode()
            + f'gmail_token_file = "{legacy_state / "token.json"}"\n'.encode()
            + f'model_api_token_file = "{legacy_state / "model-token"}"\n'.encode()
        ),
        config_path,
    )
    expected = {
        "%h/.config/eom-email-watcher",
        "-%h/.local/state/eom-email-watcher",
    }

    assert loaded.path.parent == config_path.parent
    assert loaded.database_file.parent == legacy_state
    assert loaded.gmail_token_file.parent == legacy_state
    assert loaded.model_api_token_file is not None
    assert loaded.model_api_token_file.parent == legacy_state
    assert _read_write_paths(WATCHER_SERVICE) == expected
    assert _read_write_paths(MONTHLY_SERVICE) == expected


def test_systemd_config_write_preflight_matches_runtime_default_parent() -> None:
    runtime_parent = config_module.DEFAULT_CONFIG.parent
    home = Path.home()
    relative = runtime_parent.relative_to(home)
    expected = f"%h/{relative.as_posix()}"

    assert expected == "%h/.config/eom-email-watcher"
    assert expected in _read_write_paths(WATCHER_SERVICE)
    assert expected in _read_write_paths(MONTHLY_SERVICE)


def test_systemd_and_interactive_roles_share_custom_state_lock_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_home = tmp_path / "custom-state"
    service_state = state_home / "eom-email-watcher"
    expected = service_state / "config-serialization.lock"

    observed: dict[str, Path] = {}
    for role in ("watcher", "monthly"):
        monkeypatch.setenv("STATE_DIRECTORY", str(service_state))
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "ignored-state"))
        observed[role] = config_module._config_serialization_lock_path()
    for role in ("cli", "desktop"):
        monkeypatch.delenv("STATE_DIRECTORY", raising=False)
        monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
        observed[role] = config_module._config_serialization_lock_path()

    assert observed == {role: expected for role in observed}


def test_systemd_and_interactive_roles_share_every_default_state_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    state_home = tmp_path / "custom-state"
    service_state = state_home / "eom-email-watcher"
    config_bytes = b'model_base_url = "http://127.0.0.1:1234/v1"\nmodel_name = "local-model"\n'
    monkeypatch.setenv("HOME", str(home))

    observed: dict[str, tuple[Path, ...]] = {}
    for role in ("watcher", "monthly"):
        monkeypatch.setenv("STATE_DIRECTORY", str(service_state))
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "ignored-state"))
        loaded = config_module._load_config_bytes(config_bytes, tmp_path / f"{role}.toml")
        observed[role] = (
            loaded.database_file,
            loaded.gmail_credentials_file,
            loaded.microsoft_credentials_file,
            loaded.gmail_token_file,
            loaded.gmail_send_token_file,
            loaded.model_api_token_file,
            config_module._config_serialization_lock_path(),
        )
    for role in ("cli", "desktop"):
        monkeypatch.delenv("STATE_DIRECTORY", raising=False)
        monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
        loaded = config_module._load_config_bytes(config_bytes, tmp_path / f"{role}.toml")
        observed[role] = (
            loaded.database_file,
            loaded.gmail_credentials_file,
            loaded.microsoft_credentials_file,
            loaded.gmail_token_file,
            loaded.gmail_send_token_file,
            loaded.model_api_token_file,
            config_module._config_serialization_lock_path(),
        )

    expected = (
        service_state / "watcher.sqlite3",
        service_state / "credentials.json",
        service_state / "microsoft-oauth-client.json",
        service_state / "token.json",
        service_state / "send-token.json",
        service_state / "lmstudio-api-token",
        service_state / "config-serialization.lock",
    )
    assert observed == {role: expected for role in observed}
    assert "ProtectHome=read-only" in WATCHER_SERVICE.read_text()
    assert "ProtectHome=read-only" in MONTHLY_SERVICE.read_text()


def test_default_state_paths_keep_home_compatibility_without_xdg_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("STATE_DIRECTORY", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)

    loaded = config_module._load_config_bytes(
        (b'model_base_url = "http://127.0.0.1:1234/v1"\nmodel_name = "local-model"\n'),
        tmp_path / "config.toml",
    )
    expected = home / ".local/state/eom-email-watcher"

    assert loaded.database_file == expected / "watcher.sqlite3"
    assert loaded.gmail_credentials_file == expected / "credentials.json"
    assert loaded.gmail_token_file == expected / "token.json"
    assert loaded.model_api_token_file == expected / "lmstudio-api-token"
    assert config_module._config_serialization_lock_path() == (
        expected / "config-serialization.lock"
    )


def test_explicit_absolute_state_paths_override_runtime_state_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    explicit = tmp_path / "explicit"
    monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path / "service-state"))
    loaded = config_module._load_config_bytes(
        (
            b'model_base_url = "http://127.0.0.1:1234/v1"\n'
            b'model_name = "local-model"\n'
            + f'database_file = "{explicit / "watcher.sqlite3"}"\n'.encode()
            + f'gmail_token_file = "{explicit / "token.json"}"\n'.encode()
            + f'model_api_token_file = "{explicit / "model-token"}"\n'.encode()
        ),
        tmp_path / "config.toml",
    )

    assert loaded.database_file == explicit / "watcher.sqlite3"
    assert loaded.gmail_token_file == explicit / "token.json"
    assert loaded.model_api_token_file == explicit / "model-token"


def test_explicit_empty_model_token_path_does_not_select_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    with pytest.raises(
        config_module.ConfigError,
        match="model_api_token_file is required",
    ):
        config_module._load_config_bytes(
            (
                b'model_base_url = "http://127.0.0.1:1234/v1"\n'
                b'model_name = "local-model"\n'
                b'model_api_token_file = ""\n'
            ),
            tmp_path / "config.toml",
        )


@pytest.mark.parametrize("value", ["relative/state", "", "relative:other"])
def test_systemd_state_directory_must_be_one_absolute_path(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("STATE_DIRECTORY", value)

    with pytest.raises(config_module._UnsafeConfigPath):
        config_module._config_serialization_lock_path()


LEGACY_RELEASE_KEYRING = Path(".local/share/eom-email-watcher/connect-entitlement-keyring.json")
FAKE_UV = """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$INSTALLER_TEST_UV_LOG"
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
    mkdir -p "$UV_TOOL_BIN_DIR" "$UV_TOOL_DIR/eom-email-watcher/bin"
    printf '#!/bin/sh\\n' > "$UV_TOOL_BIN_DIR/eom-mail-watch"
    printf '#!/bin/sh\\nexec "$INSTALLER_TEST_PYTHON" "$@"\\n' \\
      > "$UV_TOOL_DIR/eom-email-watcher/bin/python"
    chmod 0755 "$UV_TOOL_BIN_DIR/eom-mail-watch" "$UV_TOOL_DIR/eom-email-watcher/bin/python"
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
        "INSTALLER_TEST_UV_LOG": str(tmp_path / "uv.log"),
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
def test_installer_without_an_authority_never_syncs_the_project_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"

    result = _run_installer(tmp_path, home)

    assert result.returncode == 0, result.stderr
    assert _runtime_authority(home, monkeypatch) is None
    assert [line.split()[0] for line in (tmp_path / "uv.log").read_text().splitlines()] == [
        "export",
        "tool",
    ]
    assert "Connect remains unavailable" in result.stdout
    assert "--user daemon-reload" in (tmp_path / "systemctl.log").read_text()


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
