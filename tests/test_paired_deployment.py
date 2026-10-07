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


def test_unconfigured_services_need_no_systemd_bus(tmp_path: Path, monkeypatch) -> None:
    def unexpected(*args):
        raise AssertionError("unconfigured services queried")

    monkeypatch.setattr(deployment, "_service_property", unexpected)
    deployment.verify_scheduled_readers(tmp_path / "engine", tmp_path / "missing-units")


@pytest.mark.parametrize("bad", ["legacy", "wrong-argv", "wrong-command", "ignore", "active"])
def test_mixed_scheduled_readers_block_before_request_or_migration(
    tmp_path: Path, monkeypatch, bad
):
    binary, alias, directory = units(tmp_path, tuple(deployment.SCHEDULED_COMMANDS))
    calls = []

    def read(unit, name):
        calls.append((unit, name))
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

    monkeypatch.setattr(deployment, "_service_property", read)
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

    monkeypatch.setattr(deployment, "_service_property", read)
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
    monkeypatch.setattr(deployment, "_service_property", lambda *args: metadata)
    with pytest.raises(deployment.DeploymentError):
        deployment.verify_scheduled_readers(binary, directory)


@pytest.mark.parametrize("pid", [None, False, "0", -1])
def test_invalid_active_worker_metadata_is_not_defaulted(tmp_path: Path, monkeypatch, pid):
    binary, alias, directory = units(tmp_path, ("eom-email-watcher.service",))

    def read(unit, name):
        if name == "MainPID":
            return pid
        return [[str(alias), [str(alias), "check"], False, 0, 0, 0, 0, 0, 0, 0]]

    monkeypatch.setattr(deployment, "_service_property", read)
    with pytest.raises(deployment.DeploymentError):
        deployment.verify_scheduled_readers(binary, directory)


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
        if name == "MainPID":
            return 0
        path = str(alias) if paired else str(tmp_path / "legacy-cli")
        return [[path, [path, "check"], False, 0, 0, 0, 0, 0, 0, 0]]

    monkeypatch.setattr(deployment, "_service_property", read)
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
    monkeypatch.setattr(
        deployment,
        "_service_property",
        lambda *args: [
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
        ],
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
def test_only_own_scheduled_worker_is_admitted(tmp_path, monkeypatch, pid, allowed):
    binary, alias, directory = units(tmp_path, ("eom-email-watcher.service",))

    def read(unit, name):
        if name == "MainPID":
            return pid
        return [[str(alias), [str(alias), "check"], False, 0, 0, 0, 0, 0, 0, 0]]

    monkeypatch.setattr(deployment, "_service_property", read)
    if allowed:
        deployment.verify_scheduled_readers(binary, directory, frozenset({123}))
    else:
        with pytest.raises(deployment.DeploymentError, match="still active"):
            deployment.verify_scheduled_readers(binary, directory, frozenset({123}))
