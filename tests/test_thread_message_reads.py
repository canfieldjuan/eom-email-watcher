"""M3 message pages retain logical scope, order and body-state boundaries."""

import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import timedelta

import pytest
from conftest import local_removal
from test_thread_bodies import followed
from test_thread_body_reads import forbid_body_reads, unresolved_thread
from test_thread_copy_reads import imap_selection, selection
from test_thread_reconcile import NOW, seed

from eom_email_watcher import body_state, engine_api, thread_reads


def messages(store, vendor, ref=None, **paging):
    return thread_reads.list_messages(store, vendor, ref or selection(store, vendor), **paging)


def test_owned_linked_direction_attribution_and_copy_count_are_real_derived_metadata(tmp_path):
    store, _, owner = followed(tmp_path)
    linked = store.create_vendor("Linked")
    store.add_vendor_address(linked, "linked@example.com")
    seed(
        store,
        "reply",
        sender="me@example.com",
        to=("linked@example.com",),
        locations=frozenset({"sent"}),
        received_at=(NOW - timedelta(days=1)).isoformat(),
    )
    seed(store, "unattributed", sender="other@example.com", received_at=NOW.isoformat())
    seed(store, "duplicate", rfc_message_id="copy@vendor")
    with store.connection() as db:
        db.execute("UPDATE messages SET logical_of='m' WHERE message_id='duplicate'")
    body_state.record(store, "m", ("<literal>\n> quote", 30), fetched_at=NOW)
    page = messages(store, linked)
    assert page["thread"] == {
        "thread_ref": asdict(selection(store, linked)),
        "relation": "linked",
        "owner_vendor": {"vendor_id": owner, "display_name": "Vendor"},
        "history_notice": "History partial — only locally retained messages are shown",
    }
    assert [item["message_id"] for item in page["items"]] == ["m", "reply", "unattributed"]
    first, reply, unattributed = page["items"]
    assert first == {
        "message_id": "m",
        "subject": "Quote",
        "direction": "inbound",
        "sender": "vendor@example.com",
        "sender_name": None,
        "attributed_vendor": {"vendor_id": owner, "display_name": "Vendor"},
        "received_at": (NOW - timedelta(days=2)).isoformat(),
        "copy_count": 2,
        "body_state": {"state": "stored", "stored_chars": 17, "source_chars": 30},
    }
    assert reply["direction"] == "outbound" and reply["sender"] == "me@example.com"
    assert reply["attributed_vendor"] == {"vendor_id": linked, "display_name": "Linked"}
    assert unattributed["attributed_vendor"] is None
    assert messages(store, owner)["thread"]["relation"] == "owned"
    assert page["next_cursor"] is None


def test_message_list_never_selects_body_text_or_saved_summaries(tmp_path, monkeypatch):
    store, _, vendor = followed(tmp_path)
    body_state.record(store, "m", ("hidden text", 11), fetched_at=NOW)
    ref = selection(store, vendor)
    original = store.connection

    @contextmanager
    def guarded():
        with original() as db:

            def authorize(action, table, column, _database, origin):
                # SQLite expands the logical_messages SELECT * view during
                # compilation even when the outer query names only headers.
                # Deny actual summary selection through either table or view.
                forbidden = (table, column) == ("message_bodies", "text") or (
                    column == "summary"
                    and origin is None
                    and table in {"messages", "logical_messages"}
                )
                return (
                    sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_READ and forbidden
                    else sqlite3.SQLITE_OK
                )

            db.set_authorizer(authorize)
            try:
                for sql in [
                    "SELECT summary FROM messages",
                    "SELECT summary FROM logical_messages",
                    "SELECT text FROM message_bodies",
                ]:
                    with pytest.raises(sqlite3.DatabaseError):
                        db.execute(sql)
                yield db
            finally:
                db.set_authorizer(None)

    monkeypatch.setattr(store, "connection", guarded)
    result = messages(store, vendor, ref)
    assert result["items"][0]["body_state"] == {
        "state": "stored",
        "stored_chars": 11,
        "source_chars": 11,
    }
    assert "hidden text" not in str(result)


def test_multiple_locations_of_one_source_count_as_one_copy(tmp_path):
    store, _, vendor = followed(tmp_path)
    with store.connection() as db:
        db.execute("UPDATE messages SET rfc_message_id='same@vendor' WHERE message_id='m'")
    seed(
        store,
        "location-copy",
        rfc_message_id="same@vendor",
        locations=frozenset({"inbox", "sent"}),
        to=("vendor@example.com",),
    )
    page = messages(store, vendor)
    assert len(page["items"]) == 1
    assert page["items"][0]["copy_count"] == 2
    assert len(thread_reads.list_copies(store, vendor, selection(store, vendor), "m")["items"]) == 2


def test_body_state_metadata_uses_the_membership_snapshot(tmp_path, monkeypatch):
    store, _, vendor = followed(tmp_path)
    seed(store, "second", received_at=NOW.isoformat())
    ref = selection(store, vendor)
    with store.connection() as db:
        db.execute("PRAGMA journal_mode=WAL")
    original = body_state.project
    injected = False

    def commit_before_projection(db, *args, **kwargs):
        nonlocal injected
        if not injected:
            injected = True
            body_state.record(store, "second", ("new body", 8), fetched_at=NOW)
        return original(db, *args, **kwargs)

    monkeypatch.setattr(body_state, "project", commit_before_projection)
    first = messages(store, vendor, ref)
    assert all(item["body_state"] == {"state": "pending"} for item in first["items"])
    second = messages(store, vendor, ref)
    assert second["items"][1]["body_state"] == {
        "state": "stored",
        "stored_chars": 8,
        "source_chars": 8,
    }


def test_pages_cover_equal_times_invalid_dates_and_logical_max_time_without_repeats(tmp_path):
    store, _, vendor = followed(tmp_path)
    for number in reversed(range(105)):
        seed(store, f"row-{number:03}", received_at=NOW.isoformat())
    seed(store, "later-copy", received_at=(NOW + timedelta(days=1)).isoformat())
    with store.connection() as db:
        db.execute("UPDATE messages SET received_at='invalid' WHERE message_id='row-000'")
        db.execute("UPDATE messages SET logical_of='m' WHERE message_id='later-copy'")
    found, cursor = [], None
    while True:
        page = messages(store, vendor, limit=25, cursor=cursor)
        found.extend(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
        assert len(cursor) <= 4096
    assert [row["message_id"] for row in found] == [f"row-{n:03}" for n in range(1, 105)] + [
        "m",
        "row-000",
    ]
    assert len(found) == len({row["message_id"] for row in found}) == 106
    assert found[-2]["received_at"] == (NOW + timedelta(days=1)).isoformat()
    assert found[-1]["received_at"] is None


@pytest.mark.parametrize("change", ["body", "header", "attribution", "copy", "removal"])
def test_cursor_stales_when_returned_metadata_or_visibility_changes(tmp_path, change):
    store, _, vendor = followed(tmp_path)
    seed(store, "second", received_at=NOW.isoformat())
    ref = selection(store, vendor)
    cursor = messages(store, vendor, ref, limit=1)["next_cursor"]
    if change == "body":
        body_state.record(store, "second", ("body", 4), fetched_at=NOW)
    elif change == "header":
        with store.connection() as db:
            db.execute("UPDATE messages SET sender_name='Renamed' WHERE message_id='second'")
    elif change == "attribution":
        other = store.create_vendor("Other")
        store.add_vendor_address(other, "other@example.com")
        with store.connection() as db:
            db.execute("UPDATE messages SET sender='other@example.com' WHERE message_id='second'")
    elif change == "copy":
        seed(store, "copy", received_at=NOW.isoformat())
        with store.connection() as db:
            db.execute("UPDATE messages SET logical_of='second' WHERE message_id='copy'")
    else:
        local_removal(store, "delete_message", "second")
    with pytest.raises(thread_reads.ThreadReadError) as error:
        messages(store, vendor, ref, limit=1, cursor=cursor)
    assert error.value.code == "stale_cursor"


def test_cursor_binds_vendor_thread_limit_and_operation(tmp_path):
    store, _, vendor = followed(tmp_path)
    seed(store, "second")
    other = store.create_vendor("Linked")
    store.add_vendor_address(other, "linked@example.com")
    seed(store, "linked", sender="linked@example.com")
    ref = selection(store, vendor)
    cursor = messages(store, vendor, ref, limit=1)["next_cursor"]
    for selected, limit in [(other, 1), (vendor, 2)]:
        with pytest.raises(thread_reads.ThreadReadError) as error:
            messages(store, selected, ref, limit=limit, cursor=cursor)
        assert error.value.code == "invalid_request"
    seed(store, "other-thread", thread_id="other")
    other_ref = replace(ref, thread_key="other")
    with pytest.raises(thread_reads.ThreadReadError) as error:
        messages(store, vendor, other_ref, limit=1, cursor=cursor)
    assert error.value.code == "invalid_request"
    thread_cursor = thread_reads.list_threads(store, vendor, limit=1)["next_cursor"]
    with pytest.raises(thread_reads.ThreadReadError) as error:
        messages(store, vendor, ref, limit=1, cursor=thread_cursor)
    assert error.value.code == "invalid_request"


def test_unresolved_members_share_body_state_without_reading_old_content(tmp_path, monkeypatch):
    store, vendor = unresolved_thread(tmp_path)
    ref = selection(store, vendor)
    forbid_body_reads(store, monkeypatch)
    items = messages(store, vendor, ref)["items"]
    assert {item["message_id"] for item in items} == {"m", "unattributed"}
    assert all(item["body_state"] == {"state": "mailbox_identity_unavailable"} for item in items)


def test_held_unfollow_refollow_preserves_headers_but_no_body_query(tmp_path, monkeypatch):
    from test_removal_refusal import held_runtime

    from eom_email_watcher.removal import operation_scope

    store = held_runtime(tmp_path, monkeypatch)
    vendor = store.list_vendors()[0]["vendor_id"]
    ref = selection(store, vendor)
    with operation_scope(store.path) as operation:
        store.remove_vendor_address(vendor, "vendor@example.com", operation=operation)
    store.add_vendor_address(vendor, "vendor@example.com")
    forbid_body_reads(store, monkeypatch)
    items = messages(store, vendor, ref)["items"]
    assert len(items) == 1 and items[0]["message_id"] == "m"
    assert items[0]["body_state"] == {"state": "removal_pending"}
    assert items[0]["copy_count"] == 33


def test_imap_alias_resolution_stales_the_page_and_wrong_scope_never_leaks(tmp_path):
    from test_db import _imap_message

    store, vendor, ref, _ = imap_selection(tmp_path)
    _imap_message(store, "2", "other@x", replies=("root@example.com",))
    alias = replace(ref, thread_key="retired-key")
    with store.connection() as db:
        db.execute(
            "INSERT INTO thread_key_aliases VALUES (?,?)", (alias.thread_key, ref.thread_key)
        )
    first = messages(store, vendor, alias, limit=1)
    assert first["thread"]["thread_ref"] == asdict(ref)
    assert first["next_cursor"]
    for wrong in [
        replace(alias, account_id="foreign"),
        replace(alias, provider="gmail"),
        replace(alias, mailbox_identity_key="a" * 64),
        replace(alias, mailbox_identity_key=None),
    ]:
        with pytest.raises(thread_reads.ThreadReadError) as error:
            messages(store, vendor, wrong)
        assert error.value.code == "not_found"
    with store.connection() as db:
        db.execute(
            "UPDATE messages SET thread_key='survivor' WHERE thread_key=?", (ref.thread_key,)
        )
        db.execute("UPDATE thread_key_aliases SET survivor_key='survivor'")
    with pytest.raises(thread_reads.ThreadReadError) as error:
        messages(store, vendor, alias, limit=1, cursor=first["next_cursor"])
    assert error.value.code == "stale_cursor"


def message_api_request(tmp_path, **change):
    config_path = tmp_path / "config.toml"
    config_path.write_text('timezone = "UTC"\n')
    return {
        "protocol": 1,
        "operation": "threads.messages",
        "config_path": str(config_path),
        "payload": {
            "vendor_id": "11111111-1111-4111-8111-111111111111",
            "thread_ref": asdict(thread_reads.ThreadRef("gmail", "mailbox", None, "native")),
            **change,
        },
    }


@pytest.mark.parametrize(
    "change",
    [
        {"limit": True},
        {"limit": 0},
        {"limit": 101},
        {"thread_ref": {}},
        {"message_id": "unexpected"},
        {"vendor_id": "bad"},
    ],
)
def test_invalid_message_payload_fails_before_config(tmp_path, monkeypatch, change):
    monkeypatch.setattr(
        engine_api, "load_config", lambda _: pytest.fail("invalid request read config")
    )
    result = engine_api._response(message_api_request(tmp_path, **change))
    assert result["error"]["code"] == "invalid_request"


def test_valid_message_payload_reaches_config_in_same_admission_harness(tmp_path, monkeypatch):
    monkeypatch.setattr(
        engine_api, "load_config", lambda _: pytest.fail("valid request reached config")
    )
    with pytest.raises(pytest.fail.Exception, match="valid request reached config"):
        engine_api._response(message_api_request(tmp_path))


def test_message_api_is_read_only_and_rejects_malformed_cursor(tmp_path, monkeypatch):
    from test_service import config

    store, _, vendor = followed(tmp_path)
    ref = selection(store, vendor)
    monkeypatch.setattr(
        engine_api, "load_config", lambda _: replace(config(tmp_path), database_file=store.path)
    )

    def forbidden(*args, **kwargs):
        pytest.fail("message read attempted initialization/runtime/entitlement")

    for name in ["_runtime", "load_runtime", "runtime_from_config"]:
        monkeypatch.setattr(engine_api, name, forbidden)
    monkeypatch.setattr(engine_api.Store, "initialize", forbidden)
    monkeypatch.setattr(engine_api.connect, "require_connect_entitlement", forbidden)
    request = {
        "protocol": 1,
        "operation": "threads.messages",
        "config_path": str(tmp_path / "config.toml"),
        "payload": {"vendor_id": vendor, "thread_ref": asdict(ref)},
    }
    assert engine_api._response(request)["data"]["items"][0]["message_id"] == "m"
    request["payload"]["cursor"] = "bad cursor"
    assert engine_api._response(request)["error"]["code"] == "invalid_request"
