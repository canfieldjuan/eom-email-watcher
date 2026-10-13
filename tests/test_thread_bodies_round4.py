from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from test_connect_v2_engine_api import seeded_runtime
from test_service import FakeModel
from test_thread_bodies import followed
from test_thread_reconcile import KEY, NOW, seed

from eom_email_watcher import body_state, engine_api, reconcile_state, service
from eom_email_watcher.reconcile_state import ReconcileState


@pytest.mark.parametrize("action", ["purge", "delete", "notifications", "automations"])
def test_deferred_cutover_keeps_ordinary_work(tmp_path, action):
    _, runtime = seeded_runtime(tmp_path)
    runtime = replace(runtime, config=replace(runtime.config, retention_days=1))
    with runtime.store.connection() as db:
        db.execute("DELETE FROM maintenance_state WHERE key='source_lock_protocol'")
    if action == "purge":
        runtime.store.purge(1)
    elif action == "delete":
        runtime.store.delete_message("message-1")
    elif action == "notifications":
        engine_api._pending_notification_payloads(runtime, 25)
    else:
        service.process_scheduling_automations(runtime.config, runtime.store, FakeModel())


def test_polled_other_account_does_not_authorize_first_account_purge(tmp_path, monkeypatch):
    later = NOW + timedelta(days=35)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return later if tz is not None else later.replace(tzinfo=None)

    monkeypatch.setattr(reconcile_state, "datetime", Clock)
    store, state_a, _ = followed(tmp_path)
    store.register_mail_account("gmail", "other", display_name="Other")
    store.reconcile_mailbox_identity("gmail", "other", "b" * 64)
    seed(store, "b", account_id="other", mailbox_identity_key="b" * 64)
    body_state.record(store, "m", ("A body", 6), fetched_at=NOW)
    body_state.record(store, "b", ("B body", 6), fetched_at=NOW)
    state_b = ReconcileState(store, ("gmail", "other", "b" * 64))
    for state in (state_a, state_b):
        state.observe(active=True, retention_days=30, sent_folder="inbox", now=later)
        progress = state.load_or_start(later, 30, {"units": []})
        for thread in state.threads():
            state.watermark(progress, thread)
        state.complete(progress)
    with store.connection() as db:
        assert reconcile_state.coverage_current(
            db, ("gmail", "mailbox", KEY), now=later, retention_days=30
        )
        assert reconcile_state.coverage_current(
            db, ("gmail", "other", "b" * 64), now=later, retention_days=30
        )
    # A new reply awaits A's provider cursor; only B's poll has completed.
    store.activate_mail_account("gmail", "other")
    store.purge(30, now=later, after_poll=True, polled_scope=("gmail", "other", "b" * 64))
    with store.connection() as db:
        assert db.execute("SELECT 1 FROM messages WHERE message_id='m'").fetchone(), (
            "unpolled account A deleted by account B's completed poll"
        )

    with store.connection() as db:
        assert not db.execute("SELECT 1 FROM messages WHERE message_id='b'").fetchone()


def test_deferred_cutover_does_not_prevent_scheduled_poll(tmp_path, monkeypatch):
    _, runtime = seeded_runtime(tmp_path)
    runtime = replace(runtime, config=replace(runtime.config, retention_days=1))
    with runtime.store.connection() as db:
        db.execute("DELETE FROM maintenance_state WHERE key='source_lock_protocol'")
    calls = []
    monkeypatch.setattr(service, "load_configured_mailbox", lambda *_: object())
    monkeypatch.setattr(
        service,
        "Watcher",
        lambda *_: SimpleNamespace(
            check=lambda **_: calls.append("polled") or {"active": True, "purged": 0}
        ),
    )
    result = service.run_watcher_check(runtime.config, runtime.store, FakeModel())
    assert calls == ["polled"] and result["active"]
    assert not runtime.store.recent(1)


def test_missing_poll_scope_never_authorizes_followed_deletion(tmp_path, monkeypatch):
    later = NOW + timedelta(days=35)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return later.astimezone(tz or NOW.tzinfo)

    monkeypatch.setattr(reconcile_state, "datetime", Clock)
    store, state, _ = followed(tmp_path)
    body_state.record(store, "m", ("body", 4), fetched_at=NOW)
    state.observe(active=True, retention_days=30, sent_folder="inbox", now=later)
    progress = state.load_or_start(later, 30, {"units": []})
    for thread in state.threads():
        state.watermark(progress, thread)
    state.complete(progress)
    assert store.purge(30, now=later, after_poll=True) == 0
    assert store.recent(1)


def test_real_watcher_account_switch_does_not_purge_unpolled_reply(tmp_path, monkeypatch):
    from test_service import FakeGmail, config

    from eom_email_watcher.mailbox import MailboxChanges, MailboxSession, MessageMetadata
    from eom_email_watcher.reconcile import ReconcilePage

    later = NOW + timedelta(days=35)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return later.astimezone(tz or NOW.tzinfo)

    monkeypatch.setattr(service, "datetime", Clock)
    monkeypatch.setattr(reconcile_state, "datetime", Clock)
    monkeypatch.setattr(
        service, "connect_entitlement_decision", lambda: service.EntitlementDecision.ACTIVE
    )
    store, state_a, _ = followed(tmp_path)
    store.register_mail_account("gmail", "other", display_name="Other")
    store.reconcile_mailbox_identity("gmail", "other", "b" * 64)
    seed(store, "b", account_id="other", mailbox_identity_key="b" * 64)
    for message in ("m", "b"):
        body_state.record(store, message, ("body", 4), fetched_at=NOW)
    state_b = ReconcileState(store, ("gmail", "other", "b" * 64))
    for state in (state_a, state_b):
        state.observe(active=True, retention_days=30, sent_folder="sent", now=later)
        progress = state.load_or_start(later, 30, {"units": []})
        for thread in state.threads():
            state.watermark(progress, thread)
        state.complete(progress)
        store.set_state(
            "100",
            NOW,
            provider="gmail",
            account_id=state.scope[1],
            mailbox_identity_key=state.scope[2],
        )
    polls = []

    class Gateway(FakeGmail):
        def __init__(self, account, key):
            super().__init__()
            self.account = account
            self.key = key

        def mailbox_identity_key(self):
            return self.key

        def sent_scope(self):
            return "available"

        def changes_since(self, cursor):
            polls.append(self.account)
            return MailboxChanges(("reply",) if self.account == "mailbox" else (), "200")

        def reconcile_page(self, *args, **kwargs):
            return ReconcilePage((), None)

        def metadata(self, message_id, **kwargs):
            return MessageMetadata(
                message_id,
                "native",
                "vendor@example.com",
                None,
                "Reply",
                later.isoformat(),
                frozenset({"INBOX"}),
                locations=frozenset({"inbox"}),
            )

    cfg = replace(config(tmp_path), database_file=store.path, retention_days=30)
    observed = []
    original = store.purge

    def traced(*args, **kwargs):
        observed.append(kwargs.get("polled_scope"))
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "purge", traced)
    store.activate_mail_account("gmail", "other")
    result = service.Watcher(
        cfg, store, MailboxSession("gmail", "other", Gateway("other", "b" * 64)), FakeModel()
    ).check(deliver_notifications=False)
    assert result["purged"] == 1, result
    assert polls == ["other"] and observed[-1] == ("gmail", "other", "b" * 64)
    with store.connection() as db:
        before = db.execute(
            "SELECT thread_key FROM logical_messages WHERE message_id='m'"
        ).fetchone()
        assert before, "account A was deleted before its pending reply was polled"
        old_thread = before[0]
    store.activate_mail_account("gmail", "mailbox")
    service.Watcher(
        cfg, store, MailboxSession("gmail", "mailbox", Gateway("mailbox", KEY)), FakeModel()
    ).check(deliver_notifications=False)
    assert polls == ["other", "mailbox"]
    with store.connection() as db:
        assert (
            db.execute("SELECT thread_key FROM logical_messages WHERE message_id='m'").fetchone()[0]
            == old_thread
        )
        reply = db.execute(
            "SELECT message_id,thread_key FROM logical_messages WHERE provider_message_id='reply'"
        ).fetchone()
        assert reply and reply[1] == old_thread
        assert db.execute("SELECT 1 FROM message_bodies WHERE message_id=?", (reply[0],)).fetchone()


def test_deferred_cutover_preserves_oversized_thread_then_purges_whole(tmp_path):
    from eom_email_watcher import source_locks
    from eom_email_watcher.db import SOURCE_CLEANUP_LOCK_BATCH_SIZE

    store, state, _ = followed(tmp_path)
    for index in range(SOURCE_CLEANUP_LOCK_BATCH_SIZE):
        seed(store, f"large-{index}")
    later = NOW + timedelta(days=35)
    progress = state.load_or_start(later, 30, {"units": []})
    for thread in state.threads():
        state.watermark(progress, thread)
    state.complete(progress)
    with store.connection() as db:
        db.execute("DELETE FROM maintenance_state WHERE key='source_lock_protocol'")
    scope = ("gmail", "mailbox", KEY)
    assert store.purge(30, now=later, after_poll=True, polled_scope=scope) == 0
    with store.connection() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            == SOURCE_CLEANUP_LOCK_BATCH_SIZE + 1
        )
    assert "oversized thread deletion" in store.source_lock_notice()
    source_locks.qualify_protocol(store, fresh=False)
    assert (
        store.purge(30, now=later, after_poll=True, polled_scope=scope)
        == SOURCE_CLEANUP_LOCK_BATCH_SIZE + 1
    )
