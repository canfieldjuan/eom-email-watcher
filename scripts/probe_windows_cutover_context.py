"""Read-only native token and transcript-exit investigation."""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def parse_tokens(text):
    records = []
    target = None
    for line in text.splitlines():
        stopped = re.fullmatch(r"Cutover stopping process=(\d+)", line.strip())
        if stopped:
            target = int(stopped[1])
        token = re.fullmatch(r"Cutover token role=(\S+) data=(\{.*\})", line.strip())
        if token:
            records.append({"role": token[1], "target_pid": target, "token": json.loads(token[2])})
    return records


def token_profile(process_ids, output):
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    code = r"""
$ErrorActionPreference='Stop'
$source=Get-Content -LiteralPath $env:EW_SCRIPT -Raw
$prefix=$source.Substring(0,$source.IndexOf('function Installed-Processes'))
. ([scriptblock]::Create($prefix)) -Phase Pre -InstallRoot $env:EW_PROBE_ROOT
foreach ($id in ($env:EW_PROBE_IDS | ConvertFrom-Json)) {
    Write-CutoverToken $id 'diagnostic-probe'
}
"""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", code],
        env=dict(
            os.environ,
            EW_SCRIPT=str(ROOT / "desktop/src-tauri/windows/cutover.ps1"),
            EW_PROBE_ROOT=str(output),
            EW_PROBE_IDS=json.dumps(process_ids),
        ),
        capture_output=True,
        text=True,
        timeout=40,
    )
    (output / "profile.stdout.log").write_text(result.stdout)
    (output / "profile.stderr.log").write_text(result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    records = parse_tokens(result.stdout)
    assert records, "Native token query produced no data"
    (output / "tokens.json").write_text(json.dumps(records, indent=2))
    return records


def transcript_probe(output):
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    cases = [
        ("exit-zero", "exit 0", 0),
        ("normal-return", "Stop-Transcript | Out-Null", 0),
        ("real-error", "throw 'public intentional failure'", 1),
    ]
    rows = []
    for name, statement, expected in cases:
        transcript = output / (name + ".txt")
        script = output / (name + ".ps1")
        script.write_text(
            "Start-Transcript -Path $env:EW_EXIT_TRACE -Force | Out-Null\n" + statement + "\n"
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(script)],
            env=dict(os.environ, EW_EXIT_TRACE=str(transcript)),
            capture_output=True,
            text=True,
            timeout=40,
        )
        (output / (name + ".stdout.log")).write_text(result.stdout)
        (output / (name + ".stderr.log")).write_text(result.stderr)
        row = {
            "case": name,
            "exit": result.returncode,
            "system_error_trailer": 'TerminatingError(): "System error."'
            in transcript.read_text(encoding="utf-8-sig", errors="replace"),
        }
        rows.append(row)
        (output / "transcript-results.json").write_text(json.dumps(rows, indent=2))
        assert result.returncode == expected, row
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("Requires native Windows; no token mutation or installation")
    output = args.output.resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    result = {
        "runner_tokens": token_profile([os.getpid()], output / "runner-profile"),
        "transcript_controls": transcript_probe(output / "transcript-controls"),
    }
    (output / "context-probe.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
