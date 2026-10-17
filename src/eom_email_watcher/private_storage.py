"""Configured Windows storage boundaries; Connect owns ACL inspection/creation.

POSIX callers retain their existing ownership, mode and publication protocols.
Paths stay lexical so a reparse point is inspected rather than resolved away.
"""

import os
from pathlib import Path

from connect_automate import connect_windows


class PrivateStorageError(PermissionError):
    """A selected Windows storage boundary is unsafe; never repair it silently."""


def is_windows() -> bool:
    return os.name == "nt"


def absolute_lexical_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _refusal(path: Path, cause: OSError) -> PrivateStorageError:
    return PrivateStorageError(
        f"Private storage is unsafe at {path}. Choose or create a folder private "
        f"to your Windows user; existing permissions were not changed. ({cause})"
    )


def ensure_directory(path: Path, *, root: Path | None = None) -> None:
    """Create only below a verified existing anchor, never repair existing ACLs."""
    if not is_windows():
        return
    destination = absolute_lexical_path(path)
    selected = destination if root is None else absolute_lexical_path(root)
    anchor = selected
    try:
        if not destination.is_relative_to(selected):
            raise PermissionError("private directory escapes its root")
        while True:
            try:
                anchor.lstat()
                break
            except FileNotFoundError:
                if anchor == anchor.parent:
                    raise
                anchor = anchor.parent
        connect_windows.validate_private_directory(anchor, root=anchor)
        connect_windows.ensure_private_directory(destination, root=anchor)
    except OSError as exc:
        raise _refusal(anchor, exc) from exc


def validate_file(
    path: Path, *, allow_missing: bool = False, root: Path | None = None
) -> None:
    if not is_windows():
        return
    absolute = absolute_lexical_path(path)
    selected = absolute.parent if root is None else absolute_lexical_path(root)
    try:
        connect_windows.validate_private_regular_file(
            absolute, root=selected, allow_missing=allow_missing
        )
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise _refusal(selected, exc) from exc


def prepare_file(path: Path, *, root: Path | None = None) -> None:
    """Validate a destination before authorization, file creation or refresh."""
    ensure_directory(path.parent, root=root)
    validate_file(path, allow_missing=True, root=root)


def write_text(path: Path, content: str) -> None:
    """Preserve POSIX writes; delegate ordinary Windows publication to Connect."""
    if not is_windows():
        path.write_text(content, encoding="utf-8")
        path.chmod(0o600)
        return
    prepare_file(path)
    encoded = content.encode("utf-8")
    # No new format/size limit: the owner has already produced these bytes.
    connect_windows.atomic_replace_bytes(path, encoded, len(encoded), allow_empty=True)


def validate_database(path: Path) -> None:
    """Validate SQLite's existing inputs before its first open or mutation."""
    if not is_windows():
        return
    prepare_file(path)
    for suffix in ("-wal", "-shm", "-journal"):
        validate_file(path.with_name(path.name + suffix), allow_missing=True)
