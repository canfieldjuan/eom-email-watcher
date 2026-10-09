"""Regression probes for authoritative deployment identity and proof results."""

import json
import os
import subprocess
from pathlib import Path

import pytest
import run_deployment_receiver_proof as receiver

from eom_email_watcher import deployment


def test_wrong_account_home_refuses_before_publication(paired_deployment, monkeypatch, tmp_path):
    _, _, _, directory = paired_deployment(loaded=False)
    real_home = directory.parents[2]
    wrong = tmp_path / "wrong-exported-home"
    monkeypatch.setattr(
        deployment,
        "_manager_environment",
        lambda: {"HOME": str(wrong), "XDG_CONFIG_HOME": str(real_home / ".config")},
    )
    monkeypatch.setattr(deployment, "_account_home", lambda: real_home, raising=False)
    monkeypatch.setattr(deployment, "_manager_unit_paths", lambda: (directory,), raising=False)
    monkeypatch.setattr(deployment, "_manager_action", lambda args: None)
    with pytest.raises(deployment.DeploymentError):
        deployment.install_user_services()
    assert not wrong.exists(), "wrong-home alias/lock writes preceded refusal"
    assert list(directory.iterdir()) == [], "unit writes preceded refusal"


def test_unit_path_disagreement_refuses_before_publication(paired_deployment, monkeypatch):
    _, _, _, directory = paired_deployment()
    alias = directory.parents[2] / ".local/bin/eom-mail-watch"
    before = alias.lstat().st_ino
    monkeypatch.setattr(deployment, "_account_home", lambda: directory.parents[2], raising=False)
    monkeypatch.setattr(
        deployment, "_manager_unit_paths", lambda: (Path("/public/other"),), raising=False
    )
    monkeypatch.setattr(deployment, "_manager_action", lambda args: None)
    with pytest.raises(deployment.DeploymentError, match="UnitPath"):
        deployment.install_user_services()
    assert alias.lstat().st_ino == before


@pytest.mark.parametrize(
    "summary",
    [
        "test result: ok. 0 passed; 0 failed; 0 ignored;",
        "test result: ok. 2 passed; 0 failed; 0 ignored;",
        "test result: ok. 1 passed; 0 failed; 1 ignored;",
        "test result: FAILED. 0 passed; 1 failed; 0 ignored;",
        "unrelated log says 1 passed",
    ],
)
def test_receiver_proof_rejects_zero_executed_tests(monkeypatch, tmp_path, summary):
    engine = tmp_path / "engine"
    engine.write_bytes(b"public engine fixture")
    monkeypatch.setattr(receiver, "with_shipped_user_manager", lambda root, env: env)
    monkeypatch.setattr(receiver.os.sys, "argv", ["proof", str(engine)])

    def run(command, **kwargs):
        if command[0] == "cargo":
            item = {
                "reason": "compiler-artifact",
                "profile": {"test": True},
                "executable": "/public/desktop-test",
                "target": {"name": "eom_email_watcher_desktop"},
            }
            output = json.dumps(item) + "\n"
        else:
            output = summary + "\n"
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(receiver.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="one passing"):
        receiver.main()


@pytest.mark.parametrize(
    "encoded, expected",
    [
        ("/public/plain", "/public/plain"),
        ("$'/public/user name'", "/public/user name"),
        (r"$'/public/\303\251'", "/public/\u00e9"),
        (r"$'/public/a\\b\'c'", "/public/a\\b'c"),
        ("$'/public/$(touch sentinel)'", "/public/$(touch sentinel)"),
    ],
)
def test_manager_environment_decodes_native_wire(monkeypatch, encoded, expected):
    monkeypatch.setattr(deployment, "_read_manager_command", lambda _: "HOME=" + encoded + "\n")
    assert deployment._manager_environment() == {"HOME": expected}


@pytest.mark.parametrize("wire", ["$'unfinished", r"$'/bad\q'", r"$'/bad\777'", "a 'bad", "\\"])
def test_manager_wire_rejects_malformed_values(wire):
    with pytest.raises(deployment.DeploymentError):
        deployment._manager_words(wire)


def test_admission_runs_inside_the_shared_lease(paired_deployment, monkeypatch, tmp_path):
    from eom_email_watcher.db import Store

    paired_deployment()
    original = deployment._verify_description

    def verify(*args, **kwargs):
        assert deployment._shared_leases > 0, "admission ran outside shared lease"
        return original(*args, **kwargs)

    monkeypatch.setattr(deployment, "_verify_description", verify)
    with Store(tmp_path / "public.sqlite3").connection():
        pass


def test_source_unit_verification_retains_exclusive_lease(paired_deployment, monkeypatch):
    import fcntl

    paired_deployment()
    view = deployment.manager_view()
    original = deployment._manager_output

    def snapshot(names):
        descriptor = os.open(view.lease_anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with pytest.raises(BlockingIOError, match="temporarily unavailable"):
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(descriptor)
        return original(names)

    monkeypatch.setattr(deployment, "_manager_output", snapshot)
    monkeypatch.setattr(deployment, "_manager_action", lambda args: None)
    with deployment.deployment_lease(view, exclusive=True) as inherited:
        deployment.install_source_units(inherited)


@pytest.mark.parametrize("wire", ["", "relative", "/public/unit relative", r"$'/public/\000'"])
def test_manager_unit_path_rejects_partial_or_invalid_arrays(monkeypatch, wire):
    monkeypatch.setattr(deployment, "_read_manager_command", lambda _: wire)
    with pytest.raises(deployment.DeploymentError, match="UnitPath"):
        deployment._manager_unit_paths()


def test_unit_path_uses_same_byte_decoder_as_environment(monkeypatch):
    wire = r"$'/public/user name/\303\251/systemd/user' /public/other"
    monkeypatch.setattr(deployment, "_read_manager_command", lambda _: wire)
    assert deployment._manager_unit_paths() == (
        Path("/public/user name/\u00e9/systemd/user"),
        Path("/public/other"),
    )



def test_home_replacement_during_acquisition_refuses(paired_deployment, monkeypatch):
    import fcntl
    paired_deployment()
    view = deployment.manager_view()
    original = fcntl.flock
    def acquire_then_replace(fd, flags):
        original(fd, flags)
        view.home.rename(view.home.with_name("displaced-public-home"))
        view.home.mkdir(mode=0o700)
    monkeypatch.setattr(fcntl, "flock", acquire_then_replace)
    with (
        pytest.raises(deployment.DeploymentError),
        deployment.deployment_lease(view, exclusive=True),
    ):
        pytest.fail("acquired stale coordination inode")
