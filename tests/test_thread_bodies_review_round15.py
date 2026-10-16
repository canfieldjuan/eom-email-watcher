"""Public regressions for stored-body admission and bounded derived work."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_thread_bodies import body_runner, prepare_derived
from test_thread_reconcile import KEY, NOW, seed

from eom_email_watcher import body_state, reconcile
from eom_email_watcher.gmail import GmailGateway
from eom_email_watcher.mailbox import MailboxError, MailboxMessageInvalid


def inline_gateway(data):
    response = {
        "labelIds": ["INBOX"],
        "payload": {"mimeType": "text/plain", "body": {"data": data}},
    }
    return GmailGateway(SimpleNamespace(users=lambda: SimpleNamespace(
        messages=lambda: SimpleNamespace(get=lambda **_: SimpleNamespace(execute=lambda: response))
    )))


@pytest.mark.parametrize("data", ["@@@@", "YWJj$", "a", "é"])
def test_inline_gmail_storage_rejects_malformed_encoding(data):
    with pytest.raises(MailboxMessageInvalid, match="invalid.*encoding"):
        inline_gateway(data).stored_body("m", scope=frozenset({"inbox"}))


@pytest.mark.parametrize("data,text", [("", ""), ("YQ", "a"), ("YQ==", "a"), ("8J-YgA", "😀")])
def test_inline_gmail_storage_accepts_valid_encoding(data, text):
    assert inline_gateway(data).stored_body("m", scope=frozenset({"inbox"})) == (text, len(text))


@pytest.mark.parametrize("data,stored", [("@@@@", False), ("YWJj", True)])
def test_inline_gmail_result_settles_through_reconcile(tmp_path, monkeypatch, data, stored):
    watcher, state, gateway = body_runner(tmp_path, monkeypatch, {})
    def fetch(source, *, scope):
        return inline_gateway(data).stored_body(source, scope=scope)

    gateway.stored_body = fetch
    prepare_derived(state)
    reconcile.run(watcher, KEY, "unavailable", label_selectors=(), now=NOW)
    assert not state.stale()
    with watcher.store.connection() as db:
        body = db.execute("SELECT text FROM message_bodies WHERE message_id='m'").fetchone()
        gone = db.execute(
            "SELECT reason FROM message_body_unavailable WHERE message_id='m'"
        ).fetchone()
    assert (body is not None) == stored
    assert gone is None if stored else gone[0] == "source_gone"


@pytest.mark.parametrize("limit,expected", [(0, []), (1, ["m"]), (2, ["m", "z"])])
def test_pending_snapshot_limit_preserves_zero(tmp_path, monkeypatch, limit, expected):
    watcher, state, _ = body_runner(tmp_path, monkeypatch, {})
    seed(watcher.store, "z", rfc_message_id="z@vendor")
    with watcher.store.connection() as db:
        assert body_state.pending(
            db, state.scope, cutoff=NOW-timedelta(days=30), now=NOW, limit=limit
        ) == expected


def test_derived_body_selection_is_bounded_per_run(tmp_path, monkeypatch):
    outcomes = {f"m{i:02}": ("body", 4) for i in range(12)}
    outcomes["m"] = ("body", 4)
    watcher, state, gateway = body_runner(tmp_path, monkeypatch, outcomes)
    for source in outcomes:
        if source != "m":
            seed(watcher.store, source, rfc_message_id=source + "@vendor")
    prepare_derived(state)
    calls = []
    real_pending = body_state.pending

    def pending(*args, **kwargs):
        calls.append(kwargs)
        return real_pending(*args, **kwargs)

    monkeypatch.setattr(body_state, "pending", pending)
    reconcile.run(watcher, KEY, "unavailable", label_selectors=(), now=NOW)
    assert len(gateway.body_calls) == len(outcomes)
    assert len(calls) <= 3, "pending relation rescanned for each body"
    assert all(call.get("limit") is not None for call in calls), "unbounded pending materialization"
    assert not state.stale()


def test_bounded_snapshot_does_not_starve_after_transient_prefix(tmp_path, monkeypatch):
    outcomes = {"m": MailboxError("temporary"), "z": ("body", 4)}
    watcher, state, gateway = body_runner(tmp_path, monkeypatch, outcomes)
    seed(watcher.store, "z", rfc_message_id="z@vendor")
    prepare_derived(state)
    real_budget = reconcile.ReconcileBudget
    monkeypatch.setattr(reconcile, "ReconcileBudget", lambda allowed: real_budget(allowed, calls=1))
    reconcile.run(watcher, KEY, "unavailable", label_selectors=(), now=NOW)
    reconcile.run(watcher, KEY, "unavailable", label_selectors=(), now=NOW)
    assert [source for source, _ in gateway.body_calls] == ["m", "z"]
    assert state.stale(), "transient body must still block coverage"
