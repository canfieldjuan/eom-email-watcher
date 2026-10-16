"""Held local removal is an explicit refusal, with no mutation of held units."""

import pytest
from conftest import local_removal
from test_thread_bodies import body_runner
from test_thread_reconcile import NOW, seed

from eom_email_watcher import body_state, engine_api

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


def test_held_delete_is_not_not_found_or_success(tmp_path, monkeypatch):
    store = held_runtime(tmp_path, monkeypatch)
    before = snapshot(store)
    result = engine_api._response({
        "protocol": 1, "operation": "inbox.delete", "payload": {"message_id": "m"},
    })
    assert result["ok"] is False
    assert result["error"]["code"] == "inbox_removal_held"
    assert result["error"]["message"] == MESSAGE
    assert snapshot(store) == before


@pytest.mark.parametrize("small", [False, True])
def test_clear_reports_held_and_completed_portions(tmp_path, monkeypatch, small):
    store = held_runtime(tmp_path, monkeypatch, small=small)
    before = snapshot(store)
    result = engine_api._response({"protocol": 1, "operation": "inbox.clear", "payload": {}})
    assert result["ok"], result
    data = result["data"]
    assert data.get("outcome") == ("partial" if small else "refused")
    assert data["deleted"] == int(small) and data["refused"] == 1
    assert data["held_message_ids"] == ["m"]
    assert data["message"] == MESSAGE
    if not small:
        assert snapshot(store) == before
    else:
        after = snapshot(store)
        assert len(after["messages"]) == 33
        assert after["message_bodies"] == before["message_bodies"]
        assert all(row[0] != "small" for row in after["messages"])


def test_direct_store_cannot_report_held_delete_as_absent(tmp_path, monkeypatch):
    store = held_runtime(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="Can't delete this yet"):
        local_removal(store, "delete_message", "m")


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
    assert store.remove_vendor_address(vendor_id, "vendor@example.com").removed
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
        assert result["error"]["code"] == "inbox_removal_held"
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
    # Setup may currently return the known saved-as-error defect; this probe
    # isolates the independent missing qualification cleanup.
    engine_api._response({
        "protocol": 1, "operation": "vendors.delete", "payload": {"vendor_id": vendor_id},
        "config_path": str(tmp_path / "fixture-config.toml"),
    })
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
