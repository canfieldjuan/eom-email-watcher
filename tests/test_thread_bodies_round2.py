"""Public regressions for complete IMAP observations and bounded body retention."""

import base64
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from test_imap import (
    NO_SENT_LIST,
    SENT_LIST,
    FakeImap,
    SentFolderImap,
    _sent_gateway,
    cursor,
)
from test_thread_bodies import followed
from test_thread_reconcile import KEY, NOW, seed

from eom_email_watcher import mime
from eom_email_watcher.imap import ImapError, ImapGateway


@pytest.mark.parametrize("recovery", [False, True])
@pytest.mark.parametrize("sent", [False, True])
def test_imap_partial_pages_are_undrained_until_final_page(monkeypatch, recovery, sent):
    from eom_email_watcher import imap

    monkeypatch.setattr(imap, "MAX_INCREMENTAL_MESSAGE_IDS", 2)
    client = SentFolderImap(
        list_lines=SENT_LIST,
        sequence_uids=[7, 8, 9, 10],
        uid_next=11,
        sent_sequence_uids=[7, 8, 9, 10],
        sent_uid_next=11,
    )
    # Recovery SEARCH is sequence-based, whereas incremental traversal reads UIDs.
    client.search_response = b"1 2 3 4"
    gateway = _sent_gateway(client)
    if recovery:
        first = (
            gateway.sent_recover_since(datetime.now(UTC))
            if sent
            else gateway.recover_since(frozenset(), datetime.now(UTC))
        )
    else:
        if sent:
            key = imap._folder_key("Sent Messages")
            saved = imap._sent_cursor(gateway._mailbox_id, key, 77, 7)
            first = gateway.sent_changes_since(saved)
        else:
            first = gateway.changes_since(cursor())
    assert not first.drained
    second = (
        gateway.sent_changes_since(first.cursor) if sent else gateway.changes_since(first.cursor)
    )
    assert second.drained


def test_resolved_sent_select_failure_is_not_confirmed_absence():
    client = SentFolderImap(list_lines=SENT_LIST, reject={"Sent Messages"})
    gateway = _sent_gateway(client)
    with pytest.raises(ImapError) as error:
        gateway.sent_scope()
    assert error.value.code == "imap_sent_unreadable"
    client.reject.clear()
    assert gateway.sent_scope() == "available"
    assert _sent_gateway(SentFolderImap(list_lines=NO_SENT_LIST)).sent_scope() == "unavailable"


@pytest.mark.parametrize("single_thread", [False, True])
def test_followed_purge_bounds_every_candidate_query_under_sqlite_limit(
    tmp_path, monkeypatch, single_thread
):
    store, state, _ = followed(tmp_path)
    for index in range(520):
        seed(store, f"extra-{index}", thread_id="vendor-thread" if single_thread else f"t-{index}")
    later = NOW + timedelta(days=35)
    progress = state.load_or_start(later, 30, {"units": []})
    for thread in state.threads():
        state.watermark(progress, thread)
    state.complete(progress)
    original = store.connection

    @contextmanager
    def limited():
        with original() as db:
            db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
            yield db

    monkeypatch.setattr(store, "connection", limited)
    assert (
        store.purge(30, now=later, after_poll=True, polled_scope=("gmail", "mailbox", KEY)) == 521
    )
    assert store.recent(1) == []


@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize("kind", ["text/plain", "text/html"])
def test_gmail_storage_honors_declared_charset_for_each_text_part(kind, external):
    from test_thread_bodies_review import gmail_with_payload

    text = "Café € ‘net 15’"
    raw = (f"<p>{text}</p>" if kind == "text/html" else text).encode("windows-1252")
    data = base64.urlsafe_b64encode(raw).decode()
    payload = {
        "mimeType": kind,
        "headers": [{"name": "cOnTeNt-TyPe", "value": f'{kind}; charset="windows-1252"'}],
        "body": {"attachmentId": "text", "size": len(raw)} if external else {"data": data},
    }
    gateway, _ = gmail_with_payload(payload)
    if external:
        # Keep full-response and external response distinct at the actual request boundary.
        count = 0

        def execute(request):
            nonlocal count
            count += 1
            return request.execute() if count == 1 else {"data": data}

        gateway._execute_request = execute
    assert gateway.stored_body("m", scope=frozenset({"inbox"})) == (text, len(text))


@pytest.mark.parametrize("charset", [None, "unknown-vendor-charset"])
def test_storage_charset_fallback_preserves_valid_utf8(charset):
    text = "Café €"
    headers = (
        []
        if charset is None
        else [{"name": "Content-Type", "value": f"text/plain; charset={charset}"}]
    )
    assert mime.extract_stored_body(
        {
            "mimeType": "text/plain",
            "headers": headers,
            "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()},
        }
    ) == (text, len(text))


@pytest.mark.parametrize("mode", ["partial_inbox", "failed_sent_select"])
def test_real_imap_incomplete_poll_preserves_thread_until_reply(tmp_path, monkeypatch, mode):
    from dataclasses import replace

    from test_service import FakeModel, SentPollingGateway, _active_entitlement, _imap_sent_scaffold

    from eom_email_watcher import body_state, imap, service
    from eom_email_watcher.db import AdmissionProvenance
    from eom_email_watcher.mailbox import MailboxSession, MessageMetadata
    from eom_email_watcher.reconcile_state import ReconcileState
    from eom_email_watcher.service import Watcher

    monkeypatch.setattr(imap, "MAX_INCREMENTAL_MESSAGE_IDS", 2)
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
        advanced = False

        def changes_since(self, saved):
            if mode == "partial_inbox" and self.advanced:
                return ImapGateway._incremental_changes(self, saved, sent=False)
            return super().changes_since(saved)

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
            if mode == "failed_sent_select":
                return ImapGateway.sent_scope(self)
            return super().sent_scope()

        def stored_body(self, source, *, scope):
            return "reply", 5

        def metadata(self, source):
            if mode == "partial_inbox" and not source.endswith(":10"):
                return MessageMetadata(
                    source,
                    None,
                    "unwatched@example.org",
                    None,
                    "Unrelated",
                    service.datetime.now(UTC).isoformat(),
                    frozenset(),
                    locations=frozenset({"inbox"}),
                )
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
                locations=frozenset({"inbox" if mode == "partial_inbox" else "sent"}),
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
    gateway.advanced = True
    gateway.sent_ids = () if mode == "partial_inbox" else (sent_id,)
    gateway.fail_sent = mode == "failed_sent_select"
    result = watcher.check()
    assert result["purged"] == 0
    assert any(row["message_id"] == old_id for row in store.recent(10))
    gateway.fail_sent = False
    watcher.check()
    rows = store.recent(10)
    assert len(rows) == 2
    with store.connection() as db:
        assert {r[0] for r in db.execute("SELECT thread_key FROM logical_messages")} == {old_thread}
