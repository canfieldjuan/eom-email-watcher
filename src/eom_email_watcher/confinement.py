"""Scheduled commands confine their own writes (#363).

The single owner of `plans/PR-Scheduled-Confinement-363.md` R2-R5 on Linux: the write
roots, the Landlock ruleset, the configured write targets, the private temp directory and
every fail-closed refusal.  Other platforms are unchanged.

The boundary stops a defect that misdirects the process's own writes.  It does not stop
code running inside the process: the user manager and the session bus stay reachable
(decision D4), and metadata changes outside the roots are not refused (decision D3).
"""

from __future__ import annotations

import ctypes
import functools
import logging
import os
import re
import secrets
import shutil
import stat
import struct
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .config import DEFAULT_CONFIG, Config, _runtime_state_root, _UnsafeConfigPath, load_config
from .deployment import SCHEDULED_COMMANDS

if sys.platform == "linux":
    import fcntl

logger = logging.getLogger(__name__)

SCHEDULED = frozenset(SCHEDULED_COMMANDS.values())

# R3: every path-valued `Config` field, classified once.  A scheduled command writes
# beneath a written target's directory; read targets are only read, and reads are not
# restricted.  A path-valued field missing from both lists fails the test suite.
WRITTEN_KEYS = ("database_file", "gmail_token_file", "gmail_send_token_file")
READ_KEYS = (
    "path",
    "gmail_credentials_file",
    "microsoft_credentials_file",
    "model_api_token_file",
    "model_ca_file",
)

# Landlock, `landlock(7)`.  The syscall numbers are shared by every architecture the
# engine ships for.
_SYS_CREATE_RULESET, _SYS_ADD_RULE, _SYS_RESTRICT_SELF = 444, 445, 446
_CREATE_RULESET_VERSION = 1
_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38
_WRITE_FILE = 1 << 1
_REMOVE_DIR, _REMOVE_FILE = 1 << 4, 1 << 5
_MAKE_CHAR, _MAKE_DIR, _MAKE_REG, _MAKE_SOCK = 1 << 6, 1 << 7, 1 << 8, 1 << 9
_MAKE_FIFO, _MAKE_BLOCK, _MAKE_SYM = 1 << 10, 1 << 11, 1 << 12
_REFER, _TRUNCATE = 1 << 13, 1 << 14
# Every content- and directory-entry-changing right through ABI 3 (R2).  Read and
# execute rights are not handled, so they stay unrestricted.
HANDLED_RIGHTS = (
    _WRITE_FILE | _REMOVE_DIR | _REMOVE_FILE | _MAKE_CHAR | _MAKE_DIR | _MAKE_REG
    | _MAKE_SOCK | _MAKE_FIFO | _MAKE_BLOCK | _MAKE_SYM | _REFER | _TRUNCATE
)
_NULL_DEVICE_RIGHTS = _WRITE_FILE | _TRUNCATE
MINIMUM_ABI = 3

_PRIVATE_TEMP_PARENT = "scheduled-tmp"
_RUN_NAME = re.compile(r"run-[0-9a-f]{16}\Z")


class ConfinementError(Exception):
    """A scheduled command cannot run inside its write boundary (R3, R4)."""


@dataclass(frozen=True)
class WriteRoots:
    """R2's directory roots, resolved.  The private temp directory lives in `state`."""

    config: Path
    state: Path
    legacy: Path | None

    def directories(self) -> tuple[Path, ...]:
        roots = (self.config, self.state, self.legacy)
        return tuple(dict.fromkeys(root for root in roots if root is not None))

    def contains(self, resolved: Path) -> bool:
        return any(resolved.is_relative_to(root) for root in self.directories())


def write_roots() -> WriteRoots:
    """Resolve R2's roots from fixed locations; the configuration cannot widen them."""
    try:
        state = _runtime_state_root()
    except _UnsafeConfigPath as exc:
        raise ConfinementError("The state directory is not a safe absolute path") from exc
    legacy = Path.home() / ".local" / "state" / "eom-email-watcher"
    resolved_state = _existing_directory(state, "state")
    resolved_legacy = None
    if os.path.lexists(legacy):
        resolved_legacy = _existing_directory(legacy, "legacy state")
        if resolved_legacy == resolved_state:
            resolved_legacy = None
    return WriteRoots(
        config=_existing_directory(DEFAULT_CONFIG.parent, "configuration"),
        state=resolved_state,
        legacy=resolved_legacy,
    )


def _existing_directory(path: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ConfinementError(f"The {label} directory is missing: {path}") from exc
    if not resolved.is_dir():
        raise ConfinementError(f"The {label} directory is not a directory: {path}")
    return resolved


@contextmanager
def scheduled_run(config_path: Path) -> Iterator[None]:
    """Confine this process before a scheduled command loads its configuration.

    Refusals raise `ConfinementError` before any configuration, database, mailbox, model
    or notification access.  The private temp directory is removed when the body ends.
    """
    if sys.platform != "linux":
        yield
        return
    roots = write_roots()
    abi = landlock_abi()
    if abi < MINIMUM_ABI:
        raise ConfinementError(
            f"Landlock ABI {abi} cannot refuse truncation; scheduled commands need ABI "
            f"{MINIMUM_ABI} (Linux 6.2) or newer"
        )
    _require_single_thread()
    _require_no_outside_writable_descriptor(roots)
    restrict_self(roots)
    with _private_temp(roots.state) as private:
        previous_tmpdir = os.environ.get("TMPDIR")
        previous_tempdir = tempfile.tempdir
        os.environ["TMPDIR"] = str(private)
        tempfile.tempdir = str(private)
        try:
            _admit_written_paths(load_config(config_path), roots)
            yield
        finally:
            tempfile.tempdir = previous_tempdir
            if previous_tmpdir is None:
                os.environ.pop("TMPDIR", None)
            else:
                os.environ["TMPDIR"] = previous_tmpdir


@functools.cache
def _libc() -> ctypes.CDLL:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    return libc


def _checked(result: int, step: str) -> int:
    if result < 0:
        code = ctypes.get_errno()
        raise ConfinementError(f"Write confinement was not applied: {step}: {os.strerror(code)}")
    return result


def landlock_abi() -> int:
    result = _libc().syscall(
        _SYS_CREATE_RULESET, None, ctypes.c_size_t(0), ctypes.c_uint(_CREATE_RULESET_VERSION)
    )
    if result < 0:
        code = ctypes.get_errno()
        raise ConfinementError(
            "Landlock is not available on this system, so scheduled commands cannot be "
            f"confined ({os.strerror(code)})"
        )
    return int(result)


def _require_single_thread() -> None:
    # Kernel tasks, not `threading`: native threads are invisible to Python, and a thread
    # that exists before restriction stays unconfined.
    try:
        tasks = len(os.listdir("/proc/self/task"))
    except OSError as exc:
        raise ConfinementError("The process's threads cannot be counted") from exc
    if tasks != 1:
        raise ConfinementError(
            f"The process has {tasks} threads; write confinement needs exactly one"
        )


def _require_no_outside_writable_descriptor(roots: WriteRoots) -> None:
    # A descriptor opened before restriction stays writable afterwards (measured), so none
    # may point at a file outside the roots.  Pipes and sockets are not paths.
    for name in os.listdir("/proc/self/fd"):
        descriptor = int(name)
        if descriptor <= 2:
            continue
        try:
            flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
            mode = os.fstat(descriptor).st_mode
            target = os.readlink(f"/proc/self/fd/{descriptor}")
        except OSError:
            continue  # the listing's own descriptor, closed by now
        if flags & os.O_ACCMODE == os.O_RDONLY:
            continue
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISBLK(mode)
                or stat.S_ISCHR(mode)):
            continue
        if target == os.devnull or roots.contains(Path(target)):
            continue
        raise ConfinementError(
            f"A writable descriptor outside the write directories is open: {target}"
        )


def restrict_self(roots: WriteRoots) -> None:
    attr = struct.pack("=Q", HANDLED_RIGHTS)
    ruleset = _checked(
        _libc().syscall(_SYS_CREATE_RULESET, attr, ctypes.c_size_t(len(attr)), ctypes.c_uint(0)),
        "create ruleset",
    )
    try:
        rules = [(root, HANDLED_RIGHTS) for root in roots.directories()]
        rules.append((Path(os.devnull), _NULL_DEVICE_RIGHTS))
        for path, rights in rules:
            try:
                descriptor = os.open(path, os.O_PATH | os.O_CLOEXEC)
            except OSError as exc:
                raise ConfinementError(
                    f"Write confinement was not applied: open {path}: {exc.strerror}"
                ) from exc
            try:
                rule = struct.pack("=Qi", rights, descriptor)
                _checked(
                    _libc().syscall(
                        _SYS_ADD_RULE, ctypes.c_int(ruleset), ctypes.c_int(_RULE_PATH_BENEATH),
                        rule, ctypes.c_uint(0),
                    ),
                    f"add rule for {path}",
                )
            finally:
                os.close(descriptor)
        _checked(_libc().prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0), "set no_new_privs")
        _checked(
            _libc().syscall(_SYS_RESTRICT_SELF, ctypes.c_int(ruleset), ctypes.c_uint(0)),
            "restrict self",
        )
    finally:
        os.close(ruleset)


@contextmanager
def _private_temp(state_root: Path) -> Iterator[Path]:
    """R5: one empty 0700 directory per run, kept alive by a lock the run holds."""
    parent = state_root / _PRIVATE_TEMP_PARENT
    try:
        parent.mkdir(mode=0o700, exist_ok=True)
        if not stat.S_ISDIR(os.lstat(parent).st_mode):
            raise NotADirectoryError(parent)
        with _locked(parent / ".lock"):
            name = f"run-{secrets.token_hex(8)}"
            run_lock = _open_lock(parent / f"{name}.lock", create=True)
            try:
                fcntl.flock(run_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                private = parent / name
                private.mkdir(mode=0o700)
                _remove_dead_runs(parent, keep=name)
            except BaseException:
                os.close(run_lock)
                raise
    except OSError as exc:
        raise ConfinementError(f"The private temp directory cannot be created: {exc}") from exc
    try:
        yield private
    finally:
        shutil.rmtree(private, ignore_errors=True)
        try:
            os.unlink(parent / f"{name}.lock")
        except OSError:
            logger.warning("Private temp lock was not removed; the next run removes it")
        os.close(run_lock)


def _open_lock(path: Path, *, create: bool) -> int:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    return os.open(path, flags, 0o600)


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    descriptor = os.open(path, os.O_RDONLY | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _remove_dead_runs(parent: Path, *, keep: str) -> None:
    """Remove directories of runs whose lock nobody holds.  Caller holds the parent lock.

    A run creates and locks its lock file before its directory, and the parent lock
    orders that against this sweep, so a directory without a held lock is dead.
    """
    for entry in os.listdir(parent):
        name = entry.removesuffix(".lock")
        if name == keep or _RUN_NAME.fullmatch(name) is None:
            continue
        lock_path = parent / f"{name}.lock"
        try:
            descriptor = _open_lock(lock_path, create=False)
        except FileNotFoundError:
            shutil.rmtree(parent / name, ignore_errors=True)
            continue
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue  # a live run holds it
            shutil.rmtree(parent / name, ignore_errors=True)
            os.unlink(lock_path)
        finally:
            os.close(descriptor)


def _admit_written_paths(config: Config, roots: WriteRoots) -> None:
    """R3: refuse a configured write target outside the roots before any work."""
    for key in WRITTEN_KEYS:
        value = getattr(config, key)
        if value is None:
            continue
        target = Path(value)
        # The directory takes atomic replacements and lock files; a link at the target
        # itself redirects the write.  Resolution follows symlinks in both.
        for candidate in (target.parent, target):
            try:
                inside = roots.contains(candidate.resolve())
            except (OSError, RuntimeError):
                inside = False
            if not inside:
                raise ConfinementError(
                    f"{key} is outside the folders scheduled commands may write: {target}"
                )
