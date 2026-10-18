"""Deferred removal and pre-upgrade refusal preserve every held storage fact."""

import pytest
from conftest import local_removal
from test_thread_bodies import body_runner
from test_thread_reconcile import NOW, seed

from eom_email_watcher import body_state, engine_api, removal, thread_migration
from eom_email_watcher import db as database

MESSAGE = "Can't delete this yet, the local upgrade is still finishing; try again later"
SAVED_NOTICE = (
    "Saved. Removing this thread's stored email text is waiting for the local upgrade to finish; "
    "it will finish automatically."
)


def held_runtime(tmp_path, monkeypatch, *, small=False, copies=32):
    watcher, _, _ = body_runner(tmp_path, monkeypatch, {})
    for i in range(copies):
        seed(watcher.store, f"copy-{i:02}", rfc_message_id=f"copy-{i}@vendor")
    # Retained rows left by the pre-coalescing schema, unlike fresh captures
    # which fold copies into locations without retaining another physical row.
    with watcher.store.connection() as db:
        db.execute(
            "UPDATE messages SET logical_of='m', rfc_message_id='same@vendor' "
            "WHERE message_id LIKE 'copy-%'"
        )
    body_state.record(watcher.store, "m", ("private body", 12), fetched_at=NOW)
    if small:
        seed(watcher.store, "small", rfc_message_id="small@vendor")
    with watcher.store.connection() as db:
        db.execute("DELETE FROM maintenance_state WHERE key='source_lock_protocol'")
        assert db.execute(
            "SELECT COUNT(*) FROM messages WHERE logical_of='m'"
        ).fetchone()[0] == copies
    monkeypatch.setattr(engine_api, "_runtime", lambda _: watcher)
    return watcher.store


def snapshot(store):
    with store.connection() as db:
        tables = [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )]
        return {
            table: [tuple(r) for r in db.execute(
                'SELECT * FROM "' + table.replace('"', '""') + '" ORDER BY 1'
            )]
            for table in tables
        }


def assert_held_storage_unchanged(store, before):
    # Plan 38–41/75: acceptance changes eligibility/receipts, never held bytes.
    after = snapshot(store)
    for table in (
        "messages", "message_bodies", "message_body_unavailable", "message_locations",
        "message_recipients", "message_source_observations", "suppressed_messages",
    ):
        assert after[table] == before[table], table


def test_held_delete_is_not_not_found_or_success(tmp_path, monkeypatch):
    store = held_runtime(tmp_path, monkeypatch)
    before = snapshot(store)
    result = engine_api._response({
        "protocol": 1, "operation": "inbox.delete", "payload": {"message_id": "m"},
    })
    # Plan 9/38–41/75 replaces refusal with accepted, physically pending status.
    assert result["ok"] is True, result
    data = result["data"]
    assert data["outcome"] == "deferred"
    assert (data["requested"], data["completed"], data["pending"]) == (1, 0, 1)
    assert data["deleted"] is False
    assert data["request_id"] and data["intent_ids"]
    assert data["message"] == "Deletion pending until the local upgrade completes"
    assert_held_storage_unchanged(store, before)
    with store.connection() as db:
        assert not db.execute("SELECT 1 FROM logical_messages").fetchone()


@pytest.mark.parametrize("small", [False, True])
def test_clear_reports_held_and_completed_portions(tmp_path, monkeypatch, small):
    store = held_runtime(tmp_path, monkeypatch, small=small)
    before = snapshot(store)
    result = engine_api._response({"protocol": 1, "operation": "inbox.clear", "payload": {}})
    assert result["ok"], result
    data = result["data"]
    # Plan 9/14/38–41/75/79–80: preserve physical counts; held becomes pending.
    assert data["outcome"] == "deferred"
    assert (data["requested"], data["completed"], data["pending"]) == (
        1 + int(small), int(small), 1,
    )
    assert data["deleted"] == int(small)
    assert data["message"] == "Deletion pending until the local upgrade completes"
    with store.connection() as db:
        assert {r[0] for r in db.execute(
            "SELECT DISTINCT logical_target_id FROM message_removal_targets WHERE state='pending'"
        )} == {"m"}
    if not small:
        assert_held_storage_unchanged(store, before)
    else:
        after = snapshot(store)
        assert len(after["messages"]) == 33
        assert after["message_bodies"] == before["message_bodies"]
        assert all(row[0] != "small" for row in after["messages"])


def test_direct_store_cannot_report_held_delete_as_absent(tmp_path, monkeypatch):
    store = held_runtime(tmp_path, monkeypatch)
    before = snapshot(store)
    # Plan 9/38–41/75: accepted Store outcome must not claim absence/completion.
    result = local_removal(store, "request_removal", "delete", {"message_id": "m"})
    assert (result.requested, result.completed, result.pending) == (1, 0, 1)
    assert_held_storage_unchanged(store, before)
    assert local_removal(store, "delete_message", "missing") is False


@pytest.mark.parametrize("operation", ["vendors.addresses.remove", "vendors.delete"])
def test_held_unfollow_preserves_body_and_commits_follow_transition(
    tmp_path, monkeypatch, operation,
):
    store = held_runtime(tmp_path, monkeypatch)
    with store.connection() as db:
        vendor_id = db.execute("SELECT vendor_id FROM vendor_addresses").fetchone()[0]
        body_before = tuple(db.execute("SELECT * FROM message_bodies").fetchone())
    runtime = engine_api._runtime({})
    monkeypatch.setattr(engine_api, "load_config", lambda _: runtime.config)
    payload = {"vendor_id": vendor_id}
    if operation == "vendors.addresses.remove":
        payload["address"] = "vendor@example.com"
    result = engine_api._response({
        "protocol": 1, "operation": operation, "payload": payload,
        "config_path": str(tmp_path / "fixture-config.toml"),
    })
    assert result["ok"] is True, result
    assert result["data"]["notice"] == SAVED_NOTICE
    with store.connection() as db:
        assert tuple(db.execute("SELECT * FROM message_bodies").fetchone()) == body_before
        assert not db.execute("SELECT 1 FROM thread_follow WHERE followed=1").fetchone()
        assert not db.execute(
            "SELECT 1 FROM message_derived WHERE vendor_id IS NOT NULL"
        ).fetchone()
    # Today's body consumers cannot offer an unfollowed body for capture/use.
    with store.connection() as db:
        from test_thread_reconcile import KEY
        assert body_state.pending(db, ("gmail", "mailbox", KEY), cutoff=NOW, now=NOW) == []
    for reader in ("inbox.query", "inbox.recent"):
        visible = engine_api._response({"protocol": 1, "operation": reader, "payload": {}})
        assert visible["ok"], visible
        assert "private body" not in repr(visible)


def test_small_unfollow_still_removes_body_immediately(tmp_path, monkeypatch):
    watcher, _, _ = body_runner(tmp_path, monkeypatch, {})
    store = watcher.store
    body_state.record(store, "m", ("private body", 12), fetched_at=NOW)
    with store.connection() as db:
        db.execute("DELETE FROM maintenance_state WHERE key='source_lock_protocol'")
        vendor_id = db.execute("SELECT vendor_id FROM vendor_addresses").fetchone()[0]
    # Plan 103/131: the same real operation owner precedes the Store write.
    with removal.operation_scope(store.path) as owner:
        assert store.remove_vendor_address(
            vendor_id, "vendor@example.com", operation=owner,
        ).removed
    with store.connection() as db:
        assert not db.execute("SELECT 1 FROM message_bodies").fetchone()


@pytest.mark.parametrize("rows", [31, 32, 33])
def test_exact_legacy_unit_boundary_and_explicit_retry(tmp_path, monkeypatch, rows):
    from eom_email_watcher import source_locks

    store = held_runtime(tmp_path, monkeypatch, copies=rows - 1)
    request = {"protocol": 1, "operation": "inbox.delete", "payload": {"message_id": "m"}}
    result = engine_api._response(request)
    if rows <= 32:
        assert result["ok"] and result["data"]["deleted"] is True
    else:
        # Plan 9/38–41/75/78/81: descriptor bound is unchanged; one receipt
        # remains pending, then its same frozen key completes after qualification.
        assert result["ok"] and result["data"]["outcome"] == "deferred"
        assert result["data"]["pending"] == 1 and result["data"]["completed"] == 0
        with store.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == rows
            assert db.execute("SELECT text FROM message_bodies").fetchone()[0] == "private body"
        request["payload"]["request_id"] = result["data"]["request_id"]
        source_locks.qualify_protocol(store, fresh=False)
        retry = engine_api._response(request)
        assert retry["ok"] and retry["data"]["deleted"] is True
    with store.connection() as db:
        assert not db.execute("SELECT 1 FROM messages").fetchone()
        assert not db.execute("SELECT 1 FROM message_bodies").fetchone()


@pytest.mark.parametrize("already_qualified", [False, True])
def test_qualification_finishes_held_unfollow_cleanup(tmp_path, monkeypatch, already_qualified):
    from eom_email_watcher import source_locks

    store = held_runtime(tmp_path, monkeypatch)
    with store.connection() as db:
        vendor_id = db.execute("SELECT vendor_id FROM vendor_addresses").fetchone()[0]
    runtime = engine_api._runtime({})
    monkeypatch.setattr(engine_api, "load_config", lambda _: runtime.config)
    # Plan 38/40/103: the real guarded mutation must commit before qualification.
    result = engine_api._response({
        "protocol": 1, "operation": "vendors.delete", "payload": {"vendor_id": vendor_id},
        "config_path": str(tmp_path / "fixture-config.toml"),
    })
    assert result["ok"], result
    with store.connection() as db:
        assert db.execute("SELECT text FROM message_bodies").fetchone()[0] == "private body"
        assert not db.execute("SELECT 1 FROM thread_follow WHERE followed=1").fetchone()
        if already_qualified:
            db.execute(
                "INSERT INTO maintenance_state VALUES('source_lock_protocol',?,?)",
                (source_locks.SOURCE_LOCK_PROTOCOL, NOW.isoformat()),
            )
    source_locks.qualify_protocol(store, fresh=False)
    with store.connection() as db:
        assert not db.execute("SELECT 1 FROM message_bodies").fetchone()
        assert db.execute(
            "SELECT 1 FROM maintenance_state WHERE key='checkpoint_pending'"
        ).fetchone()


@pytest.mark.parametrize("rows", [31, 32, 33])
def test_pre36_source_cleanup_keeps_legacy_bound_and_changes_no_storage(
    tmp_path, monkeypatch, rows,
):
    # Plan 9/75/81: retain the reachable physical-refusal fact before the lift.
    with monkeypatch.context() as legacy:
        legacy.setattr(database, "SCHEMA_VERSION", 35)
        store = held_runtime(tmp_path, legacy, copies=rows - 1)
    before = snapshot(store)
    with store.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 35
        ids = [r[0] for r in db.execute("SELECT message_id FROM messages")]
    with store._source_cleanup_locks(ids) as acquired:
        assert acquired is (rows <= 32)
    assert snapshot(store) == before


@pytest.mark.parametrize("operation", ["inbox.delete", "inbox.clear", "vendors.delete"])
def test_failed_schema36_upgrade_refuses_removal_without_mutation_then_retries(
    tmp_path, monkeypatch, operation,
):
    from fixture_toml import toml_path_literal
    from test_connect_v2_engine_api import api_request
    from test_engine_api import write_config

    # Plan 31–33/75–77/103/136–137: no supported runtime escapes a failed lift.
    with monkeypatch.context() as legacy:
        legacy.setattr(database, "SCHEMA_VERSION", 35)
        store = held_runtime(tmp_path, legacy)
    config_path = tmp_path / "config.toml"
    write_config(config_path, notifications_enabled=False)
    config_path.write_text("\n".join(
        "database_file = " + toml_path_literal(store.path)
        if line.startswith("database_file = ") else line
        for line in config_path.read_text().splitlines()
    ) + "\n")
    with store.connection() as db:
        vendor_id = db.execute("SELECT vendor_id FROM vendor_addresses").fetchone()[0]
        assert db.execute("PRAGMA user_version").fetchone()[0] == 35
    payload = {"message_id": "m"} if operation == "inbox.delete" else (
        {"vendor_id": vendor_id} if operation == "vendors.delete" else {}
    )
    request = api_request(config_path, operation, payload)
    before = snapshot(store)

    def fail_upgrade(db):
        raise RuntimeError("Public injected upgrade failure")

    with monkeypatch.context() as fault:
        fault.setattr(thread_migration, "check_database", fail_upgrade)
        result = engine_api._response(request)
    assert result["ok"] is False
    assert result["error"] == {
        "code": "runtime_error", "message": "Public injected upgrade failure",
    }
    assert snapshot(store) == before
    with store.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 35
        assert not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='message_removal_requests'"
        ).fetchone()
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []

    result = engine_api._response(request)
    assert result["ok"], result
    with store.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 36
        assert db.execute("SELECT COUNT(*) FROM message_removal_requests").fetchone()[0] == 1
        assert not db.execute("SELECT 1 FROM message_bodies").fetchone()
        assert bool(db.execute("SELECT 1 FROM messages").fetchone()) is (
            operation == "vendors.delete"
        )
    store.initialize()
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM message_removal_requests").fetchone()[0] == 1
