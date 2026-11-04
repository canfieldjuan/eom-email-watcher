"""M3 body reads through real visibility, identity, follow and removal owners."""

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, replace

import pytest
from conftest import local_removal
from test_thread_bodies import followed
from test_thread_copy_reads import imap_selection, selection
from test_thread_reconcile import KEY, NOW, seed, state_store

from eom_email_watcher import body_state, engine_api, thread_reads


def body(store, vendor, message="m", ref=None):
    return thread_reads.read_body(store, vendor, ref or selection(store, vendor), message)["body"]


def forbid_body_reads(store, monkeypatch, *, text_only=False):
    original = store.connection

    @contextmanager
    def guarded():
        with original() as db:

            def authorize(action, table, column, *_):
                blocked = table in {"message_bodies", "message_body_unavailable"}
                if text_only:
                    blocked = table == "message_bodies" and column == "text"
                return (
                    sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_READ and blocked
                    else sqlite3.SQLITE_OK
                )

            db.set_authorizer(authorize)
            try:
                yield db
            finally:
                db.set_authorizer(None)

    monkeypatch.setattr(store, "connection", guarded)


@pytest.mark.parametrize(
    "text,source_chars", [("", 0), ("<literal>\n> quote", 17), ("x" * 200_000, 200_004)]
)
def test_stored_empty_full_and_partial_bodies_preserve_text_and_storage_lengths(
    tmp_path, text, source_chars
):
    store, _, vendor = followed(tmp_path)
    body_state.record(store, "m", (text, source_chars), fetched_at=NOW)
    assert body(store, vendor) == {
        "state": "stored",
        "text": text,
        "stored_chars": len(text),
        "source_chars": source_chars,
    }


@pytest.mark.parametrize("reason", [None, "source_gone", "outside_folders"])
def test_unstored_states_return_only_the_discriminator(tmp_path, reason):
    store, _, vendor = followed(tmp_path)
    if reason:
        body_state.record(store, "m", None, fetched_at=NOW, reason=reason)
    assert body(store, vendor) == {"state": reason or "pending"}


def test_metadata_projection_uses_shared_state_without_selecting_text(tmp_path, monkeypatch):
    store, _, vendor = followed(tmp_path)
    body_state.record(store, "m", ("private text", 25), fetched_at=NOW)
    ref = selection(store, vendor)
    forbid_body_reads(store, monkeypatch, text_only=True)
    with thread_reads.snapshot(store) as db:
        assert body_state.project(db, "m", mailbox_identity_key=ref.mailbox_identity_key) == {
            "state": "stored",
            "stored_chars": 12,
            "source_chars": 25,
        }


def unresolved_thread(tmp_path):
    from test_db import _reset_to_schema_28

    store, _ = state_store(tmp_path)
    seed(store)
    seed(store, "unattributed", sender="other@example.com")
    with store.connection() as db:
        db.execute("DROP TRIGGER messages_admission_provenance_immutable")
        db.execute(
            "UPDATE messages SET mailbox_identity_key=NULL,admission_kind=NULL,"
            "admission_selector_id=NULL,admission_display_name=NULL,"
            "admission_mailbox_identity_key=NULL,admitted_at=NULL"
        )
        db.execute(
            "UPDATE mail_accounts SET legacy_identity_status='unresolved',"
            "legacy_identity_key=NULL,mailbox_identity_key=NULL"
        )
    _reset_to_schema_28(store)
    store.initialize()
    vendor = store.create_vendor("Vendor")
    store.add_vendor_address(vendor, "vendor@example.com")
    return store, vendor


def test_unresolved_attributed_and_unattributed_members_never_read_old_bodies_or_markers(
    tmp_path, monkeypatch
):
    store, vendor = unresolved_thread(tmp_path)
    ref = selection(store, vendor)
    assert ref.mailbox_identity_key is None
    with store.connection() as db:
        assert dict(db.execute("SELECT message_id,vendor_id FROM message_derived")) == {
            "m": vendor,
            "unattributed": None,
        }
        assert not db.execute("SELECT 1 FROM thread_follow").fetchone()
        db.execute(
            "INSERT INTO message_bodies VALUES ('m','held old text',13,13,?)", (NOW.isoformat(),)
        )
        db.execute("INSERT INTO message_body_unavailable VALUES ('unattributed','source_gone')")
    forbid_body_reads(store, monkeypatch)
    for message in ["m", "unattributed"]:
        assert body(store, vendor, message, ref) == {"state": "mailbox_identity_unavailable"}
        with thread_reads.snapshot(store) as db:
            assert body_state.project(db, message, mailbox_identity_key=None) == {
                "state": "mailbox_identity_unavailable"
            }


def test_proven_legacy_identity_uses_normal_body_rules_and_old_null_selector_stops_matching(
    tmp_path,
):
    store, vendor = unresolved_thread(tmp_path)
    old = selection(store, vendor)
    store.reconcile_mailbox_identity(
        "gmail", "mailbox", KEY, legacy_status="continuity_proven", preserve_cursor=True
    )
    ref = selection(store, vendor)
    assert ref.mailbox_identity_key == KEY
    for message in ["m", "unattributed"]:
        assert body(store, vendor, message, ref) == {"state": "pending"}
        body_state.record(store, message, ("resolved", 8), fetched_at=NOW)
        assert body(store, vendor, message, ref)["text"] == "resolved"
        with pytest.raises(thread_reads.ThreadReadError, match="not found"):
            body(store, vendor, message, old)


@pytest.mark.parametrize("kind", ["delete", "clear", "unfollow"])
def test_held_33_row_removal_precedes_bytes_and_refollow(tmp_path, monkeypatch, kind):
    from test_removal_refusal import held_runtime

    store = held_runtime(tmp_path, monkeypatch)
    vendor = store.list_vendors()[0]["vendor_id"]
    ref = selection(store, vendor)
    if kind == "unfollow":
        from eom_email_watcher.removal import operation_scope

        with operation_scope(store.path) as operation:
            store.remove_vendor_address(vendor, "vendor@example.com", operation=operation)
        store.add_vendor_address(vendor, "vendor@example.com")
    else:
        local_removal(
            store, "request_removal", kind, {"message_id": "m"} if kind == "delete" else {}
        )
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 33
        assert db.execute("SELECT text FROM message_bodies").fetchone()[0] == "private body"
    forbid_body_reads(store, monkeypatch)
    if kind == "unfollow":
        assert body(store, vendor, ref=ref) == {"state": "removal_pending"}
    else:
        with pytest.raises(thread_reads.ThreadReadError) as error:
            body(store, vendor, ref=ref)
        assert error.value.code == "not_found"


def test_body_only_barrier_precedes_unresolved_identity(tmp_path, monkeypatch):
    from test_removal_refusal import held_runtime

    store = held_runtime(tmp_path, monkeypatch)
    vendor = store.list_vendors()[0]["vendor_id"]
    from eom_email_watcher.removal import operation_scope

    with operation_scope(store.path) as operation:
        store.remove_vendor_address(vendor, "vendor@example.com", operation=operation)
    store.add_vendor_address(vendor, "vendor@example.com")
    # Exercise the shared projection's precedence directly: CONTENT must win
    # before the identity branch can supply its text-free state.
    forbid_body_reads(store, monkeypatch)
    with thread_reads.snapshot(store) as db:
        assert body_state.project(db, "m", mailbox_identity_key=None) == {
            "state": "removal_pending"
        }


def test_no_summary_or_sibling_body_fallback_and_no_physical_duplicate_selector(tmp_path):
    store, _, vendor = followed(tmp_path)
    seed(store, "duplicate", rfc_message_id="duplicate@example.com")
    with store.connection() as db:
        db.execute(
            "UPDATE messages SET logical_of='m',summary='not a body' WHERE message_id='duplicate'"
        )
        db.execute(
            "INSERT INTO message_bodies VALUES ('duplicate','sibling text',12,12,?)",
            (NOW.isoformat(),),
        )
    assert body(store, vendor) == {"state": "pending"}
    with pytest.raises(thread_reads.ThreadReadError):
        body(store, vendor, "duplicate")


def test_linked_vendor_can_read_an_unattributed_member_of_the_validated_thread(tmp_path):
    store, _, owner = followed(tmp_path)
    linked = store.create_vendor("Linked")
    store.add_vendor_address(linked, "linked@example.com")
    seed(store, "linked", sender="linked@example.com", received_at=NOW.isoformat())
    seed(store, "unattributed", sender="other@example.com")
    body_state.record(store, "unattributed", ("retained", 8), fetched_at=NOW)
    assert thread_reads.list_threads(store, linked)["items"][0]["relation"] == "linked"
    assert body(store, linked, "unattributed") == body(store, owner, "unattributed")
    assert body(store, linked, "unattributed")["text"] == "retained"


def test_noncanonical_content_target_blocks_the_whole_unit(tmp_path, monkeypatch):
    from test_removal_refusal import held_runtime

    from eom_email_watcher import removal

    store = held_runtime(tmp_path, monkeypatch)
    vendor = store.list_vendors()[0]["vendor_id"]
    ref = selection(store, vendor)
    with removal.operation_scope(store.path) as operation:
        store.remove_vendor_address(vendor, "vendor@example.com", operation=operation)
    store.add_vendor_address(vendor, "vendor@example.com")
    with store.connection() as db:
        # Retain a target on a noncanonical source: the read must not assume
        # that only the currently selected row can own a durable barrier.
        db.execute("DELETE FROM message_removal_targets WHERE message_id!='copy-00'")
        source = dict(
            db.execute(
                "SELECT provider,account_id,mailbox_identity_key,provider_message_id "
                "FROM messages WHERE message_id='copy-00'"
            ).fetchone()
        )
        # Freeze only that source, as before a unit gains another canonical
        # member. The normal unfollow fixture had frozen all33 sources per row.
        db.execute(
            "UPDATE message_removal_targets SET source_identities_json=?", (json.dumps([source]),)
        )
        assert removal.CONTENT.message_available(db, "m")
        assert not removal.CONTENT.message_available(db, "copy-00")
    forbid_body_reads(store, monkeypatch)
    assert body(store, vendor, ref=ref) == {"state": "removal_pending"}


def test_broken_follow_cache_fails_before_reading_any_body(tmp_path, monkeypatch):
    store, _, vendor = followed(tmp_path)
    ref = selection(store, vendor)
    body_state.record(store, "m", ("old text", 8), fetched_at=NOW)
    with store.connection() as db:
        db.execute("UPDATE thread_follow SET followed=0,owner_vendor_id=NULL")
    forbid_body_reads(store, monkeypatch)
    with pytest.raises(RuntimeError, match="follow state"):
        body(store, vendor, ref=ref)


def test_wrong_vendor_and_complete_scope_fail_before_body_lookup(tmp_path, monkeypatch):
    store, _, vendor = followed(tmp_path)
    ref = selection(store, vendor)
    other = store.create_vendor("Other")
    forbid_body_reads(store, monkeypatch)
    for selected, scope, message in [
        (other, ref, "m"),
        (vendor, ref, "missing"),
        (vendor, replace(ref, provider="microsoft365"), "m"),
        (vendor, replace(ref, account_id="other"), "m"),
        (vendor, replace(ref, mailbox_identity_key="a" * 64), "m"),
        (vendor, replace(ref, mailbox_identity_key=None), "m"),
        (vendor, replace(ref, thread_key="other"), "m"),
    ]:
        with pytest.raises(thread_reads.ThreadReadError) as error:
            body(store, selected, message, scope)
        assert error.value.code == "not_found"


def test_imap_alias_resolves_in_scope_without_body_fallback(tmp_path):
    store, vendor, ref, message = imap_selection(tmp_path)
    body_state.record(store, message, ("body", 4), fetched_at=NOW)
    alias = replace(ref, thread_key="retired-key")
    with store.connection() as db:
        db.execute(
            "INSERT INTO thread_key_aliases VALUES (?,?)", (alias.thread_key, ref.thread_key)
        )
    result = thread_reads.read_body(store, vendor, alias, message)
    assert result == {
        "thread_ref": asdict(ref),
        "message_id": message,
        "body": {"state": "stored", "text": "body", "stored_chars": 4, "source_chars": 4},
    }


@pytest.mark.parametrize(
    "change",
    [{"text": "different"}, {"stored_chars": 200001}, {"source_chars": -1}, {"reason": "invented"}],
)
def test_corrupt_storage_never_creates_a_successful_body_state(tmp_path, change):
    store, _, vendor = followed(tmp_path)
    body_state.record(store, "m", ("body", 4), fetched_at=NOW)
    with store.connection() as db:
        db.execute("PRAGMA ignore_check_constraints=ON")
        if "reason" in change:
            db.execute("DELETE FROM message_bodies")
            db.execute("INSERT INTO message_body_unavailable VALUES ('m',?)", (change["reason"],))
        else:
            key, value = next(iter(change.items()))
            db.execute(f"UPDATE message_bodies SET {key}=?", (value,))
    with pytest.raises(RuntimeError, match="body"):
        body(store, vendor)


def body_api_request(tmp_path, **change):
    config_path = tmp_path / "config.toml"
    config_path.write_text('timezone = "UTC"\n')
    return {
        "protocol": 1,
        "operation": "threads.body",
        "config_path": str(config_path),
        "payload": {
            "vendor_id": "11111111-1111-4111-8111-111111111111",
            "thread_ref": {
                "provider": "gmail",
                "account_id": "mailbox",
                "mailbox_identity_key": None,
                "thread_key": "native",
            },
            "message_id": "m",
            **change,
        },
    }


@pytest.mark.parametrize(
    "change",
    [
        {"message_id": ""},
        {"thread_ref": {}},
        {"thread_ref": None},
        {"message_id": False},
        {"cursor": "extra"},
        {"limit": 1},
        {"include_text": True},
    ],
)
def test_invalid_body_payload_fails_before_config(tmp_path, monkeypatch, change):
    monkeypatch.setattr(
        engine_api, "load_config", lambda _: pytest.fail("invalid payload read config")
    )
    result = engine_api._response(body_api_request(tmp_path, **change))
    assert result["error"]["code"] == "invalid_request"


def test_valid_body_payload_reaches_config_in_same_admission_harness(tmp_path, monkeypatch):
    monkeypatch.setattr(
        engine_api, "load_config", lambda _: pytest.fail("valid payload reached config")
    )
    with pytest.raises(pytest.fail.Exception, match="valid payload reached config"):
        engine_api._response(body_api_request(tmp_path))


def test_body_api_uses_read_class_and_preserves_one_snapshot(tmp_path, monkeypatch):
    from test_service import config

    store, _, vendor = followed(tmp_path)
    ref = selection(store, vendor)
    body_state.record(store, "m", ("snapshot text", 13), fetched_at=NOW)
    monkeypatch.setattr(
        engine_api, "load_config", lambda _: replace(config(tmp_path), database_file=store.path)
    )

    def forbidden(*args, **kwargs):
        pytest.fail("read initialized runtime/storage or required Connect")

    for name in ["_runtime", "load_runtime", "runtime_from_config"]:
        monkeypatch.setattr(engine_api, name, forbidden)
    monkeypatch.setattr(engine_api.Store, "initialize", forbidden)
    monkeypatch.setattr(engine_api.connect, "require_connect_entitlement", forbidden)
    request = {
        "protocol": 1,
        "operation": "threads.body",
        "config_path": str(tmp_path / "config.toml"),
        "payload": {"vendor_id": vendor, "thread_ref": asdict(ref), "message_id": "m"},
    }
    assert engine_api._response(request)["data"]["body"]["text"] == "snapshot text"
    with store.connection() as db:
        db.execute("PRAGMA journal_mode=WAL")
    original = body_state.project

    def remove_before_projection(db, *args, **kwargs):
        local_removal(store, "delete_message", "m")
        return original(db, *args, **kwargs)

    monkeypatch.setattr(body_state, "project", remove_before_projection)
    assert engine_api._response(request)["data"]["body"]["text"] == "snapshot text"
    assert engine_api._response(request)["error"]["code"] == "not_found"
