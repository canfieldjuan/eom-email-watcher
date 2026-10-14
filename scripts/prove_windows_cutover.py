"""Native existing-db qualification of the real NSIS publication boundary.

Ports the round-3 schema-34 frozen Store/native-lock/HTTP actor to Windows.
Uses public fixtures only; must run on an isolated Windows CI runner.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from probe_windows_cutover_context import assert_standard_context, parse_tokens, token_profile
from smoke_packaged_engine import _request
from windows_cutover_parity import installer_manifest
from windows_cutover_process_fixture import (
    deny_termination,
)
from windows_cutover_process_fixture import (
    run_standard_user_case as standard_case,
)
from windows_qualification_environment import powershell_environment, profile_environment_report

PREDECESSOR = "43a850d8b00cddfdad2a767b7b27fc792707b010"
ROOT = Path(__file__).resolve().parents[1]


def powershell(script: str, *, timeout: int = 40, **values: str) -> str:
    # Data travels through environment, never interpolated as PowerShell code.
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        env=powershell_environment(dict(os.environ, **values)),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return result.stdout.strip()


def wait_file(path: Path, process=None):
    deadline = time.monotonic() + 40
    while not path.exists():
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"Old actor exited before {path.name}: {process.returncode}")
        if time.monotonic() > deadline:
            raise TimeoutError(f"Missing fixture receipt: {path.name}")
        time.sleep(0.02)


def phase_outcomes(attempt: Path) -> dict:
    phases = {}
    for path in attempt.glob("eom-email-watcher-cutover-*.log"):
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        for match in re.finditer(r"^phase=(Name|Pre|Post) exit=(\S+)", text, re.MULTILINE):
            phase, code = match.groups()
            phases.setdefault(phase, []).append(code)
    (attempt / "installer.phases.json").write_text(json.dumps(phases, indent=2))
    if phases.get("Post"):
        assert phases.get("Pre") == ["0"], "Post ran without a successful Pre"
    return phases


def run_installer(installer: Path, installation: Path, folder: Path, label: str):
    attempt = folder / label
    attempt.mkdir(mode=0o700)
    result = subprocess.run(
        [str(installer), "/S", "/UPDATE", "/D=" + str(installation)],
        capture_output=True,
        timeout=240,
        env=powershell_environment(TEMP=str(attempt), TMP=str(attempt)),
    )
    (attempt / "installer.stdout").write_bytes(result.stdout)
    (attempt / "installer.stderr").write_bytes(result.stderr)
    phase_outcomes(attempt)
    return result, attempt


def isolated_environment(root: Path):
    environment = powershell_environment()
    for key in ("PYTHONHOME", "PYTHONPATH", "STATE_DIRECTORY"):
        environment.pop(key, None)
    for key in ("APPDATA", "HOME", "LOCALAPPDATA", "USERPROFILE", "XDG_CONFIG_HOME"):
        environment[key] = str(root)
    environment["XDG_STATE_HOME"] = str(root / "state")
    return environment


def build_old_actor(predecessor: Path, output: Path, keyring: Path | None = None):
    actual = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=predecessor,
        text=True,
    ).strip()
    if actual != PREDECESSOR:
        raise RuntimeError("The predecessor checkout is not the frozen schema-34 revision")
    build = output / "build"
    environment = dict(os.environ, PYTHONPATH=str(predecessor / "src"))
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--onefile",
        "--name",
        "legacy-source-owner",
        "--collect-all",
        "tzdata",
        "--paths",
        str(predecessor / "src"),
        "--distpath",
        str(build / "dist"),
        "--workpath",
        str(build / "work"),
        "--specpath",
        str(build),
    ]
    if keyring is not None:
        canonical = output / "connect-entitlement-keyring.json"
        shutil.copy2(keyring, canonical)
        command.extend(["--add-data", f"{canonical}{os.pathsep}connect_automate_data"])
    command.append(str(ROOT / "scripts/windows_cutover_legacy_actor.py"))
    subprocess.run(
        command,
        env=environment,
        check=True,
    )
    binary = build / "dist/legacy-source-owner.exe"
    content = binary.read_bytes()
    pe_offset = int.from_bytes(content[60:64], "little")
    assert content[pe_offset : pe_offset + 4] == b"PE\0\0"
    assert int.from_bytes(content[pe_offset + 4 : pe_offset + 6], "little") == 0x8664, (
        "Installer proof requires running x64 predecessor images"
    )
    return binary


def run_case(output: Path, installer: Path, new_engine: Path, old_actor: Path, case: str):
    if case == "old-then-new":
        assert_standard_context(
            token_profile([os.getpid()], output / "standard-user-profile"),
            required_roles={"diagnostic-probe", "stop-host-entry"},
            required_pids={os.getpid()},
        )
    folder = output / case
    folder.mkdir(mode=0o700)
    installation = folder / "installed"
    installation.mkdir(exist_ok=True)
    private = folder / "private"
    private.mkdir(mode=0o700)
    database = private / "mail.sqlite3"
    config = private / "config.toml"
    config.write_text(
        'timezone = "America/Chicago"\n'
        f'database_file = "{database.as_posix()}"\n'
        'model_base_url = "http://127.0.0.1:9/v1"\n'
        'model_name = "public-fixture"\nmodel_require_auth = false\n'
        "notifications_enabled = false\n",
        encoding="utf-8",
    )
    config.chmod(0o600)
    environment = isolated_environment(private)
    engine = installation / "eom-mail-engine.exe"
    desktop = installation / "eom-email-watcher-desktop.exe"
    for path in (engine, desktop):
        shutil.copy2(old_actor, path)
    posted, release = threading.Event(), threading.Event()
    posts = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            posts.append(self.path)
            posted.set()
            release.wait(150)
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"public-result")
            except (ConnectionError, OSError):
                pass  # Installer terminated the old client; preserve the POST count.

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    endpoint = f"http://127.0.0.1:{server.server_port}/public-job"
    trigger = folder / "resume-old"
    actors = []
    handles = []
    task = "EW-Cutover-" + case + "-" + str(os.getpid())
    registered = False

    def command(path, ready, mode):
        return [str(path), str(database), str(ready), str(trigger), endpoint, mode]

    def start(path, name, mode):
        ready = folder / (name + ".ready")
        log = (folder / (name + ".log")).open("w", encoding="utf-8")
        handles.append(log)
        process = subprocess.Popen(
            command(path, ready, mode), env=environment, stdout=log, stderr=subprocess.STDOUT
        )
        actors.append(process)
        wait_file(ready, process)
        return process

    try:
        subprocess.run(
            command(engine, folder / "seed.ready", "seed"), env=environment, check=True, timeout=60
        )
        with sqlite3.connect(database) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 34
        first = start(engine, "sidecar", "held" if case == "old-then-new" else "idle")
        second = start(desktop, "desktop", "idle")
        if case == "old-then-new":
            assert posted.wait(15)
        task_ready = folder / "scheduled.ready"
        task_args = subprocess.list2cmdline(command(engine, task_ready, "idle")[1:])
        powershell(
            "$ErrorActionPreference='Stop'; "
            "$a=New-ScheduledTaskAction -Execute $env:EW_ENGINE -Argument $env:EW_ARGS; "
            "$t=New-ScheduledTaskTrigger -Once -At (Get-Date).AddDays(1); "
            "Register-ScheduledTask -TaskName $env:EW_TASK -Action $a -Trigger $t | Out-Null; "
            "Start-ScheduledTask -TaskName $env:EW_TASK",
            EW_ENGINE=str(engine),
            EW_ARGS=task_args,
            EW_TASK=task,
        )
        registered = True
        wait_file(task_ready)
        if case == "new-then-old":
            # The new image opens first without a lifecycle receipt. It must
            # keep the legacy protocol and allow ordinary reads, not qualify.
            view = _request(
                new_engine,
                config_path=config,
                operation="mail.accounts.list",
                payload=None,
                working_directory=folder,
                environment=environment,
            )
            assert "upgrade pending" in view["data"]["accounts"][0]["source_lock_notice"]
            late_ready = folder / "late-old.ready"
            late = subprocess.run(
                command(engine, late_ready, "idle"),
                env=environment,
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert late.returncode != 0 and "newer than supported" in late.stderr
            assert not late_ready.exists() and not posts
        if case == "unknown-image":
            prior_digest = hashlib.sha256(engine.read_bytes()).hexdigest()
            unknown_image = installation / "not-from-package.exe"
            shutil.copy2(old_actor, unknown_image)
            unknown_ready = folder / "unknown.ready"
            unknown = subprocess.Popen(
                command(unknown_image, unknown_ready, "idle"), env=environment
            )
            try:
                wait_file(unknown_ready, unknown)
                refusal, attempt = run_installer(installer, installation, folder, "unknown-refusal")
                (folder / "installer.unknown.stdout").write_bytes(refusal.stdout)
                (folder / "installer.unknown.stderr").write_bytes(refusal.stderr)
                assert refusal.returncode != 0, "Unknown image was admitted"
                assert unknown.poll() is None and first.poll() is None and second.poll() is None
                assert hashlib.sha256(engine.read_bytes()).hexdigest() == prior_digest
                assert not (installation / "windows-source-cutover-v1.json").exists()
                assert (
                    powershell("(Get-ScheduledTask -TaskName $env:EW_TASK).State", EW_TASK=task)
                    == "Ready"
                )
                transcripts = "\n".join(
                    p.read_text(encoding="utf-8-sig", errors="replace")
                    for p in attempt.glob("cutover-*-Pre-*.txt")
                )
                assert "Unknown installation image: " + str(unknown_image) in transcripts
            finally:
                if unknown.poll() is None:
                    subprocess.run(["taskkill", "/PID", str(unknown.pid), "/T", "/F"], check=True)
                unknown.wait(timeout=10)
            return {"case": case, "result": "PASS", "unknown_survived_refusal": True}
        if case == "old-then-new":
            # A real installed owner survives the stop attempt. Refusal must
            # leave bytes intact; restoring permissions admits the same update.
            prior_digest = hashlib.sha256(engine.read_bytes()).hexdigest()
            with deny_termination(first):
                actor_profile = token_profile(
                    [os.getpid(), first.pid], folder / "protected-actor-profile"
                )
                assert_standard_context(
                    actor_profile,
                    required_roles={"diagnostic-probe", "stop-host-entry"},
                    required_pids={os.getpid(), first.pid},
                )
                refusal, attempt = run_installer(
                    installer, installation, folder, "protected-refusal"
                )
                (folder / "installer.refusal.stdout").write_bytes(refusal.stdout)
                (folder / "installer.refusal.stderr").write_bytes(refusal.stderr)
                task_state = powershell(
                    "(Get-ScheduledTask -TaskName $env:EW_TASK).State", EW_TASK=task
                )
                pending = installation / "windows-cutover-pending-v1.json"
                observed = {
                    "installer_exit": refusal.returncode,
                    "protected_pid": first.pid,
                    "protected_actor_profile": actor_profile,
                    "task_state": task_state,
                    "pending": pending.read_text(encoding="utf-8-sig")
                    if pending.exists()
                    else None,
                    "diagnostic_files": sorted(p.name for p in folder.glob("*cutover*")),
                }
                (folder / "installer.refusal.state.json").write_text(json.dumps(observed, indent=2))
                print("REFUSAL_OBSERVED", json.dumps(observed), flush=True)
                transcripts = "\n".join(
                    p.read_text(encoding="utf-8-sig", errors="replace")
                    for p in attempt.glob("cutover-*-Pre-*.txt")
                )
                observed["stopping_pids"] = [
                    int(value)
                    for value in re.findall(r"Cutover stopping process=(\d+)", transcripts)
                ]
                observed["protected_pid_selected"] = first.pid in observed["stopping_pids"]
                observed["stop_tokens"] = parse_tokens(transcripts)
                (folder / "installer.refusal.state.json").write_text(json.dumps(observed, indent=2))
                print("REFUSAL_CONTEXT", json.dumps(observed), flush=True)
                assert_standard_context(
                    observed["stop_tokens"],
                    required_roles={
                        "installer",
                        "stop-host-entry",
                        "stop-host-before-handle",
                        "actor-before-stop",
                    },
                    required_pids={first.pid},
                )
                assert f"failure-stage=process-stop process={first.pid} code=5" in transcripts, (
                    "Refusal was not caused by the protected owner's access-denied stop"
                )
                assert refusal.returncode != 0, "Installer accepted an unquiesced owner"
                assert first.poll() is None, "Protected old owner did not survive"
                assert hashlib.sha256(engine.read_bytes()).hexdigest() == prior_digest
                assert not (installation / "windows-source-cutover-v1.json").exists()
                assert task_state == "Ready", observed
        install, attempt = run_installer(installer, installation, folder, "successful-update")
        (folder / "installer.stdout").write_bytes(install.stdout)
        (folder / "installer.stderr").write_bytes(install.stderr)
        if case == "old-then-new":
            retry_tokens = parse_tokens(
                "\n".join(
                    p.read_text(encoding="utf-8-sig", errors="replace")
                    for p in attempt.glob("cutover-*-Pre-*.txt")
                )
            )
            assert_standard_context(
                retry_tokens,
                required_roles={
                    "installer",
                    "stop-host-entry",
                    "stop-host-before-terminate",
                    "actor-before-stop",
                },
                required_pids={first.pid},
            )
        assert install.returncode == 0, install.returncode
        first.wait(timeout=10)
        second.wait(timeout=10)
        assert first.returncode != 0 and second.returncode != 0
        task_state = powershell("(Get-ScheduledTask -TaskName $env:EW_TASK).State", EW_TASK=task)
        assert task_state == "Ready", task_state
        assert (
            hashlib.sha256(engine.read_bytes()).digest()
            == hashlib.sha256(new_engine.read_bytes()).digest()
        )
        receipt = json.loads(
            (installation / "windows-source-cutover-v1.json").read_text(encoding="utf-8-sig")
        )
        assert receipt["engine_sha256"] == hashlib.sha256(engine.read_bytes()).hexdigest()
        installed_catalog = installation / "windows-package-files-v1.txt"
        assert (
            receipt["program_catalog_sha256"]
            == hashlib.sha256(installed_catalog.read_bytes()).hexdigest()
        )
        payload = installer_manifest(installer, folder / "package-payload")
        packaged_programs = {
            name.replace("\\", "/").casefold()
            for name in payload
            if name.casefold().endswith(".exe") and not name.startswith("$PLUGINSDIR/")
        }
        catalog_lines = installed_catalog.read_text(encoding="utf-16").splitlines()
        assert catalog_lines[0] == "EW-PACKAGE-FILES-V1"
        catalog_programs = {
            name.replace("\\", "/").casefold()
            for name in catalog_lines[1:]
            if name.casefold().endswith(".exe") and not name.startswith("EW-APP-IMAGE|")
        }
        (folder / "package-programs.json").write_text(
            json.dumps(
                {
                    "package": sorted(packaged_programs),
                    "catalog": sorted(catalog_programs),
                },
                indent=2,
            )
        )
        assert catalog_programs == packaged_programs, (
            "Owned programs differ from the actual installer payload"
        )
        view = _request(
            engine,
            config_path=config,
            operation="mail.accounts.list",
            payload=None,
            working_directory=folder,
            environment=environment,
        )
        assert view["data"]["accounts"][0]["source_lock_notice"] is None
        _request(
            engine,
            config_path=config,
            operation="health.get",
            payload=None,
            working_directory=folder,
            environment=environment,
        )
        with sqlite3.connect(database) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 35
            assert (
                db.execute(
                    "SELECT value FROM maintenance_state WHERE key='source_lock_protocol'"
                ).fetchone()[0]
                == "source-stripe-v1"
            )
            assert db.execute("SELECT COUNT(*) FROM cutover_probe_outcomes").fetchone()[0] == 0
        # Trigger all old idle actors after marker publication. They were killed,
        # so no late legacy lock, POST or stale completion can appear.
        trigger.write_text("resume", encoding="ascii")
        release.set()
        server.shutdown()
        server.server_close()
        worker.join(5)
        assert len(posts) == (1 if case == "old-then-new" else 0), posts
        return {
            "case": case,
            "result": "PASS",
            "pre_cutover_posts": len(posts),
            "task_restored": task_state,
            "installed_sha256": receipt["engine_sha256"],
            "installer_refusal_then_retry": case == "old-then-new",
        }
    finally:
        if registered:
            powershell(
                "Stop-ScheduledTask -TaskName $env:EW_TASK; "
                "Unregister-ScheduledTask -TaskName $env:EW_TASK -Confirm:$false",
                EW_TASK=task,
            )
        for process in actors:
            if process.poll() is None:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], check=True)
            process.wait(timeout=10)
        for log in handles:
            log.close()
        release.set()
        server.shutdown()
        server.server_close()


def run_standard_user_case(output, installer, engine, actor):
    return standard_case(output, installer, engine, actor, powershell)


def run_cases(output, installer, engine, actor):
    return [
        run_case(output, installer, engine, actor, "unknown-image"),
        run_standard_user_case(output, installer, engine, actor),
        run_case(output, installer, engine, actor, "new-then-old"),
    ]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("installer", type=Path)
    parser.add_argument("engine", type=Path)
    parser.add_argument("predecessor", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--case", choices=["old-then-new"])
    parser.add_argument("--actor", type=Path)
    parser.add_argument("--tool-directory", type=Path)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("This qualification requires native Windows")
    args.output.mkdir(parents=True, mode=0o700, exist_ok=True)
    if args.case:
        if args.actor is None:
            parser.error("Standard-user child requires the prebuilt frozen actor")
        with (
            (args.output / "standard-user.stdout.log").open("w") as stdout,
            (args.output / "standard-user.stderr.log").open("w") as stderr,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            try:
                profile = profile_environment_report()
                (args.output / "standard-user-profile.json").write_text(
                    json.dumps(profile, indent=2)
                )
                if args.tool_directory is None:
                    parser.error("Standard-user child requires the explicit fixture tool directory")
                # Add only the measured extraction tool, never the administrator's PATH.
                os.environ["PATH"] = (
                    os.environ.get("PATH", "") + os.pathsep + str(args.tool_directory)
                )
                result = run_case(
                    args.output.resolve(),
                    args.installer.resolve(),
                    args.engine.resolve(),
                    args.actor.resolve(),
                    args.case,
                )
                (args.output / "standard-user-result.json").write_text(json.dumps(result, indent=2))
                print(json.dumps(result, indent=2))
            except BaseException:
                import traceback

                traceback.print_exc()
                raise
        return
    actor = build_old_actor(args.predecessor.resolve(), args.output.resolve())
    results = run_cases(
        args.output.resolve(), args.installer.resolve(), args.engine.resolve(), actor
    )
    receipt = {
        "predecessor": PREDECESSOR,
        "platform": sys.platform,
        "python": sys.version,
        "legacy_actor_sha256": hashlib.sha256(actor.read_bytes()).hexdigest(),
        "cases": results,
        "limits": (
            "Public frozen Store/native-lock/HTTP actors, not live vendor mail or "
            "live licences. Native Watcher/capture/retention/Connect integration "
            "tests run separately in this same CI job."
        ),
    }
    path = args.output / "qualification.json"
    path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
