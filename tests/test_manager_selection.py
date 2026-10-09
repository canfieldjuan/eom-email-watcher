"""Consumed manager projection, transport budgets and order-independent capture."""

import subprocess
import sys
import time
from pathlib import Path

import pytest
from packaged_proof_environment import render_unit_records, unit_records

from eom_email_watcher import deployment


@pytest.fixture
def manager_pipe(monkeypatch, tmp_path):
    original = subprocess.Popen
    calls = []

    def provide(wire, *, child=None):
        path = tmp_path / "public-manager-wire"
        path.write_bytes(wire.encode() if isinstance(wire, str) else wire)
        script = child or (
            "import sys;from pathlib import Path;"
            "sys.stdout.buffer.write(Path(sys.argv[1]).read_bytes())"
        )

        def spawn(command, **kwargs):
            process = original([sys.executable, "-c", script, str(path)], **kwargs)
            calls.append((command, process))
            return process

        monkeypatch.setattr(deployment.subprocess, "Popen", spawn)
        return calls

    return provide


def graph():
    return unit_records(
        Path("/public/home/.config/systemd/user"), loaded=True, home=Path("/public/home")
    )


@pytest.mark.parametrize("padding", ["one-large", "many-small"])
def test_unrelated_environment_does_not_consume_selected_budget(manager_pipe, padding):
    extra = (
        ("IRRELEVANT=" + "x" * 80000 + "\n")
        if padding == "one-large"
        else "".join("UNUSED_" + str(i) + "=" + "x" * 100 + "\n" for i in range(800))
    )
    wanted = {
        "HOME": "/public/home",
        "XDG_CONFIG_HOME": "/public/home/.config",
        "XDG_DATA_HOME": "/public/home/.local/share",
    }
    calls = manager_pipe(extra + "".join(key + "=" + value + "\n" for key, value in wanted.items()))
    assert deployment._manager_environment() == wanted
    assert len(calls) == 1 and calls[0][1].returncode == 0


@pytest.mark.parametrize("root_order", ["first", "last"])
def test_unrelated_platform_records_do_not_consume_selected_budget(manager_pipe, root_order):
    records = graph()
    unrelated = {}
    for i in range(500):
        name = "unrelated-" + str(i) + ".slice"
        unrelated[name] = dict(records["app.slice"], Id=name, Names=name)
    records = (records | unrelated) if root_order == "first" else (unrelated | records)
    wire = render_unit_records(records)
    assert len(wire.encode()) > deployment.MAX_MANAGER_BYTES
    calls = manager_pipe(wire)
    snapshot = deployment._manager_snapshot(deployment.UNIT_NAMES)
    assert set(snapshot) == set(graph())
    assert len(calls) == 1 and calls[0][1].returncode == 0


@pytest.mark.parametrize("order", ["first", "last"])
@pytest.mark.parametrize("ambiguous", [False, True])
def test_direct_dependency_aliases_preserve_provenance(manager_pipe, order, ambiguous):
    records = graph()
    records["eom-email-watcher.service"]["Requires"] += " future-alias.target"
    candidate = dict(
        records["basic.target"],
        Id="vendor-future.target",
        Names="vendor-future.target future-alias.target",
    )
    extra = {candidate["Id"]: candidate}
    if ambiguous:
        extra["other.target"] = dict(
            candidate, Id="other.target", Names="other.target future-alias.target"
        )
    records = (extra | records) if order == "first" else (records | extra)
    manager_pipe(render_unit_records(records))
    snapshot = deployment._manager_snapshot(deployment.UNIT_NAMES)
    assert set(extra).issubset(snapshot)
    if ambiguous:
        with pytest.raises(deployment.DeploymentError, match="ambiguous"):
            deployment._platform_dependency("future-alias.target", snapshot, declared=False)
    else:
        deployment._platform_dependency("future-alias.target", snapshot, declared=False)


@pytest.mark.parametrize("cause", ["missing-root", "missing-field", "mixed-record"])
def test_selected_missing_or_mixed_metadata_refuses(manager_pipe, cause):
    records = graph()
    if cause == "missing-root":
        records.pop("eom-email-watcher.timer")
    elif cause == "missing-field":
        records["eom-email-watcher.service"].pop("Requires")
    else:
        records["basic.target"]["unknown"] = "unexpected"
    manager_pipe(render_unit_records(records))
    with pytest.raises(deployment.DeploymentError):
        deployment._manager_snapshot(deployment.UNIT_NAMES)


def test_oversized_selected_environment_still_refuses(manager_pipe):
    calls = manager_pipe("HOME=/" + "x" * (deployment.MAX_MANAGER_BYTES + 1) + "\n")
    with pytest.raises(deployment.DeploymentError, match="too much"):
        deployment._manager_environment()
    assert calls[0][1].poll() is not None


def test_oversized_unselected_record_still_refuses(manager_pipe):
    records = graph()
    records["unrelated.target"] = dict(
        records["basic.target"],
        Id="unrelated.target",
        Names="x" * (deployment.MAX_MANAGER_BYTES + 1),
    )
    calls = manager_pipe(render_unit_records(records))
    with pytest.raises(deployment.DeploymentError, match="too much"):
        deployment._manager_snapshot(deployment.UNIT_NAMES)
    assert calls[0][1].poll() is not None


def test_selected_graph_aggregate_is_bounded(manager_pipe):
    records = graph()
    for name in deployment.UNIT_NAMES:
        records[name]["FragmentPath"] = "/" + "x" * 20000
    calls = manager_pipe(render_unit_records(records))
    with pytest.raises(deployment.DeploymentError, match="too much"):
        deployment._manager_snapshot(deployment.UNIT_NAMES)
    assert calls[0][1].poll() is not None


@pytest.mark.parametrize("delta", [-1, 0, 1])
@pytest.mark.parametrize("mode", ["all", "environment"])
def test_retained_budget_boundary(mode, delta):
    size = deployment.MAX_MANAGER_BYTES + delta
    capture = deployment._ManagerCapture(mode, (), time.monotonic() + 5)
    wire = b"HOME=" + b"x" * (size - 5)
    try:
        if delta > 0:
            with pytest.raises(deployment.DeploymentError, match="too much"):
                for offset in range(0, len(wire), 8192):
                    capture.feed(wire[offset : offset + 8192])
                capture.finish()
        else:
            for offset in range(0, len(wire), 8192):
                capture.feed(wire[offset : offset + 8192])
            assert capture.finish().encode() == wire
    finally:
        capture.close()


@pytest.mark.parametrize("cap", [0, False])
def test_zero_cap_cannot_default_to_unbounded(monkeypatch, cap):
    monkeypatch.setattr(deployment, "MAX_MANAGER_BYTES", cap)
    capture = deployment._ManagerCapture("environment", (), time.monotonic() + 5)
    with pytest.raises(deployment.DeploymentError, match="too much"):
        capture.feed(b"HOME=/public/home")


@pytest.mark.parametrize("width", [1, 2, 7, 8192])
def test_chunk_boundaries_preserve_utf8_and_record_delimiters(width):
    records = graph()
    records["eom-email-watcher.service"]["FragmentPath"] = "/public/caf" + chr(233)
    wire = render_unit_records(records).encode()
    capture = deployment._ManagerCapture("units", deployment.UNIT_NAMES, time.monotonic() + 5)
    try:
        for offset in range(0, len(wire), width):
            capture.feed(wire[offset : offset + width])
        result = capture.finish()
        assert ("/public/caf" + chr(233)) in result
        assert capture.scratch is not None
    finally:
        scratch = capture.scratch
        capture.close()
        assert scratch.closed


@pytest.mark.parametrize("width", [1, 2, 7])
def test_environment_key_and_utf8_chunk_boundaries(width):
    wire = (
        "HOME=/public/caf" + chr(233) + "\nHOME_EXTRA=ignored\nXDG_DATA_HOME=/public/data"
    ).encode()
    capture = deployment._ManagerCapture("environment", (), time.monotonic() + 5)
    for offset in range(0, len(wire), width):
        capture.feed(wire[offset : offset + width])
    assert capture.finish() == "HOME=/public/caf" + chr(233) + "\nXDG_DATA_HOME=/public/data"


@pytest.mark.parametrize("wire", [b"HOME=\xff\n", b'HOME=$"unterminated\n'])
def test_selected_invalid_wire_refuses(manager_pipe, wire):
    manager_pipe(wire)
    with pytest.raises(deployment.DeploymentError):
        deployment._manager_environment()


def test_timeout_closes_scratch_and_kills_child(manager_pipe, monkeypatch):
    monkeypatch.setattr(deployment, "MANAGER_TIMEOUT_SECONDS", 0.1)
    captures = []
    original = deployment._ManagerCapture

    def capture(*args):
        item = original(*args)
        captures.append(item)
        return item

    monkeypatch.setattr(deployment, "_ManagerCapture", capture)
    calls = manager_pipe("", child="import time;time.sleep(10)")
    with pytest.raises(deployment.DeploymentError, match="deadline"):
        deployment._manager_output(deployment.UNIT_NAMES)
    assert calls[0][1].poll() is not None
    assert captures[0].scratch.closed
