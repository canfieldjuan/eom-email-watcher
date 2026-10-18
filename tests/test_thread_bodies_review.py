"""Fail-first public fixtures for M2.4 review owner consolidation."""

import base64
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from conftest import local_removal
from test_service import (
    FakeModel,
    SentPollingGateway,
    _active_entitlement,
    _imap_sent_scaffold,
)
from test_thread_bodies import body_runner, followed, prepare_derived
from test_thread_reconcile import KEY, NOW, seed

from eom_email_watcher import body_state, mime, reconcile, service
from eom_email_watcher.db import AdmissionProvenance
from eom_email_watcher.gmail import GmailGateway
from eom_email_watcher.mailbox import MailboxMessageInvalid, MailboxSession, MessageMetadata
from eom_email_watcher.reconcile_state import ReconcileState
from eom_email_watcher.service import Watcher


def test_failed_sent_poll_preserves_covered_history_then_recovers_reply(tmp_path, monkeypatch):
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
    result = watcher.check()
    assert result["purged"] == 0
    assert any(row["message_id"] == old_id for row in store.recent(10))
    gateway.fail_sent = False
    watcher.check()
    rows = store.recent(10)
    assert len(rows) == 2
    with store.connection() as db:
        assert {r[0] for r in db.execute("SELECT thread_key FROM logical_messages")} == {old_thread}


def test_parked_derived_resumes_sync_for_a_new_follow(tmp_path, monkeypatch):
    watcher, state, gateway = body_runner(tmp_path, monkeypatch, {"m": ("quote", 5)})
    progress = prepare_derived(state)
    progress["payload"].update(replay_discovery=False, snapshots={})
    state.save(progress)
    body_state.record(watcher.store, "m", ("quote", 5), fetched_at=NOW)
    seed(watcher.store, "new", thread_id="new-thread")
    body_state.record(watcher.store, "new", ("new", 3), fetched_at=NOW)
    gateway.reconcile_page = lambda *a, **kw: reconcile.ReconcilePage((), None)
    reconcile.run(watcher, KEY, "unavailable", label_selectors=(), now=NOW)
    assert all(t["watermark"] for t in state.threads())
    reconcile.run(
        watcher,
        KEY,
        "unavailable",
        label_selectors=(),
        now=datetime.now(UTC) + timedelta(seconds=1),
    )
    assert not state.stale()


@pytest.mark.parametrize("surviving_copy", [False, True])
def test_definitively_invalid_copy_is_terminal_or_uses_a_sibling(
    tmp_path, monkeypatch, surviving_copy
):
    watcher, state, gateway = body_runner(
        tmp_path, monkeypatch, {"m": MailboxMessageInvalid("imap_bodystructure_invalid", "invalid")}
    )
    if surviving_copy:
        seed(watcher.store, "z", rfc_message_id="same@vendor")
        gateway_result = gateway.stored_body
        gateway.stored_body = lambda source, **kw: (
            ("good", 4) if source == "z" else gateway_result(source, **kw)
        )
    prepare_derived(state)
    reconcile.run(watcher, KEY, "unavailable", label_selectors=(), now=NOW)
    assert not state.stale()
    with watcher.store.connection() as db:
        if surviving_copy:
            assert db.execute("SELECT text FROM message_bodies").fetchone()[0] == "good"
        else:
            assert (
                db.execute("SELECT reason FROM message_body_unavailable").fetchone()[0]
                == "source_gone"
            )


@pytest.mark.parametrize(
    "container", ["blockquote", 'div class="gmail_quote"', 'div id="divRplyFwdMsg"']
)
def test_wrapped_quote_lines_remain_quoted(container):
    tag = container.split()[0]
    body, _ = mime.stored_body_text(
        f"<{container}>price effective\r\nNovember 1 and net 15.</{tag}>", is_html=True
    )
    assert all(line.startswith("> ") for line in body.splitlines())
    assert "November 1" in body
    pre, _ = mime.stored_body_text(f"<{container}><pre>first\nsecond</pre></{tag}>", is_html=True)
    assert pre == "> first\n> second"


def test_unclosed_html_storage_is_bounded_in_time():
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from eom_email_watcher.mime import stored_body_text; "
            "body,total=stored_body_text('<p>x'*262144,is_html=True); "
            "assert len(body)==200000 and total>len(body); print('bounded')",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "bounded"


def gmail_with_payload(payload, *, external_error=None):
    requests = []

    def full(**kwargs):
        requests.append(("full", kwargs))
        return SimpleNamespace(execute=lambda: {"labelIds": ["INBOX"], "payload": payload})

    def external(**kwargs):
        requests.append(("part", kwargs))
        def execute():
            if external_error is not None:
                raise external_error
            return {
                "data": base64.urlsafe_b64encode(
                    b"<blockquote>External quote</blockquote>"
                ).decode()
            }
        return SimpleNamespace(execute=execute)

    api = SimpleNamespace(get=full, attachments=lambda: SimpleNamespace(get=external))
    return GmailGateway(
        SimpleNamespace(users=lambda: SimpleNamespace(messages=lambda: api))
    ), requests


def test_gmail_external_text_uses_budgeted_part_fetch_instead_of_empty_capture():
    gateway, requests = gmail_with_payload(
        {"mimeType": "text/html", "body": {"attachmentId": "external", "size": 38}}
    )
    gateway._reconcile_budget = reconcile.ReconcileBudget(lambda: True)
    assert gateway.stored_body("m", scope=frozenset({"inbox"})) == ("> External quote", 16)
    assert [r[0] for r in requests] == ["full", "part"]
    assert gateway._reconcile_budget.calls == 2


@pytest.mark.parametrize("kind", ["text", "attachment"])
@pytest.mark.parametrize("status", [404, 403, 410, 429, 500])
def test_gmail_attachment_resource_errors_have_one_terminal_classification(kind, status):
    from googleapiclient.errors import HttpError

    from eom_email_watcher.gmail import GmailError
    from eom_email_watcher.mailbox import MailboxMessageUnavailable

    error = HttpError(SimpleNamespace(status=status, reason="public failure"), b'{}')
    gateway, requests = gmail_with_payload(
        {"mimeType": "text/html", "body": {"attachmentId": "external"}},
        external_error=error,
    )
    with pytest.raises(MailboxMessageUnavailable if status == 404 else GmailError) as caught:
        if kind == "text":
            gateway.stored_body("m", scope=frozenset({"inbox"}))
        else:
            gateway.attachment_bytes("m", "part", "external")
    assert isinstance(caught.value, MailboxMessageUnavailable) is (status == 404)
    assert caught.value.__cause__ is error
    assert requests[-1] == ("part", {"userId": "me", "messageId": "m", "id": "external"})


@pytest.mark.parametrize("sibling", [False, True])
def test_real_gmail_missing_text_finishes_reconcile_or_uses_sibling(tmp_path, monkeypatch, sibling):
    from googleapiclient.errors import HttpError

    provider, requests = gmail_with_payload(
        {"mimeType": "text/html", "body": {"attachmentId": "external"}},
        external_error=HttpError(SimpleNamespace(status=404, reason="gone"), b'{}'),
    )
    watcher, state, gateway = body_runner(tmp_path, monkeypatch, {"z-copy": ("quote", 5)})
    if sibling:
        seed(watcher.store, "z-copy", rfc_message_id="same@vendor")
    fallback = gateway.stored_body
    gateway.stored_body = lambda source, **kw: (
        provider.stored_body(source, **kw) if source == "m" else fallback(source, **kw)
    )
    prepare_derived(state)
    reconcile.run(watcher, KEY, "unavailable", label_selectors=(), now=NOW)
    assert not state.stale()
    assert [r[0] for r in requests] == ["full", "part"]
    with watcher.store.connection() as db:
        if sibling:
            assert db.execute("SELECT text FROM message_bodies").fetchone()[0] == "quote"
            assert not db.execute("SELECT 1 FROM message_body_unavailable").fetchone()
        else:
            assert not db.execute("SELECT 1 FROM message_bodies").fetchone()
            assert db.execute("SELECT reason FROM message_body_unavailable").fetchone()[0] == (
                "source_gone"
            )


def test_rendered_empty_html_alternative_falls_back_to_plain():
    def part(kind, data):
        return {
            "mimeType": kind,
            "body": {"data": base64.urlsafe_b64encode(data.encode()).decode()},
        }

    payload = {
        "mimeType": "multipart/alternative",
        "parts": [part("text/plain", "Faithful plain"), part("text/html", '<img src="cid:x">')],
    }
    assert mime.extract_stored_body(payload) == ("Faithful plain", 14)


def test_imap_empty_html_falls_back_inside_mixed_body():
    from test_imap import FakeImap, credentials, message_id

    from eom_email_watcher.imap import ImapGateway

    client = FakeImap()
    plain = b"Faithful plain"
    html = b'<img src="cid:x">'
    client.bodystructure = (
        b'((("TEXT" "PLAIN" NIL NIL NIL "8BIT" ' + str(len(plain)).encode() + b" 1)"
        b'("TEXT" "HTML" NIL NIL NIL "8BIT" ' + str(len(html)).encode() + b' 1)"ALTERNATIVE")'
        b'("TEXT" "PLAIN" NIL NIL NIL "8BIT" 6 1)"MIXED")'
    )
    client.sections = {"1.1": plain, "1.2": html, "2": b"footer"}
    gateway = ImapGateway(credentials(), lambda *a: client)
    assert gateway.stored_body(message_id(), scope=frozenset({"inbox"})) == (
        "Faithful plain\nfooter",
        21,
    )


def test_followed_purge_does_not_rescan_full_mailbox_per_thread(tmp_path, monkeypatch):
    store, state, _ = followed(tmp_path)
    for index in range(10):
        seed(store, f"thread-{index}", thread_id=f"native-{index}")
    later = NOW + timedelta(days=35)
    progress = state.load_or_start(later, 30, {"units": []})
    for thread in state.threads():
        state.watermark(progress, thread)
    state.complete(progress)
    original = store.connection
    queries = []

    @contextmanager
    def traced():
        with original() as db:
            db.set_trace_callback(queries.append)
            yield db

    monkeypatch.setattr(store, "connection", traced)
    assert store.purge(30, now=later, after_poll=True, polled_scope=("gmail", "mailbox", KEY)) == 11
    scans = [q for q in queries if "GROUP BY canonical" in q]
    assert len(scans) <= 8, len(scans)


def test_recovery_backoff_still_finishes_pending_body_checkpoint(tmp_path, monkeypatch):
    from test_service import config

    store, state, _ = followed(tmp_path)
    store.activate_mail_account("gmail", "mailbox")
    now = datetime.now(UTC)
    store.set_state("100", now, provider="gmail", account_id="mailbox", mailbox_identity_key=KEY)
    store.create_gmail_recovery_state(
        "mailbox", KEY, 0, (("vendor@example.com", "Vendor"),), (), 1, 2, "200", now=now
    )
    store.record_gmail_recovery_backoff(
        "mailbox",
        KEY,
        failure_code="gmail_recovery_provider_unavailable",
        next_retry_at=(now + timedelta(minutes=10)).isoformat(),
        now=now,
    )
    body_state.record(store, "m", ("body", 4), fetched_at=now)
    local_removal(store, "delete_message", "m")
    assert service._gmail_recovery_backoff_admission(store) is not None
    monkeypatch.setattr(
        service, "load_configured_mailbox", lambda *a: pytest.fail("loaded mailbox")
    )
    result = service.run_watcher_check(config(tmp_path), store, FakeModel())
    assert result["recovery_pending"]
    with store.connection() as db:
        assert not db.execute(
            "SELECT 1 FROM maintenance_state WHERE key='checkpoint_pending'"
        ).fetchone()


def test_partial_gmail_history_batch_is_not_a_completed_poll():
    from test_gmail import FakeHistoryService

    from eom_email_watcher import gmail

    response = {
        "historyId": "99999",
        "history": [
            {
                "messagesAdded": [
                    {"message": {"id": f"m-{index}", "labelIds": ["INBOX"]}}
                    for index in range(gmail.MAX_INCREMENTAL_MESSAGE_IDS + 1)
                ]
            }
        ],
    }
    gateway = GmailGateway(FakeHistoryService(response))
    partial = gateway.changes_since("12345")
    assert not partial.drained
    assert gateway.changes_since(partial.cursor).drained


@pytest.mark.parametrize("size", [15, 16, 17])
def test_storage_input_bound_rejects_only_overflow_before_parsing(monkeypatch, size):
    monkeypatch.setattr(mime, "MAX_STORED_BODY_INPUT_CHARS", 16)
    if size > 16:
        with pytest.raises(mime.StoredBodyInvalid):
            mime.stored_body_text("x" * size, is_html=True)
    else:
        assert mime.stored_body_text("x" * size, is_html=True) == ("x" * size, size)


def test_gmail_external_text_failure_never_returns_completed_empty_body():
    from eom_email_watcher.gmail import GmailError

    gateway, requests = gmail_with_payload(
        {"mimeType": "text/html", "body": {"attachmentId": "external"}}
    )

    def fail(request):
        if requests[-1][0] == "part":
            raise GmailError("temporarily unavailable")
        return request.execute()

    gateway._execute_request = fail
    with pytest.raises(GmailError):
        gateway.stored_body("m", scope=frozenset({"inbox"}))
