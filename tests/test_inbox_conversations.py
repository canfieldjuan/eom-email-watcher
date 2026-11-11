"""Conversation pages use real identity/removal/filter owners, never client dedupe."""

import base64
import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_service import config
from test_thread_reconcile import NOW, seed, state_store

from eom_email_watcher import engine_api
from eom_email_watcher import inbox_conversations as projection


def api(store, tmp_path, monkeypatch):
    monkeypatch.setattr(
        engine_api, "load_config", lambda _: replace(config(tmp_path), database_file=store.path)
    )

    def call(**query):
        return engine_api._response(
            {
                "protocol": 1,
                "operation": "inbox.conversations.query",
                "config_path": str(tmp_path / "config.toml"),
                "payload": query,
            }
        )

    return call


def test_conversations_group_before_page_limit_and_keep_non_newest_filter_match(
    tmp_path,
    monkeypatch,
):
    store, _ = state_store(tmp_path)
    seed(store, "old", sender="old@example.com", subject="Older match")
    seed(
        store, "new", sender="new@example.com", subject="New activity", received_at=NOW.isoformat()
    )
    seed(store, "other", thread_id="separate", subject="Separate chain")
    call = api(store, tmp_path, monkeypatch)
    response = call(limit=1)
    assert response["ok"], response
    page = response["data"]
    assert page["kind"] == "inbox_conversations_v1"
    assert page["totals"] == {"conversations": 2, "messages": 3, "matching_messages": 3}
    assert len(page["items"]) == 1
    row = page["items"][0]
    assert row["thread_ref"]["thread_key"] == "native"
    assert row["latest"]["message_ref"]["message_id"] == "new"
    assert row["counts"]["messages"] == 2
    assert page["next_cursor"] is not None
    filtered = call(sender="old@example.com", keyword="Older")["data"]["items"][0]
    assert filtered["thread_ref"] == row["thread_ref"]
    assert filtered["latest"] == row["latest"]
    assert filtered["matching_message_ref"]["message_id"] == "old"
    assert filtered["counts"]["matching_messages"] == 1
    assert call(sender="old@example.com", keyword="New activity")["data"]["items"] == []
    assert row["counts"]["unknown_truncation_messages"] == 2


def test_predicates_and_cues_keep_member_units_and_full_sender_labels(tmp_path, monkeypatch):
    store, _ = state_store(tmp_path)
    seed(store, "a", sender="a@example.com", sender_name="First")
    seed(store, "b", sender="b@example.com", sender_name="Second")
    with store.connection() as db:
        db.execute(
            "UPDATE messages SET priority='high',category='invoice',summary='100% Due',"
            "action_required=1,analysis_body_chars=3,analysis_body_source_chars=7 "
            "WHERE message_id='a'"
        )
        db.execute(
            "UPDATE messages SET priority='low',last_error='Synthetic failure',"
            "analysis_body_chars=7,analysis_body_source_chars=7 WHERE message_id='b'"
        )
    call = api(store, tmp_path, monkeypatch)
    row = call(sender="a@example.com", keyword="100%", category="invoice", priority="high")["data"][
        "items"
    ][0]
    assert row["counts"] == {
        "messages": 2,
        "matching_messages": 1,
        "pending_messages": 2,
        "analysis_errors": 1,
        "action_required": 1,
        "attachments": 0,
        "truncated_messages": 1,
        "unknown_truncation_messages": 0,
    }
    assert row["senders"] == [
        {"address": "a@example.com", "name": "First"},
        {"address": "b@example.com", "name": "Second"},
    ]
    assert call(sender="b@example.com", keyword="100%")["data"]["items"] == []
    assert len(call(sender="b@example.com")["data"]["items"]) == 1
    assert call(priority="untriaged")["data"]["items"] == []


def test_cursor_binds_query_limit_and_snapshot_not_flat_message_offsets(tmp_path, monkeypatch):
    store, _ = state_store(tmp_path)
    for i in range(5):
        seed(store, f"m{i}", thread_id=f"chain{i}")
    call = api(store, tmp_path, monkeypatch)
    first = call(limit=2)["data"]
    cursor = first["next_cursor"]
    second = call(limit=2, cursor=cursor)["data"]
    third = call(limit=2, cursor=second["next_cursor"])["data"]
    ids = [r["thread_ref"]["thread_key"] for page in [first, second, third] for r in page["items"]]
    assert len(ids) == len(set(ids)) == 5
    assert third["next_cursor"] is None
    for fields in [{"limit": 3}, {"keyword": "Quote"}, {"sender": "vendor@example.com"}]:
        assert (
            call(limit=fields.pop("limit", 2), cursor=cursor, **fields)["error"]["code"]
            == "invalid_request"
        )
    old = base64.urlsafe_b64encode(json.dumps([NOW.isoformat(), "m0"]).encode()).decode()
    assert call(cursor=old)["error"]["code"] == "invalid_request"
    seed(store, "arrival", thread_id="new-thread", received_at=NOW.isoformat())
    assert call(limit=2, cursor=cursor)["error"]["code"] == "stale_cursor"
    assert call(limit=2)["data"]["totals"]["conversations"] == 6


def test_query_snapshot_does_not_mix_a_concurrent_arrival(tmp_path, monkeypatch):
    store, _ = state_store(tmp_path)
    seed(store, received_at=NOW.isoformat())
    seed(store, "older-thread", thread_id="older-thread")
    original = projection.thread_reads._members

    def interleave(db, *args, **kwargs):
        seed(store, "arrived", thread_id="third")
        with store.connection() as changed:
            changed.execute("UPDATE messages SET subject='After snapshot' WHERE message_id='m'")
        return original(db, *args, **kwargs)

    monkeypatch.setattr(projection.thread_reads, "_members", interleave)
    call = api(store, tmp_path, monkeypatch)
    page = call(limit=1)["data"]
    assert page["totals"] == {"conversations": 2, "messages": 2, "matching_messages": 2}
    assert page["items"][0]["latest"]["subject"] == "Quote"
    assert len(store.query_inbox(limit=25)[0]) == 3
    monkeypatch.setattr(projection.thread_reads, "_members", original)
    assert call(limit=1, cursor=page["next_cursor"])["error"]["code"] == "stale_cursor"


def test_scope_null_identity_singletons_and_missing_derived_refuse(tmp_path, monkeypatch):
    from test_db import _upgraded_completed_legacy_message

    store, _, identity = _upgraded_completed_legacy_message(tmp_path)
    with store.connection() as db:
        db.execute(
            "UPDATE mail_accounts SET legacy_identity_status='unresolved', "
            "legacy_identity_key=NULL,mailbox_identity_key=NULL"
        )
    call = api(store, tmp_path, monkeypatch)
    assert call()["data"]["items"][0]["thread_ref"]["mailbox_identity_key"] is None
    store.reconcile_mailbox_identity("imap", "imap-account", identity, legacy_status="replacement")
    seed(
        store,
        "current",
        thread_id=None,
        provider="imap",
        account_id="imap-account",
        mailbox_identity_key=identity,
    )
    page = call()["data"]
    assert page["totals"]["conversations"] == 2
    assert {r["thread_ref"]["mailbox_identity_key"] is None for r in page["items"]} == {True, False}
    assert call(account_id="missing")["data"]["totals"]["conversations"] == 0
    with store.connection() as db:
        db.execute("DELETE FROM message_derived WHERE message_id='current'")
    assert call()["error"]["code"] == "runtime_error"


@pytest.mark.parametrize(
    ("fields", "valid"),
    [pytest.param({"limit": 2}, True, id="valid-integer-limit")]
    + [
        (fields, False)
        for fields in (
            [{"limit": value} for value in [0, -1, 101, False, "", "2", None]]
            + [
                {"keyword": " "},
                {"sender_query": "x" * 321},
                {"keyword": "x" * 201},
                {"priority": "wrong"},
                {"status": False},
                {"cursor": ""},
                {"cursor": "x" * 4097},
                {"thread_key": "unexpected"},
                {"account_id": " "},
                {"sender": "not-an-address"},
            ]
        )
    ],
)
def test_query_validates_before_config_or_database_read(tmp_path, monkeypatch, fields, valid):
    store, _ = state_store(tmp_path)
    call = api(store, tmp_path, monkeypatch)
    load_config = engine_api.load_config
    config_reads = []

    def record_config_read(path):
        config_reads.append(path)
        return load_config(path)

    monkeypatch.setattr(engine_api, "load_config", record_config_read)
    result = call(**fields)
    assert config_reads == ([tmp_path / "config.toml"] if valid else [])
    assert result["ok"] is valid, result
    if valid:
        assert result["data"]["kind"] == "inbox_conversations_v1"
    else:
        assert result["error"]["code"] == "invalid_request"


def test_response_schema_rejects_legacy_partial_unknown_counts_and_cross_scope(
    tmp_path, monkeypatch
):
    store, _ = state_store(tmp_path)
    seed(store)
    page = api(store, tmp_path, monkeypatch)()["data"]
    for path, value in [
        (["kind"], "inbox_flat"),
        (["totals", "messages"], False),
        (["items", 0, "counts", "messages"], 0),
        (["items", 0, "counts", "matching_messages"], 2),
        (["items", 0, "latest", "message_ref", "account_id"], "other"),
        (["items", 0, "latest", "sender"], "absent@example.test"),
        (["items", 0, "senders"], page["items"][0]["senders"] * 2),
    ]:
        wrong = deepcopy(page)
        target = wrong
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        with pytest.raises(ValidationError):
            projection.Page.model_validate(wrong)
    for path in [[], ["items", 0], ["items", 0, "latest", "message_ref"]]:
        wrong = deepcopy(page)
        target = wrong
        for key in path:
            target = target[key]
        target["extra"] = True
        with pytest.raises(ValidationError):
            projection.Page.model_validate(wrong)
    wrong = deepcopy(page)
    del wrong["items"][0]["thread_ref"]["mailbox_identity_key"]
    with pytest.raises(ValidationError):
        projection.Page.model_validate(wrong)
    assert projection.Query.model_validate({"limit": 100}).limit == 100
    schema = projection.Query.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["limit"]["minimum"] == 1
    assert schema["properties"]["limit"]["maximum"] == 100


def test_latest_activity_uses_canonical_source_ties_and_unknown_dates_last(tmp_path):
    store, _ = state_store(tmp_path)
    seed(store, "b", thread_id="same", received_at=NOW.isoformat())
    seed(store, "a", thread_id="same", received_at=NOW.isoformat())
    seed(store, "unknown", thread_id="unknown", received_at="not-a-date")
    seed(store, "unknown-member", thread_id="same", received_at="not-a-date")
    page = projection.query(store, projection.Query())
    assert [r["thread_ref"]["thread_key"] for r in page["items"]] == ["same", "unknown"]
    assert page["items"][0]["latest"]["message_ref"]["message_id"] == "a"
    assert page["items"][0]["counts"]["messages"] == 3
    assert page["items"][1]["latest"]["received_at"] is None


def test_real_projection_matches_the_shared_python_rust_typescript_fixture(tmp_path):
    store, _ = state_store(tmp_path)
    seed(store, "older", sender="older@example.test")
    seed(store, "newest", sender="newest@example.test", received_at=NOW.isoformat())
    seed(store, "separate", thread_id="fixture-separate")
    expected = json.loads((Path(__file__).parent / "data/inbox_conversation_page.json").read_text())
    assert projection.query(store, projection.Query(limit=1)) == expected


def test_same_native_key_stays_separate_across_accounts_providers_and_replacements(tmp_path):
    from test_thread_reconcile import KEY

    store, _ = state_store(tmp_path)
    seed(store)
    for provider, account in [("gmail", "second"), ("microsoft365", "mailbox")]:
        store.register_mail_account(provider, account, display_name="Synthetic")
        store.reconcile_mailbox_identity(provider, account, KEY)
        seed(store, provider + account, provider=provider, account_id=account)
    replacement = hashlib.sha256((KEY + "replacement").encode()).hexdigest()
    store.reconcile_mailbox_identity("gmail", "mailbox", replacement, legacy_status="replacement")
    seed(store, "replacement", mailbox_identity_key=replacement)
    page = projection.query(store, projection.Query())
    assert page["totals"] == {"conversations": 4, "messages": 4, "matching_messages": 4}
    assert {row["thread_ref"]["thread_key"] for row in page["items"]} == {"native"}
    assert len(projection.query(store, projection.Query(provider="gmail"))["items"]) == 3
    assert len(projection.query(store, projection.Query(account_id="second"))["items"]) == 1


def test_imap_component_merge_stales_old_page_and_copies_count_once(tmp_path):
    store, _ = state_store(tmp_path, provider="imap")
    seed(store, "a", provider="imap", thread_id=None, rfc_message_id="<a@example.test>")
    seed(store, "b", provider="imap", thread_id=None, rfc_message_id="<b@example.test>")
    first = projection.query(store, projection.Query(limit=1))
    assert first["totals"]["conversations"] == 2
    seed(
        store,
        "bridge",
        provider="imap",
        thread_id=None,
        rfc_message_id="<bridge@example.test>",
        reply_ids=("<a@example.test>", "<b@example.test>"),
    )
    seed(store, "copy", provider="imap", thread_id=None, rfc_message_id="<a@example.test>")
    with pytest.raises(projection.thread_reads.ThreadReadError, match="restart"):
        projection.query(store, projection.Query(limit=1, cursor=first["next_cursor"]))
    page = projection.query(store, projection.Query(limit=1))
    assert page["totals"] == {"conversations": 1, "messages": 3, "matching_messages": 3}
    assert page["next_cursor"] is None


def test_physical_context_and_removal_holds_never_match_or_read_bodies(tmp_path, monkeypatch):
    from test_removal_refusal import held_runtime
    from test_thread_body_reads import forbid_body_reads

    store = held_runtime(tmp_path, monkeypatch)
    with store.connection() as db:
        db.execute("UPDATE messages SET subject='Physical copy only' WHERE logical_of='m'")
    assert projection.query(store, projection.Query(keyword="Physical copy"))["items"] == []
    with monkeypatch.context() as reads:
        forbid_body_reads(store, reads)
        assert projection.query(store, projection.Query())["totals"]["messages"] == 1
    result = engine_api._response(
        {"protocol": 1, "operation": "inbox.delete", "payload": {"message_id": "m"}}
    )
    assert result["ok"] and result["data"]["outcome"] == "deferred"
    assert projection.query(store, projection.Query())["totals"]["conversations"] == 0
