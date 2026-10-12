"""M2.2 derived state through real capture and transaction owners."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_db import Store, _upgraded_completed_legacy_message

from eom_email_watcher import db as db_module

STAMP = "2026-10-11T12:00:00+00:00"


def seed(store, message_id="m", **overrides):
    values = dict(
        message_id=message_id,
        provider="gmail",
        account_id="mailbox",
        provider_message_id=message_id,
        thread_id="thread",
        sender="vendor@example.com",
        sender_name=None,
        subject="Invoice",
        received_at=STAMP,
    )
    values.update(overrides)
    store.add_message(**values)


def derived(store, message_id="m"):
    with store.connection() as db:
        row = db.execute(
            "SELECT direction, vendor_id, match FROM message_derived WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        return dict(row) if row else None


def follow(store):
    with store.connection() as db:
        return [dict(row) for row in db.execute("SELECT * FROM thread_follow ORDER BY thread_key")]


def test_capture_immediately_derives_vendor_and_follow(tmp_path: Path):
    store = Store(tmp_path / "mail.db")
    store.initialize()
    vendor = store.create_vendor("Vendor")
    store.add_vendor_address(vendor, "vendor@example.com")
    seed(store)
    assert derived(store) == {"direction": "inbound", "vendor_id": vendor, "match": "exact"}
    assert follow(store)[0]["owner_vendor_id"] == vendor
    assert follow(store)[0]["followed"] == 1


def test_vendor_removal_unfollows_and_clears_watermark(tmp_path: Path):
    store = Store(tmp_path / "mail.db")
    store.initialize()
    vendor = store.create_vendor("Vendor")
    store.add_vendor_address(vendor, "vendor@example.com")
    seed(store)
    row = follow(store)[0]
    with store.connection() as db:
        db.execute(
            "INSERT INTO thread_watermarks VALUES (?, ?, ?, ?, ?)",
            (
                *[row[k] for k in ("provider", "account_id", "mailbox_identity_key", "thread_key")],
                STAMP,
            ),
        )
    store.remove_vendor_address(vendor, "vendor@example.com")
    assert derived(store)["vendor_id"] is None
    assert follow(store)[0]["followed"] == 0
    with store.connection() as db:
        assert not db.execute("SELECT * FROM thread_watermarks").fetchall()


def test_sent_location_changes_direction_and_recipient_attribution(tmp_path: Path):
    store = Store(tmp_path / "mail.db")
    store.initialize()
    vendor = store.create_vendor("Vendor")
    store.add_vendor_address(vendor, "to@example.com")
    seed(store, to=("to@example.com",))
    assert derived(store)["direction"] == "inbound"
    source = store.message_source("m")
    store.record_message_location(
        provider="gmail",
        account_id="mailbox",
        mailbox_identity_key=source.mailbox_identity_key,
        provider_message_id="m",
        locations=frozenset({"sent"}),
        headers_observed=True,
        now=datetime.now(UTC),
    )
    assert derived(store) == {"direction": "outbound", "vendor_id": vendor, "match": "exact"}


def test_deleted_last_message_drops_follow_and_derived(tmp_path: Path):
    store = Store(tmp_path / "mail.db")
    store.initialize()
    seed(store)
    store.delete_message("m")
    assert derived(store) is None
    assert follow(store) == []


def test_connected_identity_is_excluded_from_vendor_attribution(tmp_path: Path):
    store = Store(tmp_path / "mail.db")
    store.initialize()
    vendor = store.create_vendor("Vendor")
    store.add_vendor_address(vendor, "vendor@example.com")
    seed(store)
    store.update_mail_account_identity(
        "gmail", "mailbox", display_name="Mail", address="vendor@example.com"
    )
    assert derived(store)["vendor_id"] is None
    assert follow(store)[0]["followed"] == 0


@pytest.mark.parametrize(
    "table,column",
    [
        ("messages", "sender"),
        ("message_locations", "location"),
        ("message_recipients", "address"),
        ("mail_accounts", "address"),
        ("vendors", "display_name"),
        ("vendor_addresses", "address"),
        ("vendor_domains", "domain"),
    ],
)
def test_each_input_table_writer_reaches_owner_before_commit(tmp_path, monkeypatch, table, column):
    store = Store(tmp_path / "mail.db")
    store.initialize()
    seed(store)
    if table == "vendor_domains":
        # The future opt-in domain writer must reach the same seam too.
        with store.connection() as db:
            db.execute("CREATE TABLE vendor_domains(domain TEXT PRIMARY KEY, vendor_id TEXT)")
    recompute = db_module._recompute_derived
    seen = []

    def observe(db, changed):
        if table in changed:
            assert db.in_transaction
            seen.append(table)
        recompute(db, changed)

    monkeypatch.setattr(db_module, "_recompute_derived", observe)
    with store.connection() as db:
        db.execute(f"UPDATE {table} SET {column}={column}")
    assert seen == [table]


def test_owner_failure_rolls_back_input_and_cache_together(tmp_path, monkeypatch):
    store = Store(tmp_path / "mail.db")
    store.initialize()
    vendor = store.create_vendor("Vendor")
    store.add_vendor_address(vendor, "vendor@example.com")
    seed(store)

    def fail(db, changed):
        if "vendor_addresses" in changed:
            raise RuntimeError("derived state failed")

    monkeypatch.setattr(db_module, "_recompute_derived", fail)
    with pytest.raises(RuntimeError, match="derived state failed"):
        store.remove_vendor_address(vendor, "vendor@example.com")
    assert derived(store)["vendor_id"] == vendor
    assert store.vendor_for_address("vendor@example.com")["vendor_id"] == vendor
    assert follow(store)[0]["followed"] == 1


def test_capture_recomputes_only_affected_thread(tmp_path, monkeypatch):
    store = Store(tmp_path / "mail.db")
    store.initialize()
    seed(store, "unrelated", thread_id="other")
    original = db_module._thread_member
    touched = []

    def observe(db, row, vendor):
        touched.append(row["message_id"])
        return original(db, row, vendor)

    monkeypatch.setattr(db_module, "_thread_member", observe)
    seed(store, "new", thread_id="new-thread")
    assert touched == ["new"]
    assert len(follow(store)) == 2


@pytest.mark.parametrize("order", [("early", "late"), ("late", "early")])
def test_follow_owner_uses_received_time_not_arrival(tmp_path, order):
    store = Store(tmp_path / "mail.db")
    store.initialize()
    early = store.create_vendor("Early")
    late = store.create_vendor("Late")
    store.add_vendor_address(early, "early@example.com")
    store.add_vendor_address(late, "late@example.com")
    for name in order:
        seed(
            store,
            name,
            sender=f"{name}@example.com",
            received_at="2026-10-10T12:00:00+00:00" if name == "early" else STAMP,
        )
    assert follow(store)[0]["owner_vendor_id"] == early
    store.delete_message("early")
    assert follow(store)[0]["owner_vendor_id"] == late


@pytest.mark.parametrize("both_followed", [True, False])
def test_imap_bridge_reads_all_components_and_recomputes_one_owner(tmp_path, both_followed):
    store = Store(tmp_path / "mail.db")
    store.initialize()
    vendors = {}
    for uid, name, received in (
        ("1", "later", STAMP),
        ("2", "earlier", "2026-10-10T12:00:00+00:00"),
    ):
        vendor = vendors[name] = store.create_vendor(name)
        if both_followed or name == "later":
            store.add_vendor_address(vendor, f"{name}@example.com")
        seed(
            store,
            uid,
            provider="imap",
            sender=f"{name}@example.com",
            rfc_message_id=f"{name}@root",
            received_at=received,
        )
    rows = follow(store)
    assert len(rows) == 2
    for row in rows:
        with store.connection() as db:
            db.execute(
                "INSERT INTO thread_watermarks VALUES (?, ?, ?, ?, ?)",
                (
                    *[
                        row[k]
                        for k in ("provider", "account_id", "mailbox_identity_key", "thread_key")
                    ],
                    STAMP,
                ),
            )
    source = store.message_source("1")
    lookup = store.followed_thread_for_metadata(
        provider="imap",
        account_id="mailbox",
        mailbox_identity_key=source.mailbox_identity_key,
        thread_id=None,
        rfc_message_id="bridge@root",
        reply_ids=("later@root", "earlier@root"),
    )
    expected_owner = "earlier" if both_followed else "later"
    assert lookup[1] == expected_owner
    assert len(follow(store)) == 2  # lookup is read-only
    from types import SimpleNamespace

    from eom_email_watcher.service import match_mailbox_admission

    decision = match_mailbox_admission(
        metadata=SimpleNamespace(
            sender="unknown@example.com", labels=frozenset(), locations=frozenset({"inbox"})
        ),
        provider="imap",
        account_id="mailbox",
        mailbox_identity_key=source.mailbox_identity_key,
        exact_senders={},
        label_selectors=(),
        admitted_at=datetime.now(UTC),
        vendor_index=store.vendor_index(),
        followed_thread=lookup,
        gated_allowed=True,
    )
    assert decision.kind == "thread_follow"
    seed(
        store,
        "bridge",
        provider="imap",
        sender="unknown@example.com",
        rfc_message_id="bridge@root",
        reply_ids=("later@root", "earlier@root"),
        admission=decision.provenance(),
    )
    with store.connection() as db:
        assert (
            db.execute("SELECT admission_kind FROM messages WHERE message_id='bridge'").fetchone()[
                0
            ]
            == "thread_follow"
        )
    assert len(follow(store)) == 1
    assert follow(store)[0]["owner_vendor_id"] == vendors[expected_owner]
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM thread_watermarks").fetchone()[0] == 0


def test_followed_old_thread_is_kept_without_widening_fetch_cutoff(tmp_path):
    store = Store(tmp_path / "mail.db")
    store.initialize()
    vendor = store.create_vendor("Vendor")
    store.add_vendor_address(vendor, "vendor@example.com")
    seed(store, received_at="2025-01-01T00:00:00+00:00")
    seed(
        store,
        "other",
        thread_id="unfollowed",
        sender="unknown@example.com",
        received_at="2025-01-01T00:00:00+00:00",
    )
    now = datetime(2026, 10, 11, tzinfo=UTC)
    assert store.purge(180, now=now) == 1
    assert derived(store) is not None
    assert derived(store, "other") is None
    assert (
        store.logical_messages_within_cutoff(("m",), cutoff=now - timedelta(days=180), now=now)
        == frozenset()
    )
    store.remove_vendor_address(vendor, "vendor@example.com")
    assert store.purge(180, now=now) == 1
    assert follow(store) == []


def test_proven_legacy_identity_joins_cache_in_its_transaction(tmp_path):
    store, root, identity = _upgraded_completed_legacy_message(tmp_path)
    with store.connection() as db:
        db.execute(
            "UPDATE mail_accounts SET mailbox_identity_key=NULL, "
            "legacy_identity_status='unresolved', legacy_identity_key=NULL"
        )
    assert follow(store) == []
    store.reconcile_mailbox_identity(
        "imap", "imap-account", identity, legacy_status="continuity_proven", preserve_cursor=True
    )
    assert follow(store)[0]["mailbox_identity_key"] == identity
    assert derived(store, root) is not None


def test_native_thread_caches_do_not_mix_replaced_mailboxes(tmp_path):
    store = Store(tmp_path / "mail.db")
    store.initialize()
    vendor = store.create_vendor("Vendor")
    store.add_vendor_address(vendor, "vendor@example.com")
    seed(store)
    old = store.message_source("m").mailbox_identity_key
    new = "b" * 64
    store.reconcile_mailbox_identity(
        "gmail", "mailbox", new, legacy_status="replacement", preserve_cursor=True
    )
    seed(store, "replacement", sender="unknown@example.com", mailbox_identity_key=new)
    rows = {r["mailbox_identity_key"]: r for r in follow(store)}
    assert rows[old]["followed"] == 1 and rows[new]["followed"] == 0
    assert (
        store.followed_thread_for_metadata(
            provider="gmail", account_id="mailbox", mailbox_identity_key=new, thread_id="thread"
        )
        is None
    )
    assert (
        store.followed_thread_for_metadata(
            provider="gmail", account_id="mailbox", mailbox_identity_key=old, thread_id="thread"
        )[1]
        == "Vendor"
    )


def test_connected_identity_keeps_vendor_record_ownership(tmp_path):
    store = Store(tmp_path / "mail.db")
    store.initialize()
    vendor = store.create_vendor("Vendor")
    store.add_vendor_address(vendor, "vendor@example.com")
    seed(store)
    store.update_mail_account_identity(
        "gmail", "mailbox", display_name="Mail", address="vendor@example.com"
    )
    assert store.vendor_for_address("vendor@example.com") == {
        "vendor_id": vendor,
        "display_name": "Vendor",
    }
    assert derived(store)["vendor_id"] is None


def test_reopen_without_input_changes_does_not_rederive(tmp_path, monkeypatch):
    store = Store(tmp_path / "mail.db")
    store.initialize()
    seed(store)
    original = db_module._thread_member
    touched = []

    def observe(db, row, vendor):
        touched.append(row["message_id"])
        return original(db, row, vendor)

    monkeypatch.setattr(db_module, "_thread_member", observe)
    store.initialize()
    assert touched == []
    assert len(follow(store)) == 1


def test_partial_recompute_materializes_only_changed_follow_rows(tmp_path, monkeypatch):
    import sqlite3
    from contextlib import contextmanager

    store = Store(tmp_path / "mail.db")
    store.initialize()
    for i in range(8):
        seed(store, f"m{i}", thread_id=f"t{i}")
    connection = store.connection
    materialized = []

    @contextmanager
    def observe():
        with connection() as db:

            def row_factory(cursor, values):
                if tuple(c[0] for c in cursor.description) == (
                    "provider",
                    "account_id",
                    "mailbox_identity_key",
                    "thread_key",
                ):
                    materialized.append(values)
                return sqlite3.Row(cursor, values)

            db.row_factory = row_factory
            yield db

    monkeypatch.setattr(store, "connection", observe)
    seed(store, "new", thread_id="t0")
    assert len(materialized) == 1


def test_follow_lookup_survives_concurrent_vendor_delete(tmp_path, monkeypatch):
    store = Store(tmp_path / "mail.db")
    store.initialize()
    vendor = store.create_vendor("Vendor")
    store.add_vendor_address(vendor, "vendor@example.com")
    seed(store)
    source = store.message_source("m")
    original = db_module._thread_member
    deleted = []

    def delete_between_reads(db, row, owner):
        result = original(db, row, owner)
        if not deleted:
            deleted.append(True)
            Store(store.path).delete_vendor(vendor)
        return result

    monkeypatch.setattr(db_module, "_thread_member", delete_between_reads)
    lookup = store.followed_thread_for_metadata(
        provider="gmail",
        account_id="mailbox",
        mailbox_identity_key=source.mailbox_identity_key,
        thread_id="thread",
    )
    assert lookup == ("thread", "Vendor")
    assert deleted == [True]
    assert (
        store.followed_thread_for_metadata(
            provider="gmail",
            account_id="mailbox",
            mailbox_identity_key=source.mailbox_identity_key,
            thread_id="thread",
        )
        is None
    )
