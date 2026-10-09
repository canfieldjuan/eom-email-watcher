"""Whole activation-input and systemd transport regression class."""

import ast
from pathlib import Path

import pytest
from packaged_proof_environment import systemd_quote

from eom_email_watcher import deployment

KINDS = ("wants", "requires", "upholds", "d")


@pytest.mark.parametrize("unit", deployment.UNIT_NAMES)
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("loaded", [True, False])
def test_auxiliary_reader_refuses_before_publication(
    paired_deployment, monkeypatch, unit, kind, loaded
):
    binary, _, _, directory = paired_deployment(loaded=loaded)
    inputs = directory / (unit + "." + kind)
    inputs.mkdir()
    (inputs / "legacy-reader.service").symlink_to("/public/legacy-reader.service")
    before = {path.name: path.lstat().st_ino for path in directory.iterdir()}
    alias = directory.parents[2] / ".local/bin/eom-mail-watch"
    identity = alias.lstat().st_ino
    monkeypatch.setattr(deployment, "_manager_action", lambda _: None)
    with pytest.raises(deployment.DeploymentError):
        deployment.install_user_services(binary)
    assert {path.name: path.lstat().st_ino for path in directory.iterdir()} == before
    assert alias.lstat().st_ino == identity


@pytest.mark.parametrize(
    "name",
    [
        "service.d",
        "eom-.service.d",
        "eom-email-.service.d",
        "timer.d",
        "eom-.timer.d",
        "eom-monthly-.timer.d",
    ],
)
def test_foreign_search_path_inputs_use_same_owner(paired_deployment, monkeypatch, tmp_path, name):
    binary, _, _, directory = paired_deployment()
    vendor = tmp_path / "vendor"
    auxiliary = vendor / name
    auxiliary.mkdir(parents=True)
    (auxiliary / "override.conf").write_text("[Unit]\nWants=legacy-reader.service\n")
    monkeypatch.setattr(deployment, "_manager_unit_paths", lambda: (directory, vendor))
    with pytest.raises(deployment.DeploymentError):
        deployment.verify_scheduled_readers(binary)


@pytest.mark.parametrize(
    "field",
    ["Requires", "Requisite", "Wants", "BindsTo", "Upholds", "OnSuccess", "OnFailure", "Triggers"],
)
@pytest.mark.parametrize("unit", deployment.UNIT_NAMES)
def test_effective_activation_is_positive_not_just_file_identity(paired_deployment, unit, field):
    binary, records, _, _ = paired_deployment()
    records[unit][field] += " legacy-reader.service"
    with pytest.raises(deployment.DeploymentError):
        deployment.verify_scheduled_readers(binary)


@pytest.mark.parametrize("field", ["Requires", "Wants", "Names", "Triggers"])
def test_missing_or_duplicate_graph_metadata_refuses(paired_deployment, field):
    binary, records, _, _ = paired_deployment()
    record = records["eom-email-watcher.timer" if field == "Triggers"
                     else "eom-email-watcher.service"]
    record.pop(field)
    with pytest.raises(deployment.DeploymentError):
        deployment.verify_scheduled_readers(binary)


def test_aliases_refuse_at_graph_owner(paired_deployment):
    binary, records, _, _ = paired_deployment()
    records["eom-email-watcher.service"]["Names"] += " legacy-reader.service"
    with pytest.raises(deployment.DeploymentError):
        deployment.verify_scheduled_readers(binary)


@pytest.mark.parametrize("publisher", ["native", "source", "shell-confirm"])
def test_absent_graph_retained_hooks_refuse_all_publishers(
    paired_deployment, monkeypatch, publisher
):
    binary, _, _, directory = paired_deployment(loaded=False)
    inputs = directory / "eom-email-watcher.service.wants"
    inputs.mkdir()
    (inputs / "legacy-reader.service").symlink_to("/public/legacy-reader.service")
    before = tuple(directory.iterdir())
    monkeypatch.setattr(deployment, "_manager_action", lambda _: None)
    with pytest.raises(deployment.DeploymentError):
        if publisher == "native":
            deployment.install_user_services(binary)
        elif publisher == "source":
            deployment.install_source_units()
        else:
            with deployment.deployment_lease(deployment.manager_view(), exclusive=True) as fd:
                deployment.confirm_source_publication(fd)
    assert tuple(directory.iterdir()) == before


def test_auxiliary_change_during_snapshot_refuses(paired_deployment, monkeypatch):
    binary, _, _, directory = paired_deployment()
    original = deployment._manager_output

    def observe(names):
        output = original(names)
        folder = directory / "eom-email-watcher.service.wants"
        folder.mkdir()
        return output

    monkeypatch.setattr(deployment, "_manager_output", observe)
    with pytest.raises(deployment.DeploymentError):
        deployment.verify_scheduled_readers(binary)


@pytest.mark.parametrize(
    "value",
    [
        "/public/plain",
        "/public/user name",
        "/public/caf" + chr(233),
        '/public/a"b',
        "/public/a\\b",
        "/public/a'b",
        "/public/a$b",
        "/public/a`b",
        "/public/a\tb",
        "/public/$(touch sentinel)",
    ],
)
@pytest.mark.parametrize("posix", [False, True])
def test_independent_wire_fixture_roundtrip(value, posix):
    assert deployment._manager_words(systemd_quote(value, posix=posix), posix=posix) == (value,)


@pytest.mark.parametrize(
    "wire",
    [
        '"/public/a""/public/b"',
        '/public/a"/public/b"',
        '"/public/a"/public/b',
        '"unfinished',
        r'"/public/a\q"',
        r'"/public/a\777"',
        r'"/public/a\xFF"',
        "a 'bad", "a'b'",
    ],
)
def test_generic_wire_rejects_malformed_or_concatenated_words(wire):
    with pytest.raises(deployment.DeploymentError):
        deployment._manager_words(wire)


@pytest.mark.parametrize(
    "wire",
    ["$'/public/a'$'/public/b'", "/public/a$'/public/b'", r"$'/public/a\q'", r"$'/public/a\777'"],
)
def test_posix_wire_rejects_malformed_or_concatenated_words(wire):
    with pytest.raises(deployment.DeploymentError):
        deployment._manager_words(wire, posix=True)


def test_one_snapshot_owner_routes_all_graph_readers():
    assert set(deployment._ACTIVATION_FIELDS).issubset(deployment._MANAGER_FIELDS)
    tree = ast.parse(Path(deployment.__file__).read_text())
    readers = []
    for function in ast.walk(tree):
        if isinstance(function, ast.FunctionDef):
            readers.extend(
                function.name
                for call in ast.walk(function)
                if isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "_manager_snapshot"
            )
    assert readers == ["_observe_graph"]


def test_ordinary_graph_and_empty_auxiliary_directory_succeed(paired_deployment):
    binary, _, calls, directory = paired_deployment()
    (directory / "eom-email-watcher.service.wants").mkdir()
    deployment.verify_scheduled_readers(binary)
    assert calls == ["snapshot"]


def test_unknown_vendor_default_target_is_portable(paired_deployment):
    binary, records, _, _ = paired_deployment()
    records["future-default.target"] = dict(
        records["basic.target"], Id="future-default.target", Names="future-default.target"
    )
    records["eom-email-watcher.service"]["Requires"] += " future-default.target"
    deployment.verify_scheduled_readers(binary)


@pytest.mark.parametrize("cause", ["user-file", "missing", "transient", "stale", "foreign-service",
                                  "user-dropin", "unknown-empty"])
def test_platform_default_requires_observed_protected_provenance(
    paired_deployment, tmp_path, cause
):
    binary, records, _, _ = paired_deployment()
    records["future-default.target"] = dict(
        records["basic.target"], Id="future-default.target", Names="future-default.target"
    )
    records["eom-email-watcher.service"]["Requires"] += " future-default.target"
    record = records["future-default.target"]
    user = tmp_path / "user.target"
    user.write_text("[Unit]\nDescription=public user unit\n")
    user.chmod(0o444)
    if cause == "user-file":
        record["FragmentPath"] = str(user)
    elif cause == "missing":
        records.pop("future-default.target")
    elif cause == "transient":
        record["Transient"] = "yes"
    elif cause == "stale":
        record["NeedDaemonReload"] = "yes"
    elif cause == "foreign-service":
        records["eom-email-watcher.service"]["Requires"] += " legacy-reader.service"
    elif cause == "user-dropin":
        record["DropInPaths"] = systemd_quote(str(user), posix=False)
    else:
        record.update(LoadState="not-found", FragmentPath="")
    with pytest.raises(deployment.DeploymentError):
        deployment.verify_scheduled_readers(binary)


def test_protected_fragment_rejects_user_owned_readonly_input(tmp_path):
    path = tmp_path / "public.target"
    path.write_text("[Unit]\nDescription=public fixture\n")
    path.chmod(0o444)
    assert not deployment._protected_fragment(path)
    assert not deployment._protected_fragment(path.with_name("missing.target"))
    assert not deployment._protected_fragment(tmp_path)


def test_full_resolver_accepts_real_space_path_wire_modes(monkeypatch):
    home = Path("/public/manager home")
    # Literal captures follow the independently retained real formatter matrix.
    environment = "HOME=$'/public/manager home'\nXDG_CONFIG_HOME=$'/public/config space'\n"
    paths = '"/public/config space/systemd/user" /usr/lib/systemd/user\n'
    def read(command, **kwargs):
        return environment if command[-1] == "show-environment" else paths
    monkeypatch.setattr(deployment, "_read_manager_command", read)
    monkeypatch.setattr(deployment, "_account_home", lambda: home)
    view = deployment.manager_view()
    assert view.home == home
    assert view.unit_directory == Path("/public/config space/systemd/user")
    assert view.unit_paths == (view.unit_directory, Path("/usr/lib/systemd/user"))


def test_shipped_helper_command_array_is_not_duplicate_scalar_metadata(paired_deployment):
    binary, _, _, _ = paired_deployment()
    snapshot = deployment._manager_snapshot(deployment.UNIT_NAMES)
    assert len(snapshot["eom-email-lmstudio.service"]["ExecStart"].splitlines()) == 2
    deployment.verify_scheduled_readers(binary)


@pytest.mark.parametrize("second", ["", "garbage", "{ path=/public/extra ; }"])
def test_scheduled_command_array_cannot_admit_multiple_paths(paired_deployment, second):
    binary, records, _, _ = paired_deployment()
    records["eom-email-watcher.service"]["ExecStart"] += "\n" + second
    with pytest.raises(deployment.DeploymentError):
        deployment.verify_scheduled_readers(binary)


@pytest.mark.parametrize("space", [chr(160), chr(8195), chr(8232)])
@pytest.mark.parametrize("posix", [False, True])
def test_native_unicode_space_is_path_data(monkeypatch, space, posix):
    value = "/public/user" + space + "name"
    wire = systemd_quote(value, posix=posix)
    assert deployment._manager_words(wire, posix=posix) == (value,)
    monkeypatch.setattr(
        deployment, "_read_manager_command", lambda _, **kwargs: "HOME=" + wire + "\n"
    )
    assert deployment._manager_environment() == {"HOME": value}


def test_scalar_manager_property_preserves_unicode_line_separator(paired_deployment):
    _, records, _, _ = paired_deployment()
    value = "/public/user" + chr(8232) + "name/eom-email-watcher.service"
    records["eom-email-watcher.service"]["FragmentPath"] = value
    snapshot = deployment._manager_snapshot(deployment.UNIT_NAMES)
    assert snapshot["eom-email-watcher.service"]["FragmentPath"] == value
