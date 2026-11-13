"""Child-process driver for tests/test_scheduled_confinement.py (#363).

Every case runs in its own process, because confinement cannot be undone.  The parent
sets HOME so the write roots are a throwaway tree, and passes a base directory outside
those roots.  Cases print one JSON document on stdout.

    python tests/confinement_probe.py CASE BASE [ARG...]
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from eom_email_watcher import cli, confinement
from eom_email_watcher.config import DEFAULT_CONFIG, load_config
from eom_email_watcher.db import Store


def outcome(action) -> str:
    try:
        result = action()
    except OSError as exc:
        return errno.errorcode.get(exc.errno, str(exc.errno))
    except confinement.ConfinementError as exc:
        return f"ConfinementError: {exc}"
    except Exception as exc:  # recorded, never hidden
        return type(exc).__name__
    return result if isinstance(result, str) else "ok"


def _shell_write(target: Path) -> str:
    subprocess.run(["sh", "-c", 'printf x > "$1"', "sh", str(target)],
                   check=False, stderr=subprocess.DEVNULL)
    return "ok" if target.exists() else "refused"


def _command(argv: list[str], shape: str) -> str:
    try:
        run = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=20)
    except FileNotFoundError:
        return "missing"
    if run.returncode != 0:
        return f"exit {run.returncode}"
    return "ok" if run.stdout.startswith(shape) else "unexpected output"


def boundary(base: Path) -> dict[str, str]:
    """Verification item 1: each refused operation outside, each operation inside."""
    roots = confinement.write_roots()
    inside, state = roots.config, roots.state
    outside = base / "outside"
    sibling = Path(f"{state}-sibling")  # shares the state root's name as a prefix
    for directory in (outside / "dir", sibling):
        directory.mkdir(parents=True, exist_ok=True)
    for name in ("existing", "victim", "trunc", "del", "moveme", "linkme", "rename_a"):
        (outside / name).write_text("seed\n")
    (inside / "existing").write_text("seed\n")
    (inside / "moveout").write_text("seed\n")
    (state / "pointer").symlink_to(outside / "victim")
    confinement.restrict_self(roots)
    ops = {
        "inside.create": lambda: (inside / "new").write_text("x"),
        "inside.append": lambda: (inside / "existing").open("a").write("x"),
        "inside.truncate": lambda: os.truncate(inside / "existing", 0),
        "inside.mkdir": lambda: (inside / "newdir").mkdir(),
        "inside.rename_between_roots": lambda: os.rename(inside / "new", state / "new"),
        "inside.unlink": lambda: os.unlink(state / "new"),
        "inside.symlink": lambda: (inside / "link").symlink_to(inside / "existing"),
        "inside.fifo": lambda: os.mkfifo(state / "fifo"),
        "inside.child_shell": lambda: _shell_write(state / "child"),
        "inside.tempfile": lambda: tempfile.NamedTemporaryFile(dir=state).close(),
        "devnull.write": lambda: Path(os.devnull).write_text("x"),
        "outside.create": lambda: (outside / "new").write_text("x"),
        "outside.open_write": lambda: (outside / "existing").open("r+").write("x"),
        "outside.truncate_syscall": lambda: os.truncate(outside / "trunc", 0),
        "outside.unlink": lambda: os.unlink(outside / "del"),
        "outside.mkdir": lambda: (outside / "newdir").mkdir(),
        "outside.rmdir": lambda: os.rmdir(outside / "dir"),
        "outside.rename": lambda: os.rename(outside / "rename_a", outside / "rename_b"),
        "outside.fifo": lambda: os.mkfifo(outside / "fifo"),
        "outside.symlink": lambda: (outside / "link").symlink_to(outside / "existing"),
        "outside.move_in": lambda: os.rename(outside / "moveme", state / "moved"),
        "outside.move_out": lambda: os.rename(inside / "moveout", outside / "moveout"),
        "outside.hard_link_in": lambda: os.link(outside / "linkme", state / "linked"),
        "outside.through_inside_symlink": lambda: (state / "pointer").write_text("x"),
        "outside.prefix_sibling": lambda: (sibling / "f").write_text("x"),
        "outside.root_itself_renamed": lambda: os.rename(inside, inside.with_name("moved")),
        "outside.default_temp": lambda: _default_temp(),
        "outside.child_shell": lambda: _shell_write(outside / "child"),
    }
    return {name: outcome(action) for name, action in ops.items()}


def _default_temp() -> None:
    tempfile.tempdir = None
    os.environ.pop("TMPDIR", None)
    with tempfile.TemporaryFile():
        pass


def limits(base: Path, socket_path: str) -> dict[str, str]:
    """Verification item 2: the five measured limits, asserted as they are."""
    roots = confinement.write_roots()
    outside = base / "outside"
    outside.mkdir(parents=True, exist_ok=True)
    for name in ("meta", "preopen", "prelinked", "thread"):
        (outside / name).write_text("seed\n")
    os.link(outside / "prelinked", roots.state / "prelink")
    preopened = os.open(outside / "preopen", os.O_WRONLY)
    release = threading.Event()
    thread_result: list[str] = []

    def earlier_thread() -> None:
        release.wait()
        thread_result.append(outcome(lambda: (outside / "thread").write_text("x")))

    worker = threading.Thread(target=earlier_thread)
    worker.start()
    # The product refuses a second thread (R4); this case measures why.
    confinement.restrict_self(roots)
    rows = {
        "metadata.chmod": outcome(lambda: os.chmod(outside / "meta", 0o640)),
        "metadata.utime": outcome(lambda: os.utime(outside / "meta", (1, 1))),
        "metadata.setxattr": outcome(
            lambda: os.setxattr(outside / "meta", "user.eom", b"1")
        ),
        "metadata.chown_same_owner": outcome(
            lambda: os.chown(outside / "meta", os.getuid(), os.getgid())
        ),
        "descriptor.opened_before": outcome(lambda: os.write(preopened, b"x") and None),
        "hard_link.existing_before": outcome(lambda: (roots.state / "prelink").write_text("x")),
        "control.write_outside": outcome(lambda: (outside / "control").write_text("x")),
    }
    release.set()
    worker.join()
    rows["thread.started_before"] = thread_result[0]
    rows.update(services(socket_path))
    return rows


def services(socket_path: str) -> dict[str, str]:
    def connect() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(socket_path)

    return {
        "service.socket_outside_roots": outcome(connect),
        "service.manager_property": _command(
            ["systemctl", "--user", "show", "--property=Version"], "Version="
        ),
        "service.session_bus_call": _command(
            ["busctl", "--user", "call", "org.freedesktop.DBus", "/org/freedesktop/DBus",
             "org.freedesktop.DBus", "GetId"],
            's "',
        ),
    }


def services_unconfined(socket_path: str) -> dict[str, str]:
    return services(socket_path)


class _Libc:
    """Delegates to the real C library, failing one Landlock step on request."""

    def __init__(self, real: ctypes.CDLL, fail: str):
        self.real, self.fail = real, fail

    def syscall(self, number, *args):
        if self.fail == "missing" or (self.fail == "restrict" and number == 446):
            ctypes.set_errno(errno.ENOSYS if self.fail == "missing" else errno.EPERM)
            return -1
        if self.fail == "abi2" and number == 444 and args[0] is None:
            return 2
        return self.real.syscall(number, *args)

    def prctl(self, *args):
        if self.fail == "no_new_privs":
            ctypes.set_errno(errno.EINVAL)
            return -1
        return self.real.prctl(*args)


def refuse(base: Path, cause: str) -> None:
    """Verification item 5: start the real CLI under one R4 condition."""
    if cause in {"missing", "abi2", "restrict", "no_new_privs"}:
        real = confinement._libc()
        confinement._libc = lambda: _Libc(real, cause)
    elif cause == "thread":
        threading.Thread(target=time.sleep, args=(5,), daemon=True).start()
    elif cause == "descriptor":
        (base / "outside").mkdir(parents=True, exist_ok=True)
        os.open(base / "outside" / "held", os.O_WRONLY | os.O_CREAT, 0o600)
    cli.main(["check"])


def hold(base: Path) -> None:
    """Verification item 6: enter a confined run, report its temp, wait for release."""
    with confinement.scheduled_run(DEFAULT_CONFIG):
        print(json.dumps({"private": os.environ["TMPDIR"]}), flush=True)
        while not (base / "release").exists():
            time.sleep(0.05)


SPILL = """
PRAGMA cache_size = -512;
CREATE TEMP TABLE spill AS
  WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 20000)
  SELECT i, randomblob(1000) AS b FROM n;
SELECT count(*) FROM (SELECT b FROM spill ORDER BY b);
"""


def sqlite_spill(base: Path, variant: str) -> dict[str, object]:
    """Verification item 7: a spill through Store.connection in a confined process."""
    with confinement.scheduled_run(DEFAULT_CONFIG):
        store = Store(confinement.write_roots().state / "spill.sqlite3")
        with store.connection() as connection:
            temp_store = connection.execute("PRAGMA temp_store").fetchone()[0]
            if variant == "without_pragma":
                connection.execute("PRAGMA temp_store = DEFAULT")
            try:
                connection.executescript(SPILL)
                result = "ok"
            except Exception as exc:
                result = f"{type(exc).__name__}: {exc}"
    return {"temp_store": temp_store, "spill": result}


def reload_outside(base: Path) -> dict[str, str]:
    """R3 on reload: a snapshot loaded after admission is admitted too."""
    with confinement.scheduled_run(DEFAULT_CONFIG):
        original = DEFAULT_CONFIG.read_text(encoding="utf-8")
        moved = original + f'database_file = "{base / "elsewhere" / "db.sqlite3"}"\n'
        DEFAULT_CONFIG.write_text(moved, encoding="utf-8")
        return {
            "reload": outcome(lambda: load_config(DEFAULT_CONFIG) and None),
            "check_body": outcome(lambda: cli._check(DEFAULT_CONFIG, dry_run=True)),
        }


def main() -> None:
    case, base, *rest = sys.argv[1:]
    base_path = Path(base)
    if case == "boundary":
        report: object = boundary(base_path)
    elif case == "limits":
        report = limits(base_path, rest[0])
    elif case == "services_unconfined":
        report = services_unconfined(rest[0])
    elif case == "refuse":
        refuse(base_path, rest[0])
        return
    elif case == "hold":
        hold(base_path)
        return
    elif case == "reload_outside":
        report = reload_outside(base_path)
    elif case == "sqlite_spill":
        report = sqlite_spill(base_path, rest[0])
    else:
        raise SystemExit(f"unknown case {case}")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
