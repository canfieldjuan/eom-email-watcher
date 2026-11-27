"""Measure hosted Landlock enforcement and SQLite spill placement in disposable fixtures."""

import errno
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import nullcontext
from pathlib import Path

from eom_email_watcher import confinement
from eom_email_watcher.config import DEFAULT_CONFIG
from eom_email_watcher.db import Store

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / "tests"), str(REPO / "scripts")]
from confinement_probe import SPILL  # noqa: E402
from test_scheduled_confinement import _env, _home, _probe, _report  # noqa: E402


def write_result(path):
    try:
        path.write_text("synthetic write probe", encoding="utf-8")
    except OSError as exc:
        return errno.errorcode[exc.errno]
    return "ok"


def open_temp_files(roots):
    result = {name: 0 for name in roots}
    for fd in Path("/proc/self/fd").iterdir():
        try:
            target = fd.readlink()
        except FileNotFoundError:
            continue
        for name, root in roots.items():
            if target.is_relative_to(root):
                result[name] += 1
    return result


def measured_spill(base, variant):
    outside = base / "outside"
    outside.mkdir()
    before = write_result(outside / "before")
    confined = variant != "file-outside-unconfined"
    state = confinement.write_roots().state
    with confinement.scheduled_run(DEFAULT_CONFIG) if confined else nullcontext():
        private = Path(os.environ["TMPDIR"]) if confined else state / "probe-temp"
        if not confined:
            private.mkdir()
        inside = write_result(private / "inside")
        after = write_result(outside / "after")
        if variant.startswith("file-outside"):
            os.environ["SQLITE_TMPDIR"] = str(outside)
        elif variant == "file-private":
            os.environ["SQLITE_TMPDIR"] = str(private)

        with Store(state / "spill.sqlite3").connection() as connection:
            options = [
                value for (value,) in connection.execute("PRAGMA compile_options")
                if value.startswith("TEMP_STORE=")
            ]
            mode_before = connection.execute("PRAGMA temp_store").fetchone()[0]
            if variant == "default":
                connection.execute("PRAGMA temp_store = DEFAULT")
            elif variant.startswith("file-"):
                connection.execute("PRAGMA temp_store = FILE")
            mode_after = connection.execute("PRAGMA temp_store").fetchone()[0]
            try:
                connection.executescript(SPILL)
            except sqlite3.OperationalError as exc:
                spill = str(exc)
                spill_error = exc.sqlite_errorname
            else:
                spill = "ok"
                spill_error = None
            temp_files = open_temp_files({"private": private, "outside": outside})

    return {
        "variant": variant,
        "confined": confined,
        "outside_write_before": before,
        "outside_write_during": after,
        "private_write_during": inside,
        "sqlite_compile_temp_store": options,
        "sqlite_mode_before": mode_before,
        "sqlite_mode_during": mode_after,
        "sqlite_spill": spill,
        "sqlite_error": spill_error,
        "open_temp_files": temp_files,
    }


def main():
    strace = shutil.which("strace")
    print(json.dumps({
        "kernel": platform.release(),
        "runner_image_os": os.environ.get("ImageOS"),  # noqa: SIM112
        "runner_image_version": os.environ.get("ImageVersion"),  # noqa: SIM112
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "landlock_abi": confinement.landlock_abi(),
        "minimum_landlock_abi": confinement.MINIMUM_ABI,
        "inherited_sqlite_tmpdir": bool(os.environ.get("SQLITE_TMPDIR")),
        "strace_available": strace is not None,
    }), flush=True)
    with tempfile.TemporaryDirectory(prefix="ew-runner-confinement-") as scratch:
        root = Path(scratch).resolve()
        base = root / "boundary"
        boundary = _report(_probe(_home(base), base, "boundary"))
        print(json.dumps({"existing_boundary": boundary}), flush=True)
        for variant in ("as_shipped", "without_pragma"):
            base = root / f"original-{variant}"
            result = _report(_probe(_home(base), base, "sqlite_spill", variant))
            print(json.dumps({"existing_sqlite_variant": variant, **result}), flush=True)
        variants = ("shipped", "default", "file-private", "file-outside", "file-outside-unconfined")
        for variant in variants:
            base = root / variant
            command = [sys.executable, str(Path(__file__).resolve()), str(base), variant]
            trace = root / f"{variant}.trace"
            if strace:
                command[:0] = [
                    strace, "-qq", "-e", "trace=open,openat,creat", "-o", str(trace), "--",
                ]
            run = subprocess.run(
                command,
                env=_env(_home(base)), check=False, capture_output=True, text=True, timeout=120,
            )
            result = _report(run)
            if trace.exists():
                result["sqlite_temp_opens"] = [
                    line.replace(str(base), "<fixture>")
                    for line in trace.read_text().splitlines()
                    if "etilqs_" in line
                ]
            print(json.dumps(result), flush=True)


if __name__ == "__main__":
    if len(sys.argv) == 3:
        print(json.dumps(measured_spill(Path(sys.argv[1]), sys.argv[2])))
    else:
        main()
