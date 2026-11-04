"""M3 reads through real logical-message, identity and removal owners."""

import base64
import json
import sqlite3
from datetime import timedelta

import pytest
from test_thread_bodies import followed
from test_thread_reconcile import KEY, NOW, seed

from eom_email_watcher import engine_api, thread_reads


def test_list_owned_linked_and_logical_copies_without_body_or_summary(tmp_path):
    store, _, owner = followed(tmp_path)
    linked = store.create_vendor("Linked vendor")
    store.add_vendor_address(linked, "linked@example.com")
    seed(store, "reply", sender="linked@example.com", received_at=NOW.isoformat())
    seed(store, "copy", rfc_message_id="copy@example.com", thread_id="copies")
    seed(store, "copy-two", rfc_message_id="copy@example.com", thread_id="copies")
    items = thread_reads.list_threads(store, owner)["items"]
    assert len(items) == 2
    assert [item["message_count"] for item in items] == [2, 1]
    assert all(item["relation"] == "owned" for item in items)
    result = thread_reads.list_threads(store, linked)
    assert len(result["items"]) == 1
    assert result["items"][0]["relation"] == "linked"
    assert result["items"][0]["owner_vendor"] == {"vendor_id": owner, "display_name": "Vendor"}
    assert not {"text", "summary", "body"} & result["items"][0].keys()


def test_account_provider_and_reused_identity_buckets_remain_separate(tmp_path):
    store, _, vendor = followed(tmp_path)
    for number, (provider, account, identity) in enumerate(
        [
            ("gmail", "other", KEY),
            ("gmail", "mailbox", "a" * 64),
            ("microsoft365", "mailbox", KEY),
        ]
    ):
        if store.mail_account(provider, account) is None:
            store.register_mail_account(provider, account, display_name="Retained account")
        store.reconcile_mailbox_identity(provider, account, identity, legacy_status="replacement")
        seed(
            store,
            f"source-{number}",
            provider=provider,
            account_id=account,
            mailbox_identity_key=identity,
        )
    items = thread_reads.list_threads(store, vendor)["items"]
    assert len(items) == 4
    scopes = {tuple(item["thread_ref"].values()) for item in items}
    assert len(scopes) == 4
    selected = thread_reads.list_threads(store, vendor, provider="gmail", account_id="mailbox")
    assert len(selected["items"]) == 2
    assert all(
        (item["thread_ref"]["provider"], item["thread_ref"]["account_id"]) == ("gmail", "mailbox")
        for item in selected["items"]
    )
    assert {item["thread_ref"]["mailbox_identity_key"] for item in selected["items"]} == {
        KEY,
        "a" * 64,
    }


def test_upgraded_legacy_bucket_remains_visible_and_uses_proven_continuity(tmp_path):
    from test_db import _upgraded_completed_legacy_message

    store, _, identity = _upgraded_completed_legacy_message(tmp_path)
    vendor = store.create_vendor("Legacy vendor")
    store.add_vendor_address(vendor, "a@b.com")
    with store.connection() as db:
        db.execute(
            "UPDATE mail_accounts SET legacy_identity_status='unresolved', "
            "legacy_identity_key=NULL,mailbox_identity_key=NULL"
        )
    with store.connection() as db:
        assert not db.execute("SELECT 1 FROM thread_follow").fetchone()
    item = thread_reads.list_threads(store, vendor)["items"][0]
    assert item["thread_ref"]["mailbox_identity_key"] is None
    assert item["relation"] == "owned"
    assert item["owner_vendor"]["vendor_id"] == vendor
    store.reconcile_mailbox_identity(
        "imap", "imap-account", identity, legacy_status="continuity_proven", preserve_cursor=True
    )
    assert (
        thread_reads.list_threads(store, vendor)["items"][0]["thread_ref"]["mailbox_identity_key"]
        == identity
    )


def test_pages_cover_tied_dates_and_invalid_legacy_dates_once(tmp_path):
    store, _, vendor = followed(tmp_path)
    for number in range(105):
        seed(store, f"row-{number:03}", thread_id=f"thread-{number:03}")
    with store.connection() as db:
        db.execute("UPDATE messages SET received_at='legacy-invalid' WHERE message_id='row-000'")
    pages, cursor = [], None
    while True:
        page = thread_reads.list_threads(store, vendor, limit=25, cursor=cursor)
        pages.extend(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(pages) == 106
    assert len({json.dumps(row["thread_ref"], sort_keys=True) for row in pages}) == 106
    assert pages[-1]["latest_received_at"] is None
    assert [row["thread_ref"]["thread_key"] for row in pages[:3]] == [
        "native",
        "thread-001",
        "thread-002",
    ]


def test_projection_change_stales_cursor_and_wrong_selector_refuses_it(tmp_path):
    store, _, vendor = followed(tmp_path)
    seed(store, "second", thread_id="second")
    first = thread_reads.list_threads(store, vendor, limit=1)
    cursor = first["next_cursor"]
    other = store.create_vendor("Other")
    with pytest.raises(thread_reads.ThreadReadError) as error:
        thread_reads.list_threads(store, other, limit=1, cursor=cursor)
    assert error.value.code == "invalid_request"
    seed(store, "third", thread_id="third", received_at=(NOW + timedelta(days=1)).isoformat())
    with pytest.raises(thread_reads.ThreadReadError) as error:
        thread_reads.list_threads(store, vendor, limit=1, cursor=cursor)
    assert error.value.code == "stale_cursor"


def test_cursor_cannot_change_limit_or_invent_a_last_position(tmp_path):
    store, _, vendor = followed(tmp_path)
    seed(store, "second", thread_id="second")
    cursor = thread_reads.list_threads(store, vendor, limit=1)["next_cursor"]
    with pytest.raises(thread_reads.ThreadReadError) as error:
        thread_reads.list_threads(store, vendor, limit=2, cursor=cursor)
    assert error.value.code == "invalid_request"
    token = json.loads(base64.urlsafe_b64decode(cursor))
    token["last"] = "0" * 64
    forged = base64.urlsafe_b64encode(json.dumps(token).encode()).decode()
    with pytest.raises(thread_reads.ThreadReadError) as error:
        thread_reads.list_threads(store, vendor, limit=1, cursor=forged)
    assert error.value.code == "invalid_request"


@pytest.mark.parametrize("cursor", ["", "!bad", "x" * 4097, 12, True, "e30="])
def test_cursor_is_strict_and_bounded(tmp_path, cursor):
    store, _, vendor = followed(tmp_path)
    with pytest.raises(thread_reads.ThreadReadError) as error:
        thread_reads.list_threads(store, vendor, cursor=cursor)
    assert error.value.code == "invalid_request"


def test_deeply_nested_cursor_is_an_invalid_request(tmp_path):
    store, _, vendor = followed(tmp_path)
    cursor = base64.urlsafe_b64encode(b"[" * 1100 + b"]" * 1100).decode()
    assert len(cursor) < 4096
    with pytest.raises(thread_reads.ThreadReadError) as error:
        thread_reads.list_threads(store, vendor, cursor=cursor)
    assert error.value.code == "invalid_request"


@pytest.mark.parametrize("raw", [b"{", b"\xff", b'{"version":1,"version":1}'])
def test_cursor_rejects_invalid_json_utf8_and_duplicate_fields(tmp_path, raw):
    store, _, vendor = followed(tmp_path)
    with pytest.raises(thread_reads.ThreadReadError) as error:
        thread_reads.list_threads(store, vendor, cursor=base64.urlsafe_b64encode(raw).decode())
    assert error.value.code == "invalid_request"


def test_api_is_read_class_without_runtime_initialization_or_entitlement(tmp_path, monkeypatch):
    from dataclasses import replace

    from test_service import config

    store, _, vendor = followed(tmp_path)
    monkeypatch.setattr(
        engine_api, "load_config", lambda _: replace(config(tmp_path), database_file=store.path)
    )

    def forbidden(*args, **kwargs):
        pytest.fail("read attempted initialization, provider/model runtime, or entitlement")

    monkeypatch.setattr(engine_api, "_runtime", forbidden)
    monkeypatch.setattr(engine_api, "load_runtime", forbidden)
    monkeypatch.setattr(engine_api, "runtime_from_config", forbidden)
    monkeypatch.setattr(engine_api.Store, "initialize", forbidden)
    monkeypatch.setattr(engine_api.connect, "require_connect_entitlement", forbidden)
    with store.connection() as db:
        before = list(db.iterdump())
    response = engine_api._response(
        {
            "protocol": 1,
            "operation": "threads.list",
            "config_path": str(tmp_path / "config.toml"),
            "payload": {"vendor_id": vendor},
        }
    )
    assert response["ok"], response
    assert response["data"]["items"][0]["message_count"] == 1
    with store.connection() as db:
        assert list(db.iterdump()) == before


@pytest.mark.parametrize(
    "operation,payload",
    [
        ("inbox.delete", {"message_id": "m"}),
        ("inbox.clear", {}),
    ],
)
def test_held_removal_hides_thread_metadata_while_33_rows_remain(
    tmp_path, monkeypatch, operation, payload
):
    from test_removal_refusal import held_runtime

    store = held_runtime(tmp_path, monkeypatch)
    vendor = store.list_vendors()[0]["vendor_id"]
    assert thread_reads.list_threads(store, vendor)["items"][0]["message_count"] == 1
    result = engine_api._response({"protocol": 1, "operation": operation, "payload": payload})
    assert result["ok"] and result["data"]["outcome"] == "deferred", result
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 33
        assert db.execute("SELECT text FROM message_bodies").fetchone()[0] == "private body"
    assert thread_reads.list_threads(store, vendor) == {"items": [], "next_cursor": None}


def test_projection_is_one_snapshot_across_a_committed_writer(tmp_path, monkeypatch):
    store, _, vendor = followed(tmp_path)
    with store.connection() as db:
        db.execute("PRAGMA journal_mode=WAL")
    original = thread_reads._members
    injected = False

    def commit_before_members(db, *scope):
        nonlocal injected
        if not injected:
            injected = True
            seed(store, "late", thread_id="late", subject="New arrival")
        return original(db, *scope)

    monkeypatch.setattr(thread_reads, "_members", commit_before_members)
    assert len(thread_reads.list_threads(store, vendor)["items"]) == 1
    assert len(thread_reads.list_threads(store, vendor)["items"]) == 2
    with (
        pytest.raises(sqlite3.OperationalError, match="readonly"),
        thread_reads.snapshot(store) as db,
    ):
        db.execute("DELETE FROM vendors")


@pytest.mark.parametrize(
    "delta",
    [
        {"unknown": 1},
        {"vendor_id": "not-a-uuid"},
        {"provider": "gmail"},
        {"account_id": "mailbox"},
        {"provider": "absent", "account_id": "mailbox"},
        {"provider": "gmail", "account_id": "x" * 129},
        {"limit": True},
        {"limit": 0},
        {"limit": 101},
        {"limit": 1.5},
        {"limit": None},
    ],
)
def test_invalid_api_selectors_fail_before_storage(tmp_path, monkeypatch, delta):
    def forbidden(*args):
        pytest.fail("invalid selector reached configuration/storage")

    monkeypatch.setattr(engine_api, "load_config", forbidden)
    result = engine_api._response(
        {
            "protocol": 1,
            "operation": "threads.list",
            "config_path": str(tmp_path / "config.toml"),
            "payload": {"vendor_id": "3de5ea6a-71cc-42a6-87ed-b96c952ad95f", **delta},
        }
    )
    assert result["error"]["code"] == "invalid_request", result


def test_missing_vendor_does_not_broaden_selection_or_create_a_database(tmp_path):
    from eom_email_watcher.db import Store

    path = tmp_path / "missing.sqlite3"
    with pytest.raises(thread_reads.ThreadReadError) as error:
        thread_reads.list_threads(Store(path), "3de5ea6a-71cc-42a6-87ed-b96c952ad95f")
    assert error.value.code == "not_found"
    assert not path.exists()
    store, _, _ = followed(tmp_path)
    with pytest.raises(thread_reads.ThreadReadError) as error:
        thread_reads.list_threads(store, "3de5ea6a-71cc-42a6-87ed-b96c952ad95f")
    assert error.value.code == "not_found"


def test_older_member_replacement_stales_cursor_even_with_equal_thread_metadata(tmp_path):
    store, _, vendor = followed(tmp_path)
    seed(store, "newest", received_at=NOW.isoformat())
    seed(store, "other", thread_id="other")
    before = thread_reads.list_threads(store, vendor, limit=1)
    with store.connection() as db:
        db.execute("UPDATE messages SET subject='Different retained header' WHERE message_id='m'")
    assert thread_reads.list_threads(store, vendor, limit=1)["items"] == before["items"]
    with pytest.raises(thread_reads.ThreadReadError) as error:
        thread_reads.list_threads(store, vendor, limit=1, cursor=before["next_cursor"])
    assert error.value.code == "stale_cursor"
