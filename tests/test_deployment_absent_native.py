"""Native Ubuntu systemd255 records from a fresh ordinary-user proof guest.

The data file is an unchanged selection of shipped-unit blocks from systemctl
--user show --all with deployment._MANAGER_FIELDS. No handwritten positive wire.
"""

import time
from pathlib import Path

import pytest

from eom_email_watcher import deployment

WIRE = (Path(__file__).parent / "data/systemd255-absent-watcher-units.txt").read_text()
BLOCKS = WIRE.strip("\n").split("\n\n")
SERVICES = [block for block in BLOCKS if "MainPID=" in block]


@pytest.mark.parametrize("block", SERVICES, ids=["lmstudio", "watcher", "monthly"])
@pytest.mark.parametrize("explicit_empty", [False, True])
def test_native_absent_service_accepts_omitted_or_empty_command(block, explicit_empty):
    assert "ExecStart=" not in block
    if explicit_empty:
        block += "\nExecStart="
    record = deployment._manager_record(block, deployment.UNIT_NAMES)
    assert record["ExecStart"] == ""
    assert record["LoadState"] == "not-found"


def test_native_absent_graph_reaches_shared_admission(paired_deployment, monkeypatch):
    binary, _, _, directory = paired_deployment(loaded=False)
    # Exercise both readers of the parser, including split pipe records.
    capture = deployment._ManagerCapture("units", deployment.UNIT_NAMES, time.monotonic() + 5)
    try:
        for offset in range(0, len(WIRE), 17):
            capture.feed(WIRE[offset:offset + 17].encode())
        wire = capture.finish()
    finally:
        capture.close()
    monkeypatch.setattr(deployment, "_manager_output", lambda names: wire)
    description = deployment.deployment_description(binary)
    snapshot = deployment._manager_snapshot(deployment.UNIT_NAMES)
    assert deployment._configured_graph(description, snapshot) is False
    deployment.verify_scheduled_readers(binary)
    assert list(directory.iterdir()) == []


@pytest.mark.parametrize("old,new", [
    ("LoadState=not-found", "LoadState=loaded"),
    ("ActiveState=inactive", "ActiveState=active"),
    ("MainPID=0", "MainPID=1"),
    ("ControlPID=0", "ControlPID=1"),
    ("MainPID=0", "MainPID="),
    ("ControlPID=0", "ControlPID=False"),
    ("MainPID=0", "MainPID=-1"),
    ("ControlPID=0", "ControlPID=4294967296"),
    ("MainPID=0\n", ""),
    ("ControlPID=0\n", ""),
    ("Requires=\n", ""),
    ("Transient=no", "Transient=no\nUnit=unexpected.service"),
    ("Transient=no", "Transient=no\nUnexpected=yes"),
    ("MainPID=0", "MainPID=0\nMainPID=0"),
])
def test_missing_command_does_not_relax_other_native_metadata(old, new):
    assert old in SERVICES[0]
    with pytest.raises(deployment.DeploymentError):
        deployment._manager_record(SERVICES[0].replace(old, new), deployment.UNIT_NAMES)


def test_native_absent_record_cannot_complete_partial_loaded_graph(paired_deployment):
    binary, records, _, _ = paired_deployment()
    absent = deployment._manager_record(SERVICES[0], deployment.UNIT_NAMES)
    records[absent["Id"]] = absent
    with pytest.raises(deployment.DeploymentError, match="partial"):
        deployment.verify_scheduled_readers(binary)
