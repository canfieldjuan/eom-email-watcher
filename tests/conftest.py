import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from pathlib import Path

import pytest

# Match direct script execution before any spec/runpy test module is collected.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

_PRODUCT_ROOT = Path(__file__).resolve().parents[1]
_FIXTURE_CONTRACTS = {}
_EXCLUSIONS = pytest.StashKey[list[dict]]()


def platform_fixture(*, platforms, owner, reason):
    """Declare applicability once where a shared fixture owns the mechanism."""
    def declare(function):
        _FIXTURE_CONTRACTS[function.__name__] = pytest.mark.platform_contract(
            platforms=platforms, owner=owner, reason=reason
        ).mark
        return function
    return declare


def _platform_tags():
    return {os.name, sys.platform}


def _checked_contract(marker):
    data = marker.kwargs
    error = "invalid platform contract: expected platforms, product owner file:line, and reason"
    if marker.args or set(data) != {"platforms", "owner", "reason"}:
        raise pytest.UsageError(error)
    platforms, owner, reason = (data[key] for key in ("platforms", "owner", "reason"))
    if (not isinstance(platforms, (list, tuple)) or not platforms
            or any(not isinstance(p, str)
                   or p not in {"posix", "nt", "linux", "win32", "darwin"} for p in platforms)
            or not isinstance(owner, str) or not isinstance(reason, str) or not reason.strip()):
        raise pytest.UsageError(error)
    match = re.fullmatch(r"(src/eom_email_watcher/[A-Za-z0-9_/]+\.py):([1-9][0-9]*)", owner)
    if match is None:
        raise pytest.UsageError(error)
    source = _PRODUCT_ROOT / match[1]
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
        if not lines[int(match[2]) - 1].strip():
            raise ValueError("empty source line")
    except (OSError, IndexError, ValueError) as exc:
        raise pytest.UsageError(error + f": {owner}") from exc
    return {"platforms": list(platforms), "owner": owner, "reason": reason}


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "platform_contract(platforms, owner, reason): cited product applicability"
    )
    config.stash[_EXCLUSIONS] = []


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    fixtures = {name: _checked_contract(mark) for name, mark in _FIXTURE_CONTRACTS.items()}
    applicable, excluded = [], []
    inventory = []
    tags = _platform_tags()
    for item in items:
        contracts = [_checked_contract(mark) for mark in item.iter_markers("platform_contract")]
        contracts += [fixtures[name] for name in item.fixturenames if name in fixtures]
        outside = [
            contract for contract in contracts if not tags.intersection(contract["platforms"])
        ]
        if outside:
            excluded.append(item)
            inventory.append({"nodeid": item.nodeid, "contracts": outside})
        else:
            applicable.append(item)
    config.stash[_EXCLUSIONS] = inventory
    if excluded:
        config.hook.pytest_deselected(items=excluded)
        items[:] = applicable


def pytest_terminal_summary(terminalreporter, config):
    inventory = config.stash[_EXCLUSIONS]
    terminalreporter.write_line(f"platform-contract excluded count: {len(inventory)}")
    terminalreporter.write_line(
        "platform-contract excluded: " + json.dumps(inventory, sort_keys=True)
    )


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    # The outer hook resumes after pytest's JUnit writer. Preserve all testcase
    # results/counts and add only the same explicit inventory printed above.
    result = yield
    xml_path = session.config.getoption("--junitxml", default=None)
    if xml_path is None:
        return result
    document = ET.parse(xml_path)
    suite = document.getroot().find("testsuite")
    if suite is None:
        raise pytest.UsageError("platform contract inventory requires a JUnit testsuite")
    properties = suite.find("properties")
    if properties is None:
        properties = ET.Element("properties")
        suite.insert(0, properties)
    inventory = session.config.stash[_EXCLUSIONS]
    for name, value in {
        "platform_contract.excluded_count": str(len(inventory)),
        "platform_contract.excluded": json.dumps(inventory, sort_keys=True),
    }.items():
        ET.SubElement(properties, "property", name=name, value=value)
    document.write(xml_path, encoding="utf-8", xml_declaration=True)
    return result


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
@platform_fixture(
    platforms=("linux",), owner="src/eom_email_watcher/deployment.py:223",
    reason="Linux deployment_lease uses fcntl and the paired systemd graph",
)
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
            return render_unit_records({
                name: {key: value for key, value in record.items()
                       if key in deployment._MANAGER_FIELDS}
                for name, record in records.items()
                if name in deployment.UNIT_NAMES or hasattr(deployment, "_PLATFORM_SELECTORS")
            })

        monkeypatch.setattr(deployment, "_manager_output", output)
        return binary, records, calls, directory

    return build


@pytest.fixture
def tmp_path(tmp_path, request):
    """Route every native qualification module through the same evidence owner."""
    if request.module.__name__ not in {
        "test_windows_context_probe",
        "test_windows_cutover",
        "test_windows_cutover_round9",
    }:
        return tmp_path
    from prove_windows_cutover import probe_output

    return probe_output(request.node.name, tmp_path)


def local_removal(store, method, *args, **kwargs):
    """Exercise the real Store method under its required native entry owner."""
    from eom_email_watcher.removal import acceptance_scope

    with acceptance_scope(store) as acceptance:
        return getattr(store, method)(*args, acceptance=acceptance, **kwargs)


@pytest.fixture
def pre36_schema(monkeypatch):
    """Seed only the pre-removal catalog; run the tested upgrade as v36.

    Plan 31–33/76–77: older fixtures must not carry schema-36 receipt tables.
    The authentic main-source v35 upgrade is also qualified independently.
    """
    from contextlib import contextmanager

    from eom_email_watcher import db as database

    @contextmanager
    def seed():
        with monkeypatch.context() as legacy:
            legacy.setattr(database, "SCHEMA_VERSION", 35)
            yield

    return seed


@pytest.fixture
def assert_private_file():
    """Use the actual platform privacy oracle on the same persisted output."""
    def check(path):
        if os.name == "nt":
            from connect_automate import connect_windows

            connect_windows.validate_private_regular_file(path, root=path.parent)
        else:
            assert path.stat().st_mode & 0o777 == 0o600
    return check
