"""Native existing-db qualification of the real NSIS publication boundary.

Ports the round-3 schema-34 frozen Store/native-lock/HTTP actor to Windows.
Uses public fixtures only; must run on an isolated Windows CI runner.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from smoke_packaged_engine import _request

PREDECESSOR = "43a850d8b00cddfdad2a767b7b27fc792707b010"
ROOT = Path(__file__).resolve().parents[1]


def powershell(script: str, **values: str) -> str:
    # Data travels through environment, never interpolated as PowerShell code.
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        env=dict(os.environ, **values),
        capture_output=True,
        text=True,
        timeout=40,
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


def isolated_environment(root: Path):
    environment = os.environ.copy()
    for key in ("PYTHONHOME", "PYTHONPATH", "STATE_DIRECTORY"):
        environment.pop(key, None)
    for key in ("APPDATA", "HOME", "LOCALAPPDATA", "USERPROFILE", "XDG_CONFIG_HOME"):
        environment[key] = str(root)
    environment["XDG_STATE_HOME"] = str(root / "state")
    return environment


def build_old_actor(predecessor: Path, output: Path):
    actual = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=predecessor,
        text=True,
    ).strip()
    if actual != PREDECESSOR:
        raise RuntimeError("The predecessor checkout is not the frozen schema-34 revision")
    build = output / "build"
    environment = dict(os.environ, PYTHONPATH=str(predecessor / "src"))
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--onefile",
            "--name",
            "legacy-source-owner",
            "--paths",
            str(predecessor / "src"),
            "--distpath",
            str(build / "dist"),
            "--workpath",
            str(build / "work"),
            "--specpath",
            str(build),
            str(ROOT / "scripts/windows_cutover_legacy_actor.py"),
        ],
        env=environment,
        check=True,
    )
    return build / "dist/legacy-source-owner.exe"


def run_case(output: Path, installer: Path, new_engine: Path, old_actor: Path, case: str):
    folder = output / case
    folder.mkdir(mode=0o700)
    installation = output / "installed"
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
        install = subprocess.run(
            [str(installer), "/S", "/UPDATE", "/D=" + str(installation)],
            capture_output=True,
            timeout=240,
        )
        (folder / "installer.stdout").write_bytes(install.stdout)
        (folder / "installer.stderr").write_bytes(install.stderr)
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
                process.kill()
            process.wait(timeout=10)
        for log in handles:
            log.close()
        release.set()
        server.shutdown()
        server.server_close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("installer", type=Path)
    parser.add_argument("engine", type=Path)
    parser.add_argument("predecessor", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("This qualification requires native Windows")
    args.output.mkdir(parents=True, mode=0o700)
    actor = build_old_actor(args.predecessor.resolve(), args.output.resolve())
    results = [
        run_case(
            args.output.resolve(), args.installer.resolve(), args.engine.resolve(), actor, case
        )
        for case in ("old-then-new", "new-then-old")
    ]
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
