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
