import os
import subprocess
import sys
from pathlib import Path

import pytest
from packaged_proof_environment import render_unit_records

from eom_email_watcher import deployment

REAL_PYTHON = sys.executable


def test_canonical_loaded_and_all_absent_graphs_pass(paired_deployment):
    binary, _, calls, _ = paired_deployment()
    deployment.verify_database_admission()
    assert calls == ["snapshot"]
    assert len(deployment.deployment_description(binary).units) == 5


def test_all_absent_graph_allows_first_start(paired_deployment):
    _, _, calls, _ = paired_deployment(loaded=False)
    deployment.verify_database_admission()
    assert calls == ["snapshot"]


@pytest.mark.parametrize("name", deployment.UNIT_NAMES)
@pytest.mark.parametrize("failure", ["missing", "edited", "masked", "partial"])
def test_every_unit_is_pinned(paired_deployment, name, failure):
    binary, records, _, directory = paired_deployment()
    if failure == "missing":
        (directory / name).unlink()
    elif failure == "edited":
        with (directory / name).open("ab") as file:
            file.write(b"\nOnSuccess=legacy-reader.service\n")
    elif failure == "masked":
        records[name]["LoadState"] = "masked"
    else:
        records[name]["LoadState"] = "not-found"
    with pytest.raises(deployment.DeploymentError):
        deployment.verify_scheduled_readers(binary)


def test_unloaded_files_do_not_mean_an_absent_graph(paired_deployment):
    binary, records, _, _ = paired_deployment()
    for item in records.values():
        item.update(LoadState="not-found", FragmentPath="")
    with pytest.raises(deployment.DeploymentError):
        deployment.verify_scheduled_readers(binary)


@pytest.mark.parametrize("timer", tuple(deployment.SCHEDULED_JOBS))
def test_redirected_timer_is_rejected(paired_deployment, timer):
    binary, _, _, _ = paired_deployment(changes={timer: {"Unit": "legacy-reader.service"}})
    with pytest.raises(deployment.DeploymentError):
        deployment.verify_scheduled_readers(binary)


@pytest.mark.parametrize("value", ["", "-1", "False", "True", "0.0", "4294967296", "1" * 200])
@pytest.mark.parametrize("key", ["MainPID", "ControlPID"])
def test_pid_metadata_is_typed_and_bounded(paired_deployment, key, value):
    binary, _, _, _ = paired_deployment(changes={"eom-email-watcher.service": {key: value}})
    with pytest.raises(deployment.DeploymentError):
        deployment.verify_scheduled_readers(binary)


@pytest.mark.parametrize("value", ["0", "4294967295"])
def test_pid_parser_accepts_both_valid_boundaries(paired_deployment, value):
    _, _, _, _ = paired_deployment(changes={"eom-email-watcher.service": {"MainPID": value}})
    assert (
        deployment._manager_snapshot(deployment.UNIT_NAMES)["eom-email-watcher.service"]["MainPID"]
        == value
    )


@pytest.mark.parametrize("key", ["MainPID", "ControlPID"])
@pytest.mark.parametrize("compatible", [False, True])
def test_active_readers_require_the_paired_inode(paired_deployment, monkeypatch, key, compatible):
    binary, _, calls, _ = paired_deployment(changes={"eom-monthly-hours.service": {key: "123"}})
    original = deployment._same_executable
    monkeypatch.setattr(
        deployment,
        "_same_executable",
        lambda value, owner: compatible if value == "/proc/123/exe" else original(value, owner),
    )
    if compatible:
        deployment.verify_scheduled_readers(binary)
    else:
        with pytest.raises(deployment.DeploymentError):
            deployment.verify_scheduled_readers(binary)
    assert calls == ["snapshot"]


def test_an_observed_exit_needs_a_subsequent_fresh_zero_snapshot(paired_deployment):
    binary, records, calls, _ = paired_deployment(
        changes={"eom-email-watcher.service": {"MainPID": "123"}}
    )
    with pytest.raises(deployment.DeploymentError):
        deployment.verify_scheduled_readers(binary)
    assert calls == ["snapshot"]
    records["eom-email-watcher.service"]["MainPID"] = "0"
    deployment.verify_scheduled_readers(binary)
    assert calls == ["snapshot", "snapshot"]


@pytest.mark.parametrize("key", ["MainPID", "ControlPID"])
def test_helper_execution_refuses_even_with_paired_inode(paired_deployment, key):
    binary, _, _, _ = paired_deployment(
        changes={"eom-email-lmstudio.service": {key: str(os.getpid())}}
    )
    with pytest.raises(deployment.DeploymentError):
        deployment.verify_scheduled_readers(binary)


def test_canonical_helper_remain_after_exit_is_allowed(paired_deployment):
    binary, _, _, _ = paired_deployment(
        changes={"eom-email-lmstudio.service": {"ActiveState": "active"}}
    )
    deployment.verify_scheduled_readers(binary)


@pytest.mark.parametrize(
    "bad",
    [
        "missing-unit",
        "duplicate-unit",
        "duplicate-field",
        "missing-field",
        "unknown-field",
        "empty",
        "oversize",
        "unknown-state",
    ],
)
def test_manager_records_are_complete_and_unambiguous(paired_deployment, monkeypatch, bad):
    binary, records, _, _ = paired_deployment()
    raw = render_unit_records(records)
    if bad == "missing-unit":
        records.pop(next(iter(records)))
        raw = render_unit_records(records)
    elif bad == "duplicate-unit":
        raw += "\n" + raw.split("\n\n")[0] + "\n"
    elif bad == "duplicate-field":
        raw = raw.replace("Id=", "Id=duplicate\nId=", 1)
    elif bad == "missing-field":
        records[next(iter(records))].pop("FragmentPath")
        raw = render_unit_records(records)
    elif bad == "unknown-field":
        raw += "unknown=value\n"
    elif bad == "empty":
        raw = ""
    elif bad == "oversize":
        raw = "x" * (deployment.MAX_MANAGER_BYTES + 1)
    else:
        records[next(iter(records))]["ActiveState"] = "unknown"
        raw = render_unit_records(records)
    monkeypatch.setattr(deployment, "_manager_output", lambda names: raw)
    with pytest.raises(deployment.DeploymentError):
        deployment.verify_scheduled_readers(binary)


def test_file_or_alias_replacement_during_snapshot_refuses(
    paired_deployment, monkeypatch, tmp_path
):
    binary, records, _, directory = paired_deployment()
    replacement = tmp_path / "replacement"
    replacement.write_bytes((directory / deployment.UNIT_NAMES[0]).read_bytes())

    def read(names):
        os.replace(replacement, directory / deployment.UNIT_NAMES[0])
        return render_unit_records(records)

    monkeypatch.setattr(deployment, "_manager_output", read)
    with pytest.raises(deployment.DeploymentError):
        deployment.verify_scheduled_readers(binary)


def test_alias_replacement_during_snapshot_refuses(paired_deployment, monkeypatch, tmp_path):
    binary, records, _, _ = paired_deployment()
    other = tmp_path / "other"
    other.write_bytes(b"other engine")
    alias = deployment.deployment_description(binary).alias
    stage = alias.with_name("stage")
    stage.symlink_to(other)

    def read(names):
        os.replace(stage, alias)
        return render_unit_records(records)

    monkeypatch.setattr(deployment, "_manager_output", read)
    with pytest.raises(deployment.DeploymentError):
        deployment.verify_scheduled_readers(binary)


def test_current_image_replacement_after_snapshot_refuses(paired_deployment, monkeypatch, tmp_path):
    binary, records, _, _ = paired_deployment()
    replacement = tmp_path / "new-engine"
    replacement.write_bytes(b"replaced public inode")

    def read(names):
        os.replace(replacement, binary)
        return render_unit_records(records)

    monkeypatch.setattr(deployment, "_manager_output", read)
    with pytest.raises(deployment.DeploymentError):
        deployment.verify_database_admission()


def test_missing_current_image_blocks_before_manager(paired_deployment, monkeypatch):
    _, _, calls, _ = paired_deployment()
    monkeypatch.setattr(deployment, "_same_executable", lambda value, owner: False)
    with pytest.raises(deployment.DeploymentError):
        deployment.verify_database_admission()
    assert calls == []


@pytest.mark.parametrize("entry", ["api", "cli", "alias"])
def test_both_entrypoints_use_startup_admission(monkeypatch, entry):
    from eom_email_watcher import cli, engine_api

    events = []
    monkeypatch.setattr(deployment, "verify_database_admission", lambda: events.append("admitted"))
    monkeypatch.setattr(cli, "main", lambda args: events.append("cli"))
    monkeypatch.setattr(engine_api, "main", lambda: events.append("api"))
    argv = {
        "api": ["eom-mail-engine"],
        "cli": ["eom-mail-engine", "--cli", "check"],
        "alias": ["eom-mail-watch", "check"],
    }[entry]
    monkeypatch.setattr(sys, "argv", argv)
    deployment.main()
    assert events == ["admitted", "api" if entry == "api" else "cli"]


@pytest.mark.parametrize(
    "args", [["--paired-cli-version"], ["--service-unit-directory"], ["--cli", "--version"]]
)
def test_readonly_installer_probes_skip_database(monkeypatch, args):
    monkeypatch.setattr(
        deployment, "verify_database_admission", lambda: pytest.fail("database admission reached")
    )
    monkeypatch.setattr(sys, "argv", ["eom-mail-engine", *args])
    if args == ["--cli", "--version"]:
        with pytest.raises(SystemExit) as error:
            deployment.main()
        assert error.value.code == 0
    else:
        deployment.main()


@pytest.mark.parametrize("frozen,platform", [(False, "linux"), (True, "win32"), (True, "darwin")])
def test_non_native_linux_lanes_remain_exempt(monkeypatch, frozen, platform):
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(
        deployment,
        "deployment_description",
        lambda binary: pytest.fail("native description reached"),
    )
    deployment.verify_database_admission()


@pytest.mark.parametrize("setting", [None, "", "relative", "absolute"])
def test_unit_directory_has_one_xdg_resolver(monkeypatch, tmp_path, setting):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    if setting is None:
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    else:
        monkeypatch.setenv(
            "XDG_CONFIG_HOME", str(tmp_path / "custom") if setting == "absolute" else setting
        )
    assert deployment.service_unit_directory() == (
        tmp_path / "custom/systemd/user"
        if setting == "absolute"
        else tmp_path / "home/.config/systemd/user"
    )


def test_resources_must_be_complete(paired_deployment):
    binary, _, _, _ = paired_deployment()
    (Path(sys._MEIPASS) / "eom_email_watcher_data/systemd" / deployment.UNIT_NAMES[0]).unlink()
    with pytest.raises(deployment.DeploymentError):
        deployment.deployment_description(binary)


def test_native_owner_pairs_exact_image_and_payloads(paired_deployment, monkeypatch):
    binary, _, _, directory = paired_deployment()
    commands = []
    monkeypatch.setattr(
        deployment.subprocess,
        "run",
        lambda args, **kwargs: (
            commands.append(args) or subprocess.CompletedProcess(args, 0, b"", b"")
        ),
    )
    deployment.install_user_services()
    description = deployment.deployment_description(binary)
    assert description.alias.samefile(binary)
    assert {path.name for path in directory.iterdir()} == set(deployment.UNIT_NAMES)
    assert all((directory / unit.name).read_bytes() == unit.content for unit in description.units)
    assert commands == [
        ["systemctl", "--user", "daemon-reload"],
        *[["systemctl", "--user", "enable", timer] for timer in deployment.SCHEDULED_JOBS],
    ]


@pytest.mark.skipif(sys.platform != "linux", reason="Linux pipe selector")
@pytest.mark.parametrize(
    "failure", ["nonzero", "timeout", "oversize", "invalid-utf8", "missing-manager"]
)
def test_manager_transport_is_bounded_and_typed(monkeypatch, failure):
    original = subprocess.Popen
    body = {
        "nonzero": "raise SystemExit(1)",
        "timeout": "import time;time.sleep(10)",
        "oversize": "import sys;sys.stdout.write('x'*65537)",
        "invalid-utf8": "import sys;sys.stdout.buffer.write(b'\\xff')",
    }
    calls = []

    def launch(command, **kwargs):
        calls.append(command)
        if failure == "missing-manager":
            raise FileNotFoundError("public manager absent")
        return original([REAL_PYTHON, "-c", body[failure]], **kwargs)

    monkeypatch.setattr(deployment.subprocess, "Popen", launch)
    if failure == "timeout":
        monkeypatch.setattr(deployment, "MANAGER_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(deployment.DeploymentError):
        deployment._manager_output(deployment.UNIT_NAMES)
    assert len(calls) == 1


def test_final_process_barrier_precedes_sqlite_open(paired_deployment, monkeypatch, tmp_path):
    from eom_email_watcher import db

    binary, _, _, _ = paired_deployment()
    original = deployment._verify_description
    replacement = tmp_path / "late-replacement"
    replacement.write_bytes(b"public replacement inode")

    def inspect(description):
        original(description)
        os.replace(replacement, binary)

    monkeypatch.setattr(deployment, "_verify_description", inspect)
    monkeypatch.setattr(
        db.sqlite3, "connect", lambda *args, **kwargs: pytest.fail("SQLite reached")
    )
    with (
        pytest.raises(deployment.DeploymentError, match="running engine was replaced"),
        db.Store(tmp_path / "public.sqlite3").connection(),
    ):
        pytest.fail("Database yielded")


@pytest.mark.parametrize("failure", ["dropin", "masked", "missing-manager"])
def test_installation_refuses_unsafe_graph_before_writing(paired_deployment, monkeypatch, failure):
    binary, records, _, directory = paired_deployment()
    description = deployment.deployment_description(binary)
    before = {unit.name: (directory / unit.name).read_bytes() for unit in description.units}
    alias_before = description.alias.lstat().st_ino
    if failure == "missing-manager":

        def unavailable(names):
            raise deployment.DeploymentError("Public manager unavailable")

        monkeypatch.setattr(deployment, "_manager_output", unavailable)
    else:
        records[deployment.UNIT_NAMES[0]]["DropInPaths" if failure == "dropin" else "LoadState"] = (
            "/public/activation.conf" if failure == "dropin" else "masked"
        )
    monkeypatch.setattr(
        deployment, "_install_units", lambda owner: pytest.fail("Unit writes reached")
    )
    monkeypatch.setattr(deployment, "_enable_timers", lambda: pytest.fail("Enable reached"))
    with pytest.raises(deployment.DeploymentError):
        deployment.install_user_services()
    assert before == {unit.name: (directory / unit.name).read_bytes() for unit in description.units}
    assert description.alias.lstat().st_ino == alias_before


def test_installation_verifies_reloaded_graph_before_enabling(paired_deployment, monkeypatch):
    _, records, _, _ = paired_deployment()
    commands = []

    def execute(arguments, **kwargs):
        commands.append(arguments)
        if arguments[-1] == "daemon-reload":
            records[deployment.UNIT_NAMES[0]]["DropInPaths"] = "/public/activation.conf"
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr(deployment.subprocess, "run", execute)
    with pytest.raises(deployment.DeploymentError):
        deployment.install_user_services()
    assert commands == [["systemctl", "--user", "daemon-reload"]]


def test_loaded_graph_requires_the_selected_alias(paired_deployment, tmp_path):
    binary, _, _, _ = paired_deployment()
    alias = deployment.deployment_description(binary).alias
    other = tmp_path / "other-engine"
    other.write_bytes(b"other public image")
    stage = alias.with_name("stage-alias")
    stage.symlink_to(other)
    os.replace(stage, alias)
    with pytest.raises(deployment.DeploymentError):
        deployment.verify_database_admission()
