"""Canonical bounded source exclusion and one-time legacy ownership drain."""

from __future__ import annotations

import hashlib
import math
import os
import re
import sqlite3
import sys
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from connect_automate.locking import (
    OperationLockBusy,
    connect_operation_lock,
    connect_source_lock_path,
    operation_lock_supported,
)
from connect_automate.locking import connect_operation_lock as protocol_guard

from . import body_state, deployment, removal

if TYPE_CHECKING:
    from .db import Store

SOURCE_LOCK_STRIPE_COUNT = 64
SOURCE_LOCK_PROTOCOL = "source-stripe-v1"
# One bound for the entire set: SDK connect/pool 1s, write 60s, read 10s,
# plus time to record/refuse the outcome. A timeout never permits deletion.
SOURCE_LOCK_WAIT_SECONDS = 90.0
LEGACY_NAME = re.compile(r"source-[0-9a-f]{64}\.lock\Z")
_context = threading.local()
_selection = threading.local()


class SourceProtocolUnavailable(RuntimeError):
    """The source-lock cutover has not been qualified; retry after draining owners."""


def source_lock_stripe(message_id: str) -> int:
    if not isinstance(message_id, str) or not message_id:
        raise ValueError("Source identity cannot be empty")
    return (
        int.from_bytes(hashlib.sha256(("source\0" + message_id).encode()).digest(), "big")
        % SOURCE_LOCK_STRIPE_COUNT
    )


def operation_lock_path(database_path: Path) -> Path:
    """The existing watcher operation exclusion, shared by all entry points."""
    return database_path.with_name(f"{database_path.name}.check.lock")


def source_context_active() -> bool:
    """Read the existing source owners; never mint or reuse their authority."""
    return any(
        held is not None and held[0] == os.getpid()
        for held in (getattr(_selection, "held", None), getattr(_context, "held", None))
    )


def source_lock_path(database_path: Path, message_id: str) -> Path:
    return stripe_path(database_path, source_lock_stripe(message_id))


def stripe_path(database_path: Path, stripe: int) -> Path:
    return database_path.parent / ".connect-locks" / f"source-stripe-v1-{stripe:02d}.lock"


def validate_timeout(timeout_seconds: float) -> None:
    if (
        isinstance(timeout_seconds, bool)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds < 0
    ):
        raise ValueError("Source-lock timeout must be nonnegative and finite")


@contextmanager
def source_locks(
    database_path: Path,
    message_ids: Iterable[str],
    *,
    timeout_seconds: float = 0,
    busy_message: str = "The Connect source attachment is being changed",
) -> Iterator[None]:
    validate_timeout(timeout_seconds)
    stripes = frozenset(source_lock_stripe(m) for m in message_ids)
    key = str(database_path.parent.resolve())
    held = getattr(_context, "held", None)
    if held is not None and held[0] == os.getpid():
        if key != held[1] or not stripes <= held[2]:
            raise RuntimeError("Source lock sets must be frozen by the outer owner")
        yield
        return
    if not stripes:
        yield
        return
    first = stripe_path(database_path, min(stripes))
    if not operation_lock_supported(first):
        raise SourceProtocolUnavailable(
            "Connect source exclusion requires native operation locking"
        )
    deadline = time.monotonic() + timeout_seconds
    with ExitStack() as stack:
        for stripe in sorted(stripes):
            stack.enter_context(
                connect_operation_lock(
                    stripe_path(database_path, stripe),
                    busy_message,
                    timeout_seconds=max(0.0, deadline - time.monotonic()),
                )
            )
        previous = held
        _context.held = (os.getpid(), key, stripes)
        try:
            yield
        finally:
            _context.held = previous


def protocol_ready(db: sqlite3.Connection) -> bool:
    row = db.execute(
        "SELECT value FROM maintenance_state WHERE key='source_lock_protocol'"
    ).fetchone()
    if row is None:
        return False
    if row[0] != SOURCE_LOCK_PROTOCOL:
        raise SourceProtocolUnavailable("The database names an unsupported source-lock protocol")
    return True


def selection_path(database_path: Path) -> Path:
    return database_path.parent / ".connect-locks" / "source-protocol-selection-v1.lock"


def cleanup_held(*, ready: bool, row_count: int, legacy_limit: int) -> bool:
    """One admission rule for indivisible cleanup before protocol qualification."""
    return not ready and row_count > legacy_limit


@contextmanager
def selected_source_locks(
    store: Store,
    message_ids: Iterable[str],
    *,
    timeout_seconds: float,
    busy_message: str,
    legacy_limit: int | None = None,
) -> Iterator[bool]:
    """Freeze protocol selection; legacy cleanup retains the guard through mutation."""
    validate_timeout(timeout_seconds)
    deadline = time.monotonic() + timeout_seconds
    ids = frozenset(message_ids)
    key = str(store.path.parent.resolve())

    def require_selected_protocol(ready):
        if not ready and legacy_limit is None:
            raise SourceProtocolUnavailable(
                "Connect handoff is deferred while the source-lock upgrade completes"
            )

    held = getattr(_selection, "held", None)
    if held is not None and held[0] == os.getpid():
        require_selected_protocol(held[3])
        subset = (
            {source_lock_stripe(m) for m in ids} <= {source_lock_stripe(m) for m in held[2]}
            if held[3]
            else ids <= held[2]
        )
        if key != held[1] or not subset:
            raise RuntimeError("Source lock sets must be frozen by the outer owner")
        yield True
        return
    if not operation_lock_supported(selection_path(store.path)):
        raise SourceProtocolUnavailable(
            "Connect source exclusion requires native operation locking"
        )
    with ExitStack() as guard, ExitStack() as locks:
        guard.enter_context(
            protocol_guard(
                selection_path(store.path),
                busy_message,
                timeout_seconds=max(0.0, deadline - time.monotonic()),
            )
        )
        with store.connection() as db:
            ready = protocol_ready(db)
        require_selected_protocol(ready)
        if not ready:
            if cleanup_held(ready=ready, row_count=len(ids), legacy_limit=legacy_limit):
                # The owner tells cleanup to preserve this indivisible unit.
                yield False
                return
            for message_id in sorted(ids):
                locks.enter_context(
                    connect_operation_lock(
                        connect_source_lock_path(store.path, message_id),
                        busy_message,
                        timeout_seconds=max(0.0, deadline - time.monotonic()),
                    )
                )
        else:
            locks.enter_context(
                source_locks(
                    store.path,
                    ids,
                    timeout_seconds=max(0.0, deadline - time.monotonic()),
                    busy_message=busy_message,
                )
            )
            # A qualified protocol cannot revert; only legacy holders need keep
            # the selection guard throughout their transaction to exclude drain.
            guard.close()
        previous = held
        _selection.held = (os.getpid(), key, ids, ready)
        try:
            yield True
        finally:
            _selection.held = previous


def cleanup_groups(units, *, legacy_limit: int | None):
    """Batch whole logical/thread units; oversized units remain indivisible."""
    keys, rows = [], set()
    for key, unit_rows in units.items():
        current = set(unit_rows)
        if keys and legacy_limit is not None and len(rows | current) > legacy_limit:
            yield keys, sorted(rows)
            keys, rows = [], set()
        keys.append(key)
        rows.update(current)
    if keys:
        yield keys, sorted(rows)


def protocol_notice(db: sqlite3.Connection) -> str | None:
    try:
        if protocol_ready(db):
            return None
    except SourceProtocolUnavailable as exc:
        return str(exc)
    suffix = (
        " Complete the Windows installer update."
        if sys.platform == "win32"
        else " Retry after the source-lock upgrade finishes."
    )
    return (
        "Source-lock upgrade pending; Connect handoffs and oversized thread deletion are deferred."
        + suffix
    )


@contextmanager
def _cutover_admission() -> Iterator[None]:
    if getattr(sys, "frozen", False):
        if sys.platform == "win32":
            from .windows_cutover import verify_installation

            verify_installation()
            yield
        elif sys.platform == "linux":
            with deployment.database_admission(exclusive=True):
                yield
        else:
            raise SourceProtocolUnavailable("This packaged platform has no qualified installer")
    else:
        # Development runs must be stopped before changing versions. The source
        # runtime has no replaced-executable admission guard and mixed versions
        # are unsupported; native drainage is still required.
        yield


def qualify_protocol(store: Store, *, fresh: bool) -> None:
    try:
        _qualify_protocol(store, fresh=fresh)
    except OperationLockBusy as exc:
        raise SourceProtocolUnavailable(
            "Source-lock upgrade deferred; a native source owner remains active"
        ) from exc
    # Never invert operation→source ordering or expand a frozen source set.
    # The ordinary operation entry point resumes after this owner releases it.
    if source_context_active():
        return
    with store.connection() as db:
        has_requests = removal.has_schema(db)
    if has_requests:
        try:
            with connect_operation_lock(
                operation_lock_path(store.path), "Another watcher operation is already running"
            ):
                removal.resume(store)
        except OperationLockBusy:
            # An existing operation keeps its exclusion. Its ordinary-check
            # entry point resumes through the same completion owner.
            pass


def _qualify_protocol(store: Store, *, fresh: bool) -> None:
    """Caller holds the upgrade lock; no Store connection may enclose admission."""
    database_path = store.path
    with store.connection() as db:
        if protocol_ready(db):
            db.execute("BEGIN IMMEDIATE")
            body_state.recompute(db)
            return
    if not operation_lock_supported(selection_path(database_path)):
        raise SourceProtocolUnavailable(
            "Connect source exclusion requires native operation locking"
        )
    admission = _cutover_admission() if not fresh else ExitStack()
    deadline = time.monotonic() + SOURCE_LOCK_WAIT_SECONDS
    with (
        protocol_guard(
            selection_path(database_path),
            "Source-lock protocol is being selected",
            timeout_seconds=max(0.0, deadline - time.monotonic()),
        ),
        admission,
    ):
        directory = database_path.parent / ".connect-locks"
        if directory.exists():
            # Old per-message files are never replaced/unlinked. The installed
            # refusal guard prevents late creators from reaching a database/POST.
            for path in directory.iterdir():
                if not LEGACY_NAME.fullmatch(path.name):
                    continue
                if not operation_lock_supported(path):
                    raise SourceProtocolUnavailable(
                        "Connect source exclusion requires native operation locking"
                    )
                with connect_operation_lock(
                    path,
                    "Legacy source handoff remains active",
                    timeout_seconds=max(0.0, deadline - time.monotonic()),
                ):
                    pass
        # Store.connection reuses this thread/process's verified exclusive
        # admission and remains the only database connection/mutation owner.
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO maintenance_state(key,value,updated_at) "
                "VALUES('source_lock_protocol',?,?) ON CONFLICT(key) DO UPDATE "
                "SET value=excluded.value,updated_at=excluded.updated_at",
                (SOURCE_LOCK_PROTOCOL, datetime.now(UTC).isoformat()),
            )
            body_state.recompute(db)
