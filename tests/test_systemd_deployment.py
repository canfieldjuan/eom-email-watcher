from pathlib import Path

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
    assert 'PYTHONPATH="$repo_dir" RELEASE_KEYRING_SOURCE="$release_keyring_source"' in script
    assert 'release_keyring_dir="$HOME/.local/share/eom-email-watcher"' in script
    assert 'uv run --project "$repo_dir" --no-dev --locked python -c' in script
    assert "validate_entitlement_keyring" in script
    stage = 'install -m 0600 "$release_keyring_source" "$release_keyring_stage"'
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
