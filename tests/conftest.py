import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

# Match direct script execution before any spec/runpy test module is collected.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


@pytest.fixture(autouse=True)
def restore_process_umask() -> Iterator[None]:
    # engine_api.main() sets a 0o077 umask for its one-shot process; in-process tests
    # must not leak it, or later tests pass only because of test order.
    previous = os.umask(0o022)
    os.umask(previous)
    yield
    os.umask(previous)


@pytest.fixture(autouse=True)
def _no_machine_entitlement(monkeypatch: pytest.MonkeyPatch) -> None:
    """The watcher's gated class (contract D-ops) must not follow this machine's license.

    Tests that exercise gated behavior set the decision themselves.
    """
    from connect_automate.entitlement import EntitlementDecision

    from eom_email_watcher import service

    monkeypatch.setattr(
        service, "connect_entitlement_decision", lambda: EntitlementDecision.MISSING
    )


@pytest.fixture
def paired_deployment(tmp_path, monkeypatch):
    """One public deployed graph fixture for origin and sibling regressions."""
    import shutil

    from packaged_proof_environment import render_unit_records, unit_records

    from eom_email_watcher import deployment

    def build(*, changes=None, wrong_argv=False, loaded=True):
        source = Path(__file__).resolve().parents[1] / "systemd"
        home = tmp_path / "home"
        directory = home / ".config/systemd/user"
        directory.mkdir(parents=True, exist_ok=True)
        binary = tmp_path / "eom-mail-engine"
        binary.write_bytes(b"public native inode")
        alias = home / ".local/bin/eom-mail-watch"
        alias.parent.mkdir(parents=True, exist_ok=True)
        alias.symlink_to(binary)
        bundle = tmp_path / "bundle/eom_email_watcher_data/systemd"
        bundle.mkdir(parents=True, exist_ok=True)
        for name in deployment.UNIT_NAMES:
            shutil.copyfile(source / name, bundle / name)
            if loaded:
                shutil.copyfile(source / name, directory / name)
        if wrong_argv:
            wrong = alias.with_name("wrong-name")
            wrong.symlink_to(binary)
            path = directory / "eom-email-watcher.service"
            path.write_text(path.read_text().replace("eom-mail-watch check", "wrong-name check"))
        records = unit_records(directory, loaded=loaded, home=home)
        for name, values in (changes or {}).items():
            records[name].update(values)
        monkeypatch.setattr(deployment, "_manager_environment",
                            lambda: {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config")})
        monkeypatch.setattr(deployment, "_account_home", lambda: home)
        monkeypatch.setattr(deployment, "_manager_unit_paths", lambda: (directory,))
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(bundle.parent.parent), raising=False)
        monkeypatch.setattr(sys, "executable", str(binary))
        monkeypatch.setattr(sys, "platform", "linux")
        original = deployment._same_executable
        monkeypatch.setattr(
            deployment,
            "_same_executable",
            lambda value, owner: (
                binary.samefile(owner) if value == "/proc/self/exe" else original(value, owner)
            ),
        )
        calls = []

        def output(names):
            assert names == deployment.UNIT_NAMES
            calls.append("snapshot")
            return render_unit_records(records)

        monkeypatch.setattr(deployment, "_manager_output", output)
        return binary, records, calls, directory

    return build
