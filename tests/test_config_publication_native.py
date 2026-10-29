"""Linux unnamed-file publication at the shared origin, without privilege grants."""

import ctypes
import errno
import os
import stat

import pytest

from eom_email_watcher import config

pytestmark = pytest.mark.platform_contract(
    platforms=("linux",), owner="src/eom_email_watcher/config.py:1536",
    reason="Native Linux O_TMPFILE and proc-fd linkat publication",
)


@pytest.fixture
def candidate(tmp_path):
    tmp_path.chmod(0o700)
    parent = os.open(tmp_path, os.O_DIRECTORY | os.O_RDONLY)
    fd = config._open_unnamed_candidate_at(parent)
    try:
        os.write(fd, b"public fully written candidate\n")
        os.fsync(fd)
        yield parent, fd
    finally:
        os.close(fd)
        os.close(parent)


def test_publication_uses_unprivileged_descriptor_link(candidate, tmp_path, monkeypatch):
    parent, fd = candidate
    native = config._linkat_function()
    calls = []

    def observe(*args):
        calls.append(args)
        native.argtypes, native.restype = observe.argtypes, observe.restype
        return native(*args)

    monkeypatch.setattr(config, "_linkat_function", lambda: observe)
    config._link_unnamed_candidate_at(fd, parent, "published")
    assert calls == [(-100, os.fsencode(f"/proc/self/fd/{fd}"), parent, b"published", 0x400)]
    result = tmp_path / "published"
    assert result.stat().st_ino == os.fstat(fd).st_ino
    assert result.read_bytes() == b"public fully written candidate\n"
    assert stat.S_IMODE(result.stat().st_mode) == 0o600


def test_native_existing_destination_is_never_overwritten(candidate, tmp_path):
    parent, fd = candidate
    target = tmp_path / "existing"
    target.write_bytes(b"existing operator configuration")
    before = target.stat()
    with pytest.raises(FileExistsError):
        config._link_unnamed_candidate_at(fd, parent, target.name)
    assert target.read_bytes() == b"existing operator configuration"
    assert target.stat().st_ino == before.st_ino
    assert os.fstat(fd).st_nlink == 0


@pytest.mark.parametrize("failure", ["missing-proc", "denied", "no-linkat"])
def test_link_failure_leaves_nothing_published(candidate, tmp_path, monkeypatch, failure):
    parent, fd = candidate
    native = config._linkat_function()

    def refuse(*args):
        if failure == "denied":
            ctypes.set_errno(errno.EACCES)
            return -1
        native.argtypes, native.restype = refuse.argtypes, refuse.restype
        # A native ENOENT from an unavailable proc-fd source; no fake success.
        return native(-100, os.fsencode(tmp_path / "absent-proc/self/fd" / str(fd)),
                      parent, b"published", 0x400)

    monkeypatch.setattr(
        config, "_linkat_function", lambda: None if failure == "no-linkat" else refuse
    )
    with pytest.raises(OSError) as error:
        config._link_unnamed_candidate_at(fd, parent, "published")
    assert error.value.errno == {
        "missing-proc": errno.ENOENT, "denied": errno.EACCES, "no-linkat": errno.ENOSYS,
    }[failure]
    assert list(tmp_path.iterdir()) == []
    assert os.fstat(fd).st_nlink == 0


def test_invalid_descriptor_cannot_publish(candidate, tmp_path):
    parent, _ = candidate
    with pytest.raises(OSError):
        config._link_unnamed_candidate_at(-1, parent, "published")
    assert list(tmp_path.iterdir()) == []
