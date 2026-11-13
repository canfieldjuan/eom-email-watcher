"""#363: scheduled commands confine their own writes.

Contract: plans/PR-Scheduled-Confinement-363.md.  Every confined step runs in a separate
process (tests/confinement_probe.py); the test runner itself is never confined.
"""

from __future__ import annotations

import ast
import json
import os
import signal
import socket
import subprocess
import sys
import typing
from pathlib import Path

import pytest
from fixture_toml import toml_path_literal

from eom_email_watcher import cli, confinement
from eom_email_watcher.config import Config, load_config

pytestmark = pytest.mark.platform_contract(
    platforms=["linux"],
    owner="src/eom_email_watcher/confinement.py:127",
    reason="Landlock write confinement is Linux-only; other platforms are unchanged",
)

PROBE = Path(__file__).with_name("confinement_probe.py")
CONSOLE_SCRIPT = Path(sys.executable).with_name("eom-mail-watch")


def _home(tmp_path: Path, *, state: bool = True, extra: str = "") -> Path:
    home = tmp_path / "home"
    config_root = home / ".config" / "eom-email-watcher"
    config_root.mkdir(parents=True, mode=0o700)
    if state:
        (home / ".local" / "state" / "eom-email-watcher").mkdir(parents=True, mode=0o700)
    config = config_root / "config.toml"
    config.write_text(
        'timezone = "America/Chicago"\n'
        'model_base_url = "http://127.0.0.1:9/v1"\n'
        'model_name = "local-model"\n'
        "model_require_auth = false\n"
        "notifications_enabled = false\n" + extra,
        encoding="utf-8",
    )
    config.chmod(0o600)
    return home


def _env(home: Path) -> dict[str, str]:
    env = {
        key: value for key, value in os.environ.items()
        if key not in {"STATE_DIRECTORY", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "TMPDIR"}
    }
    env["HOME"] = str(home)
    return env


def _probe(home: Path, base: Path, case: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(PROBE), case, str(base), *args],
        env=_env(home), capture_output=True, text=True, check=False, timeout=120,
    )


def _report(run: subprocess.CompletedProcess) -> dict:
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def _cli(home: Path, *args: str) -> subprocess.CompletedProcess:
    entry = [str(CONSOLE_SCRIPT)] if CONSOLE_SCRIPT.exists() else [
        sys.executable, "-m", "eom_email_watcher.cli"
    ]
    return subprocess.run(
        [*entry, *args], env=_env(home), capture_output=True, text=True, check=False,
        timeout=120,
    )


@pytest.fixture(autouse=True)
def _landlock_present():
    # Fail, never skip: CI is where this guarantee is proved.
    assert confinement.landlock_abi() >= confinement.MINIMUM_ABI


# Item 1: the boundary, both sides.  Expectations follow the measured reproduction.
BOUNDARY = {
    "inside.create": "ok",
    "inside.append": "ok",
    "inside.truncate": "ok",
    "inside.mkdir": "ok",
    "inside.rename_between_roots": "ok",
    "inside.unlink": "ok",
    "inside.symlink": "ok",
    "inside.fifo": "ok",
    "inside.child_shell": "ok",
    "inside.tempfile": "ok",
    "devnull.write": "ok",
    "outside.create": "EACCES",
    "outside.open_write": "EACCES",
    "outside.truncate_syscall": "EACCES",
    "outside.unlink": "EACCES",
    "outside.mkdir": "EACCES",
    "outside.rmdir": "EACCES",
    "outside.rename": "EACCES",
    "outside.fifo": "EACCES",
    "outside.symlink": "EACCES",
    "outside.move_in": "EACCES",
    "outside.move_out": "EACCES",
    "outside.hard_link_in": "EXDEV",
    "outside.through_inside_symlink": "EACCES",
    "outside.prefix_sibling": "EACCES",
    "outside.root_itself_renamed": "EACCES",
    "outside.default_temp": "ENOENT",
    "outside.child_shell": "refused",
}


def test_boundary_refuses_outside_and_allows_inside(tmp_path):
    home = _home(tmp_path)
    assert _report(_probe(home, tmp_path, "boundary")) == BOUNDARY


# Item 2: the stated limits, asserted as they are, so a kernel change shows up here.
LIMITS = {
    "metadata.chmod": "ok",
    "metadata.utime": "ok",
    "metadata.setxattr": "ok",
    "metadata.chown_same_owner": "ok",
    "descriptor.opened_before": "ok",
    "hard_link.existing_before": "ok",
    "thread.started_before": "ok",
    "control.write_outside": "EACCES",
    "service.socket_outside_roots": "ok",
}


def test_stated_limits_are_not_refused(tmp_path):
    home = _home(tmp_path)
    socket_path = tmp_path / "service.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(socket_path))
        server.listen(4)
        control = _report(_probe(home, tmp_path, "services_unconfined", str(socket_path)))
        report = _report(_probe(home, tmp_path, "limits", str(socket_path)))
    assert {key: report[key] for key in LIMITS} == LIMITS
    # D4: confinement does not change what the user manager and the session bus answer.
    # Where a user session exists (the settling host), both answer "ok".
    for key in ("service.manager_property", "service.session_bus_call"):
        assert report[key] == control[key]


# Item 4: R3.
def test_written_path_keys_are_one_closed_list():
    hints = typing.get_type_hints(Config)
    path_keys = {name for name, hint in hints.items() if Path in (hint, *typing.get_args(hint))}
    written, read = set(confinement.WRITTEN_KEYS), set(confinement.READ_KEYS)
    assert not written & read
    assert path_keys == written | read


def _roots(tmp_path: Path) -> confinement.WriteRoots:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    state.chmod(0o700)
    config_root = tmp_path / "config"
    config_root.mkdir(exist_ok=True)
    config_root.chmod(0o700)  # the configuration loader requires a private folder
    return confinement.WriteRoots(config=config_root.resolve(), state=state.resolve(),
                                  legacy=None)


def _config_with(tmp_path: Path, **paths: Path) -> Config:
    path = tmp_path / "config" / "config.toml"
    path.parent.mkdir(exist_ok=True)
    lines = "".join(f"{key} = {toml_path_literal(value)}\n" for key, value in paths.items())
    path.write_text(
        'timezone = "America/Chicago"\nmodel_base_url = "http://127.0.0.1:9/v1"\n'
        'model_name = "m"\nmodel_require_auth = false\n' + lines,
        encoding="utf-8",
    )
    path.chmod(0o600)
    return load_config(path)


@pytest.mark.parametrize("key", confinement.WRITTEN_KEYS)
def test_written_key_outside_roots_is_refused_and_inside_is_admitted(tmp_path, monkeypatch, key):
    monkeypatch.setenv("STATE_DIRECTORY", str((tmp_path / "state").resolve()))
    roots = _roots(tmp_path)
    inside = _config_with(tmp_path, **{key: roots.state / "target"})
    confinement._admit_written_paths(inside, roots)
    (tmp_path / "elsewhere").mkdir()
    outside = _config_with(tmp_path, **{key: tmp_path / "elsewhere" / "target"})
    with pytest.raises(confinement.ConfinementError, match=key):
        confinement._admit_written_paths(outside, roots)
    (roots.state / "link").symlink_to(tmp_path / "elsewhere" / "target")
    linked = _config_with(tmp_path, **{key: roots.state / "link"})
    with pytest.raises(confinement.ConfinementError, match=key):
        confinement._admit_written_paths(linked, roots)
    # Lock files and atomic replacements land in the target's own folder, even when the
    # target links back inside.
    (tmp_path / "elsewhere" / "back").symlink_to(roots.state / "target")
    folder_outside = _config_with(tmp_path, **{key: tmp_path / "elsewhere" / "back"})
    with pytest.raises(confinement.ConfinementError, match=key):
        confinement._admit_written_paths(folder_outside, roots)


def test_written_key_outside_roots_stops_the_real_entry_before_network(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        listener.settimeout(0.5)
        port = listener.getsockname()[1]
        (tmp_path / "elsewhere").mkdir()
        home = _home(
            tmp_path,
            extra=f"database_file = {toml_path_literal(tmp_path / 'elsewhere' / 'db.sqlite3')}\n",
        )
        config = home / ".config" / "eom-email-watcher" / "config.toml"
        config.write_text(
            config.read_text().replace("127.0.0.1:9", f"127.0.0.1:{port}"), encoding="utf-8"
        )
        run = _cli(home, "check")
        with pytest.raises(TimeoutError):
            listener.accept()
    assert run.returncode == 2
    assert run.stdout == ""
    assert run.stderr.strip().splitlines()[-1].startswith("error: database_file is outside")
    assert not (tmp_path / "elsewhere" / "db.sqlite3").exists()


# Item 5: R4.  Each condition: exit 2, one error line, empty stdout, no access.
REFUSALS = {
    "missing": "error: Landlock is not available on this system",
    "abi2": "error: Landlock ABI 2 cannot refuse truncation",
    "thread": "error: The process has 2 threads",
    "descriptor": "error: A writable descriptor outside the write directories is open",
    "restrict": "error: Write confinement was not applied: restrict self",
    "no_new_privs": "error: Write confinement was not applied: set no_new_privs",
}


def _assert_refused_without_access(run, home: Path, message: str) -> None:
    assert run.returncode == 2, run.stderr
    assert run.stdout == ""
    errors = [line for line in run.stderr.splitlines() if line.startswith("error:")]
    assert len(errors) == 1 and errors[0].startswith(message), run.stderr
    state = home / ".local" / "state" / "eom-email-watcher"
    # Loading the configuration takes this lock; opening the database creates the file.
    assert not (state / "config-serialization.lock").exists()
    assert not (state / "watcher.sqlite3").exists()


@pytest.mark.parametrize("cause", sorted(REFUSALS))
def test_each_refusal_exits_two_before_any_access(tmp_path, cause):
    home = _home(tmp_path)
    run = _probe(home, tmp_path, "refuse", cause)
    _assert_refused_without_access(run, home, REFUSALS[cause])


def test_missing_state_root_refuses_through_the_real_entry(tmp_path):
    home = _home(tmp_path, state=False)
    run = _cli(home, "check")
    assert run.returncode == 2 and run.stdout == ""
    assert run.stderr.strip().splitlines()[-1].startswith("error: The state directory is missing")


def test_private_temp_failure_refuses_through_the_real_entry(tmp_path):
    home = _home(tmp_path)
    (home / ".local" / "state" / "eom-email-watcher" / "scheduled-tmp").write_text("x")
    run = _cli(home, "send-hours", "--dry-run")
    assert run.returncode == 2 and run.stdout == ""
    assert run.stderr.strip().splitlines()[-1].startswith(
        "error: The private temp directory cannot be created"
    )


def _environment_keys(source: Path) -> set[str]:
    keys = set()
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Subscript) and ast.unparse(node.value) == "os.environ":
            keys.add(ast.unparse(node.slice))
        if isinstance(node, ast.Call) and ast.unparse(node.func) in {
            "os.environ.get", "os.environ.pop", "os.getenv"
        }:
            keys.add(ast.unparse(node.args[0]))
    return keys


def test_no_environment_variable_steers_confinement():
    # The only variable the owner touches is the TMPDIR it sets for the run (R5).
    assert _environment_keys(Path(confinement.__file__)) == {"'TMPDIR'"}


# Item 6: R5.
def _start_hold(home: Path, base: Path) -> tuple[subprocess.Popen, Path]:
    process = subprocess.Popen(
        [sys.executable, str(PROBE), "hold", str(base)],
        env=_env(home), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    line = process.stdout.readline()
    assert line, process.stderr.read()
    return process, Path(json.loads(line)["private"])


def test_private_temp_is_per_run_survives_live_runs_and_is_swept_after_kill(tmp_path):
    home = _home(tmp_path)
    parent = home / ".local" / "state" / "eom-email-watcher" / "scheduled-tmp"
    first, first_dir = _start_hold(home, tmp_path / "a")
    second, second_dir = _start_hold(home, tmp_path / "b")
    try:
        assert first_dir != second_dir
        assert first_dir.parent == second_dir.parent == parent
        assert first_dir.is_dir() and second_dir.is_dir()  # a live run's directory survives
        assert oct(first_dir.stat().st_mode & 0o777) == "0o700"
        first.send_signal(signal.SIGKILL)
        first.wait(timeout=10)
        assert first_dir.is_dir()  # a killed run leaves it behind
        third, third_dir = _start_hold(home, tmp_path / "c")
        assert not first_dir.exists()  # the next run removes it
        assert second_dir.is_dir()
        for base in (tmp_path / "b", tmp_path / "c"):
            base.mkdir(parents=True, exist_ok=True)
            (base / "release").write_text("")
        assert second.wait(timeout=10) == 0 and third.wait(timeout=10) == 0
        assert not second_dir.exists() and not third_dir.exists()  # removed on exit
    finally:
        for process in (first, second):
            if process.poll() is None:
                process.kill()


# Item 7: SQLite temporary storage.
def test_confined_spill_succeeds_and_fails_without_the_pragma(tmp_path):
    home = _home(tmp_path)
    assert _report(_probe(home, tmp_path, "sqlite_spill", "as_shipped")) == {
        "temp_store": 2, "spill": "ok",
    }
    without = _report(_probe(home, tmp_path, "sqlite_spill", "without_pragma"))
    assert without == {
        "temp_store": 2, "spill": "OperationalError: unable to open database file",
    }


def test_every_unconfined_connection_keeps_temp_storage_in_memory(tmp_path):
    from eom_email_watcher.db import Store

    store = Store(tmp_path / "db.sqlite3")
    for _ in range(2):
        with store.connection() as connection:
            assert connection.execute("PRAGMA temp_store").fetchone()[0] == 2


# R1 wiring: one call in main, for scheduled commands only.
@pytest.mark.parametrize("command,confined", [
    (["check"], True), (["check", "--dry-run"], True), (["send-hours", "--dry-run"], True),
    (["send-hours", "--test-to", "a@example.com"], True), (["recent"], False),
    (["doctor"], False),
])
def test_main_confines_scheduled_commands_only(monkeypatch, command, confined):
    events = []

    class Entered(Exception):
        pass

    def scheduled_run(config_path):
        events.append(config_path)
        raise Entered

    monkeypatch.setattr(confinement, "scheduled_run", scheduled_run)
    monkeypatch.setattr(cli, "_dispatch", lambda args: events.append("dispatched") or 0)
    if confined:
        with pytest.raises(Entered):
            cli.main(["--config", "/x/config.toml", *command])
        assert events == [Path("/x/config.toml")]
    else:
        with pytest.raises(SystemExit):
            cli.main(["--config", "/x/config.toml", *command])
        assert events == ["dispatched"]


def test_main_reports_a_refusal_as_one_error_line(monkeypatch, capsys):
    def refuse(config_path):
        raise confinement.ConfinementError("refused for the test")

    monkeypatch.setattr(confinement, "scheduled_run", refuse)
    monkeypatch.setattr(cli, "_dispatch", lambda args: pytest.fail("body reached"))
    with pytest.raises(SystemExit) as exited:
        cli.main(["check"])
    assert exited.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: refused for the test\n"
