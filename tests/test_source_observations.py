"""Source header facts and folder-event ordering at the storage boundary."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Event

import pytest
from test_db import Store, _coalesced_pair, _source_scope

from eom_email_watcher import db as db_module

FIRST = datetime(2090, 1, 1, tzinfo=UTC)


def _stamps(store, scope):
    with store.connection() as db:
        return [
            r[0]
            for r in db.execute(
                "SELECT recorded_at FROM message_locations WHERE provider = ? AND account_id = ? "
                "AND mailbox_identity_key = ? AND provider_message_id = ? ORDER BY location",
                tuple(
                    scope[k]
                    for k in (
                        "provider",
                        "account_id",
                        "mailbox_identity_key",
                        "provider_message_id",
                    )
                ),
            )
        ]


@pytest.mark.parametrize("to", [(), ("vendor@example.com",)])
def test_retained_fetched_headers_persist(tmp_path: Path, to):
    store, root, _ = _coalesced_pair(tmp_path)
    scope = _source_scope(store, root)
    store.record_message_location(
        **scope, locations=frozenset({"inbox"}), headers_observed=True, to=to, now=FIRST
    )
    assert _stamps(store, scope) == [FIRST.isoformat()]
    store = Store(store.path)
    store.initialize()
    later = FIRST + timedelta(seconds=1)
    store.record_message_location(
        **scope, locations=frozenset({"inbox"}), headers_observed=False, now=later
    )
    assert _stamps(store, scope) == [later.isoformat()]
    with store.connection() as db:
        assert (
            db.execute(
                "SELECT capture_timezone FROM messages WHERE message_id = ?", (root,)
            ).fetchone()[0]
            is None
        )


def test_unfetched_copy_cannot_borrow_capture_context(tmp_path: Path):
    store, root, duplicate = _coalesced_pair(tmp_path)
    with store.connection() as db:
        db.execute(
            "UPDATE messages SET capture_timezone = ? WHERE message_id = ?",
            ("America/Chicago", root),
        )
    scope = _source_scope(store, duplicate)
    store.record_message_location(
        **scope, locations=frozenset({"inbox"}), headers_observed=False, now=FIRST
    )
    assert _stamps(store, scope) == [None]


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("newer_complete", [False, True])
def test_folder_order_not_commit_order(tmp_path: Path, reverse, newer_complete):
    store, root, _ = _coalesced_pair(tmp_path)
    scope = _source_scope(store, root)
    store.record_message_location(
        **scope, locations=frozenset({"inbox"}), headers_observed=True, now=FIRST
    )
    events = [
        (FIRST + timedelta(microseconds=1), not newer_complete),
        (FIRST + timedelta(microseconds=2), newer_complete),
    ]
    ordered = list(reversed(events)) if reverse else events
    barrier, first_committed = Barrier(2), Event()

    def write(index):
        at, complete = ordered[index]
        barrier.wait(timeout=10)
        if index == 1:
            assert first_committed.wait(timeout=10)
        Store(store.path).record_message_location(
            **scope,
            locations=frozenset({"inbox"}),
            headers_observed=False,
            scope_complete=complete,
            now=at,
        )
        if index == 0:
            first_committed.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(write, i) for i in range(2)]
        for future in futures:
            future.result(timeout=15)
    assert _stamps(store, scope) == [(events[-1][0].isoformat() if newer_complete else None)]


@pytest.mark.parametrize("reverse", [False, True])
def test_equal_time_incomplete_wins(tmp_path: Path, reverse):
    store, root, _ = _coalesced_pair(tmp_path)
    scope = _source_scope(store, root)
    for complete in [True, False] if reverse else [False, True]:
        store.record_message_location(
            **scope,
            locations=frozenset({"inbox"}),
            headers_observed=True,
            scope_complete=complete,
            now=FIRST,
        )
    assert _stamps(store, scope) == [None]


def test_outside_response_stores_nothing(tmp_path: Path):
    store, root, _ = _coalesced_pair(tmp_path)
    scope = _source_scope(store, root)
    with pytest.raises(ValueError, match="admitted"):
        store.record_message_location(
            **scope,
            locations=frozenset(),
            headers_observed=True,
            to=("bad@example.com",),
            now=FIRST,
        )
    store.record_message_location(
        **scope,
        locations=frozenset({"inbox"}),
        headers_observed=False,
        now=FIRST + timedelta(seconds=1),
    )
    assert _stamps(store, scope) == [None]
    with store.connection() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM message_recipients WHERE message_id = ?", (root,)
            ).fetchone()[0]
            == 0
        )


def test_gap_preserves_headers_and_rejects_delayed_complete(tmp_path: Path):
    store, root, _ = _coalesced_pair(tmp_path)
    scope = _source_scope(store, root)
    store.record_message_location(
        **scope, locations=frozenset({"inbox"}), headers_observed=True, now=FIRST
    )
    gap = FIRST + timedelta(seconds=2)
    store.clear_location_stamps(
        scope["provider"], scope["account_id"], scope["mailbox_identity_key"], now=gap
    )
    store.record_message_location(
        **scope,
        locations=frozenset({"inbox"}),
        headers_observed=False,
        now=FIRST + timedelta(seconds=1),
    )
    assert _stamps(store, scope) == [None]
    later = gap + timedelta(seconds=1)
    store.record_message_location(
        **scope, locations=frozenset({"inbox"}), headers_observed=False, now=later
    )
    assert _stamps(store, scope) == [later.isoformat()]


def test_old_accepted_headers_do_not_replace_newer_folder_state(tmp_path: Path):
    store, root, _ = _coalesced_pair(tmp_path)
    scope = _source_scope(store, root)
    later = FIRST + timedelta(seconds=1)
    store.record_message_location(
        **scope,
        locations=frozenset({"inbox"}),
        headers_observed=False,
        scope_complete=False,
        now=later,
    )
    store.record_message_location(
        **scope, locations=frozenset({"inbox"}), headers_observed=True, now=FIRST
    )
    assert _stamps(store, scope) == [None]
    with store.connection() as db:
        row = db.execute(
            "SELECT headers_observed_at, folder_observed_at, folder_complete "
            "FROM message_source_observations WHERE provider_message_id = ?",
            (scope["provider_message_id"],),
        ).fetchone()
    assert tuple(row) == (FIRST.isoformat(), later.isoformat(), 0)


def test_rejected_recipient_write_rolls_back_observations(tmp_path: Path, monkeypatch):
    store, root, _ = _coalesced_pair(tmp_path)
    scope = _source_scope(store, root)
    with store.connection() as db:
        before = [tuple(r) for r in db.execute("SELECT * FROM message_source_observations")]

    def reject(*args, **kwargs):
        raise RuntimeError("injected recipient failure")

    monkeypatch.setattr(db_module, "_record_recipients", reject)
    with pytest.raises(RuntimeError, match="injected recipient failure"):
        store.record_message_location(
            **scope, locations=frozenset({"sent"}), headers_observed=True, to=("",), now=FIRST
        )
    with store.connection() as db:
        assert [tuple(r) for r in db.execute("SELECT * FROM message_source_observations")] == before
    assert store.message_locations(root) == ["inbox"]


def test_source_observations_deleted_with_unit(tmp_path: Path):
    store, root, _ = _coalesced_pair(tmp_path)
    scope = _source_scope(store, root)
    store.record_message_location(
        **scope, locations=frozenset({"inbox"}), headers_observed=True, now=FIRST
    )
    store.delete_message(root)
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM message_source_observations").fetchone()[0] == 0


def test_migration_is_conservative_and_rolls_back(tmp_path: Path, monkeypatch):
    store, root, _ = _coalesced_pair(tmp_path)
    scope = _source_scope(store, root)
    store.record_message_location(
        **scope, locations=frozenset({"inbox"}), headers_observed=True, now=FIRST
    )
    with store.connection() as db:
        db.execute("PRAGMA user_version = 30")
    migrate = db_module._migrate_source_observations

    def interrupted(db, at):
        migrate(db, at)
        raise RuntimeError("migration injected failure")

    monkeypatch.setattr(db_module, "_migrate_source_observations", interrupted)
    with pytest.raises(RuntimeError, match="injected"):
        store.initialize()
    assert _stamps(store, scope) == [FIRST.isoformat()]
    with store.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 30
    monkeypatch.setattr(db_module, "_migrate_source_observations", migrate)
    store.initialize()
    assert _stamps(store, scope) == [None]
    with store.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 31
        assert (
            db.execute(
                "SELECT count(*) FROM message_source_observations "
                "WHERE headers_observed_at IS NOT NULL"
            ).fetchone()[0]
            == 0
        )
    monkeypatch.setattr(db_module, "SCHEMA_VERSION", 30)
    with pytest.raises(RuntimeError, match="newer than supported"):
        store.initialize()


@pytest.mark.parametrize("operation", ["clear", "purge"])
def test_cleanup_removes_all_source_observations(tmp_path: Path, operation):
    store, root, _ = _coalesced_pair(tmp_path)
    scope = _source_scope(store, root)
    store.record_message_location(
        **scope, locations=frozenset({"inbox"}), headers_observed=True, now=FIRST
    )
    if operation == "clear":
        store.clear_messages()
    else:
        store.purge_with_outcome(1, now=FIRST)
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM message_source_observations").fetchone()[0] == 0


def test_startup_coalescing_moves_source_evidence(tmp_path: Path):
    from test_db import _imap_message

    store = Store(tmp_path / "db.sqlite3")
    store.initialize()
    root = _imap_message(store, "1", "one@example.com")
    child = _imap_message(store, "2", "two@example.com")
    with store.connection() as db:
        db.execute(
            "UPDATE messages SET rfc_message_id = 'one@example.com' WHERE message_id = ?", (child,)
        )
        before = {
            r[0]: r[1]
            for r in db.execute(
                "SELECT provider_message_id, headers_observed_at FROM message_source_observations"
            )
        }
    store.initialize()
    with store.connection() as db:
        rows = db.execute(
            "SELECT message_id, provider_message_id, headers_observed_at "
            "FROM message_source_observations"
        ).fetchall()
    assert {r[0] for r in rows} == {root}
    assert {r[1]: r[2] for r in rows} == before
    assert len(rows) == 2


def test_unknown_source_cannot_mint_header_evidence(tmp_path: Path):
    store, root, _ = _coalesced_pair(tmp_path)
    scope = _source_scope(store, root)
    scope["provider_message_id"] = "unknown"
    assert (
        store.record_message_location(
            **scope, locations=frozenset({"inbox"}), headers_observed=True, now=FIRST
        )
        == 0
    )
    with store.connection() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM message_source_observations "
                "WHERE provider_message_id = 'unknown'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    "field,value",
    [("provider", "gmail"), ("account_id", "other"), ("mailbox_identity_key", "b" * 64)],
)
def test_source_key_isolation(tmp_path: Path, field, value):
    store, root, _ = _coalesced_pair(tmp_path)
    scope = _source_scope(store, root)
    store.record_message_location(
        **scope, locations=frozenset({"inbox"}), headers_observed=True, now=FIRST
    )
    other = {**scope, field: value}
    assert (
        store.record_message_location(
            **other, locations=frozenset({"sent"}), headers_observed=True, now=FIRST
        )
        == 0
    )
    assert _stamps(store, scope) == [FIRST.isoformat()]
    with store.connection() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM message_source_observations "
                "WHERE headers_observed_at IS NOT NULL"
            ).fetchone()[0]
            == 1
        )


def test_real_schema30_upgrade_creates_table_atomically(tmp_path: Path, monkeypatch):
    """Construct a schema-30 file without dropping or editing the source database."""
    original, root, _ = _coalesced_pair(tmp_path)
    scope = _source_scope(original, root)
    original.record_message_location(
        **scope, locations=frozenset({"inbox"}), headers_observed=True, now=FIRST
    )
    upgraded = Store(tmp_path / "old-schema.sqlite3")
    excluded = {
        "message_source_observations",
        "idx_source_observations_message",
        "messages_delete_source_observations",
    }
    with original.connection() as source, upgraded.connection() as target:
        schema = source.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        tables = [r for r in schema if r[0] == "table" and r[1] not in excluded]
        for _, _name, sql in tables:
            target.execute(sql)
        for _, name, _ in tables:
            rows = source.execute(f'SELECT * FROM "{name}"').fetchall()
            if rows:
                slots = ",".join("?" for _ in rows[0])
                target.executemany(
                    f'INSERT INTO "{name}" VALUES ({slots})', [tuple(r) for r in rows]
                )
        for kind, name, sql in schema:
            if kind != "table" and name not in excluded:
                target.execute(sql)
        target.execute("PRAGMA user_version = 30")
        before = [tuple(r) for r in target.execute("SELECT * FROM messages ORDER BY message_id")]
    migrate = db_module._migrate_source_observations

    def fail(db, at):
        migrate(db, at)
        raise RuntimeError("injected upgrade failure")

    monkeypatch.setattr(db_module, "_migrate_source_observations", fail)
    with pytest.raises(RuntimeError, match="injected upgrade failure"):
        upgraded.initialize()
    with upgraded.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 30
        assert (
            db.execute(
                "SELECT name FROM sqlite_master WHERE name = 'message_source_observations'"
            ).fetchone()
            is None
        )
    assert _stamps(upgraded, scope) == [FIRST.isoformat()]
    monkeypatch.setattr(db_module, "_migrate_source_observations", migrate)
    upgraded.initialize()
    with upgraded.connection() as db:
        assert [
            tuple(r) for r in db.execute("SELECT * FROM messages ORDER BY message_id")
        ] == before
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert _stamps(upgraded, scope) == [None]
