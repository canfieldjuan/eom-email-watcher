"""Guard every Inbox-origin read through real membership and removal owners."""

from copy import deepcopy
from dataclasses import asdict, replace

import pytest
from conftest import local_removal
from test_inbox_thread_resolver import error, origin, success
from test_service import config
from test_thread_bodies import followed
from test_thread_body_reads import forbid_body_reads, unresolved_thread
from test_thread_copy_reads import attachment
from test_thread_reconcile import KEY, NOW, seed, state_store

from eom_email_watcher import body_state, engine_api, removal, thread_reads

CHILDREN = [
    ("threads.messages", {}),
    ("threads.body", {"message_id": "m"}),
    ("threads.copies", {"message_id": "m"}),
    ("threads.copies", {"message_id": "m", "copy_ref": "m"}),
]


def api(store, tmp_path, monkeypatch):
    monkeypatch.setattr(
        engine_api, "load_config", lambda _: replace(config(tmp_path), database_file=store.path)
    )
    monkeypatch.setattr(engine_api, "Store", lambda path: store)

    def call(operation, selector, **fields):
        return engine_api._response({
            "protocol": 1, "operation": operation,
            "config_path": str(tmp_path / "config.toml"),
            "payload": {**selector, **fields},
        })

    return call


def setup_context(tmp_path, *, vendor):
    if vendor:
        store, _, selected = followed(tmp_path)
    else:
        store, _ = state_store(tmp_path)
        seed(store)
        selected = None
    ref = thread_reads.resolve_inbox(store, thread_reads.InboxMessageRef(**origin()))[
        "context"
    ]["thread"]["thread_ref"]
    selector = (
        {"vendor_id": selected, "thread_ref": ref, "inbox_message": origin()}
        if vendor else {"inbox_thread": {"thread_ref": ref, "message_ref": origin()}}
    )
    return store, selector


def source_ref(selector):
    return selector.get("inbox_message") or selector["inbox_thread"]["message_ref"]


def thread_ref(selector):
    return selector.get("thread_ref") or selector["inbox_thread"]["thread_ref"]


def unfollow(store, vendor):
    with removal.operation_scope(store.path) as operation:
        store.remove_vendor_address(vendor, "vendor@example.com", operation=operation)


@pytest.mark.parametrize("vendor", [False, True])
@pytest.mark.parametrize("operation,fields", CHILDREN)
def test_each_child_accepts_only_its_current_guarded_context(
    tmp_path, monkeypatch, vendor, operation, fields
):
    store, selector = setup_context(tmp_path, vendor=vendor)
    store.replace_attachments("m", [attachment()])
    with store.connection() as db:
        db.execute("UPDATE messages SET summary='Saved analysis' WHERE message_id='m'")
    if vendor:
        body_state.record(store, "m", ("Stored body", 11), fetched_at=NOW)
    result = success(api(store, tmp_path, monkeypatch)(operation, selector, **fields))
    state = {"state": "not_followed"} if not vendor else {
        "state": "stored", "stored_chars": 11, "source_chars": 11,
    }
    if operation == "threads.messages":
        assert [item["message_id"] for item in result["items"]] == ["m"]
        assert result["items"][0]["body_state"] == state
        assert result["thread"]["relation"] == ("owned" if vendor else "non_vendor")
        assert (result["thread"]["owner_vendor"] is None) is (not vendor)
    elif operation == "threads.body":
        assert result["body"] == (state | {"text": "Stored body"} if vendor else state)
    elif "copy_ref" in fields:
        assert result["items"][0]["filename"] == "part.pdf"
    else:
        assert result["items"][0]["summary"] == "Saved analysis"
        assert result["items"][0]["attachment_count"] == 1


REF = {"provider": "gmail", "account_id": "mailbox", "mailbox_identity_key": KEY,
       "thread_key": "native"}
VALID_INBOX = {"thread_ref": REF, "message_ref": origin()}
VALID_VENDOR = "12345678-1234-4234-8234-123456789abc"
INVALID_SELECTORS = [
    ({}, "vendor_id must be a canonical UUIDv4 string"),
    ({"vendor_id": None, "thread_ref": REF}, "vendor_id must be a canonical UUIDv4 string"),
    *[({"vendor_id": VALID_VENDOR, "thread_ref": REF, "inbox_message": value}, reason)
      for value, reason in [
          (None, "message_ref requires exactly the complete scope"),
          ({}, "message_ref requires exactly the complete scope"),
          (origin(extra=True), "message_ref requires exactly the complete scope"),
          (origin(message_id=""), "message_id must be a non-empty string"),
          (origin(message_id=4), "message_id must be a non-empty string"),
      ]],
    *[({"inbox_thread": value}, "inbox_thread requires a thread and message reference")
      for value in [None, {}, [], {"thread_ref": REF}, {"message_ref": origin()},
                    {**VALID_INBOX, "extra": 1}]],
    ({"inbox_thread": {**VALID_INBOX, "thread_ref": None}},
     "thread_ref requires exactly the complete scope"),
    ({"inbox_thread": {**VALID_INBOX, "message_ref": None}},
     "message_ref requires exactly the complete scope"),
    ({"inbox_thread": {**VALID_INBOX, "message_ref": origin(account_id=None)}},
     "account_id must be a non-empty string"),
    ({"inbox_thread": {**VALID_INBOX, "message_ref": origin(mailbox_identity_key="unknown")}},
     "mailbox identity key must be a lower-case SHA-256 digest"),
    ({"inbox_thread": {**VALID_INBOX, "message_ref": {
        k: v for k, v in origin().items() if k != "mailbox_identity_key"
    }}}, "message_ref requires exactly the complete scope"),
    *[({"inbox_thread": VALID_INBOX, key: value}, "Thread selectors cannot be mixed")
      for key, value in [
        ("vendor_id", VALID_VENDOR), ("vendor_id", None), ("thread_ref", REF),
        ("inbox_message", origin()), ("inbox_message", None),
    ]],
    ({"inbox_thread": VALID_INBOX, "thread_followed": False},
     "Unsupported payload fields: thread_followed"),
]


@pytest.fixture
def selector_admission(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"

    def forbidden(config_path):
        assert config_path == path
        pytest.fail("Selector reached configuration/storage")

    monkeypatch.setattr(engine_api, "load_config", forbidden)

    def call(operation, selector, **fields):
        return engine_api._response({
            "protocol": 1, "operation": operation, "config_path": str(path),
            "payload": {**selector, **fields},
        })

    return call


@pytest.mark.parametrize("operation,fields", CHILDREN)
@pytest.mark.parametrize("selector,reason", INVALID_SELECTORS)
def test_invalid_selector_never_reaches_configuration(
    selector_admission, operation, fields, selector, reason,
):
    result = selector_admission(operation, selector, **fields)
    error(result, "invalid_request")
    assert result["error"]["message"] == reason


@pytest.mark.parametrize("operation,fields", CHILDREN)
@pytest.mark.parametrize("selector", [
    {"vendor_id": VALID_VENDOR, "thread_ref": REF},
    {"vendor_id": VALID_VENDOR, "thread_ref": REF, "inbox_message": origin()},
    {"inbox_thread": VALID_INBOX},
])
def test_valid_selector_reaches_configuration_in_same_admission_harness(
    selector_admission, operation, fields, selector,
):
    with pytest.raises(pytest.fail.Exception, match="Selector reached configuration/storage"):
        selector_admission(operation, selector, **fields)


@pytest.mark.parametrize("identity", [None, KEY])
@pytest.mark.parametrize("include_text", [False, True])
def test_body_projection_requires_explicit_thread_classification(
    tmp_path, monkeypatch, identity, include_text,
):
    store, _ = setup_context(tmp_path, vendor=False)
    forbid_body_reads(store, monkeypatch)
    with thread_reads.snapshot(store) as db:
        with pytest.raises(TypeError, match="required keyword-only argument: 'thread_followed'"):
            body_state.project(db, "m", mailbox_identity_key=identity, include_text=include_text)
        assert body_state.project(
            db, "m", mailbox_identity_key=identity,
            thread_followed=False, include_text=include_text,
        ) == {"state": "not_followed"}


@pytest.mark.parametrize("vendor", [False, True])
@pytest.mark.parametrize("operation,fields", CHILDREN)
def test_complete_origin_and_requested_scope_are_independently_bound(
    tmp_path, monkeypatch, vendor, operation, fields
):
    store, selector = setup_context(tmp_path, vendor=vendor)
    call = api(store, tmp_path, monkeypatch)
    success(call(operation, selector, **fields))
    # Even a valid retained thread cannot accept an origin from another scope.
    for field, value, expected in [
        ("provider", "microsoft365", "not_found"),
        ("account_id", "foreign", "not_found"),
        ("mailbox_identity_key", "b" * 64, "stale_selection"),
        ("mailbox_identity_key", None, "stale_selection"),
        ("message_id", "missing", "not_found"),
    ]:
        wrong = deepcopy(selector)
        source_ref(wrong)[field] = value
        error(call(operation, wrong, **fields), expected)
    for field, value in [
        ("provider", "microsoft365"), ("account_id", "foreign"),
        ("mailbox_identity_key", "b" * 64), ("mailbox_identity_key", None),
        ("thread_key", "unrelated"),
    ]:
        wrong = deepcopy(selector)
        thread_ref(wrong)[field] = value
        error(call(operation, wrong, **fields), "not_found")
    seed(store, "outside", thread_id="outside")
    wrong = deepcopy(selector)
    source_ref(wrong)["message_id"] = "outside"
    error(call(operation, wrong, **fields), "not_found")
    seed(store, "physical", rfc_message_id="separate@example.com")
    with store.connection() as db:
        db.execute("UPDATE messages SET logical_of='m' WHERE message_id='physical'")
    wrong = deepcopy(selector)
    source_ref(wrong)["message_id"] = "physical"
    error(call(operation, wrong, **fields), "not_found")


@pytest.mark.parametrize("vendor", [False, True])
@pytest.mark.parametrize("operation,fields", CHILDREN)
def test_context_changes_require_resolution_again(tmp_path, monkeypatch, vendor, operation, fields):
    store, selector = setup_context(tmp_path, vendor=vendor)
    call = api(store, tmp_path, monkeypatch)
    success(call(operation, selector, **fields))
    if vendor:
        unfollow(store, selector["vendor_id"])
    else:
        selected = store.create_vendor("New vendor")
        store.add_vendor_address(selected, "vendor@example.com")
    error(call(operation, selector, **fields), "stale_selection")
    # Replacement with another represented vendor is not implicit consent.
    if vendor:
        replacement = store.create_vendor("Replacement")
        store.add_vendor_address(replacement, "vendor@example.com")
        error(call(operation, selector, **fields), "stale_selection")


@pytest.mark.parametrize("vendor", [False, True])
@pytest.mark.parametrize("operation,fields", CHILDREN)
def test_guard_refuses_incomplete_derived_membership(
    tmp_path, monkeypatch, vendor, operation, fields
):
    store, selector = setup_context(tmp_path, vendor=vendor)
    seed(store, "missing-derived", sender="other@example.com")
    with store.connection() as db:
        db.execute("DELETE FROM message_derived WHERE message_id='missing-derived'")
    error(api(store, tmp_path, monkeypatch)(operation, selector, **fields), "runtime_error")


@pytest.mark.parametrize("unresolved", [False, True])
def test_non_vendor_state_precedes_identity_and_residual_bytes_or_markers(
    tmp_path, monkeypatch, unresolved
):
    if unresolved:
        store, vendor = unresolved_thread(tmp_path)
        unfollow(store, vendor)
        identity = None
    else:
        store, _ = setup_context(tmp_path, vendor=False)
        seed(store, "unattributed", sender="other@example.com")
        identity = KEY
    message = origin(mailbox_identity_key=identity)
    ref = thread_reads.resolve_inbox(store, thread_reads.InboxMessageRef(**message))[
        "context"
    ]["thread"]["thread_ref"]
    selector = {"inbox_thread": {"thread_ref": ref, "message_ref": message}}
    with store.connection() as db:
        db.execute("INSERT INTO message_bodies VALUES ('m','residual',8,8,?)", (NOW.isoformat(),))
        db.execute("INSERT INTO message_body_unavailable VALUES ('unattributed','source_gone')")
    forbid_body_reads(store, monkeypatch)
    call = api(store, tmp_path, monkeypatch)
    page = success(call("threads.messages", selector))
    assert len(page["items"]) == 2
    assert all(item["body_state"] == {"state": "not_followed"} for item in page["items"])
    for target in ["m", "unattributed"]:
        assert success(call("threads.body", selector, message_id=target))["body"] == {
            "state": "not_followed",
        }


def test_guarded_unresolved_vendor_keeps_existing_identity_state(tmp_path, monkeypatch):
    store, vendor = unresolved_thread(tmp_path)
    message = origin("unattributed", mailbox_identity_key=None)
    ref = thread_reads.resolve_inbox(store, thread_reads.InboxMessageRef(**message))[
        "context"
    ]["thread"]["thread_ref"]
    selector = {"vendor_id": vendor, "thread_ref": ref, "inbox_message": message}
    original_connection = store.connection
    forbid_body_reads(store, monkeypatch)
    call = api(store, tmp_path, monkeypatch)
    assert all(item["body_state"] == {"state": "mailbox_identity_unavailable"}
               for item in success(call("threads.messages", selector))["items"])
    for target in ["m", "unattributed"]:
        assert success(call("threads.body", selector, message_id=target))["body"] == {
            "state": "mailbox_identity_unavailable",
        }
    monkeypatch.setattr(store, "connection", original_connection)
    store.reconcile_mailbox_identity(
        "gmail", "mailbox", KEY, legacy_status="continuity_proven", preserve_cursor=True,
    )
    for operation, fields in CHILDREN:
        error(call(operation, selector, **fields), "stale_selection")


@pytest.mark.parametrize("kind", ["delete", "clear", "unfollow"])
def test_held_33_row_barriers_keep_origin_and_content_unreadable(tmp_path, monkeypatch, kind):
    from test_removal_refusal import held_runtime

    store = held_runtime(tmp_path, monkeypatch)
    message = store.query_inbox(limit=25)[0][0]["thread_entry_ref"]
    resolved = thread_reads.resolve_inbox(store, thread_reads.InboxMessageRef(**message))
    vendor = resolved["context"]["vendor"]["vendor_id"]
    ref = resolved["context"]["thread"]["thread_ref"]
    selector = {"vendor_id": vendor, "thread_ref": ref, "inbox_message": message}
    if kind == "unfollow":
        unfollow(store, vendor)
        selector = {"inbox_thread": {"thread_ref": ref, "message_ref": message}}
    else:
        result = local_removal(
            store, "request_removal", kind, {"message_id": "m"} if kind == "delete" else {},
        )
        assert result.data()["outcome"] == "deferred"
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 33
        assert db.execute("SELECT text FROM message_bodies").fetchone()[0] == "private body"
    original_connection = store.connection
    forbid_body_reads(store, monkeypatch)
    call = api(store, tmp_path, monkeypatch)
    for operation, fields in CHILDREN:
        response = call(operation, selector, **fields)
        if kind != "unfollow":
            error(response, "not_found")
        elif operation == "threads.body":
            assert success(response)["body"] == {"state": "removal_pending"}
        elif operation == "threads.messages":
            assert success(response)["items"][0]["body_state"] == {"state": "removal_pending"}
        else:
            success(response)  # CONTENT hides bodies, not retained copy metadata.
    if kind == "unfollow":
        with thread_reads.snapshot(store) as db:
            assert body_state.project(
                db, "m", mailbox_identity_key=None, thread_followed=False, include_text=True,
            ) == {"state": "removal_pending"}
        monkeypatch.setattr(store, "connection", original_connection)
        store.add_vendor_address(vendor, "vendor@example.com")
        forbid_body_reads(store, monkeypatch)
        error(call("threads.body", selector, message_id="m"), "stale_selection")
        selector = {"vendor_id": vendor, "thread_ref": ref, "inbox_message": message}
        assert success(call("threads.body", selector, message_id="m"))["body"] == {
            "state": "removal_pending",
        }


@pytest.mark.parametrize("vendor", [False, True])
@pytest.mark.parametrize("operation,fields", [CHILDREN[0], CHILDREN[2], CHILDREN[3]])
def test_every_paged_read_binds_origin_and_context(
    tmp_path, monkeypatch, vendor, operation, fields
):
    store, selector = setup_context(tmp_path, vendor=vendor)
    seed(store, "reply", sender="person@example.com", received_at=NOW.isoformat())
    with store.connection() as db:
        db.execute("UPDATE messages SET rfc_message_id='m@example.com' WHERE message_id='m'")
    seed(store, "copy", rfc_message_id="m@example.com")
    store.replace_attachments("m", [attachment("one"), attachment("two", position=1)])
    call = api(store, tmp_path, monkeypatch)
    first = success(call(operation, selector, **fields, limit=1))
    cursor = first["next_cursor"]
    assert cursor
    second = success(call(operation, selector, **fields, limit=1, cursor=cursor))
    key = "message_id" if operation == "threads.messages" else (
        "part_id" if "copy_ref" in fields else "copy_ref"
    )
    expected = ["m", "reply"] if key == "message_id" else (
        ["one", "two"] if key == "part_id" else ["copy", "m"]
    )
    assert [item[key] for page in [first, second] for item in page["items"]] == expected
    assert second["next_cursor"] is None
    other_origin = deepcopy(selector)
    source_ref(other_origin)["message_id"] = "reply"
    error(call(operation, other_origin, **fields, limit=1, cursor=cursor), "invalid_request")
    if vendor:
        without_origin = {k: v for k, v in selector.items() if k != "inbox_message"}
        error(call(operation, without_origin, **fields, limit=1, cursor=cursor), "invalid_request")
        legacy_cursor = success(call(operation, without_origin, **fields, limit=1))["next_cursor"]
        error(call(operation, selector, **fields, limit=1, cursor=legacy_cursor), "invalid_request")
    # Revocation applies to append as well as the first page.
    if vendor:
        unfollow(store, selector["vendor_id"])
    else:
        added = store.create_vendor("Now followed")
        store.add_vendor_address(added, "vendor@example.com")
    error(call(operation, selector, **fields, limit=1, cursor=cursor), "stale_selection")


@pytest.mark.parametrize("vendor", [False, True])
@pytest.mark.parametrize("operation,fields", CHILDREN)
def test_origin_guard_and_projection_share_one_read_only_snapshot(
    tmp_path, monkeypatch, vendor, operation, fields
):
    store, selector = setup_context(tmp_path, vendor=vendor)
    store.replace_attachments("m", [attachment()])
    if vendor:
        body_state.record(store, "m", ("Snapshot body", 13), fetched_at=NOW)
    call = api(store, tmp_path, monkeypatch)

    def forbidden(*args, **kwargs):
        pytest.fail("Local thread read initialized runtime/storage or required entitlement")

    for name in ["_runtime", "load_runtime", "runtime_from_config"]:
        monkeypatch.setattr(engine_api, name, forbidden)
    monkeypatch.setattr(type(store), "initialize", forbidden)
    monkeypatch.setattr(engine_api.connect, "require_connect_entitlement", forbidden)
    with store.connection() as db:
        db.execute("PRAGMA journal_mode=WAL")
        before = list(db.iterdump())
    expected = success(call(operation, selector, **fields))
    with store.connection() as db:
        assert list(db.iterdump()) == before
    original = thread_reads._selected_thread
    committed = False

    def remove_after_guard(db, *args, **kwargs):
        nonlocal committed
        selected = original(db, *args, **kwargs)
        assert db.in_transaction and db.execute("PRAGMA query_only").fetchone()[0] == 1
        if not committed:
            committed = True
            local_removal(store, "delete_message", "m")
        return selected

    monkeypatch.setattr(thread_reads, "_selected_thread", remove_after_guard)
    assert success(call(operation, selector, **fields)) == expected
    error(call(operation, selector, **fields), "not_found")


@pytest.mark.parametrize("vendor", [False, True])
@pytest.mark.parametrize("operation,fields", CHILDREN)
def test_imap_alias_requires_origin_in_the_same_scoped_survivor(
    tmp_path, monkeypatch, vendor, operation, fields
):
    from test_thread_copy_reads import imap_selection

    store, selected, ref, message_id = imap_selection(tmp_path)
    if not vendor:
        with removal.operation_scope(store.path) as operation_scope:
            store.remove_vendor_address(selected, "a@b.com", operation=operation_scope)
    message = next(row["thread_entry_ref"] for row in store.query_inbox(limit=25)[0]
                   if row["message_id"] == message_id)
    alias = asdict(replace(ref, thread_key="retired-key"))
    selector = (
        {"vendor_id": selected, "thread_ref": alias, "inbox_message": message}
        if vendor else {"inbox_thread": {"thread_ref": alias, "message_ref": message}}
    )
    with store.connection() as db:
        db.execute("INSERT INTO thread_key_aliases VALUES (?,?)", ("retired-key", ref.thread_key))
    fields = {k: message_id if k == "message_id" else
              store.message_source(message_id).provider_message_id for k in fields}
    call = api(store, tmp_path, monkeypatch)
    result = success(call(operation, selector, **fields))
    assert (result.get("thread") or result)["thread_ref"] == asdict(ref)
    for key, value in [("account_id", "other"), ("mailbox_identity_key", "b" * 64)]:
        wrong = deepcopy(selector)
        thread_ref(wrong)[key] = value
        error(call(operation, wrong, **fields), "not_found")
    with store.connection() as db:
        db.execute("UPDATE thread_key_aliases SET survivor_key='retired-key'")
    error(call(operation, selector, **fields), "not_found")


def test_thread_listing_has_no_non_vendor_or_origin_bypass(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid listing reached config")

    monkeypatch.setattr(engine_api, "load_config", forbidden)
    for payload in [{"inbox_thread": VALID_INBOX}, {"vendor_id": None},
                    {"vendor_id": "vendor", "inbox_message": origin()}]:
        error(engine_api._response({
            "protocol": 1, "operation": "threads.list", "payload": payload,
        }), "invalid_request")


def test_linked_context_accepts_an_unattributed_origin_but_not_a_replacement_vendor(
    tmp_path, monkeypatch
):
    store, selector = setup_context(tmp_path, vendor=True)
    owner = selector["vendor_id"]
    linked = store.create_vendor("Linked")
    store.add_vendor_address(linked, "linked@example.com")
    seed(store, "linked", sender="linked@example.com", received_at=NOW.isoformat())
    seed(store, "unattributed", sender="other@example.com", received_at=NOW.isoformat())
    selector["vendor_id"] = linked
    selector["inbox_message"] = origin("unattributed")
    call = api(store, tmp_path, monkeypatch)
    for operation, fields in CHILDREN:
        result = success(call(operation, selector, **fields))
        if operation == "threads.messages":
            assert result["thread"]["relation"] == "linked"
            assert result["thread"]["owner_vendor"]["vendor_id"] == owner
            assert {item["message_id"]: item["attributed_vendor"]
                    for item in result["items"]}["unattributed"] is None
    with removal.operation_scope(store.path) as operation:
        store.remove_vendor_address(linked, "linked@example.com", operation=operation)
    for operation, fields in CHILDREN:
        error(call(operation, selector, **fields), "stale_selection")


def test_canonical_promotion_never_redirects_an_old_origin_to_a_valid_new_target(
    tmp_path, monkeypatch
):
    from test_db import _retained_duplicate, _upgraded_completed_legacy_message

    from eom_email_watcher import db as database

    store, root, identity = _upgraded_completed_legacy_message(tmp_path)
    store.mark_skipped(root)
    child = _retained_duplicate(store, "2", "same@x")
    original_source = store.message_source(root).provider_message_id
    with store.connection() as db:
        db.execute(
            "UPDATE messages SET provider_message_id=?,status='skipped',logical_of=? "
            "WHERE message_id=?", (original_source, root, child),
        )
        database._reparent_logical_sources(db, child, root)
        db.execute("UPDATE messages SET status='pending' WHERE message_id=?", (root,))
        db.execute("UPDATE mail_accounts SET mailbox_identity_key=NULL")
    old_origin = store.query_inbox(limit=25)[0][0]["thread_entry_ref"]
    assert old_origin["message_id"] == root
    store.reconcile_mailbox_identity(
        "imap", "imap-account", identity, legacy_status="continuity_proven",
    )
    fresh_origin = store.query_inbox(limit=25)[0][0]["thread_entry_ref"]
    assert fresh_origin["message_id"] == child
    context = thread_reads.resolve_inbox(
        store, thread_reads.InboxMessageRef(**fresh_origin),
    )["context"]
    ref = context["thread"]["thread_ref"]
    selector = (
        {"vendor_id": context["vendor"]["vendor_id"], "thread_ref": ref,
         "inbox_message": fresh_origin}
        if context["kind"] == "vendor" else
        {"inbox_thread": {"thread_ref": ref, "message_ref": fresh_origin}}
    )
    stale = deepcopy(selector)
    source_ref(stale).update(old_origin)
    call = api(store, tmp_path, monkeypatch)
    for operation, fields in CHILDREN:
        fields = {k: child if k == "message_id" else
                  store.message_source(child).provider_message_id for k in fields}
        success(call(operation, selector, **fields))
        error(call(operation, stale, **fields), "not_found")


def test_internal_non_vendor_discriminator_cannot_omit_origin(tmp_path):
    store, selector = setup_context(tmp_path, vendor=False)
    ref = thread_reads.ThreadRef(**thread_ref(selector))
    with pytest.raises(thread_reads.ThreadReadError) as caught:
        thread_reads.list_messages(store, thread_reads.InboxThreadSelector(None), ref)
    assert caught.value.code == "invalid_request"


@pytest.mark.parametrize("vendor", [False, True])
@pytest.mark.parametrize("operation,fields", CHILDREN)
def test_deleted_origin_refuses_a_still_visible_target_after_reopening_store(
    tmp_path, monkeypatch, vendor, operation, fields
):
    store, selector = setup_context(tmp_path, vendor=vendor)
    seed(store, "origin", sender="other@example.com", received_at=NOW.isoformat())
    source_ref(selector)["message_id"] = "origin"
    call = api(store, tmp_path, monkeypatch)
    success(call(operation, selector, **fields))
    result = local_removal(store, "request_removal", "delete", {"message_id": "origin"})
    assert result.data()["outcome"] == "completed"
    restarted = type(store)(store.path)
    assert [row["message_id"] for row in restarted.query_inbox(limit=25)[0]] == ["m"]
    call = api(restarted, tmp_path, monkeypatch)
    error(call(operation, selector, **fields), "not_found")
    refreshed = deepcopy(selector)
    source_ref(refreshed)["message_id"] = "m"
    success(call(operation, refreshed, **fields))
