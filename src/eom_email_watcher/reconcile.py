"""Bounded, resumable discovery and thread sync (accepted M2.3)."""

import imaplib
import logging
import time
from collections import deque
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from . import body_state
from .mailbox import (
    MailboxError,
    MailboxMessageInvalid,
    MailboxMessageOutsideScope,
    MailboxMessageUnavailable,
)
from .reconcile_state import WHERE, ReconcileState

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReconcileItem:
    message_id: str
    thread_id: str | None = None
    received_at: str | None = None


@dataclass(frozen=True)
class ReconcilePage:
    items: tuple[ReconcileItem, ...]
    token: str | int | dict | None


class ReconcileTokenInvalid(MailboxError):
    """Restart this unit's listing using its original frozen query."""


class ReconcilePaused(Exception):
    pass


class ReconcileBudget:
    def __init__(self, allowed, *, seconds=30, calls=200):
        self.allowed = allowed
        self.deadline = time.monotonic() + seconds
        self.maximum = calls
        self.calls = 0

    def check(self):
        active = self.allowed()
        remaining = self.deadline - time.monotonic()
        if self.calls >= self.maximum or remaining <= 0 or not active:
            raise ReconcilePaused()
        return remaining

    def before_call(self):
        remaining = self.check()
        self.calls += 1
        return remaining


@contextmanager
def budget_session(gateway, budget):
    old = getattr(gateway, "_reconcile_budget", None)
    gateway._reconcile_budget = budget
    client = getattr(gateway, "_active_client", None)
    command = getattr(client, "_command", None)
    complete = getattr(client, "_command_complete", None)
    timeout = None
    http_client = getattr(gateway, "_client", None)
    http_timeout = getattr(http_client, "timeout", None)
    if callable(command):
        sock = getattr(client, "sock", None)
        timeout = sock.gettimeout() if sock is not None else None

        def guarded_command(*args, **kwargs):
            remaining = budget.before_call()
            try:
                if sock is not None:
                    sock.settimeout(remaining)
                return command(*args, **kwargs)
            except (OSError, imaplib.IMAP4.error) as exc:
                raise MailboxError("Mail server reconcile transport failed") from exc

        client._command = guarded_command
    if callable(complete):

        def guarded_complete(*args, **kwargs):
            try:
                return complete(*args, **kwargs)
            except (OSError, imaplib.IMAP4.error) as exc:
                raise MailboxError("Mail server reconcile transport failed") from exc

        client._command_complete = guarded_complete
    try:
        yield
    finally:
        gateway._reconcile_budget = old
        if http_timeout is not None:
            http_client.timeout = http_timeout
        if callable(command):
            client._command = command
            if sock is not None:
                with suppress(OSError):
                    sock.settimeout(timeout)
        if callable(complete):
            client._command_complete = complete


def _unit(name, folder="all", **kwargs):
    result = dict(
        name=name, folder=folder, token=None, listed=False, done=False, attempts=0, retry_at=None
    )
    result.update(kwargs)
    return result


def sent_identity(gateway, scope):
    if scope != "available":
        return scope
    reader = getattr(gateway, "reconcile_sent_identity", None)
    return reader() if callable(reader) else scope


def observe(watcher, identity, active, sent_scope, now):
    state = ReconcileState(
        watcher.store, (watcher.mailbox.provider, watcher.mailbox.account_id, identity)
    )
    state.observe(
        active=active,
        retention_days=watcher.config.retention_days,
        sent_folder=sent_identity(watcher.gateway, sent_scope),
        now=now,
    )
    return state


def run(watcher, identity, sent_scope, *, label_selectors, now=None, vendor_snapshot=None):
    now = now or datetime.now(UTC)
    state = observe(watcher, identity, True, sent_scope, now)
    if sent_scope is None or not state.stale():
        return 0
    provider = watcher.mailbox.provider
    folders = ["inbox", "sent"] if sent_scope == "available" else ["inbox"]
    snapshot = vendor_snapshot if vendor_snapshot is not None else watcher.store.vendor_index()
    addresses = sorted(a for a in snapshot.addresses if a not in snapshot.identities)
    if provider == "gmail":
        units = [
            _unit(str(i), addresses=addresses[i : i + 25]) for i in range(0, len(addresses), 25)
        ]
    elif provider == "imap":
        units = (
            [_unit(folder, folder, addresses=addresses) for folder in folders] if addresses else []
        )
    else:
        units = [_unit(folder, folder, addresses=addresses) for folder in folders]
    discovery = state.needs_discovery()
    initial = dict(
        stage="discovery" if discovery else "sync_init",
        units=units if discovery else [],
        folders=folders,
        replay_discovery=discovery,
    )
    if provider == "imap":
        initial.update(
            stage="snapshot",
            units=[_unit(folder, folder) for folder in folders],
            next_stage="discovery" if discovery else "sync_init",
            discovery=units if discovery else [],
            snapshots={},
        )
    progress = state.load_or_start(now, watcher.config.retention_days, initial)
    if progress["payload"]["stage"] == "catchup_wait":
        if now <= datetime.fromisoformat(progress["started_at"]):
            return 0
        initial.update(
            replay_discovery=False,
            stage="snapshot" if provider == "imap" else "sync_init",
            units=[_unit(folder, folder) for folder in folders] if provider == "imap" else [],
        )
        if provider == "imap":
            initial.update(next_stage="sync_init", discovery=[])
        progress = state.next_interval(progress, now, initial)
    start = datetime.fromisoformat(progress["started_at"])
    cutoff = start - timedelta(days=progress["retention_days"])

    def allowed():
        active = watcher._gated_class_allowed()
        if not active:
            observe(watcher, identity, False, "not_polled", datetime.now(UTC))
        return active

    budget = ReconcileBudget(allowed)
    added = 0

    def resume_catchup():
        if any(not t["watermark"] for t in state.threads()):
            progress["payload"].update(stage="sync_init", units=[])
            state.save(progress)
            return True
        if state.catchup_pending(progress):
            progress["payload"]["stage"] = "catchup_wait"
            state.save(progress)
            return True
        return False

    def fetch(item, *, gap=False, since=cutoff):
        nonlocal added
        source = watcher.store.reconcile_source(*state.scope, item["source_id"])
        if source is not None:
            if source["received_epoch"] is None or source["received_epoch"] < since.timestamp():
                return
            if source["confirmed"] and not gap:
                return
        elif watcher.store.has_seen_message(
            item["source_id"],
            provider=provider,
            account_id=watcher.mailbox.account_id,
            mailbox_identity_key=identity,
        ):
            return  # Suppression belongs to the existing source owner.
        preflight = getattr(watcher.gateway, "reconcile_preflight", None)
        if callable(preflight) and not preflight(item["source_id"], start=start, cutoff=since):
            return
        if not getattr(watcher.gateway, "reconcile_transport_budget", False):
            budget.before_call()
        # Graph's durable listing origin survives a process restart.
        if provider == "microsoft365":
            watcher.gateway._delta_folders[item["source_id"]] = (
                "sentitems" if item["folder"] == "sent" else "inbox"
            )
        metadata = watcher.gateway.metadata(item["source_id"])
        if metadata.message_id != item["source_id"]:
            raise MailboxMessageInvalid(
                "invalid_message_id", "Reconcile metadata identity mismatch"
            )
        try:
            received = datetime.fromisoformat(metadata.received_at)
            if received.tzinfo is None:
                raise ValueError("missing timezone")
            received = received.astimezone(UTC)
        except (TypeError, ValueError) as exc:
            raise MailboxMessageInvalid(
                "invalid_received_at", "Reconcile receive time is invalid"
            ) from exc
        if not since <= received < start:
            return
        added += watcher._capture_metadata(
            metadata,
            mailbox_identity_key=identity,
            label_selectors=label_selectors,
            gated_allowed=True,
            vendor_snapshot=snapshot,
            checked_at=start,
            retention_cutoff=cutoff,
            folders=frozenset(progress["payload"]["folders"]),
            refresh_known=True,
        )

    body_candidates = None
    try:
        with budget_session(watcher.gateway, budget):
            while True:
                budget.check()
                if state.generation() != progress["generation"]:
                    break
                payload = progress["payload"]
                stage = payload["stage"]
                units = payload["units"]
                if stage == "derived":
                    retries = payload.setdefault("body_retries", {})
                    if body_candidates is None:
                        waiting = [
                            m for m, retry in retries.items()
                            if datetime.fromisoformat(retry["retry_at"]) > datetime.now(UTC)
                        ]
                        with watcher.store.connection() as db:
                            body_candidates = deque(body_state.pending(
                                db, state.scope, cutoff=cutoff, now=start,
                                limit=budget.maximum, exclude=waiting,
                            ))
                    if not body_candidates:
                        # Exhaustion is not completion: a bounded slice may leave
                        # later work, a backoff, or a newly invalidated body.
                        with watcher.store.connection() as db:
                            remains = body_state.pending(
                                db, state.scope, cutoff=cutoff, now=start, limit=1
                            )
                        if remains:
                            break
                        if resume_catchup():
                            body_candidates = None
                            if payload["stage"] == "catchup_wait":
                                break
                            continue
                        state.complete(progress)
                        break
                    message_id = body_candidates.popleft()
                    try:
                        with watcher.store.connection() as db:
                            copies = body_state.sources(
                                db, message_id, state.scope, frozenset(folders)
                            )
                        work = payload.setdefault("body_work", {}).setdefault(
                            message_id, dict(skipped=[], outside=False)
                        )
                        outside = work["outside"] or not copies
                        result = None
                        for copy in copies:
                            source_id = copy[0]
                            if copy in work["skipped"]:
                                continue
                            if not getattr(watcher.gateway, "reconcile_transport_budget", False):
                                budget.before_call()
                            try:
                                result = watcher.gateway.stored_body(
                                    source_id, scope=frozenset(folders)
                                )
                                break
                            except MailboxMessageOutsideScope:
                                outside = True
                            except (MailboxMessageUnavailable, MailboxMessageInvalid):
                                pass
                            work["outside"] = outside
                            work["skipped"].append(copy)
                            state.save(progress)
                        body_state.record(
                            watcher.store,
                            message_id,
                            result,
                            fetched_at=datetime.now(UTC),
                            reason=None
                            if result is not None
                            else ("outside_folders" if outside else "source_gone"),
                        )
                        retries.pop(message_id, None)
                        payload["body_work"].pop(message_id, None)
                    except MailboxError:
                        attempts = retries.get(message_id, {}).get("attempts", 0) + 1
                        retries[message_id] = dict(
                            attempts=attempts,
                            retry_at=(
                                datetime.now(UTC)
                                + timedelta(minutes=min(15, 2 ** min(attempts, 4)))
                            ).isoformat(),
                        )
                        logger.warning("Reconcile body deferred after provider failure")
                    state.save(progress)
                    continue
                if all(unit["done"] for unit in units):
                    if stage == "snapshot":
                        payload.update(
                            stage=payload.get("next_stage", "discovery"),
                            units=payload.pop("discovery"),
                        )
                        state.save(progress)
                        continue
                    if stage in {"discovery", "sync_init"}:
                        threads = state.threads()
                        if not payload["replay_discovery"] or stage == "sync_init":
                            targets = state.catchup_targets(progress)
                            threads = [
                                t
                                for t in threads
                                if not t["watermark"]
                                or (t["key"] in targets and t["watermark"] < targets[t["key"]])
                            ]
                        payload.update(stage="sync", threads=threads, units=[])
                        # Gmail/Graph candidates require admitted-folder listings;
                        # IMAP source identities already bind folder and epoch.
                        for thread in threads:
                            thread["sources"] = state.sources(thread["key"])
                        if provider == "gmail":
                            if threads:
                                since = min(
                                    max(cutoff, datetime.fromisoformat(t["watermark"]))
                                    if t["watermark"]
                                    else cutoff
                                    for t in threads
                                )
                                payload["units"].append(
                                    _unit("native", threads=threads, since=since.isoformat())
                                )
                        elif provider == "microsoft365" and not payload["replay_discovery"]:
                            if threads:
                                since = min(
                                    max(cutoff, datetime.fromisoformat(t["watermark"]))
                                    if t["watermark"]
                                    else cutoff
                                    for t in threads
                                )
                                payload["units"] = [
                                    _unit(folder, folder, threads=threads, since=since.isoformat())
                                    for folder in folders
                                ]
                        else:
                            for thread in threads:
                                since = (
                                    max(cutoff, datetime.fromisoformat(thread["watermark"]))
                                    if thread["watermark"]
                                    else cutoff
                                )
                                payload["units"].append(
                                    _unit(
                                        thread["key"],
                                        threads=[thread],
                                        folder=folders[0],
                                        since=since.isoformat(),
                                        folder_index=0,
                                        ids=thread["ids"],
                                        sources=[
                                            source
                                            for source in thread["sources"]
                                            if (
                                                stored := watcher.store.reconcile_source(
                                                    *state.scope, source
                                                )
                                            )
                                            is not None
                                            and stored["received_epoch"] is not None
                                            and since.timestamp()
                                            <= stored["received_epoch"]
                                            < start.timestamp()
                                        ]
                                        if provider == "imap" and not thread["ids"]
                                        else [],
                                    )
                                )
                        if provider == "microsoft365" and payload["replay_discovery"]:
                            for unit in payload["units"]:
                                if unit.get("stored"):
                                    continue
                                native = unit["threads"][0]["native"]
                                with watcher.store.connection() as db:
                                    rows = db.execute(
                                        f"SELECT * FROM reconcile_items WHERE {WHERE} "
                                        "AND generation=? AND phase='discovery'",
                                        (*state.scope, progress["generation"]),
                                    ).fetchall()
                                for row in rows:
                                    if (
                                        row["native_thread_id"] in native
                                        or row["source_id"] in unit["threads"][0]["sources"]
                                    ) and row["received_at"] >= unit["since"]:
                                        state.put_items(
                                            progress,
                                            "sync",
                                            unit["name"],
                                            (
                                                ReconcileItem(
                                                    row["source_id"],
                                                    row["native_thread_id"],
                                                    row["received_at"],
                                                ),
                                            ),
                                            row["folder"],
                                        )
                                unit["listed"] = True
                        if provider == "imap":
                            # Gaps precede provider component expansion; aged rows wait.
                            from .db import _effective_message_identity_sql

                            with watcher.store.connection() as db:
                                db.execute("BEGIN IMMEDIATE")
                                db.execute(
                                    f"""DELETE FROM imap_reply_header_gaps WHERE message_id IN (
                                    SELECT m.message_id FROM messages m
                                    WHERE m.provider=? AND m.account_id=?
                                    AND {_effective_message_identity_sql("m")} IS NULL)""",
                                    state.scope[:2],
                                )
                                rows = db.execute(
                                    f"""SELECT m.provider_message_id,m.received_at FROM
                                    imap_reply_header_gaps g
                                    JOIN logical_messages m USING(message_id)
                                    WHERE m.provider=? AND m.account_id=?
                                    AND {_effective_message_identity_sql("m")}=?""",
                                    state.scope,
                                ).fetchall()
                            gap = _unit("gaps", gap=True, listed=True)
                            payload["units"].insert(0, gap)
                            state.put_items(
                                progress,
                                "sync",
                                "gaps",
                                tuple(
                                    ReconcileItem(r[0], received_at=r[1])
                                    for r in rows
                                    if datetime.fromisoformat(r[1]).astimezone(UTC) >= cutoff
                                ),
                                "inbox",
                            )
                        state.save(progress)
                        continue
                    if resume_catchup():
                        if payload["stage"] == "catchup_wait":
                            break
                        continue
                    payload["stage"] = "derived"
                    state.save(progress)
                    continue
                unit = next(
                    (
                        u
                        for u in units
                        if not u["done"]
                        and (
                            u["retry_at"] is None
                            or datetime.fromisoformat(u["retry_at"]) <= datetime.now(UTC)
                        )
                    ),
                    None,
                )
                if unit is None:
                    break
                phase = "discovery" if stage == "discovery" else "sync"
                item = state.pending(progress, phase, unit["name"])
                try:
                    if stage == "snapshot":
                        if not getattr(watcher.gateway, "reconcile_transport_budget", False):
                            budget.before_call()
                        payload["snapshots"][unit["folder"]] = watcher.gateway.reconcile_snapshot(
                            unit["folder"]
                        )
                        unit["done"] = True
                        state.save(progress)
                        continue
                    if item is not None:
                        if (
                            phase == "discovery"
                            or provider == "imap"
                            or unit.get("gap")
                            or any(
                                item["native_thread_id"] in t["native"]
                                or item["source_id"] in t["sources"]
                                for t in unit.get("threads", ())
                            )
                        ):
                            try:
                                fetch(
                                    item,
                                    gap=unit.get("gap", False),
                                    since=max(cutoff, datetime.fromisoformat(unit["since"]))
                                    if unit.get("since")
                                    else cutoff,
                                )
                            except (MailboxMessageUnavailable, MailboxMessageInvalid):
                                if unit.get("gap"):
                                    source = watcher.store.reconcile_source(
                                        *state.scope, item["source_id"]
                                    )
                                    if source is not None:
                                        with watcher.store.connection() as db:
                                            db.execute(
                                                "DELETE FROM imap_reply_header_gaps "
                                                "WHERE message_id=?",
                                                (source["message_id"],),
                                            )
                        state.finish_item(progress, item)
                        continue
                    if unit["listed"] and unit["token"] is None:
                        if provider == "imap" and phase == "sync" and not unit.get("gap"):
                            unit["folder_index"] += 1 if phase == "sync" else 0
                            if phase == "sync" and unit["folder_index"] < len(payload["folders"]):
                                unit.update(
                                    listed=False, folder=payload["folders"][unit["folder_index"]]
                                )
                                state.save(progress)
                                continue
                            if phase == "sync":
                                current = next(
                                    (
                                        t
                                        for t in state.threads()
                                        if t["key"] == unit["threads"][0]["key"]
                                    ),
                                    None,
                                )
                                if (
                                    current is not None
                                    and current["ids"] != unit["ids"]
                                    and current["aliases"] == unit["threads"][0]["aliases"]
                                ):
                                    unit.update(
                                        ids=current["ids"],
                                        folder_index=0,
                                        folder=payload["folders"][0],
                                        listed=False,
                                    )
                                    state.save(progress)
                                    continue
                        unit["done"] = True
                        if phase == "sync":
                            for thread in unit.get("threads", ()):
                                if all(
                                    other["done"]
                                    for other in units
                                    if any(
                                        t["key"] == thread["key"] for t in other.get("threads", ())
                                    )
                                ):
                                    state.watermark(progress, thread)
                        state.save(progress)
                        continue
                    if not getattr(watcher.gateway, "reconcile_transport_budget", False):
                        budget.before_call()
                    reader = getattr(watcher.gateway, "reconcile_page", None)
                    if not callable(reader):
                        raise MailboxError("Provider has no reconcile listing adapter")
                    if provider == "imap":
                        unit["snapshot"] = payload["snapshots"][unit["folder"]]
                    page = reader(unit, start=start, cutoff=cutoff)
                    items = tuple(
                        i
                        for i in page.items
                        if i.received_at is None
                        or cutoff <= datetime.fromisoformat(i.received_at).astimezone(UTC) < start
                    )
                    state.put_items(progress, phase, unit["name"], items, unit["folder"])
                    unit.update(token=page.token, listed=True, attempts=0, retry_at=None)
                    state.save(progress)
                except ReconcileTokenInvalid:
                    if provider == "imap":
                        state.gap(now=datetime.now(UTC))
                        break
                    unit.update(token=None, listed=False)
                    state.save(progress)
                except MailboxError:
                    unit["attempts"] += 1
                    unit["retry_at"] = (
                        datetime.now(UTC)
                        + timedelta(minutes=min(15, 2 ** min(unit["attempts"], 4)))
                    ).isoformat()
                    state.save(progress)
                    logger.warning("Reconcile unit deferred after provider failure")
    except ReconcilePaused:
        state.save(progress)
    return added
