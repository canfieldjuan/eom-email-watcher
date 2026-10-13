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
    operation_lock_supported,
)

from . import deployment

if TYPE_CHECKING:
    from .db import Store

SOURCE_LOCK_STRIPE_COUNT = 64
SOURCE_LOCK_PROTOCOL = "source-stripe-v1"
# One bound for the entire set: SDK connect/pool 1s, write 60s, read 10s,
# plus time to record/refuse the outcome. A timeout never permits deletion.
SOURCE_LOCK_WAIT_SECONDS = 90.0
LEGACY_NAME = re.compile(r"source-[0-9a-f]{64}\.lock\Z")
_context = threading.local()


class SourceProtocolUnavailable(RuntimeError):
    """The source-lock cutover has not been qualified; retry after draining owners."""


def source_lock_stripe(message_id: str) -> int:
    if not isinstance(message_id, str) or not message_id:
        raise ValueError("Source identity cannot be empty")
    return (
        int.from_bytes(hashlib.sha256(("source\0" + message_id).encode()).digest(), "big")
        % SOURCE_LOCK_STRIPE_COUNT
    )


def source_lock_path(database_path: Path, message_id: str) -> Path:
    return stripe_path(database_path, source_lock_stripe(message_id))


def stripe_path(database_path: Path, stripe: int) -> Path:
    return database_path.parent / ".connect-locks" / f"source-stripe-v1-{stripe:02d}.lock"


@contextmanager
def source_locks(
    database_path: Path,
    message_ids: Iterable[str],
    *,
    timeout_seconds: float = 0,
    busy_message: str = "The Connect source attachment is being changed",
) -> Iterator[None]:
    if (
        isinstance(timeout_seconds, bool)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds < 0
    ):
        raise ValueError("Source-lock timeout must be nonnegative and finite")
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


@contextmanager
def _cutover_admission() -> Iterator[None]:
    if getattr(sys, "frozen", False):
        if sys.platform != "linux":
            raise SourceProtocolUnavailable(
                "Close all engines before the source-lock upgrade; installer stop proof is required"
            )
        with deployment.database_admission(exclusive=True):
            yield
    else:
        # Development runs must be stopped before changing versions. The source
        # runtime has no replaced-executable admission guard and mixed versions
        # are unsupported; native drainage is still required.
        yield


def qualify_protocol(store: Store, *, fresh: bool) -> None:
    """Caller holds the upgrade lock; no Store connection may enclose admission."""
    database_path = store.path
    with store.connection() as db:
        if protocol_ready(db):
            return
    admission = _cutover_admission() if not fresh else ExitStack()
    with admission:
        directory = database_path.parent / ".connect-locks"
        deadline = time.monotonic() + SOURCE_LOCK_WAIT_SECONDS
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
                try:
                    with connect_operation_lock(
                        path,
                        "Legacy source handoff remains active",
                        timeout_seconds=max(0.0, deadline - time.monotonic()),
                    ):
                        pass
                except OperationLockBusy as exc:
                    raise SourceProtocolUnavailable(
                        "Legacy source handoff remains active; source-lock upgrade deferred"
                    ) from exc
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
