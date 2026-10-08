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
    snapshot = 'uv tool install --force --reinstall --constraints "$constraints_file" "$repo_dir"'
    unit_install = "from eom_email_watcher.deployment import install_source_units"
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
    assert "$tool_bin_dir/eom-mail-watch setup" in script


def test_systemd_services_use_stable_cli_snapshot() -> None:
    watcher = WATCHER_SERVICE.read_text()
    monthly = MONTHLY_SERVICE.read_text()

    assert "ExecStart=%h/.local/bin/eom-mail-watch check" in watcher
    assert "ExecStart=%h/.local/bin/eom-mail-watch send-hours" in monthly
    assert "/eom-email-watcher/.venv/" not in watcher
    assert "/eom-email-watcher/.venv/" not in monthly
    working_directory = "WorkingDirectory=%h/Desktop/01 - Effingham Office Maids/eom-email-watcher"
    assert working_directory not in watcher
    assert working_directory not in monthly


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
FAKE_SYSTEMCTL = "#!" + sys.executable + "\n" + f"""
import os,shlex,sys
from pathlib import Path
sys.path[:0] = [{str(ROOT / 'src')!r}, {str(ROOT / 'scripts')!r}]
from packaged_proof_environment import unit_records, render_unit_records, UNIT_NAMES
args=sys.argv[1:]
home=Path(os.environ['INSTALLER_TEST_MANAGER_HOME'])
config=Path(os.environ.get('INSTALLER_TEST_MANAGER_CONFIG',''))
config=config if config.is_absolute() else home/'.config'
if args==['--user','show-environment']:
    values=dict(HOME=str(home),XDG_CONFIG_HOME=str(config),
                XDG_DATA_HOME=os.environ.get('XDG_DATA_HOME',str(home/'.local/share')))
    print('\\n'.join(k+'='+shlex.quote(v) for k,v in values.items()))
elif args[:2]==['--user','show']:
    directory=config/'systemd/user'
    loaded=all((directory/name).is_file() for name in UNIT_NAMES)
    print(render_unit_records(unit_records(directory,loaded=loaded,home=home)))
else:
    with open(os.environ['INSTALLER_TEST_SYSTEMCTL_LOG'],'a') as output:
        output.write(' '.join(args)+'\\n')
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


def _write_native_engine(path: Path, body: str) -> None:
    """An ELF launcher fixture; actual frozen bundle is qualified separately."""
    compiler = shutil.which("cc")
    if compiler is None:
        pytest.skip("native installer fixture needs a C compiler")
    script = path.with_suffix(".sh")
    script.write_text(body)
    source = (
        "#include <unistd.h>\n#include <stdlib.h>\n"
        "int main(int argc, char **argv) {\n"
        "char **args = calloc(argc + 2, sizeof(char *));\n"
        'args[0] = "/bin/sh"; args[1] = ' + json.dumps(str(script)) + ";\n"
        "for (int i = 1; i < argc; ++i) args[i + 1] = argv[i];\n"
        "execv(args[0], args); return 127; }\n"
    )
    result = subprocess.run(
        [compiler, "-x", "c", "-o", str(path), "-"],
        input=source,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert path.read_bytes()[:4] == b"\x7fELF"


def _run_installer(
    tmp_path: Path,
    home: Path,
    keyring_source: Path | None = None,
    engine_body: str | None = None,
    config_home: str | None = None,
    source_engine: Path | None = None,
    data_home: str | None = None,
    source_first: bool = True,
    artifact: bytes | None = None,
    installer_args: list[str] | None = None,
    artifact_reader_body: str | None = None,
) -> subprocess.CompletedProcess[str]:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    for name, body in (("uv", FAKE_UV), ("systemctl", FAKE_SYSTEMCTL)):
        tool = fake_bin / name
        tool.write_text(body)
        tool.chmod(0o755)
    if engine_body is not None:
        engine = fake_bin / "eom-mail-engine"
        _write_native_engine(engine, engine_body)
    if artifact is not None:
        engine = fake_bin / "eom-mail-engine"
        engine.write_bytes(artifact)
        engine.chmod(0o700)
    source_bin = tmp_path / "source-path"
    if source_engine is not None:
        source_bin.mkdir()
        (source_bin / "eom-mail-engine").symlink_to(source_engine)
    # Source-only fixture: do not discover an unrelated installed desktop on the host.
    for name in (
        "bash",
        "python3",
        "dirname",
        "mktemp",
        "rm",
        "mkdir",
        "install",
        "mv",
        "chmod",
        "ln",
        "readlink",
        "rmdir",
        "od",
    ):
        if name == "od" and artifact_reader_body is not None:
            tool = fake_bin / name
            tool.write_text(artifact_reader_body)
            tool.chmod(0o700)
            continue
        target = shutil.which(name)
        assert target is not None
        (fake_bin / name).symlink_to(target)
    home.mkdir(exist_ok=True)
    environment = {
        "HOME": str(home),
        "INSTALLER_TEST_MANAGER_HOME": str(home),
        "PATH": str(fake_bin),
        "TMPDIR": str(tmp_path),
        "INSTALLER_TEST_PYTHON": sys.executable,
        "INSTALLER_TEST_SYSTEMCTL_LOG": str(tmp_path / "systemctl.log"),
        "INSTALLER_TEST_UV_LOG": str(tmp_path / "uv.log"),
    }
    if source_engine is not None:
        directories = [source_bin, fake_bin] if source_first else [fake_bin, source_bin]
        environment["PATH"] = os.pathsep.join(str(directory) for directory in directories)
    if data_home is not None:
        environment["XDG_DATA_HOME"] = data_home
    if config_home is not None:
        environment["XDG_CONFIG_HOME"] = config_home
        environment["INSTALLER_TEST_MANAGER_CONFIG"] = config_home
    if keyring_source is not None:
        environment["LOCAL_CONNECT_ENTITLEMENT_KEYRING_FILE"] = str(keyring_source)
    return subprocess.run(
        [
            "bash",
            str(INSTALLER),
            *(
                installer_args
                if installer_args is not None
                else (
                    ["--engine", str(fake_bin / "eom-mail-engine")]
                    if engine_body is not None or artifact is not None
                    else ["--source"]
                )
            ),
        ],
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


@posix_installer
def test_packaged_entrypoint_dispatches_existing_cli_without_reading_api_input(
    tmp_path: Path,
) -> None:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "src")
    result = subprocess.run(
        [sys.executable, str(ROOT / "packaging/engine_entry.py"), "--cli", "--version"],
        input="",
        capture_output=True,
        text=True,
        env=environment,
        cwd=tmp_path,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    from eom_email_watcher import __version__

    assert result.stdout.strip() == __version__


@posix_installer
def test_installer_rejects_incompatible_desktop_before_publishing_services(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    result = _run_installer(
        tmp_path,
        home,
        engine_body='#!/bin/sh\nprintf "old-api-only-engine\\n"\nexit 2\n',
    )
    assert result.returncode != 0, "incompatible desktop admitted an independent CLI"
    assert not (tmp_path / "uv.log").exists()
    assert not (tmp_path / "systemctl.log").exists()
    assert not (home / ".config/systemd/user/eom-email-watcher.service").exists()


@posix_installer
def test_source_installer_preserves_existing_paired_native_alias(tmp_path):
    home = tmp_path / "home"
    alias = home / ".local/bin/eom-mail-watch"
    alias.parent.mkdir(parents=True)
    _write_native_engine(alias, "#!/bin/sh\nexit 0\n")
    before = alias.read_bytes()
    result = _run_installer(tmp_path, home, installer_args=["--source"])
    assert result.returncode == 2, "source installation overwrote the paired native alias"
    assert alias.read_bytes() == before
    assert not (tmp_path / "uv.log").exists()
    assert not (tmp_path / "systemctl.log").exists()


@posix_installer
def test_source_installer_refuses_unreadable_artifact_identity(tmp_path):
    home = tmp_path / "home"
    alias = home / ".local/bin/eom-mail-watch"
    alias.parent.mkdir(parents=True)
    _write_native_engine(alias, "#!/bin/sh\nexit 0\n")
    before = alias.read_bytes()
    result = _run_installer(
        tmp_path, home, installer_args=["--source"], artifact_reader_body="#!/bin/sh\nexit 5\n"
    )
    assert result.returncode == 2, "an unreadable native identity was classified as source"
    assert alias.read_bytes() == before
    assert not (tmp_path / "uv.log").exists()
    assert not (tmp_path / "systemctl.log").exists()


@posix_installer
def test_no_arguments_never_discovers_native_engine_on_path(tmp_path):
    result = _run_installer(
        tmp_path,
        tmp_path / "home",
        installer_args=[],
        engine_body='#!/bin/sh\nprintf invoked > "$HOME/native-invoked"\nexit 0\n',
    )
    assert result.returncode == 2
    assert not (tmp_path / "home/native-invoked").exists()
    assert not (tmp_path / "uv.log").exists()
    assert not (tmp_path / "systemctl.log").exists()


@posix_installer
def test_installer_pairs_both_services_without_installing_another_snapshot(tmp_path: Path) -> None:
    from eom_email_watcher.deployment import PAIRED_CLI_PROTOCOL

    home = tmp_path / "home"
    result = _run_installer(
        tmp_path,
        home,
        engine_body=(
            '#!/bin/sh\ncase "$1" in\n'
            f'--paired-cli-version) printf "%s\\n" "{PAIRED_CLI_PROTOCOL}" ;;\n'
            '--service-unit-directory) printf "%s\\n" "$HOME/.config/systemd/user" ;;\n'
            '--install-user-services) printf "%s\\n" "${0%.sh}" > "$HOME/native-owner"; '
            'echo "Paired scheduled intake" ;;\n*) exit 2 ;;\nesac\n'
        ),
    )
    assert result.returncode == 0, result.stderr
    assert Path((home / "native-owner").read_text().strip()).samefile(
        tmp_path / "fake-bin/eom-mail-engine"
    )
    assert not (tmp_path / "uv.log").exists()
    assert not (tmp_path / "systemctl.log").exists(), "shell duplicated the native owner's install"
    assert "Paired scheduled intake" in result.stdout


@posix_installer
@pytest.mark.parametrize("setting", ["", "relative", "custom"])
def test_source_installer_consumes_runtime_unit_directory(tmp_path, monkeypatch, setting):
    from eom_email_watcher import deployment

    home = tmp_path / "home"
    value = str(tmp_path / "custom config") if setting == "custom" else setting
    result = _run_installer(tmp_path, home, config_home=value)
    assert result.returncode == 0, result.stderr
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", value)
    monkeypatch.setattr(
        deployment, "_manager_environment", lambda: {"HOME": str(home), "XDG_CONFIG_HOME": value}
    )
    directory = deployment.service_unit_directory()
    assert (directory / "eom-email-watcher.service").is_file()
    assert (directory / "eom-monthly-hours.service").is_file()


@posix_installer
@pytest.mark.parametrize("spelling", ["symlink", "dotdot"])
def test_source_reinstall_recognizes_canonical_uv_engine(tmp_path, spelling):
    physical = tmp_path / "physical-home"
    physical.mkdir()
    if spelling == "symlink":
        home = tmp_path / "home-alias"
        home.symlink_to(physical, target_is_directory=True)
    else:
        (physical / "nested").mkdir()
        home = physical / "nested/.."
    source = physical / ".local/share/uv/tools/eom-email-watcher/bin/eom-mail-engine"
    source.parent.mkdir(parents=True)
    source.write_text('#!/bin/sh\nprintf "source API has no paired metadata\\n" >&2\nexit 2\n')
    source.chmod(0o755)
    result = _run_installer(tmp_path, home, source_engine=source)
    assert result.returncode == 0, "source snapshot was mistaken for desktop: " + result.stderr
    assert (tmp_path / "uv.log").exists()
    assert (home / ".config/systemd/user/eom-monthly-hours.service").is_file()


@posix_installer
@pytest.mark.parametrize("spelling", ["symlink", "dotdot", "spaces"])
def test_source_reinstall_recognizes_canonical_xdg_data_engine(tmp_path, spelling):
    home = tmp_path / "home"
    home.mkdir()
    physical = tmp_path / "physical data"
    physical.mkdir()
    if spelling == "symlink":
        data = tmp_path / "data-alias"
        data.symlink_to(physical, target_is_directory=True)
    elif spelling == "dotdot":
        (physical / "nested").mkdir()
        data = physical / "nested/.."
    else:
        data = physical
    source = physical / "uv/tools/eom-email-watcher/bin/eom-mail-engine"
    source.parent.mkdir(parents=True)
    source.write_text("#!/bin/sh\nexit 2\n")
    source.chmod(0o755)
    result = _run_installer(tmp_path, home, source_engine=source, data_home=str(data))
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "uv.log").exists()
    assert (home / ".config/systemd/user/eom-monthly-hours.service").is_file()


@posix_installer
def test_source_environment_shim_is_not_a_packaged_engine(tmp_path):
    source = tmp_path / "source-environment/bin/eom-mail-engine"
    source.parent.mkdir(parents=True)
    checkout = ROOT
    source.write_text(
        "#!"
        + sys.executable
        + "\nimport sys\nsys.path.insert(0,"
        + repr(str(checkout / "src"))
        + ")\n"
        + "from eom_email_watcher.engine_api import main\nmain()\n"
    )
    source.chmod(0o700)
    result = _run_installer(tmp_path, tmp_path / "home", source_engine=source)
    assert result.returncode == 0, "source-environment shim aborted installer: " + result.stderr
    assert (tmp_path / "uv.log").exists()


@posix_installer
@pytest.mark.parametrize("source_first", [True, False])
def test_explicit_native_engine_is_independent_of_path_order(tmp_path, source_first):
    from eom_email_watcher.deployment import PAIRED_CLI_PROTOCOL

    source = tmp_path / "arbitrary runner environment/bin/eom-mail-engine"
    source.parent.mkdir(parents=True)
    marker = tmp_path / "source-executed"
    source.write_text("#!/bin/sh\nprintf invoked > " + json.dumps(str(marker)) + "\nexit 2\n")
    source.chmod(0o700)
    body = (
        '#!/bin/sh\ncase "$1" in\n'
        f'--paired-cli-version) printf "%s\\n" "{PAIRED_CLI_PROTOCOL}" ;;\n'
        '--service-unit-directory) printf "%s\\n" "$HOME/.config/systemd/user" ;;\n'
        '--install-user-services) printf "%s\\n" "${0%.sh}" > "$HOME/native-owner"; '
        'echo "Paired scheduled intake" ;;\n*) exit 2 ;;\nesac\n'
    )
    home = tmp_path / "home"
    result = _run_installer(
        tmp_path, home, engine_body=body, source_engine=source, source_first=source_first
    )
    assert result.returncode == 0, result.stderr
    assert not marker.exists(), "source shim was executed for artifact classification"
    assert not (tmp_path / "uv.log").exists()
    assert Path((home / "native-owner").read_text().strip()).samefile(
        tmp_path / "fake-bin/eom-mail-engine"
    )


@posix_installer
@pytest.mark.parametrize("artifact", [b"", b"unknown executable", b"\x7fELFnot-a-valid-bundle"])
def test_unrecognized_or_invalid_native_artifacts_fail_before_publication(tmp_path, artifact):
    home = tmp_path / "home"
    result = _run_installer(tmp_path, home, artifact=artifact)
    assert result.returncode == 2, result.stdout + result.stderr
    assert not (tmp_path / "uv.log").exists()
    assert not (tmp_path / "systemctl.log").exists()
    assert not (home / ".config/systemd/user").exists()


@posix_installer
def test_source_shim_with_no_native_bundle_is_never_executed(tmp_path):
    source = tmp_path / "arbitrary-venv/bin/eom-mail-engine"
    source.parent.mkdir(parents=True)
    marker = tmp_path / "source-executed"
    source.write_text("#!/bin/sh\nprintf invoked > " + json.dumps(str(marker)) + "\nexit 2\n")
    source.chmod(0o700)
    result = _run_installer(tmp_path, tmp_path / "home", source_engine=source)
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert (tmp_path / "uv.log").exists()


@posix_installer
def test_two_native_candidates_use_the_explicit_desktop(tmp_path):
    body = (
        '#!/bin/sh\ncase "$1" in\n'
        "--paired-cli-version) echo eom-mail-engine-paired-cli-v1 ;;\n"
        '--install-user-services) printf "%s\\n" "${0%.sh}" > "$HOME/native-owner" ;;\n'
        "*) exit 2 ;;\nesac\n"
    )
    stale = tmp_path / "stale-engine"
    _write_native_engine(stale, body)
    home = tmp_path / "home"
    result = _run_installer(
        tmp_path, home, engine_body=body, source_engine=stale, source_first=True
    )
    assert result.returncode == 0, result.stderr
    selected = Path((home / "native-owner").read_text().strip())
    assert selected.samefile(tmp_path / "fake-bin/eom-mail-engine")
    assert not selected.samefile(stale)


@posix_installer
def test_installation_mode_must_be_explicit(tmp_path):
    result = _run_installer(tmp_path, tmp_path / "home", installer_args=[])
    assert result.returncode == 2
    assert not (tmp_path / "uv.log").exists()
    assert not (tmp_path / "systemctl.log").exists()


@posix_installer
def test_protocol_mismatch_cannot_delegate_install(tmp_path):
    body = (
        '#!/bin/sh\ncase "$1" in\n'
        "--paired-cli-version) echo incompatible-protocol ;;\n"
        '--install-user-services) echo reached > "$HOME/native-owner" ;;\n'
        "*) exit 2 ;;\nesac\n"
    )
    home = tmp_path / "home"
    result = _run_installer(tmp_path, home, engine_body=body)
    assert result.returncode == 2
    assert not (home / "native-owner").exists()


@posix_installer
def test_source_shell_retains_exclusive_lease_through_snapshot_writes(tmp_path, monkeypatch):
    probe = """
python3 - <<'LOCK_PROBE'
import fcntl, os
from pathlib import Path
path=Path(os.environ['HOME'])/'.local/state/eom-email-watcher/deployment.lock'
with path.open('r') as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        pass
    else:
        raise SystemExit('source snapshot writes lost the exclusive publication lease')
LOCK_PROBE
"""
    monkeypatch.setattr(sys.modules[__name__], "FAKE_UV",
                        FAKE_UV.replace('case "$1" in', probe + 'case "$1" in'))
    home = tmp_path / "home"
    result = _run_installer(tmp_path, home)
    assert result.returncode == 0, result.stdout + result.stderr
    log = (tmp_path / "uv.log").read_text()
    assert "export" in log and "tool install" in log
