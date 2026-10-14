"""Evidence reader controls; native observations come from windows-package."""

import json
import os
from pathlib import Path

import pytest
from probe_windows_cutover_context import parse_tokens, token_profile, transcript_probe


@pytest.mark.parametrize("enabled", [False, True])
def test_context_preserves_privilege_state_and_target(enabled):
    token = {
        "pid": 42,
        "elevated": True,
        "integrity_rid": 12288,
        "debug_present": True,
        "debug_enabled": enabled,
    }
    text = (
        "public transcript header\nCutover stopping process=99\r\n"
        "Cutover token role=stop-host-before-terminate data=" + json.dumps(token) + "\r\n"
    )
    assert parse_tokens(text) == [
        {"role": "stop-host-before-terminate", "target_pid": 99, "token": token}
    ]
    assert parse_tokens("Cutover exception type=public fault\n") == []


def test_token_diagnostics_do_not_mutate_token_or_replace_oracle():
    source = (
        Path(__file__).resolve().parents[1] / "desktop/src-tauri/windows/cutover.ps1"
    ).read_text()
    assert "GetTokenInformation" in source
    assert "AdjustTokenPrivileges" not in source and "SetTokenInformation" not in source
    assert "catch { Write-Warning" in source[source.index("function Write-CutoverToken") :]


@pytest.mark.skipif(os.name != "nt", reason="native Windows token queries and transcript controls")
def test_native_context_probe_is_repeatable_and_exit_controls_are_recorded(tmp_path):
    first = token_profile([os.getpid()], tmp_path / "first")
    second = token_profile([os.getpid()], tmp_path / "second")
    for records in (first, second):
        actual = [r["token"] for r in records if r["role"] == "diagnostic-probe"]
        assert len(actual) == 1 and actual[0]["pid"] == os.getpid()
    assert first[-1]["token"] == second[-1]["token"]
    rows = transcript_probe(tmp_path / "transcripts")
    assert [row["exit"] for row in rows] == [0, 0, 1]


def test_medium_context_rejects_admin_debug_missing_and_mixed_records():
    import probe_windows_cutover_context as probe

    good = {
        "pid": 1,
        "elevated": False,
        "integrity_rid": 8192,
        "debug_present": False,
        "debug_enabled": False,
    }
    rows = [{"role": "installer", "target_pid": None, "token": good}]
    probe.assert_standard_context(rows, required_roles={"installer"})
    with pytest.raises(AssertionError, match="standard-user"):
        probe.assert_standard_context(rows, required_roles={"installer"}, required_pids={2})
    for changed in (
        {"elevated": True},
        {"integrity_rid": 12288},
        {"debug_present": True},
        {"debug_enabled": True},
    ):
        with pytest.raises(AssertionError, match="standard-user"):
            probe.assert_standard_context(
                rows + [{"role": "stop-host", "token": good | changed}],
                required_roles={"installer"},
            )
    for rows in ([], [{"role": "installer", "token": {"pid": 1}}]):
        with pytest.raises(AssertionError, match="standard-user"):
            probe.assert_standard_context(rows, required_roles={"installer"})
    with pytest.raises(AssertionError, match="standard-user"):
        probe.assert_standard_context(
            [{"role": "actor", "token": good}], required_roles={"installer"}
        )


def test_only_protected_case_is_dispatched_as_standard_user(monkeypatch, tmp_path):
    import prove_windows_cutover as proof

    calls = []

    def normal(*args):
        calls.append(("normal", args[-1]))
        return {"case": args[-1], "result": "PASS"}

    def standard(*args):
        calls.append(("standard", "old-then-new"))
        return {"case": "old-then-new", "result": "PASS"}

    monkeypatch.setattr(proof, "run_case", normal)
    monkeypatch.setattr(proof, "run_standard_user_case", standard, raising=False)
    assert len(proof.run_cases(tmp_path, Path("installer"), Path("engine"), Path("actor"))) == 3
    assert calls == [
        ("normal", "unknown-image"),
        ("standard", "old-then-new"),
        ("normal", "new-then-old"),
    ]


def test_standard_launcher_scopes_permissions_and_cleans_up(monkeypatch, tmp_path):
    from types import SimpleNamespace

    import windows_cutover_process_fixture as fixture

    monkeypatch.setattr(fixture, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(fixture, "fixture_payloads", lambda output, *paths: paths)
    monkeypatch.setattr(fixture.shutil, "which", lambda name: "/public tool/bin/7z")
    calls = []
    result = {"case": "old-then-new", "result": "PASS"}
    (tmp_path / "standard-user-result.json").write_text(json.dumps(result))

    def shell(script, **values):
        calls.append((script, values))
        if "EW_PROBE_LAUNCHER" in values:
            assert "--tool-directory" in values["EW_PROBE_ARGS"]
            assert values["timeout"] == 700
            assert '"--case"' not in values["EW_PROBE_ARGS"]  # normal argv quoting
            assert "--case old-then-new" in values["EW_PROBE_ARGS"]
        return ""

    assert (
        fixture.run_standard_user_case(
            tmp_path, Path("installer"), Path("engine"), Path("actor"), shell
        )
        == result
    )
    grants = [values for script, values in calls if " /grant " in script]
    removals = [values for script, values in calls if " /remove:g " in script]
    assert [v["EW_PROBE_PATH"] for v in removals] == [v["EW_PROBE_PATH"] for v in grants][::-1]
    assert [v["EW_PROBE_ACCESS"] for v in grants].count("M") == 1
    assert grants[-1]["EW_PROBE_PATH"] == str(tmp_path)
    assert all(
        v["timeout"] == 300
        for script, v in calls
        if " /grant " in script or " /remove:g " in script
    )
    assert "Remove-LocalUser" in calls[-1][0]
    assert all("S-1-5-32-544" not in script for script, _ in calls)


def test_standard_launcher_does_not_report_pass_after_child_failure(monkeypatch, tmp_path):
    from types import SimpleNamespace

    import windows_cutover_process_fixture as fixture

    monkeypatch.setattr(fixture, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(fixture, "fixture_payloads", lambda output, *paths: paths)
    monkeypatch.setattr(fixture.shutil, "which", lambda name: "/public tool/bin/7z")
    scripts = []

    def shell(script, **values):
        scripts.append(script)
        if "EW_PROBE_LAUNCHER" in values:
            raise RuntimeError("public child failure")
        return ""

    with pytest.raises(RuntimeError, match="public child failure"):
        fixture.run_standard_user_case(
            tmp_path, Path("installer"), Path("engine"), Path("actor"), shell
        )
    assert " /remove:g " in scripts[-2]
    assert "Remove-LocalUser" in scripts[-1]


@pytest.mark.parametrize(
    "key,value",
    [
        ("PSModulePath", "public-PS7"),
        ("psmodulepath", "public-PS7"),
        ("PSMODULEPATH", ""),
        ("PSModulePath", "public-PS5"),
    ],
)
def test_powershell_children_do_not_inherit_module_search_path(monkeypatch, key, value):
    from types import SimpleNamespace

    import prove_windows_cutover as proof

    captured = {}
    monkeypatch.setenv(key, value)

    def run(*args, **values):
        captured.update(values["env"])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(proof.subprocess, "run", run)
    proof.powershell("public no-op")
    assert not any(name.casefold() == "psmodulepath" for name in captured)


def test_launcher_cleanup_attempts_all_removals_after_mixed_failure(monkeypatch, tmp_path):
    from types import SimpleNamespace

    import windows_cutover_process_fixture as fixture

    monkeypatch.setattr(fixture, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(fixture, "fixture_payloads", lambda output, *paths: paths)
    monkeypatch.setattr(fixture.shutil, "which", lambda name: "/public tool/bin/7z")
    calls = []

    def shell(script, **values):
        calls.append((script, values))
        if "EW_PROBE_LAUNCHER" in values:
            raise RuntimeError("public launch failure")
        if " /remove:g " in script:
            raise RuntimeError("public cleanup failure")
        return ""

    with pytest.raises(ExceptionGroup, match="Standard-user fixture and cleanup failed"):
        fixture.run_standard_user_case(
            tmp_path, Path("installer"), Path("engine"), Path("actor"), shell
        )
    grants = [values["EW_PROBE_PATH"] for script, values in calls if " /grant " in script]
    removed = [values["EW_PROBE_PATH"] for script, values in calls if " /remove:g " in script]
    assert removed == grants[::-1]
    assert "Remove-LocalUser" in calls[-1][0]


def test_launcher_never_grants_the_built_checkout_root(monkeypatch, tmp_path):
    from types import SimpleNamespace

    import windows_cutover_process_fixture as fixture

    monkeypatch.setattr(fixture, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(fixture, "fixture_payloads", lambda output, *paths: paths)
    monkeypatch.setattr(fixture.shutil, "which", lambda name: "/public tool/bin/7z")
    calls = []
    (tmp_path / "standard-user-result.json").write_text(
        json.dumps({"case": "old-then-new", "result": "PASS"})
    )

    def shell(script, **values):
        calls.append((script, values))
        return ""

    fixture.run_standard_user_case(
        tmp_path, Path("installer"), Path("engine"), Path("actor"), shell
    )
    root = Path(fixture.__file__).resolve().parents[1]
    assert str(root) not in [
        values["EW_PROBE_PATH"] for script, values in calls if " /grant " in script
    ]


def test_fresh_profile_environment_accepts_good_and_rejects_partial_or_admin():
    from windows_qualification_environment import assert_profile_environment

    good = {
        "USERPROFILE": "/public/standard",
        "APPDATA": "/public/standard/Roaming",
        "LOCALAPPDATA": "/public/standard/Local",
    }
    assert_profile_environment(good, good)
    for key in good:
        for wrong in (None, "", "/public/admin"):
            with pytest.raises(AssertionError, match="profile environment mismatch"):
                assert_profile_environment(good | {key: wrong}, good)
    with pytest.raises(AssertionError, match="profile environment mismatch"):
        assert_profile_environment(good, {})


def test_fixture_payload_copies_are_exact_and_have_receipts(tmp_path):
    import hashlib

    from windows_cutover_process_fixture import fixture_payloads

    inputs = []
    for label in ("installer original.exe", "engine original.exe", "actor original.exe"):
        path = tmp_path / label
        path.write_bytes(b"public exact input: " + label.encode())
        inputs.append(path)
    copies = fixture_payloads(tmp_path, *inputs)
    receipt = json.loads((tmp_path / "standard-inputs.json").read_text())
    for original, copied in zip(inputs, copies, strict=True):
        assert original.read_bytes() == copied.read_bytes()
        assert (
            receipt[copied.name]["source_sha256"]
            == hashlib.sha256(original.read_bytes()).hexdigest()
        )
        assert receipt[copied.name]["copied_sha256"] == receipt[copied.name]["source_sha256"]


def test_native_launcher_uses_os_profile_environment_and_explicit_executable():
    source = (Path(__file__).resolve().parents[1] / "scripts/windows_standard_user.ps1").read_text()
    assert "CreateProcessWithLogonW(user,domain,password,1,app,new StringBuilder(command)" in source
    assert "IntPtr.Zero,directory,ref startup" in source
    assert "CloseHandle(process.thread); CloseHandle(process.process)" in source
    assert "Start-Process" not in source and "UseNewEnvironment" not in source


def test_partial_acl_acquisition_is_also_cleaned_up(monkeypatch, tmp_path):
    from types import SimpleNamespace

    import windows_cutover_process_fixture as fixture

    monkeypatch.setattr(fixture, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(fixture, "fixture_payloads", lambda output, *paths: paths)
    monkeypatch.setattr(fixture.shutil, "which", lambda name: "/public/bin/7z")
    calls = []

    def shell(script, **values):
        calls.append((script, values))
        if " /grant " in script:
            raise RuntimeError("public partial recursive grant")
        return ""

    with pytest.raises(RuntimeError, match="public partial recursive grant"):
        fixture.run_standard_user_case(
            tmp_path, Path("installer"), Path("engine"), Path("actor"), shell
        )
    assert [v["EW_PROBE_PATH"] for s, v in calls if " /grant " in s] == [
        v["EW_PROBE_PATH"] for s, v in calls if " /remove:g " in s
    ]
    assert "Remove-LocalUser" in calls[-1][0]


def test_cleanup_failures_preserve_primary_and_attempt_account_delete(monkeypatch, tmp_path):
    from types import SimpleNamespace

    import windows_cutover_process_fixture as fixture

    monkeypatch.setattr(fixture, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(fixture, "fixture_payloads", lambda output, *paths: paths)
    monkeypatch.setattr(fixture.shutil, "which", lambda name: "/public/bin/7z")

    def shell(script, **values):
        if "EW_PROBE_LAUNCHER" in values:
            raise RuntimeError("public primary failure")
        if " /remove:g " in script or "Remove-LocalUser" in script:
            raise RuntimeError("public cleanup failure")
        return ""

    with pytest.raises(ExceptionGroup) as caught:
        fixture.run_standard_user_case(
            tmp_path, Path("installer"), Path("engine"), Path("actor"), shell
        )
    assert str(caught.value.exceptions[0]) == "public primary failure"
    report = json.loads((tmp_path / "standard-user-cleanup.json").read_text())
    assert report["primary_error"] == "public primary failure"
    assert len(report["cleanup_errors"]) == len(report["acl_timings"]) // 2 + 1


@pytest.mark.skipif(os.name != "nt", reason="native Windows default-module controls")
def test_native_default_modules_and_context_are_recorded(tmp_path):
    from probe_windows_cutover_context import module_probe

    rows = module_probe(tmp_path)
    assert rows[-1]["context"] == "windows-default" and rows[-1]["exit"] == 0
    assert "PSVersion=" in rows[-1]["stdout"] and "PSModulePath=" in rows[-1]["stdout"]


def test_every_windows_host_producer_uses_the_environment_owner():
    """Closed producer inventory includes NSIS, whose PowerShell child is indirect."""
    import ast

    root = Path(__file__).resolve().parents[1]
    for filename in (
        "scripts/prove_windows_cutover.py",
        "scripts/prove_windows_cutover_full.py",
        "scripts/probe_windows_cutover_context.py",
        "tests/test_windows_cutover.py",
        "tests/test_windows_cutover_round9.py",
    ):
        tree = ast.parse((root / filename).read_text())
        for function in ast.walk(tree):
            if (
                not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
                or function.name == "module_probe"
            ):
                continue  # Deliberately labelled inherited-vs-default diagnostic control.
            for call in ast.walk(function):
                if (
                    not isinstance(call, ast.Call)
                    or not isinstance(call.func, ast.Attribute)
                    or call.func.attr != "run"
                    or not call.args
                ):
                    continue
                argv = call.args[0]
                if not isinstance(argv, ast.List) or not argv.elts:
                    continue
                host = ast.unparse(argv.elts[0])
                if host != "'powershell.exe'" and "installer" not in host:
                    continue
                env = next(
                    (keyword.value for keyword in call.keywords if keyword.arg == "env"), None
                )
                assert (
                    isinstance(env, ast.Call)
                    and isinstance(env.func, ast.Name)
                    and env.func.id == "powershell_environment"
                ), (filename, function.name, call.lineno)
