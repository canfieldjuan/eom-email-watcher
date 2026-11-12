"""M3 local projections; existing logical/derived/removal owners decide visibility."""

import base64
import binascii
import hashlib
import json
import math
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Literal, TypedDict

from . import body_state
from .db import (
    Store,
    _decode_json_bytes,
    _effective_message_identity_sql,
    _follow_owner,
    _logical_unit,
    _thread_member,
)

MAX_CURSOR_BYTES = 4096
HISTORY_NOTICE = "History partial — only locally retained messages are shown"


class ThreadReadError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ThreadRef:
    provider: str
    account_id: str
    mailbox_identity_key: str | None
    thread_key: str


@dataclass(frozen=True)
class InboxMessageRef:
    provider: str
    account_id: str
    mailbox_identity_key: str | None
    message_id: str


@dataclass(frozen=True)
class InboxThreadSelector:
    """Explicit non-vendor alternative; it can never omit its origin."""

    message_ref: InboxMessageRef


class VendorRef(TypedDict):
    vendor_id: str
    display_name: str


class ThreadItem(TypedDict):
    thread_ref: dict[str, str | None]
    relation: Literal["owned", "linked"]
    owner_vendor: VendorRef
    subject: str
    latest_received_at: str | None
    message_count: int


class ThreadPage(TypedDict):
    items: list[ThreadItem]
    next_cursor: str | None


class CopyItem(TypedDict):
    copy_ref: str
    locations: list[str]
    summary: str | None
    attachment_count: int


class InventoryItem(TypedDict):
    part_id: str
    filename: str
    media_type: str
    byte_size: int | None


class CopyPage(TypedDict):
    thread_ref: dict[str, str | None]
    message_id: str
    items: list[CopyItem] | list[InventoryItem]
    next_cursor: str | None


class BodyResponse(TypedDict):
    thread_ref: dict[str, str | None]
    message_id: str
    body: body_state.Body


class ThreadContext(TypedDict):
    thread_ref: dict[str, str | None]
    relation: Literal["owned", "linked", "non_vendor"]
    owner_vendor: VendorRef | None
    subject: str
    history_notice: str


class MessageItem(TypedDict):
    message_id: str
    subject: str
    direction: Literal["inbound", "outbound"]
    sender: str
    sender_name: str | None
    attributed_vendor: VendorRef | None
    received_at: str | None
    body_state: body_state.BodyStatus | body_state.StoredBodyMetadata
    copy_count: int


class MessagePage(TypedDict):
    thread: ThreadContext
    items: list[MessageItem]
    next_cursor: str | None


def list_messages(
    store: Store,
    vendor_id: str | InboxThreadSelector,
    requested: ThreadRef,
    *,
    inbox_message: InboxMessageRef | None = None,
    limit: int = 25,
    cursor: str | None = None,
) -> MessagePage:
    with snapshot(store) as db:
        ref, members, vendors = _selected_thread(
            db, vendor_id, requested, inbox_message=inbox_message,
        )
        owner = _follow_owner(member for _, member in members)
        thread: ThreadContext = {
            "thread_ref": asdict(ref),
            "relation": "non_vendor" if owner is None else (
                "owned" if owner[2] == vendor_id else "linked"
            ),
            "owner_vendor": vendors[owner[2]] if owner is not None else None,
            "subject": _thread_title(members)[0],
            "history_notice": HISTORY_NOTICE,
        }
        projected = []
        for row, member in members:
            epoch = member[0]
            unit = _logical_unit(db, row["message_id"])
            assert unit is not None
            key = (*_newest(member), row["message_id"])
            item: MessageItem = {
                "message_id": row["message_id"],
                "subject": row["subject"],
                "direction": row["direction"],
                "sender": row["sender"],
                "sender_name": row["sender_name"],
                "attributed_vendor": vendors[row["vendor_id"]] if row["vendor_id"] else None,
                "received_at": datetime.fromtimestamp(epoch, UTC).isoformat()
                if math.isfinite(epoch)
                else None,
                "body_state": body_state.project(
                    db,
                    row["message_id"],
                    mailbox_identity_key=ref.mailbox_identity_key,
                    thread_followed=owner is not None,
                ),
                # Match the copy reader's source grouping without selecting saved
                # summaries or attachment catalogs just to compute this count.
                "copy_count": len({source.provider_message_id for source in unit.sources}),
            }
            projected.append((key, item))
        projected.sort(key=lambda entry: entry[0])
        rows = [item for _, item in projected]
        page = _page(
            rows,
            [key for key, _ in projected],
            selector={
                "operation": "threads.messages",
                **_read_selector(vendor_id, requested, inbox_message),
            },
            revision={"thread": thread, "messages": projected},
            limit=limit,
            cursor=cursor,
            ordering="newest_first",
        )
        return {"thread": thread, **page}


def read_body(
    store: Store,
    vendor_id: str | InboxThreadSelector,
    requested: ThreadRef,
    message_id: str,
    *,
    inbox_message: InboxMessageRef | None = None,
) -> BodyResponse:
    with snapshot(store) as db:
        ref, members, _ = _selected_thread(
            db, vendor_id, requested, inbox_message=inbox_message,
        )
        _selected_message(members, message_id)
        return {
            "thread_ref": asdict(ref),
            "message_id": message_id,
            "body": body_state.project(
                db,
                message_id,
                mailbox_identity_key=ref.mailbox_identity_key,
                thread_followed=_follow_owner(member for _, member in members) is not None,
                include_text=True,
            ),
        }


def _json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_json(value)).hexdigest()


def _cursor(value: object) -> dict | None:
    if value is None:
        return None
    try:
        if not isinstance(value, str) or not 1 <= len(value) <= MAX_CURSOR_BYTES:
            raise ValueError("cursor length")
        raw = base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(raw).decode() != value:
            raise ValueError("cursor encoding")
        decoded = _decode_json_bytes(raw, field="thread cursor")
        if not isinstance(decoded, dict) or set(decoded) != {
            "version",
            "selector",
            "revision",
            "last",
        }:
            raise ValueError("cursor fields")
        if type(decoded["version"]) is not int or decoded["version"] != 1:
            raise ValueError("cursor version")
        for key in ("selector", "revision", "last"):
            text = decoded[key]
            if (
                not isinstance(text, str)
                or len(text) != 64
                or any(c not in "0123456789abcdef" for c in text)
            ):
                raise ValueError("cursor digest")
        return decoded
    except (ValueError, UnicodeError, binascii.Error, RuntimeError) as error:
        raise ThreadReadError("invalid_request", "Malformed thread cursor") from error


def _page(
    rows: list[dict],
    keys: list[tuple],
    *,
    selector: dict,
    revision: object,
    limit: int,
    cursor: object,
    ordering: Literal[1, "newest_first"] = 1,
) -> dict:
    """A bounded keyset token binds the actual last tuple, never a trusted offset.

    Hashing the tuple keeps even long retained source IDs within the 4 KiB bound.
    Every page re-derives the tuple from the visible ordered projection.
    """
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ThreadReadError("invalid_request", "limit must be an integer between 1 and 100")
    decoded = _cursor(cursor)
    binding = _digest({**selector, "limit": limit, "ordering": ordering})
    version = _digest(revision)
    after = 0
    if decoded is not None:
        if decoded["selector"] != binding:
            raise ThreadReadError("invalid_request", "Thread cursor does not match this selection")
        if decoded["revision"] != version:
            raise ThreadReadError("stale_cursor", "Local history changed; restart this list")
        last_keys = [_digest(key) for key in keys]
        try:
            after = last_keys.index(decoded["last"]) + 1
        except ValueError as error:
            raise ThreadReadError(
                "invalid_request", "Thread cursor position is not visible"
            ) from error
    end = min(after + limit, len(rows))
    token = None
    if end < len(rows):
        token = base64.urlsafe_b64encode(
            _json(
                {
                    "version": 1,
                    "selector": binding,
                    "revision": version,
                    "last": _digest(keys[end - 1]),
                }
            )
        ).decode()
    return {"items": rows[after:end], "next_cursor": token}


@contextmanager
def snapshot(store: Store):
    """Never initialize, upgrade, reconcile, or repair in a read operation."""
    if not store.path.is_file():
        raise ThreadReadError("not_found", "Retained vendor was not found")
    with store.connection() as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        yield db


def _vendors(db) -> dict[str, VendorRef]:
    return {
        row["vendor_id"]: dict(row)
        for row in db.execute("SELECT vendor_id,display_name FROM vendors")
    }


def _members(db, provider=None, account_id=None, *, thread: ThreadRef | None = None):
    condition, parameters = (
        ("", ())
        if provider is None
        else ("WHERE m.provider=? AND m.account_id=?", (provider, account_id))
    )
    if thread is not None:
        assert (provider, account_id) == (thread.provider, thread.account_id)
        condition += (
            f" AND {_effective_message_identity_sql('m')} IS ? AND m.thread_key=?"
        )
        parameters += (thread.mailbox_identity_key, thread.thread_key)
    # logical_messages owns RECORD eligibility, including the whole held unit.
    # Explicit columns keep body bytes and saved summaries out of list reads.
    rows = db.execute(
        f"""SELECT m.message_id,m.provider,m.account_id,m.thread_key,
        m.subject,m.sender,m.sender_name,m.received_at,
        {_effective_message_identity_sql("m")} AS identity,
        d.direction,d.vendor_id
        FROM logical_messages m JOIN message_derived d ON d.message_id=m.message_id
        {condition}""",
        parameters,
    ).fetchall()
    grouped = defaultdict(list)
    for row in rows:
        ref = ThreadRef(row["provider"], row["account_id"], row["identity"], row["thread_key"])
        member = _thread_member(db, row, row["vendor_id"])
        grouped[ref].append((row, member))
    return grouped


def _inbox_thread(db, origin: InboxMessageRef):
    """Bind one visible canonical row before deriving its complete conversation."""
    row = db.execute(
        f"""SELECT m.thread_key, {_effective_message_identity_sql('m')} AS identity
        FROM logical_messages m
        WHERE m.provider=? AND m.account_id=? AND m.message_id=?""",
        (origin.provider, origin.account_id, origin.message_id),
    ).fetchone()
    if row is None:
        raise ThreadReadError("not_found", "Retained Inbox message was not found")
    if row["identity"] != origin.mailbox_identity_key:
        raise ThreadReadError("stale_selection", "Inbox identity changed; refresh the selection")
    ref = ThreadRef(origin.provider, origin.account_id, row["identity"], row["thread_key"])
    if not isinstance(ref.thread_key, str) or not ref.thread_key:
        raise RuntimeError("Retained thread mapping is inconsistent")
    # D-identity rekeys canonical rows in the merge transaction. An origin still
    # naming a retired key is inconsistent, not authority to follow another row.
    if ref.provider == "imap" and db.execute(
        "SELECT 1 FROM thread_key_aliases WHERE old_key=?", (ref.thread_key,)
    ).fetchone():
        raise RuntimeError("Retained thread mapping is inconsistent")
    expected_ids = {
        row[0] for row in db.execute(
            f"""SELECT m.message_id FROM logical_messages m
            WHERE m.provider=? AND m.account_id=?
            AND {_effective_message_identity_sql('m')} IS ? AND m.thread_key=?""",
            (ref.provider, ref.account_id, ref.mailbox_identity_key, ref.thread_key),
        )
    }
    members = _members(db, ref.provider, ref.account_id, thread=ref).get(ref, [])
    # The ordinary member projection joins derived rows. Missing derived data
    # must not shrink the conversation or masquerade as a zero-vendor result.
    if origin.message_id not in expected_ids or expected_ids != {
        row["message_id"] for row, _ in members
    }:
        raise RuntimeError("Retained thread membership is inconsistent")
    return ref, members


def resolve_inbox(
    store: Store,
    origin: InboxMessageRef,
    *,
    vendor_id: str | None = None,
    limit: int = 25,
    cursor: str | None = None,
) -> dict:
    """Metadata-only entry; absence of a vendor never makes a valid row unreadable."""
    with snapshot(store) as db:
        ref, members = _inbox_thread(db, origin)
        vendors = _vendors(db)
        represented = {row["vendor_id"] for row, _ in members} - {None}
        if not represented <= vendors.keys():
            raise RuntimeError("Retained thread attribution is inconsistent")
        candidates = [vendors[key] for key in sorted(represented)]
        owner = _follow_owner(member for _, member in members)
        owner_vendor = vendors[owner[2]] if owner is not None else None
        message_ref = asdict(origin)
        # Validate even when a previously paged chooser has become zero/one.
        page = _page(
            candidates,
            [(item["vendor_id"],) for item in candidates],
            selector={"operation": "threads.resolve", "message_ref": message_ref},
            revision={
                "thread_ref": asdict(ref), "owner_vendor": owner_vendor,
                "candidates": candidates,
            },
            limit=limit,
            cursor=cursor,
        )
        if vendor_id is not None and vendor_id not in represented:
            raise ThreadReadError("stale_selection", "Vendor context changed; resolve again")
        if vendor_id is None and len(candidates) > 1:
            return {
                "state": "choose_vendor", "message_ref": message_ref,
                "thread_ref": asdict(ref), "owner_vendor": owner_vendor, **page,
            }
        selected = vendor_id or (candidates[0]["vendor_id"] if candidates else None)
        subject, order = _thread_title(members)
        epoch = order[0]
        thread = {
            "thread_ref": asdict(ref),
            "subject": subject,
            "latest_received_at": datetime.fromtimestamp(epoch, UTC).isoformat()
            if math.isfinite(epoch) else None,
            "message_count": len(members),
            "relation": "non_vendor" if selected is None else (
                "owned" if owner[2] == selected else "linked"
            ),
            "owner_vendor": owner_vendor,
        }
        context = {"kind": "non_vendor", "thread": thread} if selected is None else {
            "kind": "vendor", "vendor": vendors[selected], "thread": thread,
        }
        return {"state": "resolved", "message_ref": message_ref, "context": context}


def _newest(member):
    epoch, source, _ = member
    return (not math.isfinite(epoch), -epoch if math.isfinite(epoch) else 0, source)


def _thread_title(members):
    """One title rule for list rows and open-thread context, before pagination."""
    latest, order = min(members, key=lambda item: _newest(item[1]))
    return latest["subject"] or "No subject", order


def _resolve_thread_ref(db, requested: ThreadRef) -> ThreadRef:
    ref = requested
    if ref.provider == "imap":
        seen = set()
        while True:
            if ref.thread_key in seen:
                raise ThreadReadError("not_found", "Retained thread was not found")
            seen.add(ref.thread_key)
            alias = db.execute(
                "SELECT survivor_key FROM thread_key_aliases WHERE old_key=?", (ref.thread_key,)
            ).fetchone()
            if alias is None:
                break
            ref = replace(ref, thread_key=alias[0])
    return ref


def _selected_thread(
    db,
    vendor_id: str | InboxThreadSelector,
    requested: ThreadRef,
    *,
    inbox_message: InboxMessageRef | None = None,
):
    """Resolve and guard current membership inside the projection snapshot."""
    non_vendor = isinstance(vendor_id, InboxThreadSelector)
    if non_vendor:
        if inbox_message is not None:
            raise ThreadReadError("invalid_request", "Thread selectors cannot be mixed")
        origin = vendor_id.message_ref
        if not isinstance(origin, InboxMessageRef):
            raise ThreadReadError("invalid_request", "A complete Inbox origin is required")
    else:
        if not isinstance(vendor_id, str) or not vendor_id:
            raise ThreadReadError("invalid_request", "A vendor selector is required")
        origin = inbox_message
    vendors = _vendors(db)
    ref = _resolve_thread_ref(db, requested)
    if origin is not None:
        origin_ref, members = _inbox_thread(db, origin)
        if ref != origin_ref:
            raise ThreadReadError("not_found", "Inbox message is not in the selected thread")
        represented = {row["vendor_id"] for row, _ in members} - {None}
        if not represented <= vendors.keys():
            raise RuntimeError("Retained thread attribution is inconsistent")
    else:
        members = _members(db, ref.provider, ref.account_id).get(ref, [])
        represented = {row["vendor_id"] for row, _ in members} - {None}
    if non_vendor:
        if represented:
            raise ThreadReadError("stale_selection", "Thread context changed; resolve again")
    elif vendor_id not in vendors or vendor_id not in represented:
        raise ThreadReadError(
            "stale_selection" if origin is not None else "not_found",
            "Vendor context changed; resolve again" if origin is not None
            else "Retained thread was not found",
        )
    return ref, members, vendors


def _read_selector(vendor_id, requested, inbox_message):
    if isinstance(vendor_id, InboxThreadSelector):
        return {"inbox_thread": {
            "thread_ref": asdict(requested), "message_ref": asdict(vendor_id.message_ref),
        }}
    selector = {"vendor_id": vendor_id, "thread_ref": asdict(requested)}
    if inbox_message is not None:
        selector["inbox_message"] = asdict(inbox_message)
    return selector


def _selected_message(members, message_id):
    # A physical duplicate ID is not a canonical message selector.
    for row, _ in members:
        if row["message_id"] == message_id:
            return row
    raise ThreadReadError("not_found", "Retained message was not found")


def _copy_projection(db, message_id):
    unit = _logical_unit(db, message_id)
    assert unit is not None  # Caller validated canonical RECORD-visible membership.
    rows = db.execute(
        "SELECT message_id,provider_message_id,summary FROM messages "
        "WHERE message_id IN (SELECT value FROM json_each(?)) ORDER BY message_id",
        (json.dumps(unit.rows),),
    ).fetchall()
    copies = {
        source.provider_message_id: {
            "copy_ref": source.provider_message_id,
            "locations": set(),
            "summary": None,
            "attachment_count": 0,
        }
        for source in unit.sources
    }
    for row in rows:
        copies[row["provider_message_id"]]["summary"] = row["summary"]
    for row in db.execute(
        "SELECT provider_message_id,location FROM message_locations WHERE message_id=?",
        (message_id,),
    ):
        copies[row["provider_message_id"]]["locations"].add(row["location"])
    inventories = defaultdict(list)
    # The catalog's recorded source owns it, even for a location-only copy.
    # Legacy catalogs fall back to their actual stored row's source binding.
    for row in db.execute(
        """SELECT COALESCE(a.source_provider_message_id,m.provider_message_id) AS source,
        a.message_id,a.part_id,a.filename,a.media_type,a.byte_size,a.byte_size_known,a.position
        FROM message_attachments a JOIN messages m USING(message_id)
        WHERE a.message_id IN (SELECT value FROM json_each(?))""",
        (json.dumps(unit.rows),),
    ):
        if row["source"] not in copies:
            continue  # No membership in the current logical unit; never invent a copy.
        copies[row["source"]]["attachment_count"] += 1
        item: InventoryItem = {
            "part_id": row["part_id"],
            "filename": row["filename"],
            "media_type": row["media_type"],
            "byte_size": row["byte_size"] if row["byte_size_known"] else None,
        }
        inventories[row["source"]].append(
            ((row["position"], row["part_id"], row["message_id"]), item)
        )
    for copy in copies.values():
        copy["locations"] = sorted(copy["locations"])
    for inventory in inventories.values():
        inventory.sort(key=lambda entry: entry[0])
    return [copies[key] for key in sorted(copies)], dict(inventories)


def list_copies(
    store: Store,
    vendor_id: str | InboxThreadSelector,
    thread_ref: ThreadRef,
    message_id: str,
    *,
    inbox_message: InboxMessageRef | None = None,
    copy_ref: str | None = None,
    limit: int = 25,
    cursor: str | None = None,
) -> CopyPage:
    with snapshot(store) as db:
        ref, members, _ = _selected_thread(
            db, vendor_id, thread_ref, inbox_message=inbox_message,
        )
        _selected_message(members, message_id)
        owner = _follow_owner(member for _, member in members)
        copies, inventories = _copy_projection(db, message_id)
        if copy_ref is None:
            rows, keys = copies, [(copy["copy_ref"],) for copy in copies]
        else:
            if copy_ref not in {copy["copy_ref"] for copy in copies}:
                raise ThreadReadError("not_found", "Retained copy was not found")
            inventory = inventories.get(copy_ref, [])
            rows, keys = [item for _, item in inventory], [key for key, _ in inventory]
        page = _page(
            rows,
            keys,
            selector={
                "operation": "threads.copies",
                **_read_selector(vendor_id, thread_ref, inbox_message),
                "message_id": message_id,
                "copy_ref": copy_ref,
            },
            revision={
                "resolved_ref": asdict(ref),
                "relationship": {
                    "owner": owner[2] if owner is not None else None,
                    "attribution": sorted(
                        (row["message_id"], row["vendor_id"]) for row, _ in members
                    ),
                },
                "copies": copies,
                "inventories": inventories,
            },
            limit=limit,
            cursor=cursor,
        )
        return {"thread_ref": asdict(ref), "message_id": message_id, **page}


def list_threads(
    store: Store,
    vendor_id: str,
    *,
    provider: str | None = None,
    account_id: str | None = None,
    limit: int = 25,
    cursor: str | None = None,
) -> ThreadPage:
    if (provider is None) != (account_id is None):
        raise ThreadReadError(
            "invalid_request", "provider and account_id must be supplied together"
        )
    with snapshot(store) as db:
        vendors = _vendors(db)
        if vendor_id not in vendors:
            raise ThreadReadError("not_found", "Retained vendor was not found")
        projected = []
        for ref, members in _members(db, provider, account_id).items():
            # Reuse D-derived's owner function, including the unresolved legacy
            # bucket for which the persisted follow cache cannot carry a key.
            owner = _follow_owner(member for _, member in members)
            if owner is None or not any(row["vendor_id"] == vendor_id for row, _ in members):
                continue
            subject, order = _thread_title(members)
            epoch = order[0]
            source = min(member[1] for _, member in members)
            key = (
                *_newest(order)[:2],
                source,
                ref.provider,
                ref.account_id,
                ref.mailbox_identity_key or "",
                ref.thread_key,
            )
            item: ThreadItem = {
                "thread_ref": asdict(ref),
                "relation": "owned" if owner[2] == vendor_id else "linked",
                "owner_vendor": vendors[owner[2]],
                "subject": subject,
                "latest_received_at": datetime.fromtimestamp(epoch, UTC).isoformat()
                if math.isfinite(epoch)
                else None,
                "message_count": len(members),
            }
            # Changes to older membership/attribution must stale cursors even
            # when count, newest subject and thread owner happen to be equal.
            membership = sorted(
                (
                    row["message_id"],
                    row["vendor_id"],
                    row["direction"],
                    row["subject"],
                    row["sender"],
                    row["sender_name"],
                    row["received_at"],
                    _newest(member),
                )
                for row, member in members
            )
            projected.append((key, item, membership))
        projected.sort(key=lambda entry: entry[0])
        return _page(
            [item for _, item, _ in projected],
            [key for key, _, _ in projected],
            selector={
                "operation": "threads.list",
                "vendor_id": vendor_id,
                "provider": provider,
                "account_id": account_id,
            },
            revision=projected,
            limit=limit,
            cursor=cursor,
        )
