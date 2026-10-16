"""D-removal: real migration, frozen requests, and shared eligibility owners."""

import sqlite3
import stat
from datetime import timedelta

import pytest
from test_thread_bodies import followed, legacy_body_store
from test_thread_reconcile import NOW

from eom_email_watcher import body_state, thread_migration
from eom_email_watcher import db as database


def prior_store(tmp_path, monkeypatch, version):
    if version == 34:
        return legacy_body_store(tmp_path, monkeypatch)
    with monkeypatch.context() as prior:
        prior.setattr(database, "SCHEMA_VERSION", 35)
        store = followed(tmp_path)[0]
        body_state.record(store, "m", ("retained body", 13), fetched_at=NOW)
    return store


def test_schema36_request_shape_has_real_foreign_keys(tmp_path):
    store = database.Store(tmp_path / "mail.db")
    store.initialize()
    with store.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 36
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert {r[1] for r in db.execute("PRAGMA table_info(message_removal_requests)")} == {
            "request_id", "kind", "parameters_sha256", "accepted_at",
        }
        assert db.execute("PRAGMA foreign_key_list(message_removal_intents)").fetchall()
        assert db.execute("PRAGMA foreign_key_list(message_removal_targets)").fetchall()
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("version", [34, 35])
def test_schema36_upgrade_backups_precede_any_write_and_preserve_rows(
    tmp_path, monkeypatch, version,
):
    store = prior_store(tmp_path, monkeypatch, version)
    with sqlite3.connect(store.path) as db:
        before = db.execute("SELECT * FROM messages ORDER BY message_id").fetchall()
        assert db.execute("PRAGMA user_version").fetchone()[0] == version
    original = thread_migration.write_backup
    seen = []

    def backup(path, db, *, target_schema=33):
        assert db.execute("PRAGMA user_version").fetchone()[0] == version
        assert target_schema == 36
        assert not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='message_removal_requests'"
        ).fetchone()
        result = original(path, db, target_schema=target_schema)
        assert stat.S_IMODE(result.stat().st_mode) == 0o600
        with sqlite3.connect(result) as snapshot:
            assert snapshot.execute("PRAGMA user_version").fetchone()[0] == version
            assert snapshot.execute("SELECT * FROM messages ORDER BY message_id").fetchall() == before
            if version == 35:
                assert snapshot.execute("SELECT text FROM message_bodies").fetchone()[0] == (
                    "retained body"
                )
        seen.append(result)
        return result

    monkeypatch.setattr(thread_migration, "write_backup", backup)
    store.initialize()
    assert len(seen) == 1
    assert not seen[0].exists()
    with store.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 36
        assert [tuple(r) for r in db.execute("SELECT * FROM messages ORDER BY message_id")] == before
        assert db.execute("SELECT COUNT(*) FROM message_removal_requests").fetchone()[0] == 0
    store.initialize()
    assert len(seen) == 1, "Reopening the same schema must not repeat backup/migration"


def test_schema35_binary_refuses36_before_any_ddl(tmp_path, monkeypatch):
    store = database.Store(tmp_path / "mail.db")
    store.initialize()
    with sqlite3.connect(store.path) as db:
        before = db.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall()
    monkeypatch.setattr(database, "SCHEMA_VERSION", 35)
    with pytest.raises(RuntimeError, match="newer than supported"):
        store.initialize()
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall() == before


@pytest.mark.parametrize("step", range(1, 10))
def test_schema36_ddl_fault_rolls_back_everything_and_keeps_private_backup(
    tmp_path, monkeypatch, step,
):
    from eom_email_watcher import removal

    store = prior_store(tmp_path, monkeypatch, 35)
    with sqlite3.connect(store.path) as db:
        before = tuple(db.iterdump())
    original = removal.ensure_schema

    def fail(db):
        class Fault:
            count = 0

            def execute(self, statement, *parameters):
                result = db.execute(statement, *parameters)
                self.count += 1
                if self.count == step:
                    raise RuntimeError("injected v36 DDL fault")
                return result

        original(Fault())

    monkeypatch.setattr(removal, "ensure_schema", fail)
    with pytest.raises(RuntimeError, match="injected v36 DDL fault"):
        store.initialize()
    with sqlite3.connect(store.path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 35
        assert tuple(db.iterdump()) == before
    backups = list(tmp_path.glob("*.pre-v36.*.bak"))
    assert len(backups) == 1
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    with sqlite3.connect(backups[0]) as db:
        assert tuple(db.iterdump()) == before
    monkeypatch.setattr(removal, "ensure_schema", original)
    store.initialize()
    assert not backups[0].exists()


def test_schema36_backup_full_disk_does_not_start_migration(tmp_path, monkeypatch):
    import errno

    store = prior_store(tmp_path, monkeypatch, 35)
    with sqlite3.connect(store.path) as db:
        before = tuple(db.iterdump())

    def full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "public full-disk fixture")

    monkeypatch.setattr(thread_migration, "write_backup", full)
    with pytest.raises(OSError, match="public full-disk fixture"):
        store.initialize()
    with sqlite3.connect(store.path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 35
        assert tuple(db.iterdump()) == before


def test_schema36_foreign_keys_prevent_unbound_intents_and_cascade_only_metadata(tmp_path):
    store = followed(tmp_path)[0]
    body_state.record(store, "m", ("retained body", 13), fetched_at=NOW)
    intent = "00000000-0000-4000-8000-000000000000"
    with store.connection() as db:
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            db.execute(
                "INSERT INTO message_removal_intents VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (intent, "absent", "clear", "gmail", "account", None,
                 NOW.isoformat(), "pending", 1, 0, None),
            )
        db.execute("INSERT INTO message_removal_requests VALUES(?,?,?,?)",
                   ("request", "clear", "a" * 64, NOW.isoformat()))
        db.execute("INSERT INTO message_removal_intents VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (intent, "request", "clear", "gmail", "account", None,
                    NOW.isoformat(), "pending", 1, 0, None))
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            db.execute("INSERT INTO message_removal_targets VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (intent, "m", "m", "gmail", "other-account", None,
                        "source", "[]", "pending", None))
        db.execute("INSERT INTO message_removal_targets VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (intent, "m", "m", "gmail", "account", None,
                    "source", "[]", "pending", None))
        db.execute("DELETE FROM message_removal_requests WHERE request_id='request'")
        assert not db.execute("SELECT 1 FROM message_removal_intents").fetchone()
        assert not db.execute("SELECT 1 FROM message_removal_targets").fetchone()
        assert db.execute("SELECT 1 FROM messages WHERE message_id='m'").fetchone()
        assert db.execute("SELECT text FROM message_bodies").fetchone()[0] == "retained body"


def test_request_key_is_signed_and_expiry_does_not_replace_retained_receipt(tmp_path):
    from eom_email_watcher import removal

    store = database.Store(tmp_path / "mail.db")
    store.initialize()
    key = removal.issue_key(store, "clear", {}, retention_days=30, now=NOW)
    fingerprint = removal.request_hash("clear", {})
    with store.connection() as db:
        removal.validate_key(db, key, "clear", fingerprint, now=NOW)
        assert removal.outcome(db, key) is None
        with pytest.raises(removal.RemovalRequestInvalid, match="expired"):
            removal.validate_key(db, key, "clear", fingerprint, now=NOW + timedelta(days=31))
        forged = key[:-1] + ("0" if key[-1] != "0" else "1")
        with pytest.raises(removal.RemovalRequestInvalid, match="does not match"):
            removal.validate_key(db, forged, "clear", fingerprint, now=NOW)
        with pytest.raises(removal.RemovalRequestInvalid, match="does not match"):
            removal.validate_key(db, key, "clear", removal.request_hash("clear", {"scope": "other"}),
                                 now=NOW)
        db.execute("INSERT INTO message_removal_requests VALUES(?,?,?,?)",
                   (key, "clear", fingerprint, NOW.isoformat()))
        # Lookup is independent of expiry and retains the accepted empty selection.
        assert removal.outcome(db, key, kind="clear", fingerprint=fingerprint).data() == {
            "request_id": key, "intent_ids": [], "outcome": "completed", "requested": 0,
            "completed": 0, "pending": 0, "message": None,
        }
        with pytest.raises(removal.RemovalRequestInvalid, match="different parameters"):
            removal.outcome(db, key, kind="delete")


@pytest.mark.parametrize("key", [None, False, 0, "", "rm1.0.bad.bad", "x" * 1000])
def test_malformed_request_keys_are_invalid_without_mutation(tmp_path, key):
    from eom_email_watcher import removal

    store = database.Store(tmp_path / "mail.db")
    store.initialize()
    with store.connection() as db:
        before = tuple(db.iterdump())
        with pytest.raises(removal.RemovalRequestInvalid, match="Malformed"):
            removal.validate_key(db, key, "clear", removal.request_hash("clear", {}), now=NOW)
        assert tuple(db.iterdump()) == before


@pytest.mark.parametrize("days", [0, False, -1, "30", 10 ** 30])
def test_invalid_request_lifetimes_do_not_default(tmp_path, days):
    from eom_email_watcher import removal

    store = database.Store(tmp_path / "mail.db")
    store.initialize()
    with pytest.raises(removal.RemovalRequestInvalid, match="retention interval"):
        removal.issue_key(store, "clear", {}, retention_days=days, now=NOW)


@pytest.mark.parametrize("kind,parameters", [(None, {}), ([], {}), ("other", {}),
                                           ("clear", []), ("clear", {"bad": float("nan")})])
def test_request_parameter_hash_rejects_invalid_input(kind, parameters):
    from eom_email_watcher import removal

    with pytest.raises(removal.RemovalRequestInvalid):
        removal.request_hash(kind, parameters)
