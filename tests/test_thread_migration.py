"""Schema-32 upgrade fixtures and transactional schema-33 failure probes."""

import errno
import os
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path

import pytest
from test_db import Store
from test_thread_derived import derived, follow, seed

from eom_email_watcher import db as database
from eom_email_watcher import thread_migration


def test_migration_owner_excludes_runtime_tracking_during_messages_rebuild(tmp_path):
    store = Store(tmp_path / "schema36.sqlite3")
    store.initialize()
    vendor = store.create_vendor("Vendor")
    store.add_vendor_address(vendor, "vendor@example.com")
    seed(store)
    with store.connection() as db:
        before = [tuple(row) for row in db.execute("SELECT * FROM messages ORDER BY message_id")]
        assert db.execute("PRAGMA user_version").fetchone()[0] == 36
    with thread_migration.upgrade_owner(store.path) as owner, owner.transaction(store) as db:
        # A migration can update an input before replacing its schema. Runtime
        # TEMP triggers must never span the DROP/RENAME intermediate state.
        db.execute("UPDATE mail_accounts SET active=active")
        thread_migration.rebuild_messages(db)
        database._recompute_derived(db, database.DERIVED_INPUT_TABLES)
        thread_migration.check_database(db)
    store.initialize()
    with store.connection() as db:
        assert [
            tuple(row) for row in db.execute("SELECT * FROM messages ORDER BY message_id")
        ] == before
        assert db.execute("PRAGMA user_version").fetchone()[0] == 36
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_migration_owner_preserves_row_evidence_across_schema_step(tmp_path):
    store = Store(tmp_path / "schema36.sqlite3")
    store.initialize()
    vendor = store.create_vendor("Vendor")
    store.add_vendor_address(vendor, "vendor@example.com")
    seed(store)
    assert derived(store)["vendor_id"] == vendor
    with thread_migration.upgrade_owner(store.path) as owner, owner.transaction(store) as db:
        db.execute("UPDATE mail_accounts SET active=active")
        before = [tuple(row) for row in db.execute("SELECT * FROM derived_changed_config")]
        assert before == [("mail_accounts",)]
        during = set()

        def inspect_drop(sql):
            if sql == "DROP TABLE messages":
                # SQLite can repeat a trace statement while processing triggers.
                # Keep every distinct observed tracker state, not callback counts.
                during.add(
                    tuple(
                        tuple(row)
                        for row in db.execute(
                            "SELECT name FROM sqlite_temp_master "
                            "WHERE type='trigger' AND name GLOB 'derived_*'"
                        )
                    )
                )

        db.set_trace_callback(inspect_drop)
        thread_migration.rebuild_messages(db)
        db.set_trace_callback(None)
        assert during == {()}
        assert (
            db.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name GLOB 'derived_*'"
            ).fetchall()
            == []
        )
        assert [tuple(row) for row in db.execute("SELECT * FROM derived_changed_config")] == before
        db.execute("DELETE FROM vendor_addresses WHERE vendor_id = ?", (vendor,))
        assert {row[0] for row in db.execute("SELECT * FROM derived_changed_config")} == {
            "mail_accounts",
            "vendor_addresses",
        }
        db.execute("UPDATE messages SET sender=sender WHERE message_id='m'")
        assert [row[0] for row in db.execute("SELECT * FROM derived_changed_messages")] == ["m"]
    assert derived(store)["vendor_id"] is None
    assert follow(store)[0]["followed"] == 0
    store.initialize()
    assert derived(store)["vendor_id"] is None
    with store.connection() as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize(
    "table, expected",
    [
        ("messages", True),
        ("mail_accounts", True),
        ("message_bodies", True),
        ("message_body_unavailable", True),
        ("thread_follow", True),
        ("logical_messages", False),
        ("messages_delete_attachments", False),
    ],
)
def test_migration_tracking_boundary_uses_referenced_tables(tmp_path, table, expected):
    store = Store(tmp_path / "schema36.sqlite3")
    store.initialize()
    with thread_migration.upgrade_owner(store.path) as owner, owner.transaction(store) as db:
        assert owner.tracking_references_replaced_tables(db, {table}) is expected
        assert (
            owner.tracking_references_replaced_tables(db, {"logical_messages", table}) is expected
        )
        assert owner.tracking_references_replaced_tables(db, set()) is False
        db.execute("UPDATE mail_accounts SET active=active")
        triggers = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_temp_master WHERE type='trigger' AND name GLOB 'derived_*'"
            )
        }
        assert triggers
        for replaced in ({table}, {"logical_messages", table}, set()):
            suspend = expected and bool(replaced)
            with owner.schema_step(db, replaced_tables=replaced):
                assert db._derived_enabled is (not suspend)
                assert {
                    row[0]
                    for row in db.execute(
                        "SELECT name FROM sqlite_temp_master "
                        "WHERE type='trigger' AND name GLOB 'derived_*'"
                    )
                } == (set() if suspend else triggers)
            assert db._derived_enabled is True
            assert {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_temp_master "
                    "WHERE type='trigger' AND name GLOB 'derived_*'"
                )
            } == triggers


def test_unchanged_reopen_keeps_tracking_across_view_and_trigger_refresh(tmp_path, monkeypatch):
    store = Store(tmp_path / "schema36.sqlite3")
    store.initialize()
    seed(store)
    connection = Store.connection
    recompute = database._recompute_derived
    member = database._thread_member
    statements, evidence, touched = [], [], []

    @contextmanager
    def observe_connection(self):
        with connection(self) as db:
            db.set_trace_callback(statements.append)
            yield db

    def observe_recompute(db, changed):
        if changed:
            tracked = db.execute(
                "SELECT 1 FROM sqlite_temp_master WHERE name='derived_changed_messages'"
            ).fetchone()
            evidence.append(
                (
                    bool(tracked),
                    [tuple(row) for row in db.execute("SELECT * FROM derived_changed_config")],
                    [tuple(row) for row in db.execute("SELECT * FROM derived_changed_messages")],
                )
            )
        return recompute(db, changed)

    def observe_member(db, row, vendor):
        touched.append(row["message_id"])
        return member(db, row, vendor)

    monkeypatch.setattr(Store, "connection", observe_connection)
    monkeypatch.setattr(database, "_recompute_derived", observe_recompute)
    monkeypatch.setattr(database, "_thread_member", observe_member)
    store.initialize()
    assert "DROP VIEW IF EXISTS logical_messages" in statements
    assert "DROP TRIGGER IF EXISTS messages_delete_recipients_and_locations" in statements
    assert evidence == [(True, [], [])]
    assert touched == []
    assert len(follow(store)) == 1


@pytest.fixture
def schema35_binary(monkeypatch):
    """Keep the original rebuild/backup oracle before the additive v36 lift."""
    monkeypatch.setattr(database, "SCHEMA_VERSION", 35)

    def current_binary():
        monkeypatch.setattr(database, "SCHEMA_VERSION", 36)

    return current_binary


def assert_empty_schema36(store):
    with store.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 36
        for table in (
            "message_removal_requests",
            "message_removal_intents",
            "message_removal_targets",
        ):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("target_schema", [33, 36])
def test_windows_unsafe_retained_backup_refuses_before_sqlite_open(
    tmp_path, monkeypatch, target_schema
):
    import os
    from types import SimpleNamespace

    from connect_automate import connect_windows

    from eom_email_watcher import private_storage

    store = Store(tmp_path / "mail.db")
    store.initialize()
    with store.connection() as db:
        backup = thread_migration.write_backup(store.path, db, target_schema=target_schema)
    before = store.path.read_bytes()
    monkeypatch.setattr(private_storage, "os", SimpleNamespace(**(vars(os) | {"name": "nt"})))
    monkeypatch.setattr(connect_windows, "_private_windows_acl", lambda path: path != backup)
    connect = sqlite3.connect
    opened = []

    def observe(*args, **kwargs):
        opened.append(args[0])
        return connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", observe)
    with pytest.raises(OSError, match="unsafe"):
        store.initialize()
    assert opened == [], "An unsafe retained backup was checked only after SQLite opened"
    assert store.path.read_bytes() == before
    assert backup.exists()


def test_windows_retiring_backup_does_not_block_private_database_reads(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from connect_automate import connect_windows

    from eom_email_watcher import private_storage

    store = Store(tmp_path / "mail.db")
    store.initialize()
    with store.connection() as db:
        backup = thread_migration.write_backup(store.path, db)
    monkeypatch.setattr(private_storage, "os", SimpleNamespace(**(vars(os) | {"name": "nt"})))
    monkeypatch.setattr(connect_windows, "_private_windows_acl", lambda _path: True)
    validate = connect_windows.validate_private_regular_file

    def retire_between_enumeration_and_validation(path, **kwargs):
        if path == backup:
            backup.unlink()
        return validate(path, **kwargs)

    monkeypatch.setattr(
        connect_windows, "validate_private_regular_file", retire_between_enumeration_and_validation
    )
    with store.connection() as db:
        assert db.execute("SELECT 1").fetchone()[0] == 1
    assert not backup.exists()


@pytest.mark.parametrize("target_schema", [33, 36])
def test_backup_inventory_and_cleanup_keep_schema_namespaces_separate(tmp_path, target_schema):
    store = Store(tmp_path / "mail.db")
    store.initialize()
    with store.connection() as db:
        backups = {
            version: thread_migration.write_backup(store.path, db, target_schema=version)
            for version in (33, 36)
        }
    unrelated = tmp_path / "mail1.db.pre-v36.public.bak"
    malformed = tmp_path / "mail.db.pre-vunknown.public.bak"
    unrelated.write_bytes(b"public unrelated file")
    malformed.write_bytes(b"public unrelated file")
    assert set(thread_migration._backup_paths(store.path)) == set(backups.values())
    assert thread_migration._backup_paths(store.path, target_schema=target_schema) == [
        backups[target_schema]
    ]
    thread_migration.cleanup_backups(store.path, success=True, target_schema=target_schema)
    assert not backups[target_schema].exists()
    assert backups[36 if target_schema == 33 else 33].exists()
    assert unrelated.read_bytes() == b"public unrelated file"
    assert malformed.read_bytes() == b"public unrelated file"


def legacy_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "mail.db")
    store.initialize()
    vendor = store.create_vendor("Vendor")
    store.add_vendor_address(vendor, "vendor@example.com")
    seed(store, to=("recipient@example.com",))
    # Only this disposable fixture rewrites its schema catalog. This reconstructs
    # the prior closed CHECK without running the migration under test to do it.
    with closing(sqlite3.connect(store.path)) as db, db:
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
    with closing(sqlite3.connect(path)) as db:
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


def test_fixture_sqlite_connections_are_closed(tmp_path, monkeypatch):
    connect = sqlite3.connect
    opened = []

    def capture(*args, **kwargs):
        db = connect(*args, **kwargs)
        opened.append(db)
        return db

    monkeypatch.setattr(sqlite3, "connect", capture)
    store = legacy_store(tmp_path)
    assert snapshot(store.path)[0] == 32
    assert opened
    for db in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            db.execute("SELECT 1")


def test_upgrade_preserves_records_and_schema_and_backfills(
    tmp_path,
    monkeypatch,
    schema35_binary,
):
    store = legacy_store(tmp_path)
    before = snapshot(store.path)
    rebuild = thread_migration.rebuild_messages
    observed = []

    def inspect_backup(db):
        backups = list(tmp_path.glob("mail.db.pre-v33.*.bak"))
        assert len(backups) == 1
        if os.name == "nt":
            from connect_automate.connect_windows import validate_private_regular_file

            validate_private_regular_file(backups[0], root=tmp_path)
        else:
            assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
        assert snapshot(backups[0]) == before
        observed.append(True)
        rebuild(db)

    monkeypatch.setattr(thread_migration, "rebuild_messages", inspect_backup)
    store.initialize()
    after = snapshot(store.path)
    assert after[0] == 35
    assert after[1:4] == before[1:4]
    assert set(before[4]) <= set(after[4])
    assert observed == [True]
    assert derived(store)["vendor_id"] is not None
    assert follow(store)[0]["followed"] == 1
    assert not list(tmp_path.glob("mail.db.pre-v33.*.bak"))
    store.initialize()
    assert observed == [True]
    schema35_binary()
    store.initialize()
    assert_empty_schema36(store)
    assert snapshot(store.path)[1:4] == before[1:4]
    assert observed == [True]


def test_failed_rebuild_rolls_back_and_keeps_newest_backup(
    tmp_path,
    monkeypatch,
    schema35_binary,
):
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
    if os.name == "nt":
        from connect_automate.connect_windows import validate_private_regular_file

        validate_private_regular_file(backups[0], root=tmp_path)
    else:
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


def test_simultaneous_initializers_rebuild_once(tmp_path, monkeypatch, schema35_binary):
    store = legacy_store(tmp_path)
    rebuild = thread_migration.rebuild_messages
    observed = []
    backup = thread_migration.write_backup
    backup_targets = []

    def track_backup(path, db, *, target_schema=33):
        backup_targets.append(target_schema)
        return backup(path, db, target_schema=target_schema)

    def count_rebuild(db):
        observed.append(True)
        rebuild(db)

    monkeypatch.setattr(thread_migration, "rebuild_messages", count_rebuild)
    monkeypatch.setattr(thread_migration, "write_backup", track_backup)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: Store(store.path).initialize(), range(2)))
    assert observed == [True]
    assert snapshot(store.path)[0] == 35
    assert derived(store)["vendor_id"] is not None
    schema35_binary()
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: Store(store.path).initialize(), range(2)))
    assert_empty_schema36(store)
    assert observed == [True]
    assert backup_targets == [33, 36]


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
