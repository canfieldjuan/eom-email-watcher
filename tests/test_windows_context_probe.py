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
    calls = []
    result = {"case": "old-then-new", "result": "PASS"}
    (tmp_path / "standard-user-result.json").write_text(json.dumps(result))

    def shell(script, **values):
        calls.append((script, values))
        if "Start-Process" in script:
            assert "-Credential $credential -LoadUserProfile" in script
            assert "Remove-Item Env:EW_PROBE_PASSWORD" in script
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
    assert "Remove-LocalUser" in calls[-1][0]
    assert all("S-1-5-32-544" not in script for script, _ in calls)


def test_standard_launcher_does_not_report_pass_after_child_failure(monkeypatch, tmp_path):
    from types import SimpleNamespace

    import windows_cutover_process_fixture as fixture

    monkeypatch.setattr(fixture, "os", SimpleNamespace(name="nt"))
    scripts = []

    def shell(script, **values):
        scripts.append(script)
        if "Start-Process" in script:
            raise RuntimeError("public child failure")
        return ""

    with pytest.raises(RuntimeError, match="public child failure"):
        fixture.run_standard_user_case(
            tmp_path, Path("installer"), Path("engine"), Path("actor"), shell
        )
    assert " /remove:g " in scripts[-2]
    assert "Remove-LocalUser" in scripts[-1]
