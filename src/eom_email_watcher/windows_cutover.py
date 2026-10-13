"""Validate the receipt published by the Windows installer lifecycle owner."""

from __future__ import annotations

import hashlib
import json
import stat
import sys
from pathlib import Path

RECEIPT_NAME = "windows-source-cutover-v1.json"


def _read_receipt(path: Path) -> bytes:
    from connect_automate.connect_windows import read_bounded_regular_file

    return read_bounded_regular_file(path, 4096, require_private_acl=False)


def verify_installation() -> None:
    from .source_locks import SOURCE_LOCK_PROTOCOL, SourceProtocolUnavailable

    binary = Path(sys.executable)
    try:
        record = json.loads(_read_receipt(binary.parent / RECEIPT_NAME).decode("utf-8-sig"))
        if (
            not isinstance(record, dict)
            or type(record.get("version")) is not int
            or record["version"] != 1
        ):
            raise ValueError("Invalid installer receipt")
        if record.get("protocol") != SOURCE_LOCK_PROTOCOL:
            raise ValueError("Unsupported installer protocol")
        before = binary.stat(follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Installed engine must be a regular file")
        with binary.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        after = binary.stat(follow_symlinks=False)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or digest != record.get("engine_sha256"):
            raise ValueError("Installer receipt does not name this engine")
    except (OSError, ValueError, RuntimeError) as exc:
        raise SourceProtocolUnavailable(
            "Windows source-lock upgrade is deferred; complete the Email Watcher installer update"
        ) from exc
