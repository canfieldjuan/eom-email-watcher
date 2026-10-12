"""The accepted reconcile pass through real database and watcher owners."""

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from test_db import Store
from test_service import FakeGmail, FakeModel, config

from eom_email_watcher import reconcile, service
from eom_email_watcher.mailbox import (
    MailboxChanges,
    MailboxMessageUnavailable,
    MailboxSession,
    MessageMetadata,
)
from eom_email_watcher.reconcile_state import ReconcileState
from eom_email_watcher.service import Watcher


def test_vendor_change_invalidates_each_configured_identity(tmp_path):
    store = Store(tmp_path / "mail.db")
    store.initialize()
    for account, key in (("first", "a" * 64), ("inactive", "b" * 64)):
        store.register_mail_account("gmail", account, display_name=account)
        store.reconcile_mailbox_identity("gmail", account, key)
    vendor = store.create_vendor("Vendor")
    with store.connection() as db:
        before = dict(db.execute("SELECT account_id,generation FROM coverage_generation"))
    store.add_vendor_address(vendor, "vendor@example.com")
    with store.connection() as db:
        after = dict(db.execute("SELECT account_id,generation FROM coverage_generation"))
    assert after == {account: generation + 1 for account, generation in before.items()}
    assert set(after) == {"first", "inactive"}


class HistoricalGmail(FakeGmail):
    def changes_since(self, cursor):
        return MailboxChanges((), "200")

    def metadata(self, message_id, **kwargs):
        return MessageMetadata(
            message_id,
            "vendor-thread",
            "vendor@example.com",
            None,
            "Earlier quote",
            (datetime.now(UTC) - timedelta(days=2)).isoformat(),
            frozenset({"INBOX"}),
            locations=frozenset({"inbox"}),
        )

    def reconcile_page(self, unit, **kwargs):
        from eom_email_watcher.reconcile import ReconcileItem, ReconcilePage

        return ReconcilePage((ReconcileItem("historical", "vendor-thread"),), None)


def test_check_backfills_vendor_mail_before_current_poll_cursor(tmp_path, monkeypatch):
    monkeypatch.setattr(
        service, "connect_entitlement_decision", lambda: service.EntitlementDecision.ACTIVE
    )
    cfg = replace(config(tmp_path), senders=())
    store = Store(cfg.database_file)
    store.initialize()
    vendor = store.create_vendor("Vendor")
    store.add_vendor_address(vendor, "vendor@example.com")
    watcher = Watcher(cfg, store, HistoricalGmail(), FakeModel())
    watcher.check(deliver_notifications=False)
    with store.connection() as db:
        row = db.execute(
            "SELECT admission_kind FROM messages WHERE provider_message_id='historical'"
        ).fetchone()
    assert row is not None and row[0] == "vendor_address"


def test_imap_reconcile_uses_nested_search_and_internal_date_bound():
    from test_imap import FakeImap, credentials

    from eom_email_watcher.imap import ImapGateway

    class Dates(FakeImap):
        def uid(self, command, *args):
            if command == "FETCH" and args[-1] == "(UID INTERNALDATE)":
                self.calls.append(("uid", command, *args))
                stamp = (
                    "04-Sep-2026 10:16:00 -0500"
                    if str(args[0]) == "1"
                    else "03-Sep-2026 00:00:00 +1400"
                )
                return "OK", [f'{args[0]} (UID {args[0]} INTERNALDATE "{stamp}")'.encode()]
            return super().uid(command, *args)

    client = Dates(search=b"1 2", uid_next=3)
    gateway = ImapGateway(credentials())
    gateway._active_client = client
    gateway._ensure_selected(client, "INBOX")
    unit = dict(folder="inbox", addresses=['a"b\\c@example.com'], token=None)
    unit["snapshot"] = gateway.reconcile_snapshot("inbox")
    start = datetime(2026, 9, 5, tzinfo=UTC)
    cutoff = datetime(2026, 9, 3, tzinfo=UTC)
    items = []
    for _ in range(10):
        page = gateway.reconcile_page(unit, start=start, cutoff=cutoff)
        items.extend(page.items)
        unit["token"] = page.token
        if page.token is None:
            break
    assert len(items) == 1
    search = next(c for c in client.calls if c[:2] == ("uid", "SEARCH"))
    assert search == (
        "uid",
        "SEARCH",
        None,
        "UID",
        "1:2",
        "SINCE",
        "02-Sep-2026",
        "OR",
        "FROM",
        '"a\\"b\\\\c@example.com"',
        "OR",
        "TO",
        '"a\\"b\\\\c@example.com"',
        "CC",
        '"a\\"b\\\\c@example.com"',
    )
    assert not any("HEADER" in str(c) for c in client.calls)


NOW = datetime(2026, 10, 11, 12, tzinfo=UTC)
KEY = "f" * 64


def state_store(tmp_path, provider="gmail"):
    store = Store(tmp_path / "mail.db")
    store.initialize()
    store.register_mail_account(provider, "mailbox", display_name="Mailbox")
    store.reconcile_mailbox_identity(provider, "mailbox", KEY)
    state = ReconcileState(store, (provider, "mailbox", KEY))
    state.observe(active=True, retention_days=30, sent_folder="inbox", now=NOW)
    return store, state


def runner(tmp_path, monkeypatch, gateway, provider="gmail"):
    store, state = state_store(tmp_path, provider)
    vendor = store.create_vendor("Vendor")
    store.add_vendor_address(vendor, "vendor@example.com")
    monkeypatch.setattr(
        service, "connect_entitlement_decision", lambda: service.EntitlementDecision.ACTIVE
    )
    watcher = Watcher(
        replace(config(tmp_path), senders=(), retention_days=30),
        store,
        MailboxSession(provider, "mailbox", gateway, KEY),
        FakeModel(),
    )
    return watcher, state


def seed(store, source="m", *, provider="gmail", **kwargs):
    values = dict(
        message_id=source,
        provider=provider,
        account_id="mailbox",
        mailbox_identity_key=KEY,
        provider_message_id=source,
        thread_id="native",
        sender="vendor@example.com",
        sender_name=None,
        subject="Quote",
        received_at=(NOW - timedelta(days=2)).isoformat(),
        observed_at=NOW - timedelta(seconds=1),
    )
    values.update(kwargs)
    store.add_message(**values)


def test_progress_frozen_generation_restart_and_completion_cas(tmp_path):
    store, state = state_store(tmp_path)
    first = state.load_or_start(NOW, 30, {"stage": "discovery", "units": []})
    with store.connection() as db, pytest.raises(sqlite3.IntegrityError, match="frozen"):
        db.execute(
            "UPDATE reconcile_progress SET started_at=?, version=version+1",
            ((NOW + timedelta(days=1)).isoformat(),),
        )
    resumed = state.load_or_start(NOW + timedelta(days=1), 20, {})
    assert resumed["started_at"] == first["started_at"]
    assert resumed["retention_days"] == 30
    state.save(resumed)
    with pytest.raises(RuntimeError, match="concurrently"):
        state.complete(first)
    assert state.stale()
    state.complete(resumed)
    assert not state.stale()
    vendor = store.create_vendor("Another")
    progress = state.load_or_start(NOW, 30, {"token": "old"})
    store.add_vendor_address(vendor, "new@example.com")
    newer = state.load_or_start(NOW + timedelta(hours=1), 30, {"token": None})
    assert newer["generation"] > progress["generation"]
    assert newer["payload"]["token"] is None
    assert newer["started_at"] != progress["started_at"]


def test_generation_noop_change_reversal_and_rollback(tmp_path):
    store, state = state_store(tmp_path)
    vendor = store.create_vendor("Vendor")
    original = state.generation()
    with store.connection() as db:
        db.execute("UPDATE vendors SET display_name=display_name")
    assert state.generation() == original
    with store.connection() as db:
        db.execute("UPDATE vendors SET display_name='Temporary' WHERE vendor_id=?", (vendor,))
        db.execute("UPDATE vendors SET display_name='Vendor' WHERE vendor_id=?", (vendor,))
    assert state.generation() == original + 2
    current = state.generation()
    with pytest.raises(RuntimeError), store.connection() as db:
        db.execute("UPDATE vendors SET display_name='Rolled back'")
        raise RuntimeError("rollback")
    assert state.generation() == current
    with store.connection() as db, pytest.raises(sqlite3.IntegrityError, match="backwards"):
        db.execute("UPDATE coverage_generation SET generation=generation-1")


@pytest.mark.parametrize("change", ["retention", "sent", "entitlement", "cursor"])
def test_gap_inputs_clear_watermarks_and_stamps_at_owner(tmp_path, change):
    store, state = state_store(tmp_path)
    vendor = store.create_vendor("Vendor")
    store.add_vendor_address(vendor, "vendor@example.com")
    seed(store)
    progress = state.load_or_start(NOW, 30, {"units": []})
    assert state.watermark(progress, state.threads()[0])
    if change == "retention":
        state.observe(active=True, retention_days=60, sent_folder="inbox", now=NOW)
    elif change == "sent":
        state.observe(active=True, retention_days=30, sent_folder="another", now=NOW)
    elif change == "entitlement":
        state.observe(active=False, retention_days=30, sent_folder="inbox", now=NOW)
        state.observe(active=True, retention_days=30, sent_folder="inbox", now=NOW)
    else:
        state.gap(now=NOW)
    with store.connection() as db:
        assert not db.execute("SELECT * FROM thread_watermarks").fetchall()
        if change in {"entitlement", "cursor"}:
            assert all(
                r[0] is None for r in db.execute("SELECT recorded_at FROM message_locations")
            )
    assert state.generation() > progress["generation"]


class Pages:
    def __init__(self, *, provider="gmail", native="native", rejected=False):
        self.calls = []
        self.fetches = []
        self.native = native
        self.rejected = rejected
        self.provider = provider
        self._delta_folders = {}

    def reconcile_page(self, unit, *, start, cutoff):
        self.calls.append((unit["name"], unit["token"], start, cutoff))
        if unit["token"] == "next" and self.rejected:
            self.rejected = False
            raise reconcile.ReconcileTokenInvalid("expired")
        if self.provider == "microsoft365":
            if unit["folder"] == "sent":
                return reconcile.ReconcilePage((), None)
            return reconcile.ReconcilePage(
                tuple(
                    reconcile.ReconcileItem(m, self.native, (NOW - timedelta(days=2)).isoformat())
                    for m in ("a-reply", "z-vendor")
                ),
                None,
            )
        if unit.get("threads"):
            return reconcile.ReconcilePage(
                (
                    reconcile.ReconcileItem("reply", self.native),
                    reconcile.ReconcileItem("unrelated", "other"),
                ),
                None,
            )
        if unit["token"] is None:
            return reconcile.ReconcilePage(
                (reconcile.ReconcileItem("vendor", self.native),), "next"
            )
        return reconcile.ReconcilePage((), None)

    def metadata(self, source):
        self.fetches.append(source)
        return MessageMetadata(
            source,
            self.native,
            "vendor@example.com" if source in {"vendor", "z-vendor"} else "new@example.com",
            None,
            "Message",
            (NOW - timedelta(days=2)).isoformat(),
            frozenset({"INBOX"}),
            locations=frozenset({"inbox"}),
        )


def test_budget_resume_frozen_bounds_and_rejected_token_no_refetch(tmp_path, monkeypatch):
    gateway = Pages(rejected=True)
    watcher, state = runner(tmp_path, monkeypatch, gateway)
    budget_type = reconcile.ReconcileBudget
    monkeypatch.setattr(reconcile, "ReconcileBudget", lambda allowed: budget_type(allowed, calls=2))
    for index in range(8):
        reconcile.run(
            watcher, KEY, "unavailable", label_selectors=(), now=NOW + timedelta(hours=index)
        )
        if not state.stale():
            break
    assert not state.stale()
    assert gateway.fetches == ["vendor", "reply"]
    assert all(call[2] == NOW and call[3] == NOW - timedelta(days=30) for call in gateway.calls)
    with watcher.store.connection() as db:
        assert (
            db.execute("SELECT synced_through FROM thread_watermarks").fetchone()[0]
            == NOW.isoformat()
        )
        assert not db.execute("SELECT * FROM reconcile_progress").fetchall()


def test_microsoft_late_follow_reuses_listing_and_headers(tmp_path, monkeypatch):
    gateway = Pages(provider="microsoft365", native="n" * 1000)
    watcher, state = runner(tmp_path, monkeypatch, gateway, "microsoft365")
    reconcile.run(watcher, KEY, "available", label_selectors=(), now=NOW)
    assert not state.stale()
    assert gateway.fetches == ["a-reply", "z-vendor"]
    assert [c[0] for c in gateway.calls] == ["inbox", "sent"]
    with watcher.store.connection() as db:
        assert (
            db.execute(
                "SELECT admission_kind FROM messages WHERE provider_message_id='a-reply'"
            ).fetchone()[0]
            == "thread_follow"
        )


def test_entitlement_removed_after_listing_stops_before_metadata(tmp_path, monkeypatch):
    gateway = Pages()
    watcher, state = runner(tmp_path, monkeypatch, gateway)
    original = gateway.reconcile_page

    def revoke(*args, **kwargs):
        page = original(*args, **kwargs)
        monkeypatch.setattr(
            service, "connect_entitlement_decision", lambda: service.EntitlementDecision.MISSING
        )
        return page

    gateway.reconcile_page = revoke
    reconcile.run(watcher, KEY, "unavailable", label_selectors=(), now=NOW)
    assert not gateway.fetches
    with watcher.store.connection() as db:
        assert db.execute("SELECT * FROM reconcile_progress").fetchone() is not None
        assert not db.execute("SELECT * FROM reconcile_coverage").fetchall()


@pytest.mark.parametrize("sent_scope", ["available", "unavailable"])
def test_unconfirmed_stored_message_is_refreshed_once_without_replacing_admission(
    tmp_path, monkeypatch, sent_scope
):
    gateway = Pages()
    watcher, state = runner(tmp_path, monkeypatch, gateway)
    seed(watcher.store, "vendor", scope_complete=False)
    reconcile.run(watcher, KEY, sent_scope, label_selectors=(), now=NOW)
    assert gateway.fetches.count("vendor") == 1
    assert not state.stale()
    assert watcher.store.reconcile_source("gmail", "mailbox", KEY, "vendor")["confirmed"] == (
        sent_scope == "available"
    )


def test_disappeared_candidate_completes_coverage(tmp_path, monkeypatch):
    gateway = Pages()
    watcher, state = runner(tmp_path, monkeypatch, gateway)

    def gone(source):
        gateway.fetches.append(source)
        raise MailboxMessageUnavailable("gone")

    gateway.metadata = gone
    reconcile.run(watcher, KEY, "unavailable", label_selectors=(), now=NOW)
    assert not state.stale()
    assert gateway.fetches == ["vendor"]


def test_provider_error_backs_off_without_certifying_coverage(tmp_path, monkeypatch):
    gateway = Pages()
    watcher, state = runner(tmp_path, monkeypatch, gateway)

    def failed(*args, **kwargs):
        raise reconcile.MailboxError("unavailable")

    gateway.reconcile_page = failed
    reconcile.run(watcher, KEY, "unavailable", label_selectors=(), now=NOW)
    progress = state.load_or_start(NOW, 30, {})
    assert progress["payload"]["units"][0]["attempts"] == 1
    assert datetime.fromisoformat(progress["payload"]["units"][0]["retry_at"]) > datetime.now(UTC)
    assert state.stale()


def test_gmail_adapter_quotes_addresses_and_never_gets_a_thread():
    from test_gmail import FakeRecoveryService

    from eom_email_watcher.gmail import GmailGateway

    transport = FakeRecoveryService(
        {"messages": [{"id": "m", "threadId": "native"}], "nextPageToken": "next"}
    )
    gateway = GmailGateway(transport)
    gateway.scope_folders(frozenset({"inbox", "sent"}))
    page = gateway.reconcile_page(
        dict(addresses=['a"b OR in:anywhere@example.com'], token=None),
        start=NOW,
        cutoff=NOW - timedelta(days=30),
    )
    query = transport.messages.calls[0]["q"]
    assert 'from:"a\\"b OR in:anywhere@example.com"' in query
    assert query.startswith("(in:inbox OR in:sent) after:")
    assert "before:" + str(int(NOW.timestamp())) in query
    assert page.items[0].thread_id == "native" and page.token == "next"


@pytest.mark.parametrize("offset", [-31, -30, -1, 0, 1])
def test_graph_listing_window_filters_before_header_fetch(offset):
    from eom_email_watcher.microsoft365 import Microsoft365Gateway

    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": "m",
                        "conversationId": "native",
                        "receivedDateTime": (NOW + timedelta(days=offset)).isoformat(),
                    }
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        gateway = Microsoft365Gateway("test", "owner@example.com", client)
        page = gateway.reconcile_page(
            dict(folder="inbox", token=None), start=NOW, cutoff=NOW - timedelta(days=30)
        )
    assert bool(page.items) == (-30 <= offset < 0)
    assert "$skip" not in requests[0].url.params
    assert "$filter" not in requests[0].url.params
    assert "$search" not in requests[0].url.params
    assert requests[0].url.params["$select"] == "id,receivedDateTime,conversationId"


def test_transport_budget_counts_each_graph_request_and_restores_timeout():
    from eom_email_watcher.microsoft365 import Microsoft365Gateway

    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"value": []}))
    ) as client:
        gateway = Microsoft365Gateway("test", "owner@example.com", client)
        old = client.timeout
        budget = reconcile.ReconcileBudget(lambda: True, calls=1)
        with reconcile.budget_session(gateway, budget):
            gateway.reconcile_page(
                dict(folder="inbox", token=None), start=NOW, cutoff=NOW - timedelta(days=30)
            )
            with pytest.raises(reconcile.ReconcilePaused):
                gateway.reconcile_page(
                    dict(folder="inbox", token=None), start=NOW, cutoff=NOW - timedelta(days=30)
                )
        assert client.timeout == old
        assert budget.calls == 1


def test_dry_run_stale_account_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(
        service, "connect_entitlement_decision", lambda: service.EntitlementDecision.ACTIVE
    )
    cfg = replace(config(tmp_path), senders=())
    store = Store(cfg.database_file)
    store.initialize()
    vendor = store.create_vendor("Vendor")
    store.add_vendor_address(vendor, "vendor@example.com")
    watcher = Watcher(cfg, store, HistoricalGmail(), FakeModel())
    watcher.bootstrap()
    with store.connection() as db:
        before = list(db.iterdump())
    watcher.check(dry_run=True, deliver_notifications=False)
    with store.connection() as db:
        assert list(db.iterdump()) == before


@pytest.mark.parametrize("badcharset", [False, True])
def test_imap_unicode_literal_and_badcharset_bounded_fallback(badcharset):
    from test_imap import FakeImap, credentials

    from eom_email_watcher.imap import ImapGateway

    class LiteralClient(FakeImap):
        def uid(self, command, *args):
            if command == "SEARCH":
                self.calls.append(("uid", command, *args, getattr(self, "literal", None)))
                if "CHARSET" in args and badcharset:
                    return "NO", [b"[BADCHARSET (US-ASCII)] not supported"]
                return "OK", [b"1"]
            if command == "FETCH" and args[-1] == "(UID INTERNALDATE)":
                self.calls.append(("uid", command, *args))
                return "OK", [b'1 (UID 1 INTERNALDATE "04-Sep-2026 10:16:00 -0500")']
            return super().uid(command, *args)

    client = LiteralClient(uid_next=2)
    gateway = ImapGateway(credentials())
    gateway._active_client = client
    unit = dict(folder="inbox", addresses=["caf\u00e9@example.com"], token=None)
    unit["snapshot"] = gateway.reconcile_snapshot("inbox")
    page = gateway.reconcile_page(
        unit, start=datetime(2026, 9, 5, tzinfo=UTC), cutoff=datetime(2026, 9, 3, tzinfo=UTC)
    )
    assert len(page.items) == 1 and page.token is None
    literal = next(c for c in client.calls if "CHARSET" in c)
    assert literal[-1] == "caf\u00e9@example.com".encode()
    if badcharset:
        assert ("uid", "SEARCH", None, "UID", "1:1", "SINCE", "02-Sep-2026", None) in client.calls
    assert not any("HEADER" in str(c) for c in client.calls)


@pytest.mark.parametrize("operand", ["", "a\r\nb", "a\x00b"])
def test_imap_search_operand_rejects_protocol_injection(operand):
    from eom_email_watcher.imap import ImapError, _search_operand

    with pytest.raises(ImapError, match="Unsafe"):
        _search_operand(operand)


def test_imap_interrupted_search_preserves_partial_uid_state():
    from test_imap import FakeImap, credentials

    from eom_email_watcher.imap import ImapGateway

    client = FakeImap(uid_next=2)
    real = client.uid
    calls = []

    def limited(command, *args):
        calls.append(command)
        if len(calls) == 2:
            raise reconcile.ReconcilePaused()
        return real(command, *args)

    client.uid = limited
    gateway = ImapGateway(credentials())
    gateway._active_client = client
    unit = dict(folder="inbox", addresses=["vendor@example.com"], token=None)
    unit["snapshot"] = gateway.reconcile_snapshot("inbox")
    with pytest.raises(reconcile.ReconcilePaused):
        gateway.reconcile_page(
            unit, start=datetime(2026, 9, 5, tzinfo=UTC), cutoff=datetime(2026, 9, 3, tzinfo=UTC)
        )
    assert unit["_page_state"]["uids"] == [1]
    assert unit["_page_state"]["searched"] == 1

    def date(command, *args):
        calls.append(command)
        return "OK", [b'1 (UID 1 INTERNALDATE "04-Sep-2026 10:16:00 -0500")']

    client.uid = date
    page = gateway.reconcile_page(
        unit, start=datetime(2026, 9, 5, tzinfo=UTC), cutoff=datetime(2026, 9, 3, tzinfo=UTC)
    )
    assert len(page.items) == 1
    assert calls == ["SEARCH", "FETCH", "FETCH"]


def test_imap_merge_resyncs_survivor_from_cutoff(tmp_path, monkeypatch):
    class ImapPages:
        calls = []
        fetched = False

        def reconcile_snapshot(self, folder):
            return dict(validity=1, highest=3, folder_key=None)

        def reconcile_page(self, unit, **bounds):
            self.calls.append((unit.get("ids"), unit.get("since")))
            if "ids" not in unit:
                return reconcile.ReconcilePage((), None)
            if not self.fetched:
                self.fetched = True
                return reconcile.ReconcilePage(
                    (
                        reconcile.ReconcileItem(
                            "bridge", received_at=(NOW - timedelta(days=1)).isoformat()
                        ),
                    ),
                    None,
                )
            return reconcile.ReconcilePage((), None)

        def metadata(self, source):
            return MessageMetadata(
                source,
                None,
                "new@example.com",
                None,
                "Reply",
                (NOW - timedelta(days=1)).isoformat(),
                frozenset({"INBOX"}),
                rfc_message_id="bridge@root",
                reply_ids=("one@root", "two@root"),
                locations=frozenset({"inbox"}),
            )

    gateway = ImapPages()
    watcher, state = runner(tmp_path, monkeypatch, gateway, "imap")
    seed(watcher.store, "one", provider="imap", rfc_message_id="one@root")
    seed(watcher.store, "two", provider="imap", rfc_message_id="two@root")
    progress = state.load_or_start(
        NOW,
        30,
        {
            "stage": "discovery",
            "units": [],
            "folders": ["inbox"],
            "snapshots": {"inbox": gateway.reconcile_snapshot("inbox")},
        },
    )
    for thread in state.threads():
        state.watermark(progress, thread)
    state.gap(now=NOW)
    # Begin with watermarks later than the cutoff, so an old-component SINCE
    # cannot certify the merged component.
    progress = state.load_or_start(
        NOW,
        30,
        {
            "stage": "discovery",
            "units": [],
            "folders": ["inbox"],
            "snapshots": {"inbox": gateway.reconcile_snapshot("inbox")},
        },
    )
    for thread in state.threads():
        with watcher.store.connection() as db:
            db.execute(
                "INSERT INTO thread_watermarks VALUES (?,?,?,?,?)",
                (*state.scope, thread["key"], (NOW - timedelta(days=3)).isoformat()),
            )
    reconcile.run(watcher, KEY, "unavailable", label_selectors=(), now=NOW)
    assert not state.stale()
    assert len(state.threads()) == 1
    cutoff = (NOW - timedelta(days=30)).isoformat()
    assert any(
        set(ids or ()) >= {"one@root", "two@root", "bridge@root"} and since == cutoff
        for ids, since in gateway.calls
    )


def test_imap_gap_refresh_retains_existing_component_and_registers_missing_id(tmp_path):
    store, state = state_store(tmp_path, "imap")
    vendor = store.create_vendor("Vendor")
    store.add_vendor_address(vendor, "vendor@example.com")
    seed(store, "one", provider="imap", rfc_message_id=None)
    before = state.threads()[0]["key"]
    metadata = MessageMetadata(
        "one",
        None,
        "vendor@example.com",
        None,
        "Reply",
        (NOW - timedelta(days=2)).isoformat(),
        frozenset({"INBOX"}),
        rfc_message_id="new@root",
        locations=frozenset({"inbox"}),
    )
    store.record_reconcile_headers(
        "imap",
        "mailbox",
        KEY,
        metadata,
        locations=frozenset({"inbox"}),
        observed_at=NOW,
        scope_complete=True,
    )
    assert state.threads()[0]["key"] == before
    assert state.threads()[0]["ids"] == ["new@root"]


@pytest.mark.parametrize("listed", [False, True])
def test_gmail_missing_native_id_still_requires_listing_in_admitted_folders(
    tmp_path, monkeypatch, listed
):
    class EmptyListing(Pages):
        def reconcile_page(self, unit, **bounds):
            items = (reconcile.ReconcileItem("archived"),) if listed and unit.get("threads") else ()
            return reconcile.ReconcilePage(items, None)

    gateway = EmptyListing()
    watcher, state = runner(tmp_path, monkeypatch, gateway)
    seed(watcher.store, "archived", thread_id=None, scope_complete=False)
    reconcile.run(watcher, KEY, "available", label_selectors=(), now=NOW)
    assert gateway.fetches == (["archived"] if listed else [])
    assert not state.stale()


@pytest.mark.parametrize("maximum", [0, 1, 200])
def test_provider_call_budget_boundary_and_zero_are_not_defaulted(maximum):
    observations = []
    budget = reconcile.ReconcileBudget(lambda: observations.append(True) or True, calls=maximum)
    for _ in range(maximum):
        budget.before_call()
    with pytest.raises(reconcile.ReconcilePaused):
        budget.before_call()
    assert budget.calls == maximum
    assert len(observations) == maximum + 1


def test_deadline_expiry_stops_without_a_provider_call():
    budget = reconcile.ReconcileBudget(lambda: True, seconds=0)
    with pytest.raises(reconcile.ReconcilePaused):
        budget.before_call()
    assert budget.calls == 0


def test_verified_identity_mutation_invalidates_other_configured_accounts(tmp_path):
    store, state = state_store(tmp_path)
    store.register_mail_account("gmail", "other", display_name="Other")
    store.reconcile_mailbox_identity("gmail", "other", "a" * 64)
    before = state.generation()
    store.update_mail_account_identity(
        "gmail", "other", display_name="Other", address="vendor@example.com"
    )
    assert state.generation() > before
    stable = state.generation()
    store.update_mail_account_identity(
        "gmail", "other", display_name="Renamed", address="vendor@example.com"
    )
    assert state.generation() == stable


def test_generation_change_during_page_discards_token_on_next_check(tmp_path, monkeypatch):
    gateway = Pages()
    watcher, state = runner(tmp_path, monkeypatch, gateway)
    listing = gateway.reconcile_page

    def change(unit, **bounds):
        page = listing(unit, **bounds)
        watcher.store.create_vendor("Added mid-page")
        gateway.reconcile_page = listing
        return page

    gateway.reconcile_page = change
    reconcile.run(watcher, KEY, "unavailable", label_selectors=(), now=NOW)
    assert state.stale()
    assert not gateway.fetches
    reconcile.run(watcher, KEY, "unavailable", label_selectors=(), now=NOW + timedelta(hours=1))
    assert gateway.calls[1][1] is None
    assert gateway.calls[1][2] == NOW + timedelta(hours=1)
    assert not state.stale()


def test_aged_imap_reply_header_gap_waits_for_retention_raise(tmp_path, monkeypatch):
    class GapMailbox:
        fetched = []

        def reconcile_snapshot(self, folder):
            return dict(validity=1, highest=1, folder_key=None)

        def reconcile_page(self, unit, **bounds):
            return reconcile.ReconcilePage((), None)

        def metadata(self, source):
            self.fetched.append(source)
            return MessageMetadata(
                source,
                None,
                "vendor@example.com",
                None,
                "Old reply",
                (NOW - timedelta(days=45)).isoformat(),
                frozenset({"INBOX"}),
                rfc_message_id="old@root",
                reply_ids=("parent@root",),
                locations=frozenset({"inbox"}),
            )

    gateway = GapMailbox()
    watcher, state = runner(tmp_path, monkeypatch, gateway, "imap")
    seed(
        watcher.store,
        "old",
        provider="imap",
        rfc_message_id="old@root",
        received_at=(NOW - timedelta(days=45)).isoformat(),
    )
    with watcher.store.connection() as db:
        db.execute("INSERT OR IGNORE INTO imap_reply_header_gaps VALUES ('old')")
    reconcile.run(watcher, KEY, "unavailable", label_selectors=(), now=NOW)
    assert gateway.fetched == []
    with watcher.store.connection() as db:
        assert db.execute("SELECT * FROM imap_reply_header_gaps").fetchone() is not None
    watcher.config = replace(watcher.config, retention_days=60)
    reconcile.run(watcher, KEY, "unavailable", label_selectors=(), now=NOW)
    assert gateway.fetched == ["old"]
    with watcher.store.connection() as db:
        assert db.execute("SELECT * FROM imap_reply_header_gaps").fetchone() is None
    assert not state.stale()


def test_imap_closed_socket_restore_preserves_the_provider_failure():
    import socket
    from types import SimpleNamespace

    left, right = socket.socketpair()

    def failed(*args):
        left.close()
        raise OSError("transport closed")

    client = SimpleNamespace(sock=left, _command=failed)
    gateway = SimpleNamespace(_active_client=client)
    try:
        with (
            pytest.raises(reconcile.MailboxError, match="transport failed"),
            reconcile.budget_session(gateway, reconcile.ReconcileBudget(lambda: True)),
        ):
            client._command("UID", "SEARCH")
        assert client._command is failed
    finally:
        left.close()
        right.close()
