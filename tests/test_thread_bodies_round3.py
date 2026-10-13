"""Public regressions for the accepted M2.4 owner consolidation."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_imap import NO_SENT_LIST, SENT_LIST, FakeImap, SentFolderImap, _sent_gateway
from test_service import FakeModel, _imap_sent_scaffold

from eom_email_watcher import engine_api
from eom_email_watcher.imap import ImapError, ImapGateway
from eom_email_watcher.mailbox import MailboxSession
from eom_email_watcher.service import Watcher


@pytest.mark.parametrize("confirmed_before", [False, True])
def test_unreadable_sent_records_health_and_recovers(tmp_path, monkeypatch, confirmed_before):
    cfg, store, account, _, identity, _ = _imap_sent_scaffold(tmp_path)
    client = SentFolderImap(list_lines=SENT_LIST, reject={"Sent Messages"})
    gateway = _sent_gateway(client)
    watcher = Watcher(cfg, store, MailboxSession("imap", account, gateway), FakeModel())
    if confirmed_before:
        store.set_sent_scope("imap", account, "available")
    assert (
        watcher._settle_sent(
            folders=frozenset({"inbox", "sent"}), mailbox_identity_key=identity, dry_run=False
        )
        is None
    )
    monkeypatch.setattr(engine_api, "mail_account_connected", lambda *_: True)
    public = engine_api._mail_account_public(
        SimpleNamespace(store=store, config=cfg), store.mail_account("imap", account)
    )
    assert public.get("sent_poll_failure") == "unreadable", public
    client.reject.clear()
    assert (
        watcher._settle_sent(
            folders=frozenset({"inbox", "sent"}), mailbox_identity_key=identity, dry_run=False
        )
        == "available"
    )
    public = engine_api._mail_account_public(
        SimpleNamespace(store=store, config=cfg), store.mail_account("imap", account)
    )
    assert public["sent_poll_failure"] is None
    assert public["sent_scope"] == "available"


def test_absent_sent_has_no_failed_poll(tmp_path, monkeypatch):
    cfg, store, account, _, identity, _ = _imap_sent_scaffold(tmp_path)
    watcher = Watcher(
        cfg,
        store,
        MailboxSession("imap", account, _sent_gateway(SentFolderImap(list_lines=NO_SENT_LIST))),
        FakeModel(),
    )
    assert (
        watcher._settle_sent(
            folders=frozenset({"inbox", "sent"}), mailbox_identity_key=identity, dry_run=False
        )
        == "unavailable"
    )
    monkeypatch.setattr(engine_api, "mail_account_connected", lambda *_: True)
    public = engine_api._mail_account_public(
        SimpleNamespace(store=store, config=cfg), store.mail_account("imap", account)
    )
    assert public.get("sent_poll_failure") is None
    assert public["sent_scope"] == "unavailable"


@pytest.mark.parametrize(
    "action", ["dry_failure", "dry_recovery", "inactive", "identity", "removed"]
)
def test_sent_failure_follows_current_identity_and_real_poll(tmp_path, action):
    cfg, store, account, _, identity, _ = _imap_sent_scaffold(tmp_path)
    client = SentFolderImap(list_lines=SENT_LIST, reject={"Sent Messages"})
    watcher = Watcher(
        cfg, store, MailboxSession("imap", account, _sent_gateway(client)), FakeModel()
    )
    settle = dict(folders=frozenset({"inbox", "sent"}), mailbox_identity_key=identity)
    if action == "dry_failure":
        watcher._settle_sent(**settle, dry_run=True)
        assert store.sent_poll_failure("imap", account) is None
        return
    watcher._settle_sent(**settle, dry_run=False)
    assert store.sent_poll_failure("imap", account) == "unreadable"
    if action == "dry_recovery":
        client.reject.clear()
        watcher._settle_sent(**settle, dry_run=True)
        assert store.sent_poll_failure("imap", account) == "unreadable"
        return
    if action == "inactive":
        watcher._settle_sent(
            folders=frozenset({"inbox"}), mailbox_identity_key=identity, dry_run=False
        )
    elif action == "identity":
        store.reconcile_mailbox_identity("imap", account, "f" * 64)
    else:
        with store.connection() as db:
            db.execute(
                "DELETE FROM mail_accounts WHERE provider='imap' AND account_id=?", (account,)
            )
    assert store.sent_poll_failure("imap", account) is None
    with store.connection() as db:
        assert not db.execute("SELECT 1 FROM mailbox_sent_poll_failures").fetchone()


def test_real_imap_failed_sent_health_pauses_and_recovers(tmp_path, monkeypatch):
    from dataclasses import replace

    from test_service import FakeModel, SentPollingGateway, _active_entitlement, _imap_sent_scaffold

    from eom_email_watcher import body_state, service
    from eom_email_watcher.db import AdmissionProvenance
    from eom_email_watcher.mailbox import MailboxSession, MessageMetadata
    from eom_email_watcher.reconcile_state import ReconcileState
    from eom_email_watcher.service import Watcher

    cfg, store, account, credential, identity, cursor = _imap_sent_scaffold(tmp_path)
    cfg = replace(cfg, retention_days=30)
    now = datetime.now(UTC)
    old_id = f"eom-imap-v2:{credential}:44:1"
    sent_id = f"eom-imap-sent-v1:{credential}:{'a' * 64}:77:3"
    vendor = store.create_vendor("Vendor")
    store.add_vendor_address(vendor, "billing@vendor.com")
    store.add_message(
        message_id=old_id,
        provider="imap",
        account_id=account,
        provider_message_id=old_id,
        mailbox_identity_key=identity,
        sender="billing@vendor.com",
        sender_name=None,
        subject="Quote",
        thread_id=None,
        rfc_message_id="quote@vendor.com",
        admission=AdmissionProvenance(
            kind="exact_sender",
            selector_id="sender:billing@vendor.com",
            display_name=None,
            mailbox_identity_key=identity,
            admitted_at=now.isoformat(),
        ),
        received_at=(now - timedelta(days=2)).isoformat(),
    )
    body_state.record(store, old_id, ("quote", 5), fetched_at=now)

    class Gateway(SentPollingGateway):
        @contextmanager
        def _mailbox(self):
            yield FakeImap(uid_next=11, sequence_uids=[7, 8, 9, 10])

        def _resolve_sent_folder(self, client):
            return "Sent Messages"

        def _ensure_selected(self, client, folder):
            if folder == "Sent Messages" and self.fail_sent:
                raise ImapError("imap_protocol_error", "SELECT failed")
            ImapGateway._ensure_selected(self, client, folder)

        def sent_scope(self):
            return ImapGateway.sent_scope(self)

        def stored_body(self, source, *, scope):
            return "reply", 5

        def metadata(self, source):
            return MessageMetadata(
                source,
                None,
                "owner@example.com",
                None,
                "Re: Quote",
                service.datetime.now(UTC).isoformat(),
                frozenset(),
                rfc_message_id="reply@owner.com",
                reply_ids=("quote@vendor.com",),
                to=("billing@vendor.com",),
                locations=frozenset({"sent"}),
            )

    gateway = Gateway(credential, identity, cursor)
    gateway._mailbox_id = credential
    _active_entitlement(monkeypatch)
    watcher = Watcher(cfg, store, MailboxSession("imap", account, gateway), FakeModel())
    watcher.check()
    state = ReconcileState(store, ("imap", account, identity))
    assert not state.stale()
    with store.connection() as db:
        old_thread = db.execute(
            "SELECT thread_key FROM logical_messages WHERE message_id=?", (old_id,)
        ).fetchone()[0]
    later = now + timedelta(days=35)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return later.astimezone(tz or UTC)

    monkeypatch.setattr(service, "datetime", Clock)
    gateway.sent_ids = (sent_id,)
    gateway.fail_sent = True
    pending_id = f"eom-imap-message-v2:{credential}:44:2"
    store.add_message(
        message_id=pending_id,
        provider="imap",
        account_id=account,
        provider_message_id=pending_id,
        mailbox_identity_key=identity,
        sender="billing@vendor.com",
        sender_name=None,
        subject="Pending body",
        thread_id=None,
        admission=AdmissionProvenance(
            kind="exact_sender",
            selector_id="sender:billing@vendor.com",
            display_name=None,
            mailbox_identity_key=identity,
            admitted_at=later.isoformat(),
        ),
        rfc_message_id="pending@vendor.com",
        reply_ids=("quote@vendor.com",),
        received_at=later.isoformat(),
    )
    result = watcher.check()
    with store.connection() as db:
        has_body = bool(
            db.execute("SELECT 1 FROM message_bodies WHERE message_id=?", (pending_id,)).fetchone()
        )
    from eom_email_watcher import engine_api

    monkeypatch.setattr(engine_api, "mail_account_connected", lambda *args: True)
    public = engine_api._mail_account_public(
        SimpleNamespace(store=store, config=cfg), store.mail_account("imap", account)
    )
    print("failed_select_watch_result:", result)
    print("coverage_stale:", state.stale(), "pending_body_captured:", has_body)
    print("health_account_projection:", public)
    assert state.stale() and not has_body
    assert public["sent_poll_failure"] == "unreadable"
    assert result["purged"] == 0
    assert any(row["message_id"] == old_id for row in store.recent(10))
    gateway.fail_sent = False
    watcher.check()
    public = engine_api._mail_account_public(
        SimpleNamespace(store=store, config=cfg), store.mail_account("imap", account)
    )
    assert public["sent_poll_failure"] is None
    assert not state.stale()
    with store.connection() as db:
        assert {row[0] for row in db.execute("SELECT thread_key FROM logical_messages")} == {
            old_thread
        }
        assert db.execute(
            "SELECT 1 FROM message_bodies WHERE message_id=?", (pending_id,)
        ).fetchone()
