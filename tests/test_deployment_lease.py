import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from eom_email_watcher import deployment, engine_api
from eom_email_watcher.db import Store

REAL_PYTHON = sys.executable
ROOT = Path(__file__).resolve().parents[1]
CHILD = """
import json,sys
from pathlib import Path
from eom_email_watcher import deployment as d
from eom_email_watcher.db import Store
sys.path.insert(0,sys.argv[1])
from packaged_proof_environment import unit_records,render_unit_records
v=json.loads(sys.argv[2]);home=Path(v['home']);binary=Path(v['binary'])
sys.frozen=True;sys._MEIPASS=v['bundle'];sys.executable=str(binary)
d.LEASE_TIMEOUT_SECONDS=2
d._manager_environment=lambda: {'HOME':str(home)}
d._account_home=lambda:home
d._manager_unit_paths=lambda:(Path(v['units']),)
d._manager_output=lambda names: render_unit_records(
 unit_records(Path(v['units']),loaded=True,home=home))
original=d._same_executable
d._same_executable=lambda value,owner: (
 binary.samefile(owner) if value=='/proc/self/exe' else original(value,owner))
if v['mode']=='exclusive':
 with d.deployment_lease(d.manager_view(),exclusive=True):
  print('READY',flush=True);sys.stdin.readline()
else:
 with Store(Path(v['db'])).connection() as connection:
  print('READY',flush=True);sys.stdin.readline()
"""


def start_holder(binary, directory, tmp_path, mode):
    values = dict(
        home=str(directory.parents[2]),
        binary=str(binary),
        units=str(directory),
        bundle=sys._MEIPASS,
        db=str(tmp_path / "holder.sqlite3"),
        mode=mode,
    )
    environment = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    process = subprocess.Popen(
        [REAL_PYTHON, "-c", CHILD, str(ROOT / "scripts"), json.dumps(values)],
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout.readline().strip() == "READY"
    return process


def stop_holder(process):
    output, error = process.communicate("release\n", timeout=5)
    assert process.returncode == 0, output + error


@pytest.mark.parametrize("publisher", ["native", "units", "shell"])
def test_every_publisher_waits_for_shared_connection(
    paired_deployment, monkeypatch, tmp_path, publisher
):
    binary, _, _, directory = paired_deployment()
    monkeypatch.setattr(deployment, "LEASE_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(deployment, "_manager_action", lambda args: None)
    process = start_holder(binary, directory, tmp_path, "shared")
    before = (directory / "eom-email-watcher.service").stat().st_ino
    try:
        if publisher == "native":
            action = deployment.install_user_services
        elif publisher == "units":
            action = deployment.install_source_units
        else:
            # A shell must not even launch until its parent owns the exclusive lease.
            monkeypatch.setattr(
                deployment.subprocess,
                "run",
                lambda *a, **kw: pytest.fail("shell launched before exclusive lease"),
            )

            def action():
                deployment.publish_source_snapshot(ROOT / "scripts/install-user-services.sh")

        with pytest.raises(deployment.DeploymentError, match="Close the app or stop the timers"):
            action()
        assert (directory / "eom-email-watcher.service").stat().st_ino == before
    finally:
        stop_holder(process)


def test_reader_timeout_retains_typed_api_refusal(paired_deployment, monkeypatch, tmp_path):
    binary, _, _, directory = paired_deployment()
    monkeypatch.setattr(deployment, "LEASE_TIMEOUT_SECONDS", 0.1)
    process = start_holder(binary, directory, tmp_path, "exclusive")
    try:
        with (
            pytest.raises(deployment.DeploymentError, match="being updated"),
            Store(tmp_path / "reader.sqlite3").connection(),
        ):
            pytest.fail("database opened under exclusive publication")
        assert not (tmp_path / "reader.sqlite3").exists()
        result = engine_api._response({"protocol": 1, "operation": "health.get", "payload": {}})
        assert result["error"]["code"] == "deployment_refused"
    finally:
        stop_holder(process)


def test_shared_readers_coexist_and_exception_releases_lease(
    paired_deployment, monkeypatch, tmp_path
):
    binary, _, _, directory = paired_deployment()
    monkeypatch.setattr(deployment, "_manager_action", lambda args: None)
    with Store(tmp_path / "reader.sqlite3").connection():
        process = start_holder(binary, directory, tmp_path, "shared")
        stop_holder(process)
    cause = OSError("public operation I/O failure")
    with pytest.raises(OSError) as result, Store(tmp_path / "reader.sqlite3").connection():
        raise cause
    assert result.value is cause
    deployment.install_user_services()


def test_connection_close_precedes_lease_release(paired_deployment, monkeypatch, tmp_path):
    import eom_email_watcher.db as db

    paired_deployment()
    events = []

    class Connection:
        row_factory = None

        def execute(self, _):
            raise RuntimeError("public setup failure")

        def rollback(self):
            events.append("rollback")

        def close(self):
            events.append("close")

    monkeypatch.setattr(db.sqlite3, "connect", lambda _: Connection())
    original = deployment.deployment_lease
    from contextlib import contextmanager

    @contextmanager
    def lease(*args, **kwargs):
        with original(*args, **kwargs) as value:
            try:
                yield value
            finally:
                events.append("lease-release")

    monkeypatch.setattr(deployment, "deployment_lease", lease)
    with (
        pytest.raises(RuntimeError, match="setup failure"),
        Store(tmp_path / "reader.sqlite3").connection(),
    ):
        pytest.fail("setup failed before body")
    assert events == ["rollback", "close", "lease-release"]


@pytest.mark.parametrize("suffix", ["direct", "indirect", "parent"])
def test_alias_selection_refuses_before_writes(paired_deployment, tmp_path, suffix):
    binary, _, _, _ = paired_deployment()
    alias = deployment.deployment_description(binary).alias
    if suffix == "direct":
        selected = alias
    elif suffix == "indirect":
        selected = tmp_path / "through-alias"
        selected.symlink_to(alias)
    else:
        parent = tmp_path / "home-link"
        parent.symlink_to(alias.parents[2], target_is_directory=True)
        selected = parent / ".local/bin/eom-mail-watch"
    original = alias.readlink()
    with pytest.raises(deployment.DeploymentError, match="sidecar"):
        deployment.deployment_description(selected)
    assert alias.readlink() == original


def test_loaded_execstart_must_name_manager_alias(paired_deployment):
    binary, _, _, _ = paired_deployment(
        changes={
            "eom-email-watcher.service": {"ExecStart": "{ path=/public/stale ; argv[]=old ; }"}
        }
    )
    with pytest.raises(deployment.DeploymentError, match="ExecStart"):
        deployment.verify_scheduled_readers(binary)


def test_publication_waits_until_other_connection_closes(paired_deployment, monkeypatch, tmp_path):
    import threading

    binary, _, _, directory = paired_deployment()
    monkeypatch.setattr(deployment, "_manager_action", lambda args: None)
    monkeypatch.setattr(deployment, "LEASE_TIMEOUT_SECONDS", 2)
    process = start_holder(binary, directory, tmp_path, "shared")
    started, completed = threading.Event(), threading.Event()
    errors = []

    def publish():
        started.set()
        try:
            deployment.install_user_services()
        except Exception as error:
            errors.append(error)
        finally:
            completed.set()

    thread = threading.Thread(target=publish)
    thread.start()
    assert started.wait(2)
    try:
        assert not completed.wait(0.1), "publication completed during an open connection"
    finally:
        stop_holder(process)
        thread.join(timeout=5)
    assert completed.is_set()
    assert errors == []


@pytest.mark.parametrize(
    "environment", [{}, {"HOME": ""}, {"HOME": "relative"}, {"HOME": "/bad\npath"}]
)
def test_manager_path_missing_or_invalid_refuses_without_invoking_fallback(
    monkeypatch, environment
):
    monkeypatch.setenv("HOME", "/public/invoking")
    monkeypatch.setattr(deployment, "_manager_environment", lambda: environment)
    with pytest.raises(deployment.DeploymentError, match="manager"):
        deployment.manager_view()


def test_manager_environment_parser_keeps_quoted_paths_and_ignores_other_keys(monkeypatch):
    monkeypatch.setattr(
        deployment,
        "_read_manager_command",
        lambda _: (
            "HOME=$'/public/manager home'\n"
            "XDG_CONFIG_HOME=$'/public/custom config'\nOTHER=ignored\n"
        ),
    )
    monkeypatch.setattr(deployment, "_account_home", lambda: Path("/public/manager home"))
    monkeypatch.setattr(
        deployment, "_manager_unit_paths", lambda: (Path("/public/custom config/systemd/user"),)
    )
    view = deployment.manager_view()
    assert view.alias == Path("/public/manager home/.local/bin/eom-mail-watch")
    assert view.unit_directory == Path("/public/custom config/systemd/user")
    assert view.home == Path("/public/manager home")


@pytest.mark.parametrize("suffix", ["direct", "indirect", "parent"])
def test_publication_dispatch_preserves_selected_launch_path(
    paired_deployment, monkeypatch, tmp_path, suffix
):
    binary, _, _, _ = paired_deployment()
    alias = deployment.deployment_description(binary).alias
    if suffix == "direct":
        selected = alias
    elif suffix == "indirect":
        selected = tmp_path / "through-alias"
        selected.symlink_to(alias)
    else:
        parent = tmp_path / "home-link"
        parent.symlink_to(alias.parents[2], target_is_directory=True)
        selected = parent / ".local/bin/eom-mail-watch"
    monkeypatch.setattr(sys, "argv", [str(selected), "--install-user-services"])
    actions = []
    monkeypatch.setattr(deployment, "_manager_action", lambda args: actions.append(args))
    with pytest.raises(deployment.DeploymentError, match="sidecar"):
        deployment._dispatch_entrypoint()
    assert actions == []


@pytest.mark.parametrize("publisher", ["native", "units", "shell"])
@pytest.mark.parametrize("cleanup", ["delete-file", "recreate-file", "delete-state"])
def test_state_lock_cleanup_does_not_break_open_connection_exclusion(
    paired_deployment, monkeypatch, tmp_path, publisher, cleanup
):
    binary, _, _, directory = paired_deployment()
    home = directory.parents[2]
    legacy = home / ".local/state/eom-email-watcher/deployment.lock"
    legacy.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    legacy.write_bytes(b"public legacy state lock")
    monkeypatch.setattr(deployment, "LEASE_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(deployment, "_manager_action", lambda args: None)
    process = start_holder(binary, directory, tmp_path, "shared")
    alias = home / ".local/bin/eom-mail-watch"
    alias_before = alias.lstat().st_ino
    units_before = {p.name: p.read_bytes() for p in directory.iterdir()}
    database = tmp_path / "holder.sqlite3"
    database_before = database.read_bytes()
    try:
        legacy.unlink()
        if cleanup == "recreate-file":
            legacy.write_bytes(b"replacement public state inode")
        elif cleanup == "delete-state":
            legacy.parent.rmdir()
            legacy.parent.parent.rmdir()
        if publisher == "native":
            action = deployment.install_user_services
        elif publisher == "units":
            action = deployment.install_source_units
        else:
            monkeypatch.setattr(
                deployment.subprocess, "run",
                lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0),
            )
            def action():
                deployment.publish_source_snapshot(ROOT / "scripts/install-user-services.sh")
        with pytest.raises(deployment.DeploymentError, match="Close the app or stop the timers"):
            action()
        assert process.poll() is None
        assert alias.lstat().st_ino == alias_before
        assert {p.name: p.read_bytes() for p in directory.iterdir()} == units_before
        assert database.read_bytes() == database_before
    finally:
        stop_holder(process)


def test_deployment_coordination_has_one_owner():
    tree = ast.parse(Path(deployment.__file__).read_text())
    lock_owners = []
    identity_owners = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            if isinstance(call.func, ast.Attribute) and call.func.attr == "flock":
                lock_owners.append(node.name)
            if isinstance(call.func, ast.Name) and call.func.id == "_verify_lease_identity":
                identity_owners.append(node.name)
    assert lock_owners == ["deployment_lease"], "coordination bypasses the single lease owner"
    assert identity_owners == ["deployment_lease", "deployment_lease"]


def test_unrelated_home_lock_produces_bounded_typed_refusal(
    paired_deployment, monkeypatch, tmp_path
):
    import time
    binary, _, _, directory = paired_deployment()
    home = directory.parents[2]
    code = (
        "import fcntl,os,sys;fd=os.open(sys.argv[1],os.O_RDONLY|os.O_DIRECTORY);"
        "fcntl.flock(fd,fcntl.LOCK_EX);print('READY',flush=True);sys.stdin.readline()"
    )
    process = subprocess.Popen(
        [REAL_PYTHON, "-c", code, str(home)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert process.stdout.readline().strip() == "READY"
    monkeypatch.setattr(deployment, "LEASE_TIMEOUT_SECONDS", 0.1)
    started = time.monotonic()
    try:
        with (
            pytest.raises(deployment.DeploymentError, match="being updated"),
            Store(tmp_path / "refused.sqlite3").connection(),
        ):
            pytest.fail("unrelated home lock did not block the reader")
        assert time.monotonic() - started < 2
        assert not (tmp_path / "refused.sqlite3").exists()
        response = engine_api._response({"protocol": 1, "operation": "health.get", "payload": {}})
        assert response["error"]["code"] == "deployment_refused"
    finally:
        stop_holder(process)


def test_unsupported_directory_lock_refuses_without_state_file_fallback(
    paired_deployment, monkeypatch
):
    import errno
    import fcntl
    paired_deployment()
    view = deployment.manager_view()
    def unsupported(fd, flags):
        raise OSError(errno.EOPNOTSUPP, "public unsupported directory flock")
    monkeypatch.setattr(fcntl, "flock", unsupported)
    with (
        pytest.raises(deployment.DeploymentError, match="unavailable"),
        deployment.deployment_lease(view, exclusive=False),
    ):
        pytest.fail("unsupported directory flock was admitted")
    assert not (view.home / ".local/state").exists()


def test_missing_account_home_anchor_refuses(paired_deployment):
    paired_deployment()
    view = deployment.manager_view()
    view.home.rename(view.home.with_name("displaced-public-home"))
    with (
        pytest.raises(deployment.DeploymentError, match="unavailable"),
        deployment.deployment_lease(view, exclusive=False),
    ):
        pytest.fail("missing home anchor was admitted")
