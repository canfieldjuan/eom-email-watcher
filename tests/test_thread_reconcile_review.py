"""Review regressions at the reconcile input and outcome owners."""

import imaplib
import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from test_imap import FakeImap, credentials
from test_thread_reconcile import KEY, NOW, Pages, runner, seed, state_store
from test_thread_reconcile import reconcile_clock as reconcile_clock

from eom_email_watcher import reconcile, service
from eom_email_watcher.imap import ImapGateway
from eom_email_watcher.mailbox import MailboxError
from eom_email_watcher.microsoft365 import Microsoft365Gateway


def imap_unit(gateway, client):
    gateway._active_client = client
    gateway._ensure_selected(client, "INBOX")
    return dict(
        folder="inbox",
        addresses=["vendor@example.com"],
        token=None,
        snapshot=gateway.reconcile_snapshot("inbox"),
    )


@pytest.mark.parametrize("first", [None, b'1 (UID 1 INTERNALDATE "not-a-date")'])
def test_imap_bad_candidate_does_not_finish_listing(first):
    class Dates(FakeImap):
        def uid(self, command, *args):
            if command == "FETCH" and args[-1] == "(UID INTERNALDATE)":
                uid = int(args[0])
                if uid == 1:
                    return "OK", [first] if first is not None else []
                stamp = (NOW - timedelta(days=1)).strftime("%d-%b-%Y %H:%M:%S +0000")
                return "OK", [f'{uid} (UID {uid} INTERNALDATE "{stamp}")'.encode()]
            return super().uid(command, *args)

    client = Dates(search=b"1 2", uid_next=3)
    gateway = ImapGateway(credentials())
    unit = imap_unit(gateway, client)
    items = []
    for _ in range(5):
        page = gateway.reconcile_page(unit, start=NOW, cutoff=NOW - timedelta(days=30))
        items.extend(page.items)
        unit["token"] = page.token
        if page.token is None:
            break
    assert len(items) == 1 and items[0].message_id.endswith(":2")
    assert page.token is None


def test_imap_date_fetch_selects_requested_uid():
    class Dates(FakeImap):
        def uid(self, command, *args):
            if command == "FETCH" and args[-1] == "(UID INTERNALDATE)":
                stamp = (NOW - timedelta(days=1)).strftime("%d-%b-%Y %H:%M:%S +0000")
                return "OK", [
                    b"99 (UID 99 FLAGS (\\Seen))",
                    f'1 (UID 1 INTERNALDATE "{stamp}")'.encode(),
                ]
            return super().uid(command, *args)

    client = Dates(search=b"1", uid_next=2)
    gateway = ImapGateway(credentials())
    page = gateway.reconcile_page(
        imap_unit(gateway, client), start=NOW, cutoff=NOW - timedelta(days=30)
    )
    assert len(page.items) == 1


def test_sparse_imap_search_covers_frozen_uid_range_once():
    client = FakeImap(search=b"", uid_next=100_001)
    gateway = ImapGateway(credentials())
    page = gateway.reconcile_page(
        imap_unit(gateway, client), start=NOW, cutoff=NOW - timedelta(days=30)
    )
    assert page.token is None
    searches = [call for call in client.calls if call[:2] == ("uid", "SEARCH")]
    assert len(searches) == 1 and "1:100000" in searches[0]


@pytest.mark.parametrize("error", [TimeoutError("read timed out"), imaplib.IMAP4.error("BAD")])
def test_imap_response_failure_maps_to_unit_transport_error(error):
    def response(*args, **kwargs):
        raise error

    client = SimpleNamespace(_command=lambda *args: "tag", _command_complete=response)
    gateway = SimpleNamespace(_active_client=client)
    with (
        pytest.raises(MailboxError),
        reconcile.budget_session(gateway, reconcile.ReconcileBudget(lambda: True)),
    ):
        tag = client._command("UID", "SEARCH")
        client._command_complete("UID", tag)


@pytest.mark.parametrize("path", ["mailFolders('INBOX')", "MailFolders/inbox"])
def test_graph_reconcile_accepts_canonical_folder_link_forms(path):
    link = f"https://graph.microsoft.com/v1.0/me/{path}/messages/delta?$skiptoken=next"
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            200, json={"value": [], **({"@odata.nextLink": link} if len(calls) == 1 else {})}
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        gateway = Microsoft365Gateway("test", "owner@example.com", client)
        unit = dict(folder="inbox", token=None)
        page = gateway.reconcile_page(unit, start=NOW, cutoff=NOW - timedelta(days=30))
        unit["token"] = page.token
        assert (
            gateway.reconcile_page(unit, start=NOW, cutoff=NOW - timedelta(days=30)).token is None
        )
    assert len(calls) == 2


def test_graph_bad_item_does_not_poison_valid_page():
    values = [
        {"id": "bad", "receivedDateTime": "invalid"},
        {
            "id": "good",
            "conversationId": "native",
            "receivedDateTime": (NOW - timedelta(days=1)).isoformat(),
        },
    ]
    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"value": values}))
    ) as client:
        gateway = Microsoft365Gateway("test", "owner@example.com", client)
        page = gateway.reconcile_page(
            dict(folder="inbox", token=None), start=NOW, cutoff=NOW - timedelta(days=30)
        )
    assert [item.message_id for item in page.items] == ["good"]


def test_rejected_headers_leave_no_durable_replay(tmp_path, monkeypatch):
    gateway = Pages(provider="microsoft365")
    watcher, state = runner(tmp_path, monkeypatch, gateway, "microsoft365")
    budget_type = reconcile.ReconcileBudget
    monkeypatch.setattr(reconcile, "ReconcileBudget", lambda allowed: budget_type(allowed, calls=2))
    reconcile.run(watcher, KEY, "available", label_selectors=(), now=NOW)
    with watcher.store.connection() as db:
        rows = [dict(row) for row in db.execute("SELECT * FROM reconcile_items")]
    assert rows
    assert "new@example.com" not in json.dumps(rows)
    assert "Message" not in json.dumps(rows)
    assert state.stale()


def test_unobserved_sent_does_not_invalidate_or_certify(tmp_path, monkeypatch):
    gateway = Pages()
    watcher, state = runner(tmp_path, monkeypatch, gateway)
    reconcile.observe(watcher, KEY, True, "available", NOW)
    before = state.generation()
    reconcile.run(watcher, KEY, None, label_selectors=(), now=NOW)
    assert state.generation() == before
    assert state.stale() and not gateway.calls


def test_recovery_completion_invalidates_at_transaction_owner(tmp_path):
    store, state = state_store(tmp_path)
    store.activate_mail_account("gmail", "mailbox")
    seed(store)
    progress = state.load_or_start(NOW, 30, {})
    state.complete(progress)
    before = state.generation()
    store.create_gmail_recovery_state("mailbox", KEY, 0, (), (), 1, 2, "200", now=NOW)
    store.store_gmail_recovery_page("mailbox", KEY, (), None, now=NOW)
    store.complete_gmail_recovery("mailbox", KEY, now=NOW)
    assert state.generation() == before + 1
    assert state.stale()


def test_new_follow_schedules_sync_without_discovery(tmp_path, monkeypatch):
    gateway = Pages()
    watcher, state = runner(tmp_path, monkeypatch, gateway)
    reconcile.run(watcher, KEY, "unavailable", label_selectors=(), now=NOW)
    before = state.generation()
    gateway.calls.clear()
    seed(watcher.store, "new-vendor", thread_id="new-thread")
    reconcile.run(watcher, KEY, "unavailable", label_selectors=(), now=NOW + timedelta(hours=1))
    assert state.generation() == before
    assert gateway.calls and all(call[0] == "native" for call in gateway.calls)


def test_polling_follow_records_catchup_before_pass_resumes(tmp_path, monkeypatch):
    watcher, state = runner(tmp_path, monkeypatch, Pages())
    progress = state.load_or_start(NOW, 30, {"stage": "discovery", "units": [], "catchup": {}})
    seed(watcher.store, "polled-vendor")
    resumed = state.load_or_start(NOW + timedelta(hours=1), 30, {})
    assert state.catchup_targets(resumed)
    assert resumed["version"] == progress["version"]


def test_follow_and_catchup_rollback_together(tmp_path, monkeypatch):
    from eom_email_watcher import reconcile_state

    watcher, state = runner(tmp_path, monkeypatch, Pages())
    progress = state.load_or_start(NOW, 30, {"stage": "discovery", "units": []})
    original = reconcile_state.record_follow_transition

    def fail_after_target(db, scope):
        original(db, scope)
        assert db.execute("SELECT 1 FROM reconcile_catchup").fetchone()
        raise RuntimeError("fail the follow transaction")

    monkeypatch.setattr(reconcile_state, "record_follow_transition", fail_after_target)
    with pytest.raises(RuntimeError, match="follow transaction"):
        seed(watcher.store, "polled-vendor")
    assert not state.catchup_targets(progress)
    with watcher.store.connection() as db:
        assert not db.execute("SELECT 1 FROM thread_follow WHERE followed=1").fetchone()
        assert not db.execute(
            "SELECT 1 FROM messages WHERE provider_message_id='polled-vendor'"
        ).fetchone()


def test_follow_without_progress_creates_no_catchup(tmp_path, monkeypatch):
    watcher, state = runner(tmp_path, monkeypatch, Pages())
    seed(watcher.store, "polled-vendor")
    with watcher.store.connection() as db:
        assert not db.execute("SELECT 1 FROM reconcile_catchup").fetchone()


def test_late_follow_remains_stale_until_frozen_catchup(tmp_path, monkeypatch):
    gateway = Pages(provider="microsoft365")
    watcher, state = runner(tmp_path, monkeypatch, gateway, "microsoft365")
    budget_type = reconcile.ReconcileBudget
    monkeypatch.setattr(reconcile, "ReconcileBudget", lambda allowed: budget_type(allowed, calls=1))
    reconcile.run(watcher, KEY, "available", label_selectors=(), now=NOW)
    checked = NOW + timedelta(hours=1)
    tail = replace(gateway.metadata("tail"), received_at=(NOW + timedelta(minutes=30)).isoformat())
    assert (
        watcher._capture_metadata(
            tail,
            mailbox_identity_key=KEY,
            label_selectors=(),
            gated_allowed=True,
            vendor_snapshot=watcher.store.vendor_index(),
            checked_at=checked,
            retention_cutoff=checked - timedelta(days=30),
            folders=frozenset({"inbox", "sent"}),
        )
        == 0
    )
    monkeypatch.setattr(reconcile, "ReconcileBudget", budget_type)
    reconcile.run(watcher, KEY, "available", label_selectors=(), now=checked)
    assert state.stale()
    # Reopen from disk. A fresh process must retain the obligation and must not
    # be able to certify the exhausted original interval.
    from test_db import Store

    from eom_email_watcher.reconcile_state import ReconcileState

    watcher.store = Store(watcher.store.path)
    watcher.store.initialize()
    state = ReconcileState(watcher.store, ("microsoft365", "mailbox", KEY))
    progress = state.load_or_start(checked, 30, {})
    assert progress["payload"]["stage"] == "catchup_wait"
    with pytest.raises(RuntimeError, match="catch-up remains pending"):
        state.complete(progress)
    original = gateway.reconcile_page

    def with_tail(unit, **bounds):
        if unit.get("threads") and unit["folder"] == "inbox":
            return reconcile.ReconcilePage(
                (reconcile.ReconcileItem("tail", "native", tail.received_at),), None
            )
        return original(unit, **bounds)

    gateway.reconcile_page = with_tail
    original_metadata = gateway.metadata
    gateway.metadata = lambda source: tail if source == "tail" else original_metadata(source)
    reconcile.run(watcher, KEY, "available", label_selectors=(), now=checked + timedelta(hours=1))
    assert not state.stale()
    with watcher.store.connection() as db:
        assert db.execute("SELECT 1 FROM messages WHERE provider_message_id='tail'").fetchone()


def test_scheduled_vendor_only_check_reaches_capture_owner(tmp_path, monkeypatch):
    gateway = Pages()
    watcher, state = runner(tmp_path, monkeypatch, gateway)
    monkeypatch.setattr(service, "load_configured_mailbox", lambda *args: watcher.mailbox)
    calls = []
    monkeypatch.setattr(
        service.Watcher,
        "check",
        lambda self, **kwargs: (
            calls.append(self) or {"added": 0, "summarized": 0, "fallback": 0, "purged": 0}
        ),
    )
    service.run_watcher_check(watcher.config, watcher.store, watcher.model, dry_run=True)
    assert calls


@pytest.mark.parametrize("position", ["old", "outside", "lower", "upper", "good"])
def test_gmail_preflight_blocks_headers_before_scope_check(tmp_path, monkeypatch, position):
    from test_gmail import FakeRecoveryService, FakeRequest

    from eom_email_watcher.gmail import GmailGateway

    transport = FakeRecoveryService({"messages": [{"id": "candidate", "threadId": "native"}]})
    formats = []

    def get(**kwargs):
        formats.append(kwargs["format"])
        received = (
            NOW - timedelta(days=30, milliseconds=500)
            if position == "old"
            else NOW - timedelta(days=1)
        )
        if position == "lower":
            received = NOW - timedelta(days=30)
        elif position == "upper":
            received = NOW
        return FakeRequest(
            {
                "id": "candidate",
                "threadId": "native",
                "internalDate": str(int(received.timestamp() * 1000)),
                "labelIds": ["TRASH"] if position == "outside" else ["INBOX"],
                "payload": {"headers": [{"name": "From", "value": "vendor@example.com"}]},
            }
        )

    transport.messages.get = get
    gateway = GmailGateway(transport)
    watcher, state = runner(tmp_path, monkeypatch, gateway)
    reconcile.run(watcher, KEY, "unavailable", label_selectors=(), now=NOW)
    if position in {"lower", "good"}:
        assert formats[0] == "minimal" and "metadata" in formats
        with watcher.store.connection() as db:
            assert db.execute(
                "SELECT 1 FROM messages WHERE provider_message_id='candidate'"
            ).fetchone()
    else:
        assert formats == ["minimal"]


def test_gmail_invalid_large_date_is_a_candidate_failure():
    from test_gmail import FakeRecoveryService, FakeRequest

    from eom_email_watcher.gmail import GmailGateway
    from eom_email_watcher.mailbox import MailboxMessageInvalid

    transport = FakeRecoveryService({})
    transport.messages.get = lambda **kwargs: FakeRequest(
        {
            "id": "candidate",
            "internalDate": "1" * 400,
            "labelIds": ["INBOX"],
        }
    )
    with pytest.raises(MailboxMessageInvalid):
        GmailGateway(transport).reconcile_preflight(
            "candidate", start=NOW, cutoff=NOW - timedelta(days=30)
        )


def test_gmail_listing_preserves_fractional_frozen_upper_bound():
    from test_gmail import FakeRecoveryService

    from eom_email_watcher.gmail import GmailGateway

    transport = FakeRecoveryService({})
    GmailGateway(transport).reconcile_page(
        dict(token=None), start=NOW + timedelta(milliseconds=750), cutoff=NOW - timedelta(days=30)
    )
    assert f"before:{int(NOW.timestamp()) + 1}" in transport.messages.calls[0]["q"]


@pytest.mark.parametrize("path", ["mailFolders/sentitems", "mailFolders('other')"])
def test_graph_reconcile_rejects_other_folder_before_continuation(path):
    link = f"https://graph.microsoft.com/v1.0/me/{path}/messages/delta?$skiptoken=next"
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"value": [], "@odata.nextLink": link})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        gateway = Microsoft365Gateway("test", "owner@example.com", client)
        gateway._folder_ids = {"inbox": "actual-inbox"}
        with pytest.raises(MailboxError, match="another folder"):
            gateway.reconcile_page(
                dict(folder="inbox", token=None), start=NOW, cutoff=NOW - timedelta(days=30)
            )
    assert len(calls) == 1


def test_graph_reconcile_accepts_resolved_opaque_folder_identity():
    link = "https://graph.microsoft.com/v1.0/me/mailFolders('actual-inbox')/messages/delta?$skiptoken=next"
    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"value": []}))
    ) as client:
        gateway = Microsoft365Gateway("test", "owner@example.com", client)
        gateway._folder_ids = {"inbox": "actual-inbox"}
        assert (
            gateway.reconcile_page(
                dict(folder="inbox", token=link), start=NOW, cutoff=NOW - timedelta(days=30)
            ).token
            is None
        )


def test_graph_thread_watermark_waits_for_both_sync_folders(tmp_path, monkeypatch):
    gateway = Pages(provider="microsoft365")
    watcher, state = runner(tmp_path, monkeypatch, gateway, "microsoft365")
    reconcile.observe(watcher, KEY, True, "available", NOW)
    seed(watcher.store, provider="microsoft365")
    from eom_email_watcher import body_state

    body_state.record(watcher.store, "m", ("existing", 8), fetched_at=NOW)
    state.complete(state.load_or_start(NOW, 30, {}))
    original = gateway.reconcile_page

    def fail_sent(unit, **bounds):
        if unit["folder"] == "sent":
            raise MailboxError("Sent transient error")
        return original(unit, **bounds)

    gateway.reconcile_page = fail_sent
    reconcile.run(watcher, KEY, "available", label_selectors=(), now=NOW)
    assert state.stale()
    with watcher.store.connection() as db:
        assert not db.execute("SELECT * FROM thread_watermarks").fetchall()


def test_scheduled_vendor_only_check_captures_historical_message(tmp_path, monkeypatch):
    from test_thread_reconcile import HistoricalGmail

    watcher, state = runner(tmp_path, monkeypatch, HistoricalGmail())
    monkeypatch.setattr(service, "load_configured_mailbox", lambda *args: watcher.mailbox)
    result = service.run_watcher_check(
        watcher.config, watcher.store, watcher.model, deliver_notifications=False
    )
    assert result["active"]
    with watcher.store.connection() as db:
        row = db.execute(
            "SELECT admission_kind FROM messages WHERE provider_message_id='historical'"
        ).fetchone()
    assert row and row[0] == "vendor_address"


def test_scheduled_unpaid_vendor_only_check_stays_local(tmp_path, monkeypatch):
    watcher, state = runner(tmp_path, monkeypatch, Pages())
    monkeypatch.setattr(
        service, "connect_entitlement_decision", lambda: service.EntitlementDecision.MISSING
    )
    monkeypatch.setattr(
        service,
        "load_configured_mailbox",
        lambda *args: pytest.fail("unpaid idle check loaded a gateway"),
    )
    assert not service.run_watcher_check(
        watcher.config, watcher.store, watcher.model, dry_run=True
    )["active"]
