"""M2.2 admission precedence at the common metadata boundary."""

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from test_db import Store
from test_service import FakeModel, LabelledGmail, config

from eom_email_watcher import service as service_module
from eom_email_watcher.service import match_mailbox_admission
from eom_email_watcher.thread_state import VendorIndex


def test_unwatched_vendor_address_is_admitted():
    metadata = SimpleNamespace(
        sender="vendor@example.com",
        labels=frozenset({"INBOX"}),
        locations=frozenset({"inbox"}),
        to=(),
        cc=(),
    )
    decision = match_mailbox_admission(
        metadata=metadata,
        provider="gmail",
        account_id="mailbox",
        mailbox_identity_key="a" * 64,
        exact_senders={},
        label_selectors=(),
        admitted_at=datetime(2026, 10, 11, tzinfo=UTC),
        vendor_index=VendorIndex(addresses={"vendor@example.com": ("vendor", "Vendor")}),
        gated_allowed=True,
    )
    assert decision is not None and decision.kind == "vendor_address"
    assert decision.selector_id.startswith("vendor-address:")


IDENTITY = "a" * 64
NOW = datetime(2026, 10, 11, tzinfo=UTC)


def admit(
    *, sender="vendor@example.com", locations=frozenset({"inbox"}), to=(), cc=(), **overrides
):
    values = dict(
        metadata=SimpleNamespace(
            sender=sender,
            labels=frozenset(loc.upper() for loc in locations),
            locations=locations,
            to=to,
            cc=cc,
        ),
        provider="gmail",
        account_id="mailbox",
        mailbox_identity_key=IDENTITY,
        exact_senders={},
        label_selectors=(),
        admitted_at=NOW,
        vendor_index=VendorIndex(
            addresses={"vendor@example.com": ("vendor", "Vendor")},
            domains={"domain.example": ("domain", "Domain")},
        ),
        gated_allowed=True,
    )
    values.update(overrides)
    return match_mailbox_admission(**values)


@pytest.mark.parametrize(
    ("kwargs", "kind"),
    [
        ({"exact_senders": {"vendor@example.com": "Watched"}}, "exact_sender"),
        ({}, "vendor_address"),
        ({"sender": "someone@domain.example"}, "vendor_domain"),
        (
            {
                "sender": "owner@example.com",
                "locations": frozenset({"inbox", "sent"}),
                "to": ("vendor@example.com",),
            },
            "sent_to_vendor",
        ),
        (
            {"sender": "unknown@example.com", "followed_thread": ("thread", "Owner")},
            "thread_follow",
        ),
    ],
)
def test_capture_priority(kwargs, kind):
    decision = (
        admit(followed_thread=("thread", "Owner"), **kwargs)
        if "followed_thread" not in kwargs
        else admit(**kwargs)
    )
    assert decision.kind == kind


@pytest.mark.parametrize(
    "locations", [frozenset({"inbox"}), frozenset({"sent"}), frozenset({"inbox", "sent"})]
)
def test_unpaid_vendor_or_follow_match_leaves_no_admission(locations):
    assert (
        admit(locations=locations, gated_allowed=False, followed_thread=("thread", "Owner")) is None
    )


@pytest.mark.parametrize("locations", [frozenset({"trash"}), frozenset({"archive"}), frozenset()])
def test_outside_scope_never_admits(locations):
    assert admit(locations=locations, exact_senders={"vendor@example.com": "Watched"}) is None


def test_recipient_exact_precedes_earlier_domain_and_to_precedes_cc():
    index = VendorIndex(
        addresses={"cc@exact.example": ("cc", "Cc"), "to@exact.example": ("to", "To")},
        domains={"domain.example": ("domain", "Domain")},
    )
    result = index.attribute(
        "unknown", outbound=True, to=("first@domain.example",), cc=("cc@exact.example",)
    )
    assert result[0] == "cc" and result[3] == "cc@exact.example"
    assert (
        index.attribute(
            "unknown", outbound=True, to=("to@exact.example",), cc=("cc@exact.example",)
        )[0]
        == "to"
    )
    assert index.attribute("unknown", outbound=True, to=("unknown@example.com",), cc=()) is None


def test_identity_excluded_before_exact_and_domain_lookup():
    index = VendorIndex(
        addresses={"owner@example.com": ("exact", "Exact")},
        domains={"example.com": ("domain", "Domain")},
        identities=frozenset({"owner@example.com"}),
    )
    assert index.vendor_of("owner@example.com") is None
    assert index.attribute("other", outbound=True, to=("owner@example.com",)) is None


def test_selector_hash_is_bounded_and_deterministic():
    key = "x" * 512
    decision = admit(sender="unknown", followed_thread=(key, "Owner"))
    assert decision.selector_id == "thread:" + hashlib.sha256(key.encode()).hexdigest()
    decision = admit(sender="owner", locations=frozenset({"sent"}), to=("vendor@example.com",))
    assert (
        decision.selector_id
        == "vendor-recipient:" + hashlib.sha256(b"vendor@example.com").hexdigest()
    )


@pytest.mark.parametrize("paid", [True, False])
@pytest.mark.parametrize("sent", [True, False])
def test_vendor_only_check_uses_one_entitlement_decision(tmp_path, monkeypatch, paid, sent):
    cfg = replace(config(tmp_path), senders=())
    store = Store(cfg.database_file)
    store.initialize()
    store.set_state("100", NOW)
    vendor = store.create_vendor("Vendor")
    store.add_vendor_address(vendor, "billing@vendor.com" if sent else "trusted@example.com")
    gateway = LabelledGmail(frozenset({"SENT" if sent else "INBOX"}))
    from eom_email_watcher.reconcile import ReconcileBudget

    calls = []
    budget_calls = []
    in_budget = False
    check_budget = ReconcileBudget.check

    def budget_check(self):
        nonlocal in_budget
        in_budget = True
        try:
            return check_budget(self)
        finally:
            in_budget = False

    monkeypatch.setattr(ReconcileBudget, "check", budget_check)

    def entitlement():
        (budget_calls if in_budget else calls).append(True)
        return (
            service_module.EntitlementDecision.ACTIVE
            if paid
            else service_module.EntitlementDecision.MISSING
        )

    monkeypatch.setattr(service_module, "connect_entitlement_decision", entitlement)
    result = service_module.Watcher(cfg, store, gateway, FakeModel()).check()
    assert calls == [True]
    assert bool(budget_calls) is paid
    assert result["active"] is paid
    assert result["summarized"] == (1 if paid else 0)
    with store.connection() as db:
        kinds = [r[0] for r in db.execute("SELECT admission_kind FROM messages")]
    assert kinds == (["sent_to_vendor" if sent else "vendor_address"] if paid else [])


def test_polling_precedes_purge(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    store.set_state("100", NOW)
    gateway = LabelledGmail(frozenset({"INBOX"}))
    events = []
    changes = gateway.changes_since
    purge = store.purge

    def record_changes(cursor):
        events.append("poll")
        return changes(cursor)

    def record_purge(*args, **kwargs):
        events.append("purge")
        return purge(*args, **kwargs)

    monkeypatch.setattr(gateway, "changes_since", record_changes)
    monkeypatch.setattr(store, "purge", record_purge)
    service_module.Watcher(cfg, store, gateway, FakeModel()).check()
    assert events[0] == "poll", events


@pytest.mark.parametrize("value", [0, "", False, None])
def test_selector_rejects_falsy_values_without_default(value):
    from eom_email_watcher.config import thread_capture_selector_id

    with pytest.raises(ValueError, match="unsupported"):
        thread_capture_selector_id("thread_follow", value)


@pytest.mark.parametrize("size", [1, 511, 512, 513, 10000])
def test_selector_size_independent_of_selected_key(size):
    from eom_email_watcher.config import thread_capture_selector_id

    assert (
        len(thread_capture_selector_id("thread_follow", "x" * size).encode()) == len("thread:") + 64
    )


def test_gmail_recovery_preview_keeps_legacy_admission(tmp_path, monkeypatch):
    from test_service import FakeGmail

    cfg = replace(config(tmp_path), senders=())
    store = Store(cfg.database_file)
    store.initialize()
    store.reconcile_mailbox_identity(
        "gmail", "gmail-default", "f" * 64, legacy_status="replacement", preserve_cursor=True
    )
    store.set_state("100", NOW)
    vendor = store.create_vendor("Vendor")
    store.add_vendor_address(vendor, "trusted@example.com")
    monkeypatch.setattr(service_module.Watcher, "_gated_class_allowed", staticmethod(lambda: True))
    result = service_module.Watcher(cfg, store, FakeGmail(stale=True), FakeModel()).check(
        dry_run=True
    )
    assert result["stale_cursor_recovered"] is True
    assert result["discovered"] == 0
    assert store.recent(5) == []


def test_watched_capture_does_not_resolve_follow_and_snapshots_vendors_once(tmp_path, monkeypatch):
    class BatchGmail(LabelledGmail):
        def history_message_ids(self, cursor):
            return ["allowed", "second"], "200"

        def metadata(self, message_id, **kwargs):
            return replace(super().metadata("allowed", **kwargs), message_id=message_id)

    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    store.set_state("100", NOW)
    gateway = BatchGmail(frozenset({"INBOX"}))
    monkeypatch.setattr(service_module.Watcher, "_gated_class_allowed", staticmethod(lambda: True))
    calls = []
    index = store.vendor_index

    def snapshot():
        calls.append(True)
        return index()

    def unnecessary(**kwargs):
        pytest.fail("exact sender must not resolve followed thread")

    monkeypatch.setattr(store, "vendor_index", snapshot)
    monkeypatch.setattr(store, "followed_thread_for_metadata", unnecessary)
    result = service_module.Watcher(cfg, store, gateway, FakeModel()).check()
    assert result["discovered"] == 2
    assert calls == [True]


@pytest.mark.parametrize("has_vendor", [False, True])
def test_paid_unavailable_sent_requires_vendor_work(tmp_path, monkeypatch, has_vendor):
    from test_service import SentPollingGateway, _imap_sent_scaffold

    from eom_email_watcher.mailbox import MailboxSession

    cfg, store, account_id, credential, identity, cursor = _imap_sent_scaffold(tmp_path)
    cfg = replace(cfg, senders=())
    if has_vendor:
        store.create_vendor("Vendor with no addresses")
    gateway = SentPollingGateway(credential, identity, cursor, scope="unavailable")
    monkeypatch.setattr(service_module.Watcher, "_gated_class_allowed", staticmethod(lambda: True))
    result = service_module.Watcher(
        cfg, store, MailboxSession("imap", account_id, gateway), FakeModel()
    ).check()
    assert result["active"] is True  # Missing coverage is work even without a vendor.
    gateway.sent_calls.clear()
    result = service_module.Watcher(
        cfg, store, MailboxSession("imap", account_id, gateway), FakeModel()
    ).check()
    assert result["active"] is has_vendor
    assert gateway.sent_calls == ["scope"]
    assert store.sent_scope("imap", account_id) == "unavailable"
