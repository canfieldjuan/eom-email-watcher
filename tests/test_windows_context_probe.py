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
    monkeypatch.setattr(fixture, "disposable_user_sid", lambda name, shell: "S-1-5-21-1-2-3-1001")
    monkeypatch.setattr(fixture, "set_batch_logon_right", lambda sid, grant: None)
    monkeypatch.setattr(fixture, "fixture_payloads", lambda output, *paths: paths)
    monkeypatch.setattr(fixture.shutil, "which", lambda name: "/public tool/bin/7z")
    calls = []
    result = {"case": "old-then-new", "result": "PASS"}
    (tmp_path / "standard-user-result.json").write_text(json.dumps(result))

    def shell(script, **values):
        calls.append((script, values))
        if "EW_PROBE_LAUNCHER" in values:
            (tmp_path / "native-process-state.json").write_text(
                json.dumps(
                    {"account": values["EW_PROBE_USER"], "phase": "exited", "quiesced": True}
                )
            )
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
    monkeypatch.setattr(fixture, "disposable_user_sid", lambda name, shell: "S-1-5-21-1-2-3-1001")
    monkeypatch.setattr(fixture, "set_batch_logon_right", lambda sid, grant: None)
    monkeypatch.setattr(fixture, "fixture_payloads", lambda output, *paths: paths)
    monkeypatch.setattr(fixture.shutil, "which", lambda name: "/public tool/bin/7z")
    scripts = []

    def shell(script, **values):
        scripts.append(script)
        if "EW_PROBE_LAUNCHER" in values:
            (tmp_path / "native-process-state.json").write_text(
                json.dumps(
                    {"account": values["EW_PROBE_USER"], "phase": "exited", "quiesced": True}
                )
            )
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
    monkeypatch.setattr(fixture, "disposable_user_sid", lambda name, shell: "S-1-5-21-1-2-3-1001")
    monkeypatch.setattr(fixture, "set_batch_logon_right", lambda sid, grant: None)
    monkeypatch.setattr(fixture, "fixture_payloads", lambda output, *paths: paths)
    monkeypatch.setattr(fixture.shutil, "which", lambda name: "/public tool/bin/7z")
    calls = []

    def shell(script, **values):
        calls.append((script, values))
        if "EW_PROBE_LAUNCHER" in values:
            (tmp_path / "native-process-state.json").write_text(
                json.dumps(
                    {"account": values["EW_PROBE_USER"], "phase": "exited", "quiesced": True}
                )
            )
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
    monkeypatch.setattr(fixture, "disposable_user_sid", lambda name, shell: "S-1-5-21-1-2-3-1001")
    monkeypatch.setattr(fixture, "set_batch_logon_right", lambda sid, grant: None)
    monkeypatch.setattr(fixture, "fixture_payloads", lambda output, *paths: paths)
    monkeypatch.setattr(fixture.shutil, "which", lambda name: "/public tool/bin/7z")
    calls = []
    (tmp_path / "standard-user-result.json").write_text(
        json.dumps({"case": "old-then-new", "result": "PASS"})
    )

    def shell(script, **values):
        calls.append((script, values))
        if "EW_PROBE_LAUNCHER" in values:
            (tmp_path / "native-process-state.json").write_text(
                json.dumps(
                    {"account": values["EW_PROBE_USER"], "phase": "exited", "quiesced": True}
                )
            )
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
    monkeypatch.setattr(fixture, "disposable_user_sid", lambda name, shell: "S-1-5-21-1-2-3-1001")
    monkeypatch.setattr(fixture, "set_batch_logon_right", lambda sid, grant: None)
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
    monkeypatch.setattr(fixture, "disposable_user_sid", lambda name, shell: "S-1-5-21-1-2-3-1001")
    monkeypatch.setattr(fixture, "set_batch_logon_right", lambda sid, grant: None)
    monkeypatch.setattr(fixture, "fixture_payloads", lambda output, *paths: paths)
    monkeypatch.setattr(fixture.shutil, "which", lambda name: "/public/bin/7z")

    def shell(script, **values):
        if "EW_PROBE_LAUNCHER" in values:
            (tmp_path / "native-process-state.json").write_text(
                json.dumps(
                    {"account": values["EW_PROBE_USER"], "phase": "exited", "quiesced": True}
                )
            )
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


def test_task_observer_keeps_principal_and_zero_result_and_read_errors(monkeypatch, tmp_path):
    from types import SimpleNamespace

    import prove_windows_cutover as proof

    clock = [0.0]
    monkeypatch.setattr(proof, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    seen = []
    task = {
        "State": "Ready",
        "Principal": {"UserId": "public-user", "LogonType": "Interactive", "RunLevel": "Limited"},
        "LastTaskResult": 0,
        "NumberOfMissedRuns": 0,
        "LastRunTime": "public timestamp",
    }

    def shell(code, **values):
        seen.append(values)
        if len(seen) == 2:
            raise RuntimeError("public task read failure")
        return json.dumps(task)

    monkeypatch.setattr(proof, "powershell", shell)
    path = tmp_path / "scheduled-task-state.json"
    observer = proof.task_startup_observer("public-task", path)
    observer(40.0)
    clock[0] = 2.0
    observer(40.0)
    rows = json.loads(path.read_text())
    assert rows[0]["snapshot"] == task
    assert rows[1]["read_error"] == "public task read failure"
    assert seen[0]["EW_TASK"] == "public-task"
    assert seen[0]["timeout"] <= 40.0


def test_task_observer_runs_within_the_existing_startup_deadline(monkeypatch, tmp_path):
    from types import SimpleNamespace

    import prove_windows_cutover as proof

    clock = [0.0]

    def sleep(seconds):
        clock[0] += seconds

    monkeypatch.setattr(proof, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    seen = []

    def observe(deadline):
        seen.append(deadline)

    with pytest.raises(TimeoutError, match="Missing fixture receipt"):
        proof.wait_file(tmp_path / "scheduled.ready", observe=observe)
    assert seen and set(seen) == {40.0}
    assert 40.0 <= clock[0] < 40.1


def test_task_observer_samples_even_when_actor_is_already_ready(monkeypatch, tmp_path):
    import prove_windows_cutover as proof

    ready = tmp_path / "scheduled.ready"
    ready.write_text("public ready")
    observed = []
    proof.wait_file(ready, observe=lambda deadline: observed.append(deadline))
    assert len(observed) == 1


def test_task_observer_does_not_add_budget_after_read_timeout(monkeypatch, tmp_path):
    from types import SimpleNamespace

    import prove_windows_cutover as proof

    clock = [0.0]

    def sleep(seconds):
        clock[0] += seconds

    monkeypatch.setattr(proof, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    budgets = []

    def shell(code, **values):
        budget = values["timeout"]
        budgets.append(budget)
        clock[0] += budget
        raise proof.subprocess.TimeoutExpired("public task-state query", budget)

    monkeypatch.setattr(proof, "powershell", shell)
    report = tmp_path / "scheduled-task-state.json"
    with pytest.raises(TimeoutError, match="Missing fixture receipt"):
        proof.wait_file(
            tmp_path / "scheduled.ready", observe=proof.task_startup_observer("public-task", report)
        )
    assert budgets and max(budgets) <= 5
    assert 40 <= clock[0] < 40.1
    assert all(row["error_type"] == "TimeoutExpired" for row in json.loads(report.read_text()))


def test_cleanup_admission_requires_account_bound_authoritative_quiescence():
    import windows_cutover_process_fixture as fixture

    user = "EWProbe-01234567"
    assert fixture.cleanup_admitted({"account": user, "phase": "exited", "quiesced": True}, user)
    for record in (
        None,
        {},
        {"account": user, "quiesced": False},
        {"account": "other-user", "phase": "exited", "quiesced": True},
        {"account": user, "phase": "active", "quiesced": True},
    ):
        assert not fixture.cleanup_admitted(record, user)


@pytest.mark.parametrize("change", [
    None, "max-port", "account", "task", "engine", "argv", "mode", "peer", "missing",
    "extra", "short", "long", "empty", "false", "zero", "over-port",
])
def test_elevated_s4u_request_is_bound_to_the_standard_case(tmp_path, change):
    from windows_s4u_task_fixture import validate_request

    folder = tmp_path / "old-then-new"
    engine = folder / "installed/eom-mail-engine.exe"
    args = [str(folder / "private/mail.sqlite3"), str(folder / "scheduled.ready"),
            str(folder / "resume-old"), "http://127.0.0.1:12345/public-job", "idle"]
    request = {"account": "case-user", "task": "case-task", "engine": str(engine), "argv": args}
    if change in {"account", "task", "engine"}:
        request[change] = "another-case"
    elif change == "argv":
        request["argv"][0] = str(tmp_path / "other-database")
    elif change == "mode":
        request["argv"][4] = "held"
    elif change == "peer":
        request["argv"][3] = "http://example.test:12345/public-job"
    elif change == "missing":
        request.pop("engine")
    elif change == "extra":
        request["sid"] = "another-owner"
    elif change == "short":
        request["argv"].pop()
    elif change == "long":
        request["argv"].append("extra")
    elif change in {"empty", "false"}:
        request["argv"][3] = "" if change == "empty" else False
    elif change in {"zero", "over-port", "max-port"}:
        port = {"zero": 0, "over-port": 65536, "max-port": 65535}[change]
        request["argv"][3] = f"http://127.0.0.1:{port}/public-job"
    if change in {None, "max-port"}:
        assert validate_request(request, tmp_path, "case-user", "case-task") == (engine, args)
    else:
        with pytest.raises(ValueError):
            validate_request(request, tmp_path, "case-user", "case-task")


def test_s4u_broker_registers_fixed_principal_and_keeps_denial(tmp_path):
    from windows_s4u_task_fixture import LIMITED, S4U, S4URegistration

    folder = tmp_path / "old-then-new"
    request = {"account": "case-user", "task": "case-task",
               "engine": str(folder / "installed/eom-mail-engine.exe"),
               "argv": [str(folder / "private/mail.sqlite3"), str(folder / "scheduled.ready"),
                        str(folder / "resume-old"), "http://127.0.0.1:12345/public-job", "idle"]}
    calls = []

    def shell(script, **values):
        calls.append(values)
        raise OSError("public registration denied")

    broker = S4URegistration(tmp_path, "case-user", "S-1-5-21-1-2-3-1001", "case-task", shell)
    broker.request.write_text(json.dumps(request))
    broker._serve()
    broker.close()
    result = json.loads(broker.response.read_text())
    assert result["status"] == "failed" and result["error"] == "public registration denied"
    assert calls[0]["EW_S4U_LOGON"] == str(S4U)
    assert calls[0]["EW_S4U_LEVEL"] == str(LIMITED)
    assert calls[0]["EW_S4U_SID"] == "S-1-5-21-1-2-3-1001"
    assert broker.settled is True


@pytest.mark.parametrize("operation", [None, "get", "disable", "enable", "stop"])
def test_standard_s4u_control_requires_every_operation(tmp_path, monkeypatch, operation):
    from windows_s4u_task_fixture import request_s4u_task

    monkeypatch.setenv("USERNAME", "case-user")
    sid = "S-1-5-21-1-2-3-1001"
    registration = {"account": "case-user", "task": "case-task", "status": "registered",
                    "native": {"principal": sid}}
    (tmp_path / "standard-user-task-registration.json").write_text(json.dumps(registration))
    control = {"account": "case-user", "task": "case-task", "sid": sid, "errors": [],
               "operations": {"get": True, "disable": True, "enable": True, "stop": True}}
    if operation:
        control["operations"][operation] = False

    def shell(script, **values):
        return json.dumps(control)

    if operation is None:
        assert request_s4u_task(tmp_path, "case-task", "engine", [], shell) == control
    else:
        with pytest.raises(AssertionError):
            request_s4u_task(tmp_path, "case-task", "engine", [], shell)
    assert json.loads((tmp_path / "standard-user-task-control.json").read_text()) == control


def test_s4u_registration_in_flight_does_not_claim_settled(tmp_path):
    from windows_s4u_task_fixture import S4URegistration

    waits = []

    class Thread:
        def join(self, timeout):
            waits.append(timeout)

        def is_alive(self):
            return True

    broker = S4URegistration(tmp_path, "case-user", "sid", "case-task", None)
    broker.thread = Thread()
    with pytest.raises(RuntimeError, match="retain fixture resources"):
        broker.close()
    assert waits == [40] and broker.settled is False


def test_unproven_live_case_keeps_its_identity_acls_and_batch_right(monkeypatch, tmp_path):
    from types import SimpleNamespace

    import windows_cutover_process_fixture as fixture

    monkeypatch.setattr(fixture, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(fixture, "fixture_payloads", lambda output, *paths: paths)
    monkeypatch.setattr(fixture.shutil, "which", lambda name: "/public/bin/7z")
    monkeypatch.setattr(fixture, "disposable_user_sid", lambda name, shell: "S-1-5-21-1-2-3-1001")
    leases, calls = [], []
    monkeypatch.setattr(fixture, "set_batch_logon_right", lambda sid, grant: leases.append(grant))

    def shell(script, **values):
        calls.append(script)
        if "EW_PROBE_LAUNCHER" in values:
            (tmp_path / "native-process-state.json").write_text(
                json.dumps(
                    {"account": values["EW_PROBE_USER"], "phase": "timeout-live", "quiesced": False}
                )
            )
            raise RuntimeError("public case still live")
        return ""

    with pytest.raises(ExceptionGroup, match="fixture and cleanup failed"):
        fixture.run_standard_user_case(
            tmp_path, Path("installer"), Path("engine"), Path("actor"), shell
        )
    assert leases == [True]
    assert not any(" /remove:g " in script or "Remove-LocalUser" in script for script in calls)
    report = json.loads((tmp_path / "standard-user-cleanup.json").read_text())
    assert report["resources_retained"] is True
    assert report["primary_error"] == "public case still live"


def test_batch_right_revoke_is_attempted_after_other_cleanup_errors(monkeypatch, tmp_path):
    from types import SimpleNamespace

    import windows_cutover_process_fixture as fixture

    monkeypatch.setattr(fixture, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(fixture, "fixture_payloads", lambda output, *paths: paths)
    monkeypatch.setattr(fixture.shutil, "which", lambda name: "/public/bin/7z")
    sid = "S-1-5-21-1-2-3-1001"
    monkeypatch.setattr(fixture, "disposable_user_sid", lambda name, shell: sid)
    leases, calls = [], []
    monkeypatch.setattr(
        fixture, "set_batch_logon_right", lambda account, grant: leases.append((account, grant))
    )

    def shell(script, **values):
        calls.append(script)
        if "EW_PROBE_LAUNCHER" in values:
            (tmp_path / "native-process-state.json").write_text(
                json.dumps(
                    {"account": values["EW_PROBE_USER"], "phase": "exited", "quiesced": True}
                )
            )
            raise RuntimeError("public child failure")
        if " /remove:g " in script:
            raise RuntimeError("public ACL failure")
        return ""

    with pytest.raises(ExceptionGroup):
        fixture.run_standard_user_case(
            tmp_path, Path("installer"), Path("engine"), Path("actor"), shell
        )
    assert leases == [(sid, True), (sid, False)]
    assert "Remove-LocalUser" in calls[-1]


@pytest.mark.skipif(os.name != "nt", reason="actual standard-user protected-tree timeout")
def test_native_protected_tree_timeout_is_quiescent_before_cleanup(tmp_path):
    import sys

    import prove_windows_cutover as proof
    from windows_cutover_process_fixture import run_standard_user_case

    # Payloads only satisfy immutable input staging; timeout mode never executes them.
    with pytest.raises(RuntimeError, match="fixture timeout"):
        run_standard_user_case(
            tmp_path,
            Path(sys.executable),
            Path(sys.executable),
            Path(sys.executable),
            proof.powershell,
            timeout_probe=True,
        )
    protected = json.loads((tmp_path / "timeout-protected-actor.json").read_text())
    assert protected["pid"] > 0, "Probe never reached its real PROCESS_TERMINATE deny"
    state = json.loads((tmp_path / "native-process-state.json").read_text(encoding="utf-8-sig"))
    assert state["phase"] == "timeout-quiesced" and state["quiesced"] is True
    assert state["kill_exit"] == 0 and state["process_signalled"] is True
    assert state["active_processes"] == 0
    cleanup = json.loads((tmp_path / "standard-user-cleanup.json").read_text())
    assert cleanup["resources_retained"] is False and cleanup["cleanup_errors"] == []
    assert [r["operation"] for r in cleanup["batch_logon_lease"]] == ["grant", "revoke"]


@pytest.mark.skipif(os.name != "nt", reason="native timeout outcome owner")
def test_native_timeout_outcome_requires_successful_kill_and_signal(tmp_path):
    import prove_windows_cutover as proof

    source = Path(__file__).resolve().parents[1] / "scripts/windows_standard_user.ps1"
    output = proof.powershell(
        r"""
$source=Get-Content -LiteralPath $env:EW_NATIVE_SOURCE -Raw
$prefix=$source.Substring(0,$source.IndexOf('$code=[QualificationUser]'))
. ([scriptblock]::Create($prefix))
foreach ($code in @(-1,0,5)) {
    foreach ($signal in @($false,$true)) {
        $actual=[QualificationUser]::TimeoutQuiesced($code,$signal)
        if ($actual -ne ($code -eq 0 -and $signal)) { throw 'Unsafe timeout outcome' }
        foreach ($active in @(0,1,10)) {
            $whole=[QualificationUser]::WholeTreeQuiesced($code,$signal,$active)
            if ($whole -ne ($code -eq 0 -and $signal -and $active -eq 0)) {
                throw 'Unsafe tree outcome'
            }
        }
    }
}
'TIMEOUT_OUTCOME_BOUNDARIES_PASS'
""",
        EW_NATIVE_SOURCE=str(source),
        EW_PROBE_OUTPUT=str(tmp_path),
        EW_PROBE_USER="EWProbe-public",
    )
    assert "TIMEOUT_OUTCOME_BOUNDARIES_PASS" in output.splitlines()


def test_case_cleanup_attempts_actors_after_task_failure(tmp_path):
    import prove_windows_cutover as proof

    calls = []

    def task_cleanup():
        calls.append("task")
        raise RuntimeError("public task cleanup failure")

    def actor_cleanup():
        calls.append("actor")

    result = proof.settle_case_cleanup(
        tmp_path / "case-cleanup.json",
        "public-user",
        "old-then-new",
        [("task", task_cleanup), ("actor", actor_cleanup)],
    )
    assert calls == ["task", "actor"]
    assert result["quiesced"] is False
    assert result["errors"] == [{"owner": "task", "error": "public task cleanup failure"}]


def test_case_cleanup_aggregates_failures_and_binds_success(tmp_path):
    import prove_windows_cutover as proof

    calls = []

    def failure():
        calls.append("bad")
        raise RuntimeError("public actor failure")

    result = proof.settle_case_cleanup(
        tmp_path / "case-cleanup.json",
        "public-user",
        "old-then-new",
        [("first", failure), ("second", failure), ("last", lambda: calls.append("good"))],
    )
    assert calls == ["bad", "bad", "good"] and len(result["errors"]) == 2
    assert result["quiesced"] is False
    result = proof.settle_case_cleanup(
        tmp_path / "case-cleanup.json",
        "public-user",
        "old-then-new",
        [],
    )
    assert result == json.loads((tmp_path / "case-cleanup.json").read_text())
    assert result["account"] == "public-user" and result["case"] == "old-then-new"
    assert result["quiesced"] is True


def test_native_evidence_destination_is_ci_selected_and_probe_isolated(tmp_path, monkeypatch):
    import prove_windows_cutover as proof

    monkeypatch.setenv("EW_QUALIFICATION_TEST_OUTPUT", str(tmp_path / "archive"))
    first = proof.probe_output("protected-timeout", tmp_path / "pytest")
    other = proof.probe_output("normal-exit", tmp_path / "pytest")
    assert first.parent == tmp_path / "archive" and first != other
    assert first.is_dir()
    monkeypatch.delenv("EW_QUALIFICATION_TEST_OUTPUT")
    assert proof.probe_output("protected-timeout", tmp_path / "pytest") == tmp_path / "pytest"


def test_task_state_timeout_keeps_original_failure_and_records_diagnostics(tmp_path, monkeypatch):
    import subprocess

    import prove_windows_cutover as proof

    budgets = []
    original = subprocess.TimeoutExpired("public-state", 40, output="public partial stdout")

    def query(script, *, timeout=40, **values):
        budgets.append(timeout)
        if len(budgets) == 1:
            raise original
        return "public direct scheduler state"

    monkeypatch.setattr(proof, "powershell", query)
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        proof.task_state("public-task", tmp_path / "query.json")
    assert caught.value is original and budgets == [40, 10]
    receipt = json.loads((tmp_path / "query.json").read_text())
    assert receipt["outcome"] == "timeout" and receipt["stdout"] == "public partial stdout"
    assert receipt["direct_scheduler"] == "public direct scheduler state"


@pytest.mark.skipif(os.name != "nt", reason="actual normal-exit descendant job")
def test_native_normal_exit_settles_remaining_descendants(tmp_path):
    import sys

    import prove_windows_cutover as proof
    from windows_cutover_process_fixture import run_standard_user_case

    with pytest.raises(RuntimeError, match="fixture exit"):
        run_standard_user_case(
            tmp_path,
            Path(sys.executable),
            Path(sys.executable),
            Path(sys.executable),
            proof.powershell,
            orphan_probe=True,
        )
    assert json.loads((tmp_path / "orphan-actor.json").read_text())["pid"] > 0
    native = json.loads((tmp_path / "native-process-state.json").read_text(encoding="utf-8-sig"))
    assert native["phase"] == "exited" and native["quiesced"] is True
    assert native["active_before_cleanup"] > 0 and native["active_processes"] == 0
    cleanup = json.loads((tmp_path / "standard-user-cleanup.json").read_text())
    assert cleanup["resources_retained"] is False and cleanup["cleanup_errors"] == []
    assert [row["operation"] for row in cleanup["batch_logon_lease"]] == ["grant", "revoke"]


@pytest.mark.skipif(os.name != "nt", reason="actual process cleanup after early owner failure")
def test_native_case_cleanup_fault_still_stops_every_actor(tmp_path):
    import subprocess
    import sys

    import prove_windows_cutover as proof

    actors = [
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"]) for _ in range(2)
    ]

    def fail():
        raise RuntimeError("public task cleanup fault")

    def stop(actor):
        actor.kill()
        actor.wait(timeout=10)

    try:
        result = proof.settle_case_cleanup(
            tmp_path / "case-cleanup.json",
            "public-user",
            "old-then-new",
            [("task", fail)] + [("actor", lambda p=p: stop(p)) for p in actors],
        )
        assert result["quiesced"] is False
        assert all(p.poll() is not None for p in actors)
    finally:
        for actor in actors:
            if actor.poll() is None:
                stop(actor)


@pytest.mark.skipif(os.name != "nt", reason="native receipt join owner")
def test_native_quiescence_join_rejects_missing_mixed_and_incomplete_outcomes(tmp_path):
    import prove_windows_cutover as proof

    source = Path(__file__).resolve().parents[1] / "scripts/windows_standard_user.ps1"
    output = proof.powershell(
        r"""
$source=Get-Content -LiteralPath $env:EW_NATIVE_SOURCE -Raw
$prefix=$source.Substring(0,$source.IndexOf('$code=[QualificationUser]'))
. ([scriptblock]::Create($prefix))
$native=@{account='public-user';phase='exited-tree';process_signalled=$true;kill_exit=0;active_processes=0}
$case=@{account='public-user';case='old-then-new';phase='complete';quiesced=$true}
if (-not (Join-Quiescence $native $case 'public-user')) {throw 'Faithful result rejected'}
foreach ($patch in @(
    @{account='other'},@{case='other'},@{phase='pending'},@{quiesced=$false},@{quiesced='true'}
)) {
    $bad=$case.Clone(); foreach ($key in $patch.Keys) {$bad[$key]=$patch[$key]}
    if (Join-Quiescence $native $bad 'public-user') {throw 'Unsafe case admitted'}
}
if (Join-Quiescence $native $null 'public-user') {throw 'Missing case admitted'}
foreach ($patch in @(
    @{account='other'},@{phase='active'},@{kill_exit=5},@{process_signalled=$false},
    @{active_processes=1},@{active_processes=$null}
)) {
    $bad=$native.Clone(); foreach ($key in $patch.Keys) {$bad[$key]=$patch[$key]}
    if (Join-Quiescence $bad $case 'public-user') {throw 'Unsafe tree admitted'}
}
'CASE_JOIN_BOUNDARIES_PASS'
""",
        EW_NATIVE_SOURCE=str(source),
        EW_PROBE_OUTPUT=str(tmp_path),
        EW_PROBE_USER="public-user",
    )
    assert "CASE_JOIN_BOUNDARIES_PASS" in output.splitlines()


def test_receipt_write_failure_does_not_skip_case_cleanup(tmp_path):
    import prove_windows_cutover as proof

    class Receipt:
        calls = 0

        def write_text(self, content):
            self.calls += 1
            if self.calls == 1:
                raise OSError("public receipt fault")
            return (tmp_path / "case-cleanup.json").write_text(content)

    calls = []
    result = proof.settle_case_cleanup(
        Receipt(),
        "public-user",
        "old-then-new",
        [("actor", lambda: calls.append("stopped"))],
        primary=RuntimeError("public original failure"),
    )
    assert calls == ["stopped"] and result["quiesced"] is False
    assert result["primary_error"] == "public original failure"
    assert result["errors"] == [{"owner": "receipt", "error": "public receipt fault"}]


@pytest.mark.parametrize("script", ["", "'BUSY'", '"double quotes"', "'ひとこと'", "throw 'a b'"])
def test_native_script_encoding_preserves_quotes_and_unicode(script):
    import base64

    from windows_qualification_environment import encoded_powershell_script

    encoded = encoded_powershell_script(script)
    assert base64.b64decode(encoded).decode("utf-16-le") == script
    assert not any(character in encoded for character in "'\" \r\n")


def test_scheduled_actor_terminates_held_object_before_waiting():
    import windows_cutover_process_fixture as fixture

    calls = []

    class Kernel:
        terminated = False

        def WaitForSingleObject(self, handle, timeout):
            calls.append(("wait", handle, timeout))
            return 0 if self.terminated else 258

        def TerminateProcess(self, handle, code):
            calls.append(("terminate", handle, code))
            self.terminated = True
            return True

        def CloseHandle(self, handle):
            calls.append(("close", handle))

    actor = fixture.ScheduledActor.__new__(fixture.ScheduledActor)
    actor.kernel, actor.handle = Kernel(), 42
    actor.settle()
    assert calls == [("wait", 42, 0), ("terminate", 42, 1), ("wait", 42, 10000), ("close", 42)]


def test_scheduled_actor_does_not_terminate_an_exited_held_object():
    import windows_cutover_process_fixture as fixture

    calls = []

    class Kernel:
        def WaitForSingleObject(self, handle, timeout):
            calls.append(("wait", timeout))
            return 0

        def TerminateProcess(self, handle, code):
            pytest.fail("Exited object must not be terminated")

        def CloseHandle(self, handle):
            calls.append(("close", handle))

    actor = fixture.ScheduledActor.__new__(fixture.ScheduledActor)
    actor.kernel, actor.handle = Kernel(), 42
    actor.settle()
    assert calls == [("wait", 0), ("close", 42)]


@pytest.mark.parametrize("settled", [0, 258, 0xFFFFFFFF])
def test_scheduled_actor_termination_race_uses_same_held_object(monkeypatch, settled):
    import windows_cutover_process_fixture as fixture

    calls = []

    class Kernel:
        def WaitForSingleObject(self, handle, timeout):
            calls.append(("wait", handle, timeout))
            return 258 if len(calls) == 1 else settled

        def TerminateProcess(self, handle, code):
            calls.append(("terminate", handle, code))
            return False

        def CloseHandle(self, handle):
            calls.append(("close", handle))

    monkeypatch.setattr(
        fixture.ctypes, "WinError", lambda code: OSError(code, "public denial"), raising=False
    )
    monkeypatch.setattr(fixture.ctypes, "get_last_error", lambda: 5, raising=False)
    actor = fixture.ScheduledActor.__new__(fixture.ScheduledActor)
    actor.kernel, actor.handle = Kernel(), 42
    if settled == 0:
        actor.settle()
    else:
        with pytest.raises(OSError, match="public denial"):
            actor.settle()
    assert calls == [("wait", 42, 0), ("terminate", 42, 1), ("wait", 42, 0), ("close", 42)]


def test_scheduled_actor_denial_remains_failure_and_closes_handle(monkeypatch):
    import windows_cutover_process_fixture as fixture

    calls = []

    class Kernel:
        def WaitForSingleObject(self, handle, timeout):
            return 258

        def TerminateProcess(self, handle, code):
            return False

        def CloseHandle(self, handle):
            calls.append(handle)

    monkeypatch.setattr(
        fixture.ctypes, "WinError", lambda code: OSError(code, "public denial"), raising=False
    )
    monkeypatch.setattr(fixture.ctypes, "get_last_error", lambda: 5, raising=False)
    actor = fixture.ScheduledActor.__new__(fixture.ScheduledActor)
    actor.kernel, actor.handle = Kernel(), 42
    with pytest.raises(OSError, match="public denial"):
        actor.settle()
    assert calls == [42]


@pytest.mark.skipif(os.name != "nt", reason="actual held scheduled actor cleanup")
def test_native_scheduled_actor_terminates_actual_held_process(tmp_path):
    import subprocess
    import sys

    from windows_cutover_process_fixture import ScheduledActor

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        actor = ScheduledActor(child.pid)
        actor.settle()
        child.wait(timeout=10)
        assert child.returncode != 0
        (tmp_path / "scheduled-actor-cleanup.json").write_text(
            json.dumps({"pid": child.pid, "returncode": child.returncode, "quiesced": True})
        )
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
