"""Schema-32 upgrade fixtures and transactional schema-33 failure probes."""

import errno
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from test_db import Store
from test_thread_derived import derived, follow, seed

from eom_email_watcher import thread_migration


def legacy_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "mail.db")
    store.initialize()
    vendor = store.create_vendor("Vendor")
    store.add_vendor_address(vendor, "vendor@example.com")
    seed(store, to=("recipient@example.com",))
    # Only this disposable fixture rewrites its schema catalog. This reconstructs
    # the prior closed CHECK without running the migration under test to do it.
    with sqlite3.connect(store.path) as db:
        sql = db.execute("SELECT sql FROM sqlite_master WHERE name='messages'").fetchone()[0]
        widened = (
            "admission_kind IN ("
            + ", ".join(repr(k) for k in thread_migration.ADMISSION_KINDS)
            + ")"
        )
        assert widened in sql
        db.execute("PRAGMA writable_schema=ON")
        db.execute(
            "UPDATE sqlite_master SET sql=? WHERE name='messages'",
            (sql.replace(widened, "admission_kind IN ('exact_sender', 'gmail_user_label')"),),
        )
        db.execute("PRAGMA writable_schema=OFF")
        for table in ("thread_watermarks", "thread_follow", "message_derived"):
            db.execute(f"DROP TABLE {table}")
        db.execute("PRAGMA user_version=32")
    return store


def snapshot(path):
    with sqlite3.connect(path) as db:
        return (
            db.execute("PRAGMA user_version").fetchone()[0],
            db.execute("SELECT * FROM messages").fetchall(),
            db.execute("SELECT * FROM message_recipients").fetchall(),
            db.execute("SELECT * FROM message_locations").fetchall(),
            db.execute(
                "SELECT name, sql FROM sqlite_master WHERE type IN ('index', 'trigger', "
                "'view') AND sql IS NOT NULL ORDER BY name"
            ).fetchall(),
        )


def test_upgrade_preserves_records_and_schema_and_backfills(tmp_path, monkeypatch):
    store = legacy_store(tmp_path)
    before = snapshot(store.path)
    rebuild = thread_migration.rebuild_messages
    observed = []

    def inspect_backup(db):
        backups = list(tmp_path.glob("mail.db.pre-v33.*.bak"))
        assert len(backups) == 1
        assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
        assert snapshot(backups[0]) == before
        observed.append(True)
        rebuild(db)

    monkeypatch.setattr(thread_migration, "rebuild_messages", inspect_backup)
    store.initialize()
    after = snapshot(store.path)
    assert after[0] == 33
    assert after[1:] == before[1:]
    assert observed == [True]
    assert derived(store)["vendor_id"] is not None
    assert follow(store)[0]["followed"] == 1
    assert not list(tmp_path.glob("mail.db.pre-v33.*.bak"))
    store.initialize()
    assert observed == [True]


def test_failed_rebuild_rolls_back_and_keeps_newest_backup(tmp_path, monkeypatch):
    store = legacy_store(tmp_path)
    before = snapshot(store.path)
    with store.connection() as db:
        old = thread_migration.write_backup(store.path, db)

    def fail_check(db):
        assert (
            "vendor_address"
            in db.execute("SELECT sql FROM sqlite_master WHERE name='messages'").fetchone()[0]
        )
        raise RuntimeError("injected integrity failure")

    monkeypatch.setattr(thread_migration, "check_database", fail_check)
    with pytest.raises(RuntimeError, match="injected integrity failure"):
        store.initialize()
    assert snapshot(store.path) == before
    backups = list(tmp_path.glob("mail.db.pre-v33.*.bak"))
    assert len(backups) == 1 and backups[0] != old
    assert snapshot(backups[0]) == before
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600


def test_backup_full_disk_does_not_start_migration(tmp_path, monkeypatch):
    store = legacy_store(tmp_path)
    before = snapshot(store.path)
    invoked = []

    def full_disk(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(thread_migration.os, "open", full_disk)
    monkeypatch.setattr(thread_migration, "rebuild_messages", lambda db: invoked.append(True))
    with pytest.raises(OSError, match="No space left on device"):
        store.initialize()
    assert invoked == []
    assert snapshot(store.path) == before


def test_simultaneous_initializers_rebuild_once(tmp_path, monkeypatch):
    store = legacy_store(tmp_path)
    rebuild = thread_migration.rebuild_messages
    observed = []

    def count_rebuild(db):
        observed.append(True)
        rebuild(db)

    monkeypatch.setattr(thread_migration, "rebuild_messages", count_rebuild)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: Store(store.path).initialize(), range(2)))
    assert observed == [True]
    assert snapshot(store.path)[0] == 33
    assert derived(store)["vendor_id"] is not None


@pytest.mark.parametrize(
    "kind,prefix",
    [
        ("vendor_address", "vendor-address:"),
        ("vendor_domain", "vendor-address:"),
        ("sent_to_vendor", "vendor-recipient:"),
        ("thread_follow", "thread:"),
    ],
)
def test_upgraded_kind_is_captured_and_immutable(tmp_path, kind, prefix):
    from eom_email_watcher.db import AdmissionProvenance

    store = legacy_store(tmp_path)
    store.initialize()
    source = store.message_source("m")
    grant = AdmissionProvenance(
        kind=kind,
        selector_id=prefix + "a" * 64,
        display_name="Vendor",
        mailbox_identity_key=source.mailbox_identity_key,
        admitted_at="2026-10-11T12:00:00+00:00",
    )
    seed(store, "new", thread_id="x" * 512, admission=grant)
    assert (
        store.recent(2)[0]["admission"]["kind"] == kind
        or store.recent(2)[1]["admission"]["kind"] == kind
    )
    with store.connection() as db:
        assert (
            db.execute("SELECT thread_key FROM messages WHERE message_id='new'").fetchone()[0]
            == "x" * 512
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE messages SET admission_kind='exact_sender' WHERE message_id='new'")
        thread_migration.check_database(db)


@pytest.mark.parametrize(
    "selector",
    [
        "thread:" + "a" * 63,
        "thread:" + "a" * 65,
        "thread:" + "A" * 64,
        "vendor-address:" + "a" * 64,
        "thread:",
        "",
        0,
        False,
    ],
)
def test_bad_provenance_fails_before_capture(tmp_path, selector):
    from eom_email_watcher.db import AdmissionProvenance

    store = Store(tmp_path / "mail.db")
    store.initialize()
    seed(store)
    identity = store.message_source("m").mailbox_identity_key
    grant = AdmissionProvenance(
        kind="thread_follow",
        selector_id=selector,
        display_name="Vendor",
        mailbox_identity_key=identity,
        admitted_at="2026-10-11T12:00:00+00:00",
    )
    with pytest.raises(ValueError):
        seed(store, "bad", admission=grant)
    assert derived(store, "bad") is None


def test_partial_backup_write_is_removed_and_source_unchanged(tmp_path):
    store = legacy_store(tmp_path)
    before = snapshot(store.path)

    class FullDisk:
        def execute(self, statement, parameters):
            assert statement == "VACUUM INTO ?"
            # Simulate SQLite having started writing before ENOSPC.
            Path(parameters[0]).write_bytes(b"partial SQLite backup")
            raise sqlite3.OperationalError("database or disk is full")

    with pytest.raises(sqlite3.OperationalError, match="disk is full"):
        thread_migration.write_backup(store.path, FullDisk())
    assert snapshot(store.path) == before
    assert list(tmp_path.glob("mail.db.pre-v33.*.bak")) == []


def test_previous_schema_binary_refuses_upgrade(tmp_path, monkeypatch):
    from eom_email_watcher import db as db_module

    store = Store(tmp_path / "mail.db")
    store.initialize()
    monkeypatch.setattr(db_module, "SCHEMA_VERSION", 32)
    with pytest.raises(RuntimeError, match="newer than supported"):
        store.initialize()


def test_backup_cleanup_uses_literal_database_name(tmp_path):
    store = Store(tmp_path / "mail[1].db")
    store.initialize()
    with store.connection() as db:
        own_backup = thread_migration.write_backup(store.path, db)
    unrelated = tmp_path / "mail1.db.pre-v33.20260101T000000Z.bak"
    unrelated.write_bytes(b"unrelated database backup")
    thread_migration.cleanup_backups(store.path, success=True)
    assert unrelated.read_bytes() == b"unrelated database backup"
    assert not own_backup.exists()
