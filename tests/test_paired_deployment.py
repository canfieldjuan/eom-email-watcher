from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from eom_email_watcher import deployment


def units(tmp_path: Path, names: tuple[str, ...]) -> tuple[Path, Path, Path]:
    binary = tmp_path / "engine"
    binary.write_bytes(b"public engine identity")
    alias = tmp_path / "eom-mail-watch"
    alias.symlink_to(binary)
    unit_directory = tmp_path / "systemd/user"
    unit_directory.mkdir(parents=True)
    for name in names:
        (unit_directory / name).write_text("[Service]\n")
    return binary, alias, unit_directory


def _mock_service_reader(monkeypatch, reader):
    """Service-focused fixtures explicitly have no loaded timers."""

    def read(unit, name):
        if unit in deployment.SCHEDULED_JOBS:
            return {"LoadState": "not-found", "ActiveState": "inactive"}[name]
        return reader(unit, name)

    monkeypatch.setattr(deployment, "_service_property", read)


def test_only_explicit_manager_not_found_is_unconfigured(tmp_path: Path, monkeypatch) -> None:
    calls = []

    def read(unit, name):
        calls.append((unit, name))
        if name == "LoadState":
            return "not-found"
        if name == "ActiveState":
            return "inactive"
        assert name == "MainPID"
        return 0

    monkeypatch.setattr(deployment, "_service_property", read)
    deployment.verify_scheduled_readers(tmp_path / "engine")
    assert calls == [
        (unit, name) for unit in deployment.SCHEDULED_JOBS for name in ("LoadState", "ActiveState")
    ] + [
        (unit, name) for unit in deployment.SCHEDULED_COMMANDS for name in ("LoadState", "MainPID")
    ]


@pytest.mark.parametrize("bad", ["legacy", "wrong-argv", "wrong-command", "ignore", "active"])
def test_mixed_scheduled_readers_block_before_request_or_migration(
    tmp_path: Path, monkeypatch, bad
):
    binary, alias, directory = units(tmp_path, tuple(deployment.SCHEDULED_COMMANDS))
    calls = []

    def read(unit, name):
        calls.append((unit, name))
        if name == "LoadState":
            return "loaded"
        if name == "MainPID":
            return 123 if bad == "active" and unit == "eom-monthly-hours.service" else 0
        command = deployment.SCHEDULED_COMMANDS[unit]
        executable = str(alias)
        argv = [executable, command]
        ignore = False
        if unit == "eom-monthly-hours.service":
            if bad == "legacy":
                executable = str(tmp_path / "old-snapshot")
            elif bad == "wrong-argv":
                argv[0] = str(tmp_path / "old-snapshot")
            elif bad == "wrong-command":
                argv[1] = "check"
            elif bad == "ignore":
                ignore = True
        return [[executable, argv, ignore, 0, 0, 0, 0, 0, 0, 0]]

    _mock_service_reader(monkeypatch, read)
    from eom_email_watcher import engine_api

    monkeypatch.setattr(engine_api, "main", lambda: pytest.fail("API/migration reached"))
    monkeypatch.setattr(sys, "argv", [str(binary)])
    monkeypatch.setattr(sys, "executable", str(binary))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(directory.parent.parent))
    with pytest.raises(SystemExit) as exc:
        deployment.main()
    assert exc.value.code == 2
    assert any(unit == "eom-monthly-hours.service" for unit, _name in calls)


def test_paired_services_reach_api_owner(tmp_path: Path, monkeypatch):
    binary, alias, directory = units(tmp_path, tuple(deployment.SCHEDULED_COMMANDS))

    def read(unit, name):
        if name == "LoadState":
            return "loaded" if (directory / unit).exists() else "not-found"
        if name == "MainPID":
            return 0
        return [
            [
                str(alias),
                [str(alias), deployment.SCHEDULED_COMMANDS[unit]],
                False,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
            ]
        ]

    _mock_service_reader(monkeypatch, read)
    from eom_email_watcher import engine_api

    called = []
    monkeypatch.setattr(engine_api, "main", lambda: called.append("api"))
    monkeypatch.setattr(sys, "argv", [str(binary)])
    monkeypatch.setattr(sys, "executable", str(binary))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(directory.parent.parent))
    deployment.main()
    assert called == ["api"]


@pytest.mark.parametrize("metadata", [None, [], [None], [["path"]], [[], []]])
def test_invalid_service_metadata_is_not_admitted(tmp_path: Path, monkeypatch, metadata):
    binary, _alias, directory = units(tmp_path, ("eom-email-watcher.service",))
    _mock_service_reader(monkeypatch, lambda *args: metadata)
    with pytest.raises(deployment.DeploymentError):
        deployment.verify_scheduled_readers(binary)


@pytest.mark.parametrize("pid", [None, False, "0", -1])
def test_invalid_active_worker_metadata_is_not_defaulted(tmp_path: Path, monkeypatch, pid):
    binary, alias, directory = units(tmp_path, ("eom-email-watcher.service",))

    def read(unit, name):
        if name == "LoadState":
            return "loaded" if (directory / unit).exists() else "not-found"
        if name == "MainPID":
            return pid if (directory / unit).exists() else 0
        return [[str(alias), [str(alias), "check"], False, 0, 0, 0, 0, 0, 0, 0]]

    _mock_service_reader(monkeypatch, read)
    with pytest.raises(deployment.DeploymentError):
        deployment.verify_scheduled_readers(binary)


@pytest.mark.parametrize(
    "value",
    [
        "invalid-json",
        "{}",
        '{"type":"u","data":0}',
        '{"type":"a(sasbttttuii)","data":[],"extra":1}',
    ],
)
def test_bus_metadata_reader_rejects_wrong_shapes(monkeypatch, value):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, value))
    with pytest.raises(deployment.DeploymentError):
        deployment._service_property("eom-email-watcher.service", "ExecStart")


def test_missing_bus_fails_closed(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("busctl")

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(deployment.DeploymentError):
        deployment._service_property("eom-email-watcher.service", "ExecStart")


def test_public_schema_28_partial_update_is_rejected_before_migration(tmp_path, monkeypatch):
    import hashlib
    import io
    import sqlite3

    from test_db import _reset_to_schema_28

    from eom_email_watcher import cli, engine_api
    from eom_email_watcher.db import SCHEMA_VERSION, Store

    binary, alias, directory = units(tmp_path, ("eom-email-watcher.service",))
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(directory.parent.parent))
    monkeypatch.delenv("STATE_DIRECTORY", raising=False)
    config = tmp_path / "config.toml"
    response = engine_api._response(
        {
            "protocol": 1,
            "operation": "config.initialize",
            "config_path": str(config),
            "payload": {
                "model_base_url": "http://127.0.0.1:9/v1",
                "model_name": "public-proof",
                "timezone": "America/Chicago",
            },
        }
    )
    assert response["ok"] is True
    database = state / "eom-email-watcher/watcher.sqlite3"
    store = Store(database)
    store.initialize()
    _reset_to_schema_28(store)
    with sqlite3.connect(database) as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert db.execute("PRAGMA user_version").fetchone()[0] == 28
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    paired = False

    def read(unit, name):
        if name == "LoadState":
            return "loaded" if (directory / unit).exists() else "not-found"
        if name == "MainPID":
            return 0
        path = str(alias) if paired else str(tmp_path / "legacy-cli")
        return [[path, [path, "check"], False, 0, 0, 0, 0, 0, 0, 0]]

    _mock_service_reader(monkeypatch, read)
    monkeypatch.setattr(sys, "argv", [str(binary)])
    monkeypatch.setattr(sys, "executable", str(binary))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    request = json.dumps(
        {"protocol": 1, "operation": "health.get", "config_path": str(config), "payload": {}}
    ).encode()
    stdin = io.TextIOWrapper(io.BytesIO(request))
    stdout = io.TextIOWrapper(io.BytesIO())
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    with pytest.raises(SystemExit) as rejected:
        deployment.main()
    assert rejected.value.code == 2
    assert stdin.buffer.tell() == 0
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 28
    paired = True
    with pytest.raises(SystemExit) as accepted:
        deployment.main()
    assert accepted.value.code == 0
    stdout.flush()
    assert json.loads(stdout.buffer.getvalue())["ok"] is True
    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    stdout = io.TextIOWrapper(io.BytesIO())
    monkeypatch.setattr(sys, "stdout", stdout)
    with pytest.raises(SystemExit) as cli_result:
        cli.main(["--config", str(config), "recent", "--limit", "1"])
    assert cli_result.value.code == 0
    stdout.flush()
    assert json.loads(stdout.buffer.getvalue()) == []


def test_cli_cannot_migrate_with_an_incompatible_scheduled_reader(tmp_path, monkeypatch):
    from eom_email_watcher import cli

    binary, _alias, directory = units(tmp_path, ("eom-email-watcher.service",))
    _mock_service_reader(
        monkeypatch,
        lambda unit, name: (
            "loaded"
            if name == "LoadState"
            else 0
            if name == "MainPID"
            else [
                [
                    str(tmp_path / "legacy-cli"),
                    [str(tmp_path / "legacy-cli"), "check"],
                    False,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                ]
            ]
        ),
    )
    monkeypatch.setattr(cli, "main", lambda *args: pytest.fail("CLI/migration reached"))
    monkeypatch.setattr(sys, "argv", [str(binary), "--cli", "recent"])
    monkeypatch.setattr(sys, "executable", str(binary))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(directory.parent.parent))
    with pytest.raises(SystemExit) as rejected:
        deployment.main()
    assert rejected.value.code == 2


@pytest.mark.parametrize("pid,allowed", [(123, True), (456, False)])
def test_running_reader_identity_controls_admission(tmp_path, monkeypatch, pid, allowed):
    binary, alias, directory = units(tmp_path, ("eom-email-watcher.service",))

    def read(unit, name):
        if name == "LoadState":
            return "loaded" if (directory / unit).exists() else "not-found"
        if name == "MainPID":
            return pid if (directory / unit).exists() else 0
        return [[str(alias), [str(alias), "check"], False, 0, 0, 0, 0, 0, 0, 0]]

    _mock_service_reader(monkeypatch, read)
    original = deployment._same_executable
    monkeypatch.setattr(
        deployment,
        "_same_executable",
        lambda path, current: (
            allowed if str(path).startswith("/proc/") else original(path, current)
        ),
    )
    if allowed:
        deployment.verify_scheduled_readers(binary)
    else:
        with pytest.raises(deployment.DeploymentError, match="still active"):
            deployment.verify_scheduled_readers(binary)


@pytest.mark.parametrize("xdg", ["", "relative-config"])
def test_invalid_xdg_config_cannot_hide_scheduled_readers(tmp_path, monkeypatch, xdg):
    from eom_email_watcher import engine_api

    (tmp_path / ".config").mkdir()
    binary, _alias, _directory = units(tmp_path / ".config", ("eom-email-watcher.service",))
    _mock_service_reader(
        monkeypatch,
        lambda unit, name: (
            "loaded"
            if name == "LoadState"
            else 0
            if name == "MainPID"
            else [
                [
                    "/missing-legacy-cli",
                    ["/missing-legacy-cli", "check"],
                    False,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                ]
            ]
        ),
    )
    monkeypatch.setattr(
        engine_api, "main", lambda: pytest.fail("API/migration reached through XDG")
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", xdg)
    monkeypatch.setattr(sys, "argv", [str(binary)])
    monkeypatch.setattr(sys, "executable", str(binary))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(SystemExit) as rejected:
        deployment.main()
    assert rejected.value.code == 2


@pytest.mark.parametrize("xdg", [None, "", "relative"])
def test_unit_directory_default_is_shared_and_absolute(tmp_path, monkeypatch, xdg):
    monkeypatch.setenv("HOME", str(tmp_path))
    if xdg is None:
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", xdg)
    assert deployment.service_unit_directory() == tmp_path / ".config/systemd/user"


def test_unit_directory_preserves_absolute_custom_path_with_spaces(tmp_path, monkeypatch):
    custom = tmp_path / "custom config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(custom))
    assert deployment.service_unit_directory() == custom / "systemd/user"


def test_unit_directory_metadata_is_read_only_and_uses_owner(tmp_path, monkeypatch, capsys):
    from eom_email_watcher import engine_api

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    monkeypatch.setattr(sys, "argv", ["engine", "--service-unit-directory"])
    monkeypatch.setattr(engine_api, "main", lambda: pytest.fail("API reached"))
    deployment.main()
    assert capsys.readouterr().out.strip() == str(tmp_path / ".config/systemd/user")


@pytest.mark.skipif(sys.platform != "linux", reason="Linux running executable identity")
def test_paired_active_worker_does_not_drop_overlapping_job(tmp_path, monkeypatch):
    import os

    from eom_email_watcher import cli

    binary, alias, directory = units(tmp_path, tuple(deployment.SCHEDULED_COMMANDS))
    binary.unlink()
    binary.symlink_to(sys.executable)

    def read(unit, name):
        if name == "LoadState":
            return "loaded"
        if name == "MainPID":
            return active_pid
        return [
            [
                str(alias),
                [str(alias), deployment.SCHEDULED_COMMANDS[unit]],
                False,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
            ]
        ]

    active_pid = os.getpid()
    called = []
    _mock_service_reader(monkeypatch, read)
    monkeypatch.setattr(cli, "main", lambda args: called.append(args))
    monkeypatch.setattr(sys, "argv", [str(binary), "--cli", "send-hours"])
    monkeypatch.setattr(sys, "executable", str(binary))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(directory.parent.parent))
    # Simulate the other paired service, rather than this process's own MainPID.
    monkeypatch.setattr(deployment.os, "getpid", lambda: 123456789)
    monkeypatch.setattr(deployment.os, "getppid", lambda: 123456788)
    deployment.main()
    assert called == [["send-hours"]], "overlapping paired monthly activation was dropped"


def test_loaded_legacy_reader_cannot_hide_by_removing_unit_file(tmp_path, monkeypatch):
    from eom_email_watcher import engine_api

    binary, alias, directory = units(tmp_path, ())

    def read(unit, name):
        if name == "LoadState":
            return "loaded"
        if name == "MainPID":
            return 0
        legacy = str(tmp_path / "legacy-reader")
        return [[legacy, [legacy, deployment.SCHEDULED_COMMANDS[unit]], False, 0, 0, 0, 0, 0, 0, 0]]

    _mock_service_reader(monkeypatch, read)
    monkeypatch.setattr(
        engine_api,
        "main",
        lambda: pytest.fail("API/migration reached without loaded-unit validation"),
    )
    monkeypatch.setattr(sys, "argv", [str(binary)])
    monkeypatch.setattr(sys, "executable", str(binary))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(directory.parent.parent))
    with pytest.raises(SystemExit) as rejected:
        deployment.main()
    assert rejected.value.code == 2


@pytest.mark.parametrize("state", ["", "masked", "error", "bad-setting", "loaded extra"])
def test_manager_load_state_reader_fails_closed(monkeypatch, state):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, state))
    with pytest.raises(deployment.DeploymentError):
        deployment._service_property("eom-email-watcher.service", "LoadState")


@pytest.mark.parametrize("state", ["loaded", "not-found"])
def test_manager_load_state_reader_uses_explicit_manager_result(monkeypatch, state):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, state + "\n")

    monkeypatch.setattr(subprocess, "run", run)
    assert deployment._service_property("eom-email-watcher.service", "LoadState") == state
    assert calls == [
        [
            "systemctl",
            "--user",
            "show",
            "eom-email-watcher.service",
            "--property=LoadState",
            "--value",
        ]
    ]


@pytest.mark.parametrize("failure", ["exit", "missing", "timeout"])
def test_manager_failure_is_not_unconfigured(monkeypatch, failure):
    def run(*args, **kwargs):
        if failure == "exit":
            return subprocess.CompletedProcess(args, 1, "not-found")
        if failure == "missing":
            raise FileNotFoundError("systemctl")
        raise subprocess.TimeoutExpired(args, 5)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(deployment.DeploymentError):
        deployment._service_property("eom-email-watcher.service", "LoadState")


@pytest.mark.parametrize("pid", [1, False, "0", None, -1])
def test_not_found_cannot_hide_active_or_invalid_pid(tmp_path, monkeypatch, pid):
    _mock_service_reader(
        monkeypatch,
        lambda unit, name: "not-found" if name == "LoadState" else pid,
    )
    with pytest.raises(deployment.DeploymentError):
        deployment.verify_scheduled_readers(tmp_path / "engine")


@pytest.mark.parametrize("final_pid", [0, False, "0", None, -1, 123])
def test_worker_exit_race_requires_confirmed_zero_pid(tmp_path, monkeypatch, final_pid):
    binary, alias, directory = units(tmp_path, ("eom-email-watcher.service",))
    pids = iter([123, final_pid])

    def read(unit, name):
        if name == "LoadState":
            return "loaded" if (directory / unit).exists() else "not-found"
        if name == "MainPID":
            return next(pids) if unit == "eom-email-watcher.service" else 0
        return [[str(alias), [str(alias), "check"], False, 0, 0, 0, 0, 0, 0, 0]]

    _mock_service_reader(monkeypatch, read)
    if type(final_pid) is int and final_pid == 0:
        deployment.verify_scheduled_readers(binary)
    else:
        with pytest.raises(deployment.DeploymentError):
            deployment.verify_scheduled_readers(binary)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux running inode qualification")
def test_overwritten_running_inode_is_incompatible(tmp_path, monkeypatch):
    import shutil

    binary, alias, directory = units(tmp_path, ("eom-email-watcher.service",))
    sleep = shutil.which("sleep")
    assert sleep is not None
    shutil.copy2(sleep, binary)
    worker = subprocess.Popen([str(binary), "30"])
    try:
        import time

        deadline = time.monotonic() + 5
        while not Path(f"/proc/{worker.pid}/exe").samefile(binary):
            assert time.monotonic() < deadline, "worker did not execute original inode"
            time.sleep(0.01)
        replacement = tmp_path / "replacement"
        replacement.write_bytes(b"new packaged executable identity")
        replacement.replace(binary)

        def read(unit, name):
            if name == "LoadState":
                return "loaded" if (directory / unit).exists() else "not-found"
            if name == "MainPID":
                return worker.pid
            return [[str(alias), [str(alias), "check"], False, 0, 0, 0, 0, 0, 0, 0]]

        _mock_service_reader(monkeypatch, read)
        assert alias.samefile(binary)
        assert not Path(f"/proc/{worker.pid}/exe").samefile(binary)
        with pytest.raises(deployment.DeploymentError, match="incompatible scheduled worker"):
            deployment.verify_scheduled_readers(binary)
    finally:
        worker.terminate()
        worker.wait(timeout=5)


@pytest.mark.parametrize("timer", ["eom-email-watcher.timer", "eom-monthly-hours.timer"])
def test_redirected_loaded_timer_blocks_migration(tmp_path, monkeypatch, timer):
    binary = tmp_path / "engine"
    binary.write_bytes(b"public paired binary")
    alias = tmp_path / "eom-mail-watch"
    alias.symlink_to(binary)
    calls = []

    def read(unit, name):
        calls.append((unit, name))
        if name == "LoadState":
            return "loaded"
        if name == "Unit":
            return (
                "legacy-redirected-reader.service"
                if unit == timer
                else unit.replace(".timer", ".service")
            )
        if name == "MainPID":
            return 0
        if unit == "legacy-redirected-reader.service":
            return [["/missing-old-cli", ["/missing-old-cli", "check"], False, 0, 0, 0, 0, 0, 0, 0]]
        return [
            [
                str(alias),
                [str(alias), deployment.SCHEDULED_COMMANDS[unit]],
                False,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
            ]
        ]

    monkeypatch.setattr(deployment, "_service_property", read)
    with pytest.raises(deployment.DeploymentError):
        deployment.verify_scheduled_readers(binary)


def _loaded_timer_graph(alias, overrides=None):
    overrides = overrides or {}

    def read(unit, name):
        if name == "LoadState":
            return "loaded"
        if name == "Unit":
            return overrides.get(unit, deployment.SCHEDULED_JOBS[unit][0])
        if name == "MainPID":
            return 0
        return [
            [
                str(alias),
                [str(alias), deployment.SCHEDULED_COMMANDS[unit]],
                False,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
            ]
        ]

    return read


def test_canonical_loaded_timers_and_services_are_admitted(tmp_path, monkeypatch):
    binary, alias, _directory = units(tmp_path, ())
    calls = []
    read = _loaded_timer_graph(alias)

    def record(unit, name):
        calls.append((unit, name))
        return read(unit, name)

    monkeypatch.setattr(deployment, "_service_property", record)
    deployment.verify_scheduled_readers(binary)
    assert [unit for unit, name in calls if name == "Unit"] == list(deployment.SCHEDULED_JOBS)
    assert [unit for unit, name in calls if name == "ExecStart"] == list(
        deployment.SCHEDULED_COMMANDS
    )


@pytest.mark.parametrize("timer", ["eom-email-watcher.timer", "eom-monthly-hours.timer"])
@pytest.mark.parametrize(
    "target", [None, False, 0, "", "legacy.service", ["eom-email-watcher.service"]]
)
def test_malformed_or_redirected_timer_target_is_not_defaulted(
    tmp_path, monkeypatch, timer, target
):
    binary, alias, _directory = units(tmp_path, ())
    monkeypatch.setattr(
        deployment, "_service_property", _loaded_timer_graph(alias, {timer: target})
    )
    with pytest.raises(deployment.DeploymentError, match="timer must target"):
        deployment.verify_scheduled_readers(binary)


@pytest.mark.parametrize(
    "active", ["inactive", "active", "activating", "deactivating", "", None, False, 0]
)
def test_missing_timer_requires_explicit_inactive_state(tmp_path, monkeypatch, active):
    def read(unit, name):
        if name == "LoadState":
            return "not-found"
        if name == "ActiveState":
            return active
        assert name == "MainPID"
        return 0

    monkeypatch.setattr(deployment, "_service_property", read)
    if active == "inactive":
        deployment.verify_scheduled_readers(tmp_path / "engine")
    else:
        with pytest.raises(deployment.DeploymentError):
            deployment.verify_scheduled_readers(tmp_path / "engine")


@pytest.mark.parametrize("mode", ["api", "cli"])
@pytest.mark.parametrize("timer", ["eom-email-watcher.timer", "eom-monthly-hours.timer"])
def test_redirected_timer_blocks_both_database_dispatches(tmp_path, monkeypatch, mode, timer):
    from eom_email_watcher import cli, engine_api

    binary, alias, _directory = units(tmp_path, ())
    called = []
    monkeypatch.setattr(
        deployment,
        "_service_property",
        _loaded_timer_graph(alias, {timer: "legacy-redirected-reader.service"}),
    )
    monkeypatch.setattr(engine_api, "main", lambda: called.append("api"))
    monkeypatch.setattr(cli, "main", lambda args: called.append("cli"))
    argv = [str(binary)] + (["--cli", "recent"] if mode == "cli" else [])
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(sys, "executable", str(binary))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(SystemExit) as rejected:
        deployment.main()
    assert rejected.value.code == 2
    assert called == []


@pytest.mark.parametrize(
    "name,interface,data",
    [
        ("Unit", "org.freedesktop.systemd1.Timer", "eom-email-watcher.service"),
        ("ActiveState", "org.freedesktop.systemd1.Unit", "inactive"),
    ],
)
def test_timer_property_reads_correct_typed_interface(monkeypatch, name, interface, data):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps({"type": "s", "data": data}))

    monkeypatch.setattr(subprocess, "run", run)
    assert deployment._service_property("eom-email-watcher.timer", name) == data
    assert calls[0][-2:] == [interface, name]


@pytest.mark.parametrize("name", ["Unit", "ActiveState"])
def test_timer_property_rejects_non_string_dbus_type(monkeypatch, name):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, '{"type":"u","data":0}'),
    )
    with pytest.raises(deployment.DeploymentError):
        deployment._service_property("eom-email-watcher.timer", name)


def test_systemd_base_declarations_match_canonical_deployment_jobs():
    root = Path(__file__).resolve().parents[1]
    for timer, (service, verb) in deployment.SCHEDULED_JOBS.items():
        timer_lines = (root / "systemd" / timer).read_text().splitlines()
        service_lines = (root / "systemd" / service).read_text().splitlines()
        assert "Unit=" + service in timer_lines
        assert "ExecStart=%h/.local/bin/eom-mail-watch " + verb in service_lines
