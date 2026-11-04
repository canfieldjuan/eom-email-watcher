"""Scoped M3 copy detail reads through retained logical units."""

import base64
from dataclasses import asdict, replace
from datetime import timedelta

import pytest
from conftest import local_removal
from test_thread_bodies import followed
from test_thread_reconcile import NOW, seed

from eom_email_watcher import engine_api, thread_reads
from eom_email_watcher.mime import AttachmentDescriptor


def selection(store, vendor):
    ref = thread_reads.list_threads(store, vendor)["items"][0]["thread_ref"]
    return thread_reads.ThreadRef(**ref)


def attachment(part="part", *, position=0, known=True):
    return AttachmentDescriptor(
        part_id=part,
        attachment_id="private-provider-token",
        filename=f"{part}.pdf",
        media_type="application/pdf",
        byte_size=12,
        position=position,
        byte_size_known=known,
    )


def test_location_only_copy_has_own_inventory_but_no_fabricated_summary(tmp_path):
    store, _, vendor = followed(tmp_path)
    with store.connection() as db:
        db.execute("UPDATE messages SET rfc_message_id='m@vendor' WHERE message_id='m'")
    seed(
        store,
        "location-copy",
        rfc_message_id="m@vendor",
        locations=frozenset({"inbox", "sent"}),
        to=("vendor@example.com",),
    )
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM logical_messages").fetchone()[0] == 1
        db.execute("UPDATE messages SET summary='Saved summary' WHERE message_id='m'")
    store.replace_attachments("m", [attachment()], source_provider_message_id="location-copy")
    ref = selection(store, vendor)
    page = thread_reads.list_copies(store, vendor, ref, "m")
    assert len(page["items"]) == 2
    copies = {row["copy_ref"]: row for row in page["items"]}
    assert copies["m"]["summary"] == "Saved summary"
    assert copies["m"]["attachment_count"] == 0
    assert copies["location-copy"] == {
        "copy_ref": "location-copy",
        "locations": ["inbox", "sent"],
        "summary": None,
        "attachment_count": 1,
    }
    inventory = thread_reads.list_copies(store, vendor, ref, "m", copy_ref="location-copy")
    assert inventory["items"] == [
        {
            "part_id": "part",
            "filename": "part.pdf",
            "media_type": "application/pdf",
            "byte_size": 12,
        }
    ]
    assert "private-provider-token" not in str(page) + str(inventory)


def test_wrong_vendor_message_or_copy_cannot_broaden_a_read(tmp_path):
    store, _, vendor = followed(tmp_path)
    ref = selection(store, vendor)
    other = store.create_vendor("Other")
    seed(store, "outside", thread_id="outside")
    for selected_vendor, message, copy in [
        (other, "m", None),
        (vendor, "outside", None),
        (vendor, "m", "outside"),
    ]:
        with pytest.raises(thread_reads.ThreadReadError) as error:
            thread_reads.list_copies(store, selected_vendor, ref, message, copy_ref=copy)
        assert error.value.code == "not_found"


def test_physical_copy_summary_and_legacy_inventory_stay_with_their_row(tmp_path):
    store, _, vendor = followed(tmp_path)
    seed(store, "duplicate", rfc_message_id="original-copy@example.com")
    with store.connection() as db:
        db.execute(
            "UPDATE messages SET logical_of='m',summary='Duplicate summary' "
            "WHERE message_id='duplicate'"
        )
        db.execute("UPDATE messages SET summary='Canonical summary' WHERE message_id='m'")
    store.replace_attachments("duplicate", [attachment(known=False)])
    ref = selection(store, vendor)
    copies = thread_reads.list_copies(store, vendor, ref, "m")["items"]
    assert [(c["copy_ref"], c["summary"], c["attachment_count"]) for c in copies] == [
        ("duplicate", "Duplicate summary", 1),
        ("m", "Canonical summary", 0),
    ]
    inventory = thread_reads.list_copies(store, vendor, ref, "m", copy_ref="duplicate")
    assert inventory["items"][0]["byte_size"] is None
    with pytest.raises(thread_reads.ThreadReadError):
        thread_reads.list_copies(store, vendor, ref, "duplicate")


def test_copy_and_inventory_pages_are_complete_and_bind_their_shape(tmp_path):
    store, _, vendor = followed(tmp_path)
    with store.connection() as db:
        db.execute("UPDATE messages SET rfc_message_id='m@vendor' WHERE message_id='m'")
    for number in range(104):
        seed(store, f"copy-{number:03}", rfc_message_id="m@vendor")
    store.replace_attachments("m", [attachment(f"part-{n:03}", position=n) for n in range(105)])
    ref = selection(store, vendor)
    for copy, field in [(None, "copy_ref"), ("m", "part_id")]:
        found, cursor = [], None
        while True:
            page = thread_reads.list_copies(
                store, vendor, ref, "m", copy_ref=copy, limit=25, cursor=cursor
            )
            found.extend(row[field] for row in page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert len(found) == len(set(found)) == 105
        assert found == sorted(found)
    first = thread_reads.list_copies(store, vendor, ref, "m", limit=25)
    with pytest.raises(thread_reads.ThreadReadError) as error:
        thread_reads.list_copies(
            store, vendor, ref, "m", copy_ref="m", limit=25, cursor=first["next_cursor"]
        )
    assert error.value.code == "invalid_request"
    with store.connection() as db:
        db.execute("UPDATE message_attachments SET filename='Changed.pdf' WHERE part_id='part-000'")
    with pytest.raises(thread_reads.ThreadReadError) as error:
        thread_reads.list_copies(store, vendor, ref, "m", limit=25, cursor=first["next_cursor"])
    assert error.value.code == "stale_cursor"


def imap_selection(tmp_path):
    from test_db import Store, _imap_message

    store = Store(tmp_path / "imap.db")
    store.initialize()
    vendor = store.create_vendor("IMAP vendor")
    store.add_vendor_address(vendor, "a@b.com")
    message = _imap_message(store, "1", "root@example.com")
    return store, vendor, selection(store, vendor), message


def test_imap_alias_rechecks_complete_scope_and_refuses_cycles_or_missing_targets(tmp_path):
    store, vendor, ref, message = imap_selection(tmp_path)
    alias = replace(ref, thread_key="retired-key")
    with store.connection() as db:
        db.execute(
            "INSERT INTO thread_key_aliases VALUES (?,?)", (alias.thread_key, ref.thread_key)
        )
    result = thread_reads.list_copies(store, vendor, alias, message)
    assert result["thread_ref"]["thread_key"] == ref.thread_key
    for wrong in [
        replace(alias, account_id="foreign"),
        replace(alias, mailbox_identity_key="a" * 64),
        replace(alias, mailbox_identity_key=None),
    ]:
        with pytest.raises(thread_reads.ThreadReadError) as error:
            thread_reads.list_copies(store, vendor, wrong, message)
        assert error.value.code == "not_found"
    for target in ["absent", alias.thread_key]:
        with store.connection() as db:
            db.execute("UPDATE thread_key_aliases SET survivor_key=?", (target,))
        with pytest.raises(thread_reads.ThreadReadError) as error:
            thread_reads.list_copies(store, vendor, alias, message)
        assert error.value.code == "not_found"


def test_native_provider_key_is_not_an_imap_alias(tmp_path):
    store, _, vendor = followed(tmp_path)
    ref = selection(store, vendor)
    with store.connection() as db:
        db.execute("INSERT INTO thread_key_aliases VALUES (?, 'unrelated')", (ref.thread_key,))
    assert thread_reads.list_copies(store, vendor, ref, "m")["thread_ref"]["thread_key"] == "native"


@pytest.mark.parametrize("kind", ["delete", "clear", "unfollow"])
def test_held_removal_preserves_bytes_but_applies_copy_eligibility(tmp_path, monkeypatch, kind):
    from test_removal_refusal import held_runtime

    store = held_runtime(tmp_path, monkeypatch)
    vendor = store.list_vendors()[0]["vendor_id"]
    ref = selection(store, vendor)
    with store.connection() as db:
        db.execute("UPDATE messages SET summary='Retained summary' WHERE message_id='m'")
    if kind == "unfollow":
        from eom_email_watcher.removal import operation_scope

        with operation_scope(store.path) as operation:
            store.remove_vendor_address(vendor, "vendor@example.com", operation=operation)
        store.add_vendor_address(vendor, "vendor@example.com")
        result = thread_reads.list_copies(store, vendor, ref, "m", limit=100)
        assert "Retained summary" in str(result)
        assert "private body" not in str(result)
    else:
        local_removal(
            store, "request_removal", kind, {"message_id": "m"} if kind == "delete" else {}
        )
        with pytest.raises(thread_reads.ThreadReadError) as error:
            thread_reads.list_copies(store, vendor, ref, "m")
        assert error.value.code == "not_found"
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 33
        assert db.execute("SELECT text FROM message_bodies").fetchone()[0] == "private body"


@pytest.mark.parametrize(
    "change",
    [
        {"thread_ref": None},
        {"thread_ref": {}},
        {"message_id": ""},
        {"copy_ref": False},
        {"limit": True},
        {"unexpected": 1},
    ],
)
def test_invalid_copy_request_fails_before_config(tmp_path, monkeypatch, change):
    monkeypatch.setattr(
        engine_api, "load_config", lambda _: pytest.fail("invalid request read config")
    )
    result = engine_api._response(
        {
            "protocol": 1,
            "operation": "threads.copies",
            "config_path": str(tmp_path / "config.toml"),
            "payload": {
                "vendor_id": "3de5ea6a-71cc-42a6-87ed-b96c952ad95f",
                "message_id": "m",
                "thread_ref": {
                    "provider": "gmail",
                    "account_id": "mailbox",
                    "mailbox_identity_key": None,
                    "thread_key": "key",
                },
                **change,
            },
        }
    )
    assert result["error"]["code"] == "invalid_request", result


@pytest.mark.parametrize(
    "change",
    [
        {"mailbox_identity_key": "bad"},
        {"provider": "unsupported"},
        {"account_id": ""},
        {"extra": True},
        {"thread_key": "🙂" * 129},
        {"thread_key": "\ud800"},
        {"thread_key": ""},
    ],
)
def test_thread_ref_is_strict_and_utf8_bounded(change):
    with pytest.raises(engine_api.ApiError) as error:
        engine_api._thread_ref(
            {
                "provider": "gmail",
                "account_id": "mailbox",
                "mailbox_identity_key": None,
                "thread_key": "key",
                **change,
            }
        )
    assert error.value.code == "invalid_request"
    ref = engine_api._thread_ref(
        {
            "provider": " Gmail ",
            "account_id": " mailbox ",
            "mailbox_identity_key": None,
            "thread_key": "🙂" * 128,
        }
    )
    assert ref.provider == "gmail" and ref.account_id == "mailbox"


def test_copy_api_is_read_only_and_maps_bad_cursor(tmp_path, monkeypatch):
    from test_service import config

    store, _, vendor = followed(tmp_path)
    ref = selection(store, vendor)
    monkeypatch.setattr(
        engine_api, "load_config", lambda _: replace(config(tmp_path), database_file=store.path)
    )

    def forbidden(*args, **kwargs):
        pytest.fail("copy read initialized storage/runtime or required Connect")

    monkeypatch.setattr(engine_api, "_runtime", forbidden)
    monkeypatch.setattr(engine_api, "load_runtime", forbidden)
    monkeypatch.setattr(engine_api.Store, "initialize", forbidden)
    monkeypatch.setattr(engine_api.connect, "require_connect_entitlement", forbidden)
    request = {
        "protocol": 1,
        "operation": "threads.copies",
        "config_path": str(tmp_path / "config.toml"),
        "payload": {"vendor_id": vendor, "thread_ref": asdict(ref), "message_id": "m"},
    }
    assert engine_api._response(request)["ok"]
    request["payload"]["cursor"] = base64.urlsafe_b64encode(b"{").decode()
    assert engine_api._response(request)["error"]["code"] == "invalid_request"


def test_alias_resolution_change_stales_an_existing_inventory_cursor(tmp_path):
    store, vendor, ref, message = imap_selection(tmp_path)
    store.replace_attachments(message, [attachment("one"), attachment("two", position=1)])
    alias = replace(ref, thread_key="retired-key")
    with store.connection() as db:
        db.execute(
            "INSERT INTO thread_key_aliases VALUES (?,?)", (alias.thread_key, ref.thread_key)
        )
    copy = thread_reads.list_copies(store, vendor, alias, message)["items"][0]["copy_ref"]
    first = thread_reads.list_copies(store, vendor, alias, message, copy_ref=copy, limit=1)
    # A merge changes the surviving ref while this canonical member remains visible.
    with store.connection() as db:
        db.execute(
            "UPDATE messages SET thread_key='surviving-key' WHERE thread_key=?", (ref.thread_key,)
        )
        db.execute("UPDATE thread_key_aliases SET survivor_key='surviving-key'")
    with pytest.raises(thread_reads.ThreadReadError) as error:
        thread_reads.list_copies(
            store, vendor, alias, message, copy_ref=copy, limit=1, cursor=first["next_cursor"]
        )
    assert error.value.code == "stale_cursor"


def test_copy_metadata_uses_the_membership_snapshot(tmp_path, monkeypatch):
    store, _, vendor = followed(tmp_path)
    ref = selection(store, vendor)
    with store.connection() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("UPDATE messages SET summary='Before' WHERE message_id='m'")
    projection = thread_reads._copy_projection

    def commit_between_reads(db, message):
        with store.connection() as writer:
            writer.execute("UPDATE messages SET summary='After' WHERE message_id='m'")
        return projection(db, message)

    monkeypatch.setattr(thread_reads, "_copy_projection", commit_between_reads)
    assert thread_reads.list_copies(store, vendor, ref, "m")["items"][0]["summary"] == "Before"
    assert thread_reads.list_copies(store, vendor, ref, "m")["items"][0]["summary"] == "After"


def test_relationship_change_stales_copy_cursor_without_changing_its_inventory(tmp_path):
    store, _, vendor = followed(tmp_path)
    other = store.create_vendor("Other owner")
    store.add_vendor_address(other, "other@example.com")
    seed(
        store,
        "older",
        sender="other@example.com",
        received_at=(NOW - timedelta(days=3)).isoformat(),
    )
    store.replace_attachments("m", [attachment("one"), attachment("two", position=1)])
    ref = selection(store, vendor)
    first = thread_reads.list_copies(store, vendor, ref, "m", copy_ref="m", limit=1)
    with store.connection() as db:
        db.execute("UPDATE messages SET sender='vendor@example.com' WHERE message_id='older'")
    with pytest.raises(thread_reads.ThreadReadError) as error:
        thread_reads.list_copies(
            store, vendor, ref, "m", copy_ref="m", limit=1, cursor=first["next_cursor"]
        )
    assert error.value.code == "stale_cursor"


def test_unresolved_legacy_identity_reads_only_its_retained_copy_summary(tmp_path):
    from test_db import _upgraded_completed_legacy_message

    store, message, _ = _upgraded_completed_legacy_message(tmp_path)
    vendor = store.create_vendor("Legacy vendor")
    store.add_vendor_address(vendor, "a@b.com")
    with store.connection() as db:
        db.execute(
            "UPDATE mail_accounts SET legacy_identity_status='unresolved', "
            "legacy_identity_key=NULL, mailbox_identity_key=NULL"
        )
    ref = selection(store, vendor)
    assert ref.mailbox_identity_key is None
    result = thread_reads.list_copies(store, vendor, ref, message)
    assert result["thread_ref"]["mailbox_identity_key"] is None
    assert result["items"][0]["summary"] == "Already completed"
