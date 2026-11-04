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


def read_body(store: Store, vendor_id: str, requested: ThreadRef, message_id: str) -> BodyResponse:
    with snapshot(store) as db:
        ref, members, _ = _selected_thread(db, vendor_id, requested)
        _selected_message(members, message_id)
        return {
            "thread_ref": asdict(ref),
            "message_id": message_id,
            "body": body_state.project(
                db,
                message_id,
                mailbox_identity_key=ref.mailbox_identity_key,
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
) -> dict:
    """A bounded keyset token binds the actual last tuple, never a trusted offset.

    Hashing the tuple keeps even long retained source IDs within the 4 KiB bound.
    Every page re-derives the tuple from the visible ordered projection.
    """
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ThreadReadError("invalid_request", "limit must be an integer between 1 and 100")
    decoded = _cursor(cursor)
    binding = _digest({**selector, "limit": limit, "ordering": 1})
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


def _members(db, provider=None, account_id=None):
    condition, parameters = (
        ("", ())
        if provider is None
        else ("WHERE m.provider=? AND m.account_id=?", (provider, account_id))
    )
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


def _newest(member):
    epoch, source, _ = member
    return (not math.isfinite(epoch), -epoch if math.isfinite(epoch) else 0, source)


def _selected_thread(db, vendor_id: str, requested: ThreadRef):
    """Resolve an IMAP alias, then validate complete live scope and relationship."""
    vendors = _vendors(db)
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
    members = _members(db, ref.provider, ref.account_id).get(ref, [])
    if vendor_id not in vendors or not any(row["vendor_id"] == vendor_id for row, _ in members):
        raise ThreadReadError("not_found", "Retained thread was not found")
    return ref, members, vendors


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
    vendor_id: str,
    thread_ref: ThreadRef,
    message_id: str,
    *,
    copy_ref: str | None = None,
    limit: int = 25,
    cursor: str | None = None,
) -> CopyPage:
    with snapshot(store) as db:
        ref, members, _ = _selected_thread(db, vendor_id, thread_ref)
        _selected_message(members, message_id)
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
                "vendor_id": vendor_id,
                "thread_ref": asdict(thread_ref),
                "message_id": message_id,
                "copy_ref": copy_ref,
            },
            revision={
                "resolved_ref": asdict(ref),
                "relationship": {
                    "owner": _follow_owner(member for _, member in members)[2],
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
            latest, order = min(members, key=lambda item: _newest(item[1]))
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
                "subject": latest["subject"] or "No subject",
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
