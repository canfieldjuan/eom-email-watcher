"""Inbox-origin resolution through real canonical, identity and removal owners."""

import base64
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta

import pytest
from test_service import config
from test_thread_bodies import followed
from test_thread_reconcile import KEY, NOW, seed, state_store

from eom_email_watcher import engine_api, thread_reads


def origin(message_id="m", **scope):
    return {
        "provider": "gmail",
        "account_id": "mailbox",
        "mailbox_identity_key": KEY,
        "message_id": message_id,
        **scope,
    }


def resolver(store, tmp_path, monkeypatch):
    monkeypatch.setattr(
        engine_api, "load_config", lambda _: replace(config(tmp_path), database_file=store.path)
    )

    def call(message_ref, **payload):
        return engine_api._response(
            {
                "protocol": 1,
                "operation": "threads.resolve",
                "config_path": str(tmp_path / "config.toml"),
                "payload": {"message_ref": message_ref, **payload},
            }
        )

    return call


def success(response):
    assert response["ok"], response
    return response["data"]


def error(response, code):
    assert not response["ok"], response
    assert response["error"]["code"] == code, response
    assert "data" not in response


def test_inbox_projects_complete_row_origin_and_resolves_non_vendor(tmp_path, monkeypatch):
    store, _ = state_store(tmp_path)
    seed(store)
    seed(store, "reply", sender="other@example.com", subject="Newest", received_at=NOW.isoformat())
    items, _ = store.query_inbox(limit=25)
    assert store.recent(25) == [
        {key: value for key, value in item.items() if key != "thread_entry_ref"}
        for item in items
    ]
    assert {item["message_id"]: item["thread_entry_ref"] for item in items} == {
        "m": origin(),
        "reply": origin("reply"),
    }
    result = success(resolver(store, tmp_path, monkeypatch)(origin()))
    assert result == {
        "state": "resolved",
        "message_ref": origin(),
        "context": {
            "kind": "non_vendor",
            "thread": {
                "thread_ref": {k: v for k, v in origin().items() if k != "message_id"}
                | {"thread_key": "native"},
                "relation": "non_vendor",
                "owner_vendor": None,
                "subject": "Newest",
                "latest_received_at": NOW.isoformat(),
                "message_count": 2,
            },
        },
    }


def test_one_then_many_vendors_uses_explicit_choice_even_for_unattributed_seed(
    tmp_path, monkeypatch
):
    store, _, owner = followed(tmp_path)
    seed(store, "unattributed", sender="person@example.com")
    call = resolver(store, tmp_path, monkeypatch)
    first = success(call(origin("unattributed")))
    assert first["context"]["vendor"] == {"vendor_id": owner, "display_name": "Vendor"}
    other = store.create_vendor("Linked")
    store.add_vendor_address(other, "other@example.com")
    seed(store, "linked", sender="other@example.com", received_at=NOW.isoformat())
    choice = success(call(origin("unattributed")))
    assert choice["state"] == "choose_vendor"
    assert choice["owner_vendor"]["vendor_id"] == owner
    assert [item["vendor_id"] for item in choice["items"]] == sorted([owner, other])
    assert choice["next_cursor"] is None
    selected = success(call(origin("unattributed"), vendor_id=other))["context"]
    assert selected["vendor"]["vendor_id"] == other
    assert selected["thread"]["relation"] == "linked"
    assert selected["thread"]["owner_vendor"]["vendor_id"] == owner
    store.remove_vendor_address(other, "other@example.com")
    error(call(origin("unattributed"), vendor_id=other), "stale_selection")
    store.remove_vendor_address(owner, "vendor@example.com")
    error(call(origin("unattributed"), vendor_id=other), "stale_selection")
    assert success(call(origin("unattributed")))["context"]["kind"] == "non_vendor"


@pytest.mark.parametrize(
    "scope",
    [
        {"provider": "microsoft365"},
        {"account_id": "other"},
        {"mailbox_identity_key": "a" * 64},
        {"mailbox_identity_key": None},
    ],
)
def test_origin_cannot_borrow_message_from_another_scope(tmp_path, monkeypatch, scope):
    store, _, _ = followed(tmp_path)
    call = resolver(store, tmp_path, monkeypatch)
    error(
        call(origin(**scope)), "stale_selection" if "mailbox_identity_key" in scope else "not_found"
    )
    assert success(call(origin()))["context"]["thread"]["thread_ref"] == {
        "provider": "gmail",
        "account_id": "mailbox",
        "mailbox_identity_key": KEY,
        "thread_key": "native",
    }


def test_identity_twins_return_exact_conversation_and_seed(tmp_path, monkeypatch):
    store, _, vendor = followed(tmp_path)
    call = resolver(store, tmp_path, monkeypatch)
    refs = [origin()]
    for number, (provider, account, identity) in enumerate(
        [
            ("gmail", "other", KEY),
            ("gmail", "mailbox", "a" * 64),
            ("microsoft365", "mailbox", KEY),
        ]
    ):
        if store.mail_account(provider, account) is None:
            store.register_mail_account(provider, account, display_name="Retained")
        store.reconcile_mailbox_identity(provider, account, identity, legacy_status="replacement")
        message = f"twin-{number}"
        seed(
            store,
            message,
            provider=provider,
            account_id=account,
            mailbox_identity_key=identity,
            subject=message,
        )
        refs.append(
            origin(message, provider=provider, account_id=account, mailbox_identity_key=identity)
        )
    for ref in refs:
        result = success(call(ref))
        assert result["message_ref"] == ref
        thread = result["context"]["thread"]
        assert thread["thread_ref"] == {k: v for k, v in ref.items() if k != "message_id"} | {
            "thread_key": "native",
        }
        assert thread["subject"] == ("Quote" if ref["message_id"] == "m" else ref["message_id"])
        assert thread["message_count"] == 1
        assert result["context"]["vendor"]["vendor_id"] == vendor


def test_legacy_unresolved_reference_never_substitutes_active_identity(tmp_path, monkeypatch):
    from test_db import _upgraded_completed_legacy_message

    store, _, identity = _upgraded_completed_legacy_message(tmp_path)
    with store.connection() as db:
        db.execute(
            "UPDATE mail_accounts SET legacy_identity_status='unresolved', "
            "legacy_identity_key=NULL,mailbox_identity_key=NULL"
        )
    item = store.query_inbox(limit=25)[0][0]
    ref = item["thread_entry_ref"]
    assert ref == origin(
        item["message_id"], provider="imap", account_id="imap-account", mailbox_identity_key=None
    )
    call = resolver(store, tmp_path, monkeypatch)
    assert success(call(ref))["context"]["thread"]["thread_ref"]["mailbox_identity_key"] is None
    error(call(ref | {"mailbox_identity_key": identity}), "stale_selection")
    store.reconcile_mailbox_identity(
        "imap", "imap-account", identity, legacy_status="continuity_proven", preserve_cursor=True
    )
    error(call(ref), "stale_selection")
    refreshed = store.query_inbox(limit=25)[0][0]["thread_entry_ref"]
    assert refreshed == ref | {"mailbox_identity_key": identity}
    assert success(call(refreshed))["context"]["kind"] == "non_vendor"


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        [],
        "m",
        {"provider": "gmail"},
        origin() | {"extra": True},
        origin() | {"mailbox_identity_key": "bad"},
        origin() | {"provider": "other"},
        origin() | {"account_id": None},
        origin() | {"message_id": None},
        origin() | {"message_id": ""},
        origin() | {"message_id": True},
        *[{k: v for k, v in origin().items() if k != missing} for missing in origin()],
    ],
)
def test_reference_fields_are_required_and_strict(tmp_path, monkeypatch, bad):
    store, _, _ = followed(tmp_path)
    error(resolver(store, tmp_path, monkeypatch)(bad), "invalid_request")


@pytest.mark.parametrize(
    "payload",
    [
        {"unknown": 1},
        {"limit": 0},
        {"limit": 101},
        {"limit": True},
        {"limit": None},
        {"cursor": ""},
        {"cursor": 3},
        {"cursor": "x" * 4097},
        {"cursor": "e30="},
        {"vendor_id": None},
        {"vendor_id": "bad"},
    ],
)
def test_query_rejects_malformed_fields_before_resolving(tmp_path, monkeypatch, payload):
    store, _ = state_store(tmp_path)
    seed(store)
    error(resolver(store, tmp_path, monkeypatch)(origin(), **payload), "invalid_request")


@pytest.mark.parametrize("paging", [{"limit": 25}, {"cursor": None}])
def test_explicit_vendor_never_ignores_supplied_paging(tmp_path, monkeypatch, paging):
    store, _, vendor = followed(tmp_path)
    error(
        resolver(store, tmp_path, monkeypatch)(origin(), vendor_id=vendor, **paging),
        "invalid_request",
    )


def test_message_id_is_not_trimmed_or_case_folded(tmp_path, monkeypatch):
    store, _ = state_store(tmp_path)
    seed(store, " Case ")
    call = resolver(store, tmp_path, monkeypatch)
    assert success(call(origin(" Case ")))["message_ref"]["message_id"] == " Case "
    for message in ["Case", " case "]:
        error(call(origin(message)), "not_found")


def test_canonical_copy_is_counted_once_and_physical_alias_refuses(tmp_path, monkeypatch):
    store, _, _ = followed(tmp_path)
    seed(store, "a-copy", rfc_message_id="copy@example.com")
    seed(store, "z-copy", rfc_message_id="copy@example.com")
    call = resolver(store, tmp_path, monkeypatch)
    result = success(call(origin("a-copy")))
    assert result["context"]["thread"]["message_count"] == 2
    error(call(origin("z-copy")), "not_found")


def test_real_legacy_canonical_promotion_requires_a_fresh_inbox_selection(tmp_path, monkeypatch):
    from test_db import _retained_duplicate, _upgraded_completed_legacy_message

    from eom_email_watcher import db as database

    store, root, identity = _upgraded_completed_legacy_message(tmp_path)
    store.mark_skipped(root)
    child = _retained_duplicate(store, "2", "same@x")
    original_source = store.message_source(root).provider_message_id
    with store.connection() as db:
        db.execute(
            "UPDATE messages SET provider_message_id=?,status='skipped',logical_of=? "
            "WHERE message_id=?",
            (original_source, root, child),
        )
        database._reparent_logical_sources(db, child, root)
        db.execute("UPDATE messages SET status='pending' WHERE message_id=?", (root,))
        db.execute("UPDATE mail_accounts SET mailbox_identity_key=NULL")
    ref = store.query_inbox(limit=25)[0][0]["thread_entry_ref"]
    assert ref["message_id"] == root
    call = resolver(store, tmp_path, monkeypatch)
    success(call(ref))
    store.reconcile_mailbox_identity(
        "imap", "imap-account", identity, legacy_status="continuity_proven"
    )
    error(call(ref), "not_found")
    refreshed = store.query_inbox(limit=25)[0][0]["thread_entry_ref"]
    assert refreshed["message_id"] == child
    success(call(refreshed))


@pytest.mark.parametrize("broken", ["m", "other"])
def test_missing_derived_membership_fails_instead_of_becoming_non_vendor(
    tmp_path, monkeypatch, broken
):
    store, _ = state_store(tmp_path)
    seed(store)
    seed(store, "other")
    with store.connection() as db:
        db.execute("DELETE FROM message_derived WHERE message_id=?", (broken,))
    error(resolver(store, tmp_path, monkeypatch)(origin()), "runtime_error")


@pytest.mark.parametrize(
    "operation,payload",
    [
        ("inbox.delete", {"message_id": "m"}),
        ("inbox.clear", {}),
    ],
)
def test_removal_hold_refuses_resolution_while_33_rows_and_body_remain(
    tmp_path, monkeypatch, operation, payload
):
    from test_removal_refusal import held_runtime

    store = held_runtime(tmp_path, monkeypatch)
    item = store.query_inbox(limit=25)[0][0]
    ref = item["thread_entry_ref"]
    call = resolver(store, tmp_path, monkeypatch)
    assert success(call(ref))["state"] == "resolved"
    result = engine_api._response({"protocol": 1, "operation": operation, "payload": payload})
    assert result["ok"] and result["data"]["outcome"] == "deferred", result
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 33
        assert db.execute("SELECT text FROM message_bodies").fetchone()[0] == "private body"
    error(call(ref), "not_found")


def test_read_uses_one_snapshot_and_does_not_initialize_or_invoke_runtime(tmp_path, monkeypatch):
    store, _, _ = followed(tmp_path)
    call = resolver(store, tmp_path, monkeypatch)

    def forbidden(*args, **kwargs):
        pytest.fail("resolution initialized storage/runtime or checked entitlement")

    for name in ["_runtime", "load_runtime", "runtime_from_config"]:
        monkeypatch.setattr(engine_api, name, forbidden)
    monkeypatch.setattr(engine_api.Store, "initialize", forbidden)
    monkeypatch.setattr(engine_api.connect, "require_connect_entitlement", forbidden)
    with store.connection() as db:
        db.execute("PRAGMA journal_mode=WAL")
        before = list(db.iterdump())
    success(call(origin()))
    with store.connection() as db:
        assert list(db.iterdump()) == before
    original = thread_reads._members
    inserted = False

    def concurrent(db, *args, **kwargs):
        nonlocal inserted
        if not inserted:
            inserted = True
            seed(store, "late", subject="Concurrent", received_at=NOW.isoformat())
        return original(db, *args, **kwargs)

    monkeypatch.setattr(thread_reads, "_members", concurrent)
    assert success(call(origin()))["context"]["thread"]["message_count"] == 1
    assert success(call(origin()))["context"]["thread"]["message_count"] == 2
    with (
        pytest.raises(sqlite3.OperationalError, match="readonly"),
        thread_reads.snapshot(store) as db,
    ):
        db.execute("DELETE FROM vendors")


def test_candidate_cursors_bind_origin_limit_and_full_current_projection(tmp_path, monkeypatch):
    store, _, owner = followed(tmp_path)
    other = store.create_vendor("Other")
    store.add_vendor_address(other, "other@example.com")
    seed(
        store,
        "reply",
        sender="other@example.com",
        received_at=(NOW + timedelta(days=1)).isoformat(),
    )
    call = resolver(store, tmp_path, monkeypatch)
    first = success(call(origin(), limit=1))
    cursor = first["next_cursor"]
    assert cursor and len(cursor) <= 4096
    last = success(call(origin(), limit=1, cursor=cursor))
    assert [r["vendor_id"] for r in first["items"] + last["items"]] == sorted([owner, other])
    assert last["next_cursor"] is None
    error(call(origin("reply"), limit=1, cursor=cursor), "invalid_request")
    error(call(origin(), limit=2, cursor=cursor), "invalid_request")
    token = json.loads(base64.urlsafe_b64decode(cursor))
    token["last"] = "0" * 64
    forged = base64.urlsafe_b64encode(json.dumps(token).encode()).decode()
    error(call(origin(), limit=1, cursor=forged), "invalid_request")
    seed(
        store,
        "earlier-other",
        sender="other@example.com",
        received_at=(NOW - timedelta(days=4)).isoformat(),
    )
    error(call(origin(), limit=1, cursor=cursor), "stale_cursor")
    changed_owner = success(call(origin(), limit=1))
    assert changed_owner["owner_vendor"]["vendor_id"] == other
    cursor = changed_owner["next_cursor"]
    store.rename_vendor(other, "Renamed")
    error(call(origin(), limit=1, cursor=cursor), "stale_cursor")
    cursor = success(call(origin(), limit=1))["next_cursor"]
    store.remove_vendor_address(other, "other@example.com")
    error(call(origin(), limit=1, cursor=cursor), "stale_cursor")
    store.remove_vendor_address(owner, "vendor@example.com")
    error(call(origin(), limit=1, cursor=cursor), "stale_cursor")
    error(call(origin(), cursor="invalid"), "invalid_request")


def test_more_than_100_candidates_are_never_auto_selected_or_truncated(tmp_path, monkeypatch):
    store, _ = state_store(tmp_path)
    seed(store, sender="unattributed@example.com")
    vendors = []
    for index in range(102):
        vendor = store.create_vendor(f"Vendor {index}")
        vendors.append(vendor)
        address = f"vendor-{index}@example.com"
        store.add_vendor_address(vendor, address)
        seed(store, f"vendor-{index}", sender=address)
    call = resolver(store, tmp_path, monkeypatch)
    page = success(call(origin(), limit=100))
    assert page["state"] == "choose_vendor" and len(page["items"]) == 100
    last = success(call(origin(), limit=100, cursor=page["next_cursor"]))
    assert last["state"] == "choose_vendor" and len(last["items"]) == 2
    assert [item["vendor_id"] for item in page["items"] + last["items"]] == sorted(vendors)
    assert last["next_cursor"] is None


def test_imap_merge_resolves_the_current_survivor_from_the_same_origin(tmp_path, monkeypatch):
    store, _ = state_store(tmp_path, provider="imap")
    seed(store, "a", provider="imap", thread_id=None, rfc_message_id="a@example.com")
    seed(store, "b", provider="imap", thread_id=None, rfc_message_id="b@example.com")
    call = resolver(store, tmp_path, monkeypatch)
    refs = [origin(key, provider="imap") for key in ["a", "b"]]
    before = [success(call(ref))["context"]["thread"]["thread_ref"] for ref in refs]
    assert before[0] != before[1]
    seed(
        store,
        "bridge",
        provider="imap",
        thread_id=None,
        rfc_message_id="bridge@example.com",
        reply_ids=("a@example.com", "b@example.com"),
    )
    after = [success(call(ref))["context"]["thread"] for ref in refs]
    assert after[0] == after[1]
    assert after[0]["message_count"] == 3
    assert after[0]["thread_ref"] in before
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM thread_key_aliases").fetchone()[0] == 1


def test_resolver_does_not_read_body_summary_or_attachment_content(tmp_path, monkeypatch):
    store, _, _ = followed(tmp_path)
    call = resolver(store, tmp_path, monkeypatch)
    original = engine_api.Store.connection
    reads = []

    @contextmanager
    def guarded(instance):
        with original(instance) as db:

            def authorize(action, table, column, _database, view):
                if action == sqlite3.SQLITE_READ:
                    reads.append((table, column))
                    if table in {
                        "message_bodies",
                        "message_body_unavailable",
                        "message_attachments",
                    } or (
                        column == "summary"
                        and view is None
                        and table in {"messages", "logical_messages"}
                    ):
                        return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            db.set_authorizer(authorize)
            try:
                for sql in [
                    "SELECT summary FROM messages",
                    "SELECT summary FROM logical_messages",
                    "SELECT text FROM message_bodies",
                    "SELECT * FROM message_attachments",
                ]:
                    with pytest.raises(sqlite3.DatabaseError):
                        db.execute(sql)
                yield db
            finally:
                db.set_authorizer(None)

    monkeypatch.setattr(engine_api.Store, "connection", guarded)
    result = success(call(origin()))
    assert reads
    assert not {"body", "text", "summary", "attachments"} & result["context"]["thread"].keys()


def test_outbound_origin_uses_derived_vendor_and_public_inbox_projection(tmp_path, monkeypatch):
    from types import SimpleNamespace

    store, _, vendor = followed(tmp_path)
    store.update_mail_account_identity(
        "gmail", "mailbox", display_name="Mailbox", address="me@example.com"
    )
    seed(
        store,
        "sent",
        sender="me@example.com",
        to=("vendor@example.com",),
        locations=frozenset({"sent"}),
        received_at=NOW.isoformat(),
    )
    call = resolver(store, tmp_path, monkeypatch)
    monkeypatch.setattr(engine_api, "_runtime", lambda _: SimpleNamespace(store=store))
    rows = success(
        engine_api._response(
            {
                "protocol": 1,
                "operation": "inbox.query",
                "payload": {"sender": "me@example.com"},
            }
        )
    )["items"]
    assert len(rows) == 1 and rows[0]["thread_entry_ref"] == origin("sent")
    with store.connection() as db:
        assert (
            db.execute("SELECT direction FROM message_derived WHERE message_id='sent'").fetchone()[
                0
            ]
            == "outbound"
        )
    result = success(call(rows[0]["thread_entry_ref"]))
    assert result["context"]["vendor"]["vendor_id"] == vendor
    assert result["context"]["thread"]["message_count"] == 2


def test_contradictory_imap_origin_mapping_does_not_follow_an_unrelated_alias(
    tmp_path, monkeypatch
):
    store, _ = state_store(tmp_path, provider="imap")
    seed(store, provider="imap", thread_id=None, rfc_message_id="origin@example.com")
    ref = origin(provider="imap")
    call = resolver(store, tmp_path, monkeypatch)
    key = success(call(ref))["context"]["thread"]["thread_ref"]["thread_key"]
    with store.connection() as db:
        db.execute("INSERT INTO thread_key_aliases VALUES (?,?)", (key, "unrelated"))
    error(call(ref), "runtime_error")
