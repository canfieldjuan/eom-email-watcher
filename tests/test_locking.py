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

BLOCKING_LOCK_PROBE = """
import sys
from pathlib import Path

from eom_email_watcher.locking import operation_lock

print("waiting", flush=True)
with operation_lock(Path(sys.argv[1]), "watcher busy", timeout_seconds=-1):
    print("acquired", flush=True)
"""


def test_native_operation_lock_is_available(tmp_path: Path) -> None:
    assert locking.operation_lock_uses_soft_fallback(tmp_path / "watcher.lock") is False
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


def test_operation_lock_can_wait_for_another_process_owner(tmp_path: Path) -> None:
    lock_path = tmp_path / "watcher.lock"

    with locking.operation_lock(lock_path, "watcher busy"):
        waiter = subprocess.Popen(
            [sys.executable, "-c", BLOCKING_LOCK_PROBE, str(lock_path)],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert waiter.stdout is not None
            assert waiter.stdout.readline().strip() == "waiting"
            with pytest.raises(subprocess.TimeoutExpired):
                waiter.wait(timeout=0.2)
        except Exception:
            waiter.kill()
            waiter.wait(timeout=10)
            raise

    assert waiter.stdout is not None
    assert waiter.stdout.readline().strip() == "acquired"
    assert waiter.wait(timeout=10) == 0


def test_connect_lock_paths_are_private_stable_and_namespaced(tmp_path: Path) -> None:
    database = tmp_path / "state" / "watcher.sqlite3"
    lane = locking.connect_lane_lock_path(
        database,
        protocol_version=2,
        provider_app_id="invoice-processor",
        provider_instance_id="11111111-1111-4111-8111-111111111111",
    )
    same_lane = locking.connect_lane_lock_path(
        database,
        protocol_version=2,
        provider_app_id="invoice-processor",
        provider_instance_id="11111111-1111-4111-8111-111111111111",
    )
    source = locking.connect_source_lock_path(database, "message-1")

    assert lane == same_lane
    assert lane != source
    assert lane.parent == database.parent / ".connect-locks"
    assert "invoice-processor" not in lane.name
    with (
        pytest.raises(RuntimeError, match="state directory must be initialized"),
        locking.connect_operation_lock(lane, "lane busy"),
    ):
        pytest.fail("An uninitialized state directory acquired a Connect lock")
    database.parent.mkdir(parents=True, mode=0o700)
    with locking.connect_operation_lock(lane, "lane busy"):
        if os.name == "posix":
            assert stat.S_IMODE(database.parent.stat().st_mode) == 0o700
            assert stat.S_IMODE(lane.parent.stat().st_mode) == 0o700
        assert _probe_lock(lane) == 23

    with pytest.raises(ValueError, match="cannot be empty"):
        locking.connect_source_lock_path(database, "")


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

    assert locking.operation_lock_uses_soft_fallback(lock_path) is True
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

    assert locking.operation_lock_uses_soft_fallback(lock_path) is True
    assert locking.operation_lock_supported(lock_path) is False
    assert probe_paths == [lock_path, lock_path]
    assert not lock_path.exists()
    with (
        pytest.raises(RuntimeError, match="not available"),
        locking.operation_lock(lock_path, "watcher busy"),
    ):
        raise AssertionError("a runtime soft fallback must fail closed")
    assert not lock_path.exists()
