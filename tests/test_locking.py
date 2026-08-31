from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from eom_email_watcher import locking

LOCK_PROBE = """
import sys
from pathlib import Path

from eom_email_watcher.locking import operation_lock

try:
    with operation_lock(Path(sys.argv[1]), "watcher busy"):
        pass
except RuntimeError as exc:
    raise SystemExit(23 if str(exc) == "watcher busy" else 24) from exc
"""


def test_native_operation_lock_is_available(tmp_path: Path) -> None:
    assert locking.operation_lock_supported(tmp_path / "watcher.lock") is True


def test_support_probe_does_not_require_or_create_first_run_directory(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "not-created-yet" / "watcher.lock"

    assert locking.operation_lock_supported(lock_path) is True
    assert list(tmp_path.iterdir()) == []


def _probe_lock(lock_path: Path) -> int:
    return subprocess.run(
        [sys.executable, "-c", LOCK_PROBE, str(lock_path)],
        check=False,
        timeout=10,
    ).returncode


def test_operation_lock_rejects_another_process_and_is_reusable(tmp_path: Path) -> None:
    lock_path = tmp_path / "watcher.lock"

    with locking.operation_lock(lock_path, "watcher busy"):
        assert _probe_lock(lock_path) == 23

    assert _probe_lock(lock_path) == 0


@pytest.mark.skipif(os.name != "posix", reason="POSIX lock files expose Unix permission bits")
def test_operation_lock_normalizes_private_posix_permissions(tmp_path: Path) -> None:
    lock_path = tmp_path / "watcher.lock"
    lock_path.write_text("stale", encoding="utf-8")
    lock_path.chmod(0o666)

    with locking.operation_lock(lock_path, "watcher busy"):
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600


def test_soft_lock_fallback_is_not_advertised_or_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(locking, "FileLock", locking.SoftFileLock)
    lock_path = tmp_path / "watcher.lock"

    assert locking.operation_lock_supported(lock_path) is False
    with (
        pytest.raises(RuntimeError, match="not available"),
        locking.operation_lock(lock_path, "watcher busy"),
    ):
        raise AssertionError("unsupported locking must fail closed")
    assert not lock_path.exists()


def test_runtime_soft_lock_fallback_is_not_advertised_or_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe_paths: list[Path] = []

    def soft_lock(path: Path, *args, **kwargs):
        probe_paths.append(Path(path))
        return locking.SoftFileLock(path, *args, **kwargs)

    monkeypatch.setattr(
        locking,
        "FileLock",
        soft_lock,
    )
    lock_path = tmp_path / "watcher.lock"

    assert locking.operation_lock_supported(lock_path) is False
    assert probe_paths[0].parent.parent == tmp_path
    assert not probe_paths[0].parent.exists()
    with (
        pytest.raises(RuntimeError, match="not available"),
        locking.operation_lock(lock_path, "watcher busy"),
    ):
        raise AssertionError("a runtime soft fallback must fail closed")
    assert not lock_path.exists()
