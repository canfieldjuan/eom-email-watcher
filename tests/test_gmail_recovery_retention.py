"""Recovery completion must reach the existing retention/coverage owner."""

from datetime import UTC, datetime, timedelta

import pytest
from connect_automate.entitlement import EntitlementDecision
from test_service import TEST_MAILBOX_IDENTITY_KEY, FakeGmail, FakeModel, config
from test_thread_bodies import followed
from test_thread_reconcile import KEY, NOW

from eom_email_watcher import body_state, reconcile_state, service
from eom_email_watcher.db import Store
from eom_email_watcher.mailbox import MailboxError, MailboxSession


@pytest.mark.parametrize("entry", ["resumed", "stale"])
@pytest.mark.parametrize("outcome", ["complete", "backoff", "paused"])
def test_recovery_completion_reaches_retention(tmp_path, monkeypatch, entry, outcome):
    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    store.reconcile_mailbox_identity(
        "gmail",
        "gmail-default",
        TEST_MAILBOX_IDENTITY_KEY,
        legacy_status="replacement",
        preserve_cursor=False,
    )
    now = datetime.now(UTC)
    store.set_state(
        "100", now - timedelta(minutes=10), mailbox_identity_key=TEST_MAILBOX_IDENTITY_KEY
    )
    if entry == "resumed":
        store.create_gmail_recovery_state(
            "gmail-default",
            TEST_MAILBOX_IDENTITY_KEY,
            0,
            (("trusted@example.com", "Trusted"),),
            (),
            int((now - timedelta(days=1)).timestamp()),
            int(now.timestamp()) + 1,
            "200",
            now=now,
            query_scope="inbox",
        )

    class Gateway(FakeGmail):
        def recovery_page(self, *args, **kwargs):
            if outcome == "backoff":
                raise MailboxError("public unavailable provider")
            if outcome == "paused":
                return tuple(f"blocked-{i}" for i in range(200)), "another-page"
            return (), None

    monkeypatch.setattr(
        service, "connect_entitlement_decision", lambda: EntitlementDecision.MISSING
    )
    observed = []
    original = store.purge

    def purge(*args, **kwargs):
        observed.append(kwargs["after_poll"])
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "purge", purge)
    service.Watcher(cfg, store, Gateway(stale=entry == "stale"), FakeModel()).check()
    assert (store.gmail_recovery_state("gmail-default") is None) == (outcome == "complete")
    assert observed == [outcome == "complete"], "Recovery completion lost before purge"


@pytest.mark.parametrize("completed", [False, True])
@pytest.mark.parametrize("covered", [False, True])
def test_recovery_retention_still_requires_current_coverage(
    tmp_path, monkeypatch, completed, covered
):
    from dataclasses import replace

    later = NOW + timedelta(days=35)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return later.astimezone(tz or UTC)

    monkeypatch.setattr(service, "datetime", Clock)
    monkeypatch.setattr(reconcile_state, "datetime", Clock)
    store, state, _ = followed(tmp_path)
    body_state.record(store, "m", ("expired quote", 13), fetched_at=NOW)
    if covered:
        state.observe(active=True, retention_days=30, sent_folder="inbox", now=later)
        progress = state.load_or_start(later, 30, {"units": []})
        for thread in state.threads():
            state.watermark(progress, thread)
        state.complete(progress)
    watcher = service.Watcher(
        replace(config(tmp_path), retention_days=30),
        store,
        MailboxSession("gmail", "mailbox", FakeGmail(), KEY),
        FakeModel(),
    )
    watcher._finish_active_result(
        scope=frozenset({"inbox"}),
        added=0,
        purged=0,
        recovered=True,
        dry_run=False,
        deliver_notifications=False,
        checked_at=later,
        retention_cutoff=later - timedelta(days=30),
        mailbox_identity_key=KEY,
        polling_complete=completed,
    )
    with store.connection() as db:
        retained = db.execute("SELECT 1 FROM messages WHERE message_id='m'").fetchone()
        body = db.execute("SELECT 1 FROM message_bodies WHERE message_id='m'").fetchone()
    assert bool(retained) == (not (completed and covered))
    assert bool(body) == bool(retained)
