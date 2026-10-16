"""Public proofs for accepted M2.4 body capture and secure retention."""

import sqlite3
from datetime import timedelta

import pytest
from conftest import local_removal
from test_thread_reconcile import KEY, NOW, seed, state_store

from eom_email_watcher import mime


@pytest.mark.parametrize(
    "source",
    [
        "<p>Authored</p><blockquote>Quoted</blockquote><p>Bottom reply</p>",
        '<p>Authored</p><div class="gmail_quote other">Quoted</div><p>Bottom reply</p>',
        '<p>Authored</p><div id="divRplyFwdMsg">Quoted</div><p>Bottom reply</p>',
    ],
)
def test_storage_keeps_quote_boundary_without_changing_analysis(source):
    normalizer = getattr(mime, "stored_body_text", None)
    assert callable(normalizer), "storage normalizer is missing"
    assert normalizer(source, is_html=True) == ("Authored\n> Quoted\nBottom reply", 30)
    assert ">" not in mime.html_to_text(source)


def test_body_schema_upgrade_invalidates_coverage(tmp_path):
    store, state = state_store(tmp_path)
    progress = state.load_or_start(NOW, 30, {"stage": "derived", "units": []})
    state.complete(progress)
    with store.connection() as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"message_bodies", "message_body_unavailable", "maintenance_state"} <= tables
    with store.connection() as db:
        db.execute("PRAGMA user_version=34")
    store.initialize()
    assert state.stale()


def followed(tmp_path):
    store, state = state_store(tmp_path)
    vendor = store.create_vendor("Vendor")
    store.add_vendor_address(vendor, "vendor@example.com")
    seed(store)
    return store, state, vendor


def test_pending_body_keeps_coverage_stale_and_blocks_completion(tmp_path):
    store, state, _ = followed(tmp_path)
    progress = state.load_or_start(NOW, 30, {"stage": "derived", "units": []})
    with pytest.raises(RuntimeError, match="body"):
        state.complete(progress)


def test_storage_cap_and_plain_markers():
    normalizer = getattr(mime, "stored_body_text", None)
    assert callable(normalizer), "storage normalizer is missing"
    source = "> quote\n" + "x" * 200_001
    text, count = normalizer(source, is_html=False)
    assert len(text) == 200_000
    assert count == len(source)
    assert text.startswith("> quote\n")


def test_unfollow_deletes_body_and_sets_durable_checkpoint(tmp_path):
    from eom_email_watcher import body_state

    store, state, vendor = followed(tmp_path)
    body_state.record(store, "m", ("local text", 10), fetched_at=NOW)
    store.remove_vendor_address(vendor, "vendor@example.com")
    with store.connection() as db:
        assert db.execute("PRAGMA secure_delete").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM message_bodies").fetchone()[0] == 0
        assert db.execute(
            "SELECT value FROM maintenance_state WHERE key='checkpoint_pending'"
        ).fetchone()
    store.initialize()
    assert body_state.checkpoint(store)
    with store.connection() as db:
        assert not db.execute(
            "SELECT value FROM maintenance_state WHERE key='checkpoint_pending'"
        ).fetchone()


def test_old_followed_thread_requires_current_coverage_then_purges_whole(tmp_path):
    from eom_email_watcher import body_state

    store, state, _ = followed(tmp_path)
    seed(store, "older", received_at=(NOW - timedelta(days=40)).isoformat())
    body_state.record(store, "m", ("body", 4), fetched_at=NOW)
    assert (
        store.purge(
            30,
            now=NOW + timedelta(days=35),
            after_poll=True,
            polled_scope=("gmail", "mailbox", KEY),
        )
        == 0
    )
    progress = state.load_or_start(NOW + timedelta(days=35), 30, {"stage": "derived", "units": []})
    with store.connection() as db:
        db.execute(
            "INSERT INTO thread_watermarks SELECT provider,account_id,"
            "mailbox_identity_key,thread_key,? FROM thread_follow WHERE followed=1",
            ((NOW + timedelta(days=35)).isoformat(),),
        )
    state.complete(progress)
    assert (
        store.purge(
            30,
            now=NOW + timedelta(days=35),
            after_poll=True,
            polled_scope=("gmail", "mailbox", KEY),
        )
        == 2
    )
    with store.connection() as db:
        assert not db.execute("SELECT 1 FROM message_bodies").fetchone()
        assert not db.execute("SELECT 1 FROM thread_follow").fetchone()


def test_nonpoll_purge_preserves_followed_thread_even_with_current_coverage(tmp_path):
    from eom_email_watcher import body_state

    store, state, _ = followed(tmp_path)
    body_state.record(store, "m", ("body", 4), fetched_at=NOW)
    later = NOW + timedelta(days=35)
    progress = state.load_or_start(later, 30, {"units": []})
    with store.connection() as db:
        db.execute(
            "INSERT INTO thread_watermarks SELECT provider,account_id,"
            "mailbox_identity_key,thread_key,? FROM thread_follow",
            (later.isoformat(),),
        )
    state.complete(progress)
    assert store.purge(30, now=later) == 0


def body_runner(tmp_path, monkeypatch, outcomes):
    from test_thread_reconcile import Pages, runner

    from eom_email_watcher import reconcile

    class Bodies(Pages):
        def __init__(self):
            super().__init__()
            self.body_calls = []

        def stored_body(self, source, *, scope):
            self.body_calls.append((source, scope))
            result = outcomes[source]
            if isinstance(result, Exception):
                raise result
            return result

    gateway = Bodies()
    watcher, state = runner(tmp_path, monkeypatch, gateway)
    reconcile.observe(watcher, KEY, True, "unavailable", NOW)
    seed(watcher.store, "m", rfc_message_id="same@vendor")
    return watcher, state, gateway


def prepare_derived(state):
    progress = state.load_or_start(NOW, 30, {"stage": "derived", "units": [], "folders": ["inbox"]})
    for thread in state.threads():
        state.watermark(progress, thread)
    return progress


@pytest.mark.parametrize("first", ["gone", "outside", "survives"])
def test_canonical_copy_fallback_and_completed_unavailable(tmp_path, monkeypatch, first):
    from eom_email_watcher import reconcile
    from eom_email_watcher.mailbox import MailboxMessageOutsideScope, MailboxMessageUnavailable

    error = (
        MailboxMessageOutsideScope("archived")
        if first == "outside"
        else MailboxMessageUnavailable("gone")
    )
    outcomes = {
        "m": error,
        "z-copy": ("quote", 5) if first == "survives" else MailboxMessageUnavailable("gone"),
    }
    watcher, state, gateway = body_runner(tmp_path, monkeypatch, outcomes)
    seed(watcher.store, "z-copy", rfc_message_id="same@vendor")
    prepare_derived(state)
    reconcile.run(watcher, KEY, "unavailable", label_selectors=(), now=NOW)
    assert [c[0] for c in gateway.body_calls] == ["m", "z-copy"]
    assert not state.stale()
    with watcher.store.connection() as db:
        if first == "survives":
            assert db.execute("SELECT text FROM message_bodies").fetchone()[0] == "quote"
            assert not db.execute("SELECT 1 FROM message_body_unavailable").fetchone()
        else:
            assert not db.execute("SELECT 1 FROM message_bodies").fetchone()
            assert db.execute("SELECT reason FROM message_body_unavailable").fetchone()[0] == (
                "outside_folders" if first == "outside" else "source_gone"
            )


def test_gone_copy_progress_survives_small_budget_and_restart(tmp_path, monkeypatch):
    from eom_email_watcher import reconcile
    from eom_email_watcher.db import Store
    from eom_email_watcher.mailbox import MailboxMessageUnavailable

    watcher, state, gateway = body_runner(
        tmp_path, monkeypatch, {"m": MailboxMessageUnavailable("gone"), "z-copy": ("quote", 5)}
    )
    seed(watcher.store, "z-copy", rfc_message_id="same@vendor")
    prepare_derived(state)
    original = reconcile.ReconcileBudget
    monkeypatch.setattr(reconcile, "ReconcileBudget", lambda allowed: original(allowed, calls=1))
    reconcile.run(watcher, KEY, "unavailable", label_selectors=(), now=NOW)
    watcher.store = Store(watcher.store.path)
    watcher.store.initialize()
    reconcile.run(watcher, KEY, "unavailable", label_selectors=(), now=NOW)
    assert [c[0] for c in gateway.body_calls] == ["m", "z-copy"]
    with watcher.store.connection() as db:
        assert db.execute("SELECT text FROM message_bodies").fetchone()[0] == "quote"


@pytest.mark.parametrize("reason", ["source_gone", "outside_folders"])
def test_recapture_clears_unavailable_and_fetches_body(tmp_path, monkeypatch, reason):
    from eom_email_watcher import body_state, reconcile

    watcher, state, gateway = body_runner(tmp_path, monkeypatch, {"m": ("returned", 8)})
    body_state.record(watcher.store, "m", None, reason=reason, fetched_at=NOW)
    watcher.store.record_message_location(
        provider="gmail",
        account_id="mailbox",
        mailbox_identity_key=KEY,
        provider_message_id="m",
        locations=frozenset({"inbox"}),
        headers_observed=False,
        now=NOW,
    )
    with watcher.store.connection() as db:
        assert not db.execute("SELECT 1 FROM message_body_unavailable").fetchone()
    prepare_derived(state)
    reconcile.run(watcher, KEY, "unavailable", label_selectors=(), now=NOW)
    assert [c[0] for c in gateway.body_calls] == ["m"]


def test_bodyless_old_or_unfollowed_message_is_never_fetched(tmp_path, monkeypatch):
    from eom_email_watcher import body_state, reconcile

    watcher, state, gateway = body_runner(tmp_path, monkeypatch, {"m": ("quote", 5)})
    seed(watcher.store, "old", received_at=(NOW - timedelta(days=31)).isoformat())
    seed(watcher.store, "unfollowed", sender="other@example.com", thread_id="other")
    prepare_derived(state)
    reconcile.run(watcher, KEY, "unavailable", label_selectors=(), now=NOW)
    assert [c[0] for c in gateway.body_calls] == ["m"]
    with watcher.store.connection() as db:
        assert [r[0] for r in db.execute("SELECT message_id FROM message_bodies")] == ["m"]
        assert not body_state.pending(db, state.scope, cutoff=NOW - timedelta(days=30), now=NOW)


@pytest.mark.parametrize("method", ["delete", "clear"])
def test_all_message_deletions_schedule_checkpoint_without_marker_inheritance(tmp_path, method):
    from eom_email_watcher import body_state

    store, state, _ = followed(tmp_path)
    body_state.record(store, "m", ("body", 4), fetched_at=NOW)
    if method == "delete":
        local_removal(store, "delete_message", "m")
    else:
        local_removal(store, "request_removal", "clear", {})
    with store.connection() as db:
        assert not db.execute("SELECT 1 FROM message_bodies").fetchone()
        assert db.execute(
            "SELECT 1 FROM maintenance_state WHERE key='checkpoint_pending'"
        ).fetchone()
    seed(store)
    with store.connection() as db:
        assert not db.execute("SELECT 1 FROM message_body_unavailable").fetchone()


def test_busy_checkpoint_remains_pending_until_reader_finishes(tmp_path):
    from eom_email_watcher import body_state

    store, state, _ = followed(tmp_path)
    with store.connection() as db:
        db.execute("PRAGMA journal_mode=WAL")
    body_state.record(store, "m", ("private body", 12), fetched_at=NOW)
    reader = sqlite3.connect(store.path)
    try:
        reader.execute("BEGIN")
        assert reader.execute("SELECT text FROM message_bodies").fetchone()[0] == "private body"
        local_removal(store, "delete_message", "m")
        assert not body_state.checkpoint(store)
        with store.connection() as db:
            assert db.execute(
                "SELECT value FROM maintenance_state WHERE key='checkpoint_pending'"
            ).fetchone()
    finally:
        reader.close()
    assert body_state.checkpoint(store)
    with store.connection() as db:
        assert not db.execute(
            "SELECT 1 FROM maintenance_state WHERE key='checkpoint_pending'"
        ).fetchone()


def test_gmail_storage_prefers_html_and_preserves_scope_and_transport_budget():
    import base64
    from types import SimpleNamespace

    from eom_email_watcher import reconcile
    from eom_email_watcher.gmail import GmailGateway
    from eom_email_watcher.mailbox import MailboxMessageOutsideScope

    def encode(s):
        return base64.urlsafe_b64encode(s.encode()).decode()

    response = {
        "labelIds": ["INBOX"],
        "payload": {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": encode("plain analysis")}},
                {
                    "mimeType": "text/html",
                    "body": {"data": encode("<p>Author</p><blockquote>Quote</blockquote>")},
                },
            ],
        },
    }
    requests = []

    def get(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(execute=lambda: response)

    gateway = GmailGateway(
        SimpleNamespace(users=lambda: SimpleNamespace(messages=lambda: SimpleNamespace(get=get)))
    )
    gateway._reconcile_budget = reconcile.ReconcileBudget(lambda: True)
    assert gateway.stored_body("m", scope=frozenset({"inbox"})) == ("Author\n> Quote", 14)
    assert gateway._reconcile_budget.calls == 1
    assert requests[-1]["format"] == "full"
    assert gateway.content("m", 100, scope=frozenset({"inbox"})).body == "plain analysis"
    response["labelIds"] = ["SENT"]
    with pytest.raises(MailboxMessageOutsideScope):
        gateway.stored_body("m", scope=frozenset({"inbox"}))


def test_microsoft_storage_requests_html_without_attachment_payload():
    import httpx

    from eom_email_watcher.mailbox import MailboxMessageOutsideScope
    from eom_email_watcher.microsoft365 import Microsoft365Gateway

    requests = []
    response = {
        "parentFolderId": "inbox-id",
        "hasAttachments": True,
        "body": {
            "contentType": "html",
            "content": '<div id="divRplyFwdMsg">Header</div><p>Bottom reply</p>',
        },
    }

    def handler(request):
        requests.append(request)
        assert "/attachments" not in str(request.url)
        return httpx.Response(200, json=response)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        gateway = Microsoft365Gateway("private", "owner@example.com", client)
        gateway._folder_ids = {"inbox": "inbox-id"}
        assert gateway.stored_body("m", scope=frozenset({"inbox"})) == (
            "> Header\nBottom reply",
            21,
        )
        assert 'outlook.body-content-type="text"' not in requests[0].headers.get("prefer", "")
        response["parentFolderId"] = "archive-id"
        with pytest.raises(MailboxMessageOutsideScope):
            gateway.stored_body("m", scope=frozenset({"inbox"}))


def test_imap_storage_prefers_html_keeps_full_count_and_analysis_plain():
    from test_imap import FakeImap, credentials, message_id

    from eom_email_watcher.imap import ImapGateway

    html = b"<blockquote>" + b"x" * 200_001 + b"</blockquote>"
    plain = b"plain analysis"
    client = FakeImap()
    client.bodystructure = (
        b'(("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "8BIT" '
        + str(len(plain)).encode()
        + b' 1)("TEXT" "HTML" ("CHARSET" "UTF-8") NIL NIL "8BIT" '
        + str(len(html)).encode()
        + b' 1)"ALTERNATIVE")'
    )
    client.sections = {"1": plain, "2": html}
    gateway = ImapGateway(credentials(), lambda _credentials, _context: client)
    text, count = gateway.stored_body(message_id(), scope=frozenset({"inbox"}))
    assert text.startswith("> ")
    assert len(text) == 200_000
    assert count == 200_003
    assert gateway.content(message_id(), 100, scope=frozenset({"inbox"})).body == "plain analysis"


def test_storage_mixed_body_keeps_independent_plain_footer():
    import base64

    def part(kind, text):
        return {
            "mimeType": kind,
            "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()},
        }

    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    part("text/plain", "plain alternative"),
                    part("text/html", "<blockquote>Quote</blockquote>"),
                ],
            },
            part("text/plain", "Authored footer"),
        ],
    }
    assert mime.extract_stored_body(payload) == ("> Quote\nAuthored footer", 23)


def test_imap_storage_mixed_body_keeps_independent_plain_footer():
    from test_imap import FakeImap, credentials, message_id

    from eom_email_watcher.imap import ImapGateway

    client = FakeImap()
    html = b"<blockquote>Quote</blockquote>"
    footer = b"Authored footer"
    client.bodystructure = (
        b'((("TEXT" "PLAIN" NIL NIL NIL "8BIT" 5 1)'
        + b'("TEXT" "HTML" NIL NIL NIL "8BIT" '
        + str(len(html)).encode()
        + b' 1)"ALTERNATIVE")'
        + b'("TEXT" "PLAIN" NIL NIL NIL "8BIT" '
        + str(len(footer)).encode()
        + b' 1)"MIXED")'
    )
    client.sections = {"1.1": b"plain", "1.2": html, "2": footer}
    gateway = ImapGateway(credentials(), lambda _credentials, _context: client)
    assert gateway.stored_body(message_id(), scope=frozenset({"inbox"})) == (
        "> Quote\nAuthored footer",
        23,
    )


def test_copy_returning_between_body_budget_slices_is_retried(tmp_path, monkeypatch):
    from eom_email_watcher import reconcile
    from eom_email_watcher.mailbox import MailboxMessageUnavailable

    outcomes = {"m": MailboxMessageUnavailable("gone"), "z-copy": MailboxMessageUnavailable("gone")}
    watcher, state, gateway = body_runner(tmp_path, monkeypatch, outcomes)
    seed(watcher.store, "z-copy", rfc_message_id="same@vendor")
    prepare_derived(state)
    original = reconcile.ReconcileBudget
    monkeypatch.setattr(reconcile, "ReconcileBudget", lambda allowed: original(allowed, calls=1))
    reconcile.run(watcher, KEY, "unavailable", label_selectors=(), now=NOW)
    outcomes["m"] = ("returned", 8)
    watcher.store.record_message_location(
        provider="gmail",
        account_id="mailbox",
        mailbox_identity_key=KEY,
        provider_message_id="m",
        locations=frozenset({"inbox"}),
        headers_observed=False,
        now=NOW + timedelta(seconds=1),
    )
    reconcile.run(watcher, KEY, "unavailable", label_selectors=(), now=NOW + timedelta(seconds=1))
    with watcher.store.connection() as db:
        row = db.execute("SELECT text FROM message_bodies").fetchone()
        assert row is not None and row[0] == "returned"
    assert [c[0] for c in gateway.body_calls] == ["m", "m"]


def test_recovery_retention_extension_does_not_widen_body_fetch_window(tmp_path, monkeypatch):
    from eom_email_watcher import body_state

    watcher, state, gateway = body_runner(tmp_path, monkeypatch, {})
    seed(watcher.store, "old", received_at=(NOW - timedelta(days=31)).isoformat())
    watcher.store.activate_mail_account("gmail", "mailbox")
    watcher.store.create_gmail_recovery_state(
        "mailbox",
        KEY,
        0,
        (),
        (),
        int((NOW - timedelta(days=40)).timestamp()),
        int(NOW.timestamp()),
        "200",
        retention_cutoff=NOW - timedelta(days=40),
        now=NOW,
    )
    with watcher.store.connection() as db:
        assert body_state.pending(db, state.scope, cutoff=NOW - timedelta(days=30), now=NOW) == [
            "m"
        ]


def test_gmail_invalid_folder_evidence_is_terminal_on_both_body_paths():
    from types import SimpleNamespace

    from eom_email_watcher.gmail import GmailGateway
    from eom_email_watcher.mailbox import MailboxMessageUnavailable

    response = {"labelIds": None, "payload": {}}
    gateway = GmailGateway(
        SimpleNamespace(
            users=lambda: SimpleNamespace(
                messages=lambda: SimpleNamespace(
                    get=lambda **_: SimpleNamespace(execute=lambda: response)
                )
            )
        )
    )
    with pytest.raises(MailboxMessageUnavailable):
        gateway.stored_body("m", scope=frozenset({"inbox"}))
    with pytest.raises(MailboxMessageUnavailable):
        gateway.content("m", 100, scope=frozenset({"inbox"}))


def test_microsoft_invalid_body_is_terminal():
    import httpx

    from eom_email_watcher.mailbox import MailboxMessageInvalid
    from eom_email_watcher.microsoft365 import Microsoft365Gateway

    def handler(request):
        return httpx.Response(200, json={"parentFolderId": "inbox-id", "body": ["invalid"]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        gateway = Microsoft365Gateway("private", "owner@example.com", client)
        gateway._folder_ids = {"inbox": "inbox-id"}
        with pytest.raises(MailboxMessageInvalid):
            gateway.stored_body("m", scope=frozenset({"inbox"}))


def legacy_body_store(tmp_path, monkeypatch):
    from test_db import Store

    from eom_email_watcher import body_state
    from eom_email_watcher import db as db_module

    with monkeypatch.context() as legacy:
        legacy.setattr(db_module, "SCHEMA_VERSION", 34)
        legacy.setattr(body_state, "ensure_schema", lambda db: None)
        legacy.setattr(
            db_module,
            "SENT_CAPTURE_SCHEMA",
            db_module.SENT_CAPTURE_SCHEMA.replace(
                "    DELETE FROM message_bodies WHERE message_id = OLD.message_id;\n", ""
            ).replace(
                "    DELETE FROM message_body_unavailable WHERE message_id = OLD.message_id;\n", ""
            ),
        )
        store = Store(tmp_path / "mail.db")
        store.initialize()
        store.register_mail_account("gmail", "mailbox", display_name="Mailbox")
        store.reconcile_mailbox_identity("gmail", "mailbox", KEY)
        vendor = store.create_vendor("Vendor")
        store.add_vendor_address(vendor, "vendor@example.com")
        seed(store)
        with store.connection() as db:
            db.execute(
                "INSERT INTO reconcile_coverage SELECT provider,account_id,"
                "mailbox_identity_key,generation,? FROM coverage_generation",
                (NOW.isoformat(),),
            )
    return store


def test_schema35_upgrade_retains_mail_and_invalidates_coverage_once(tmp_path, monkeypatch):
    from eom_email_watcher import body_state
    from eom_email_watcher import db as database

    store = legacy_body_store(tmp_path, monkeypatch)
    # Prove the original schema-35 boundary explicitly, then its additive v36
    # successor. Neither open may invalidate coverage after the one-time lift.
    with monkeypatch.context() as prior:
        prior.setattr(database, "SCHEMA_VERSION", 35)
        store.initialize()
    with store.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 35
        assert db.execute("SELECT message_id FROM messages").fetchone()[0] == "m"
        assert not db.execute("SELECT 1 FROM reconcile_coverage").fetchone()
        assert body_state.pending(
            db, ("gmail", "mailbox", KEY), cutoff=NOW - timedelta(days=30), now=NOW
        ) == ["m"]
        db.execute(
            "INSERT INTO reconcile_coverage SELECT provider,account_id,"
                "mailbox_identity_key,generation,? FROM coverage_generation",
            (NOW.isoformat(),),
        )
    store.initialize()
    with store.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 36
        for table in ("message_removal_requests", "message_removal_intents",
                      "message_removal_targets"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert db.execute("SELECT message_id FROM messages").fetchone()[0] == "m"
        assert db.execute("SELECT 1 FROM reconcile_coverage").fetchone()
    store.initialize()
    with store.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 36
        assert db.execute("SELECT 1 FROM reconcile_coverage").fetchone()


@pytest.mark.parametrize("upgrade", [False, True])
def test_body_cleanup_trigger_exists_and_deletes_logical_unit(tmp_path, monkeypatch, upgrade):
    store = legacy_body_store(tmp_path, monkeypatch) if upgrade else followed(tmp_path)[0]
    store.initialize()
    seed(store, "copy", rfc_message_id="different@vendor")
    with store.connection() as db:
        db.execute("UPDATE messages SET logical_of='m' WHERE message_id='copy'")
    with store.connection() as db:
        sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='messages_delete_recipients_and_locations'"
        ).fetchone()[0]
        for table in ("message_recipients", "message_locations", "message_bodies",
                      "message_body_unavailable", "messages"):
            assert f"DELETE FROM {table}" in sql
        db.execute(
            "INSERT INTO message_bodies VALUES('m','private body',12,12,?)", (NOW.isoformat(),)
        )
        db.execute("INSERT INTO message_body_unavailable VALUES('copy','source_gone')")
    # Exercise the actual trigger directly, so derived cleanup cannot conceal
    # a missing or stale trigger after fresh creation or a v34 upgrade.
    with sqlite3.connect(store.path) as db:
        db.execute("PRAGMA recursive_triggers=ON")
        db.execute("DELETE FROM messages WHERE message_id='m'")
        for table in ("messages", "message_recipients", "message_locations",
                      "message_bodies", "message_body_unavailable"):
            assert not db.execute(f"SELECT 1 FROM {table}").fetchone(), table


def test_schema35_full_disk_rolls_back_schema_mail_and_coverage(tmp_path, monkeypatch):
    from eom_email_watcher import body_state

    store = legacy_body_store(tmp_path, monkeypatch)
    with store.connection() as db:
        before = [tuple(r) for r in db.execute("SELECT name,sql FROM sqlite_master ORDER BY name")]
        coverage = [tuple(r) for r in db.execute("SELECT * FROM reconcile_coverage")]
    original = body_state.ensure_schema

    def no_room(db):
        original(db)
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(body_state, "ensure_schema", no_room)
    with pytest.raises(sqlite3.OperationalError, match="disk is full"):
        store.initialize()
    with store.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 34
        assert [
            tuple(r) for r in db.execute("SELECT name,sql FROM sqlite_master ORDER BY name")
        ] == before
        assert [tuple(r) for r in db.execute("SELECT * FROM reconcile_coverage")] == coverage
        assert db.execute("SELECT message_id FROM messages").fetchone()[0] == "m"


def test_checkpoint_never_clears_a_newer_deletion(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from eom_email_watcher import body_state

    store, state, _ = followed(tmp_path)
    seed(store, "n")
    for message_id in ("m", "n"):
        body_state.record(store, message_id, ("body", 4), fetched_at=NOW)
    local_removal(store, "delete_message", "m")
    original = store.connection
    injected = False

    class CheckpointRace:
        def __init__(self, db):
            self.db = db

        def __getattr__(self, name):
            return getattr(self.db, name)

        def execute(self, sql, *args):
            nonlocal injected
            if sql == "PRAGMA wal_checkpoint(TRUNCATE)" and not injected:
                result = self.db.execute(sql).fetchall()[0]
                injected = True
                local_removal(store, "delete_message", "n")
                return SimpleNamespace(fetchone=lambda: result)
            return self.db.execute(sql, *args)

    @contextmanager
    def connection():
        with original() as db:
            yield CheckpointRace(db)

    monkeypatch.setattr(store, "connection", connection)
    body_state.checkpoint(store)
    with original() as db:
        assert db.execute(
            "SELECT value FROM maintenance_state WHERE key='checkpoint_pending'"
        ).fetchone()
    assert body_state.checkpoint(store)
    with original() as db:
        assert not db.execute(
            "SELECT 1 FROM maintenance_state WHERE key='checkpoint_pending'"
        ).fetchone()


@pytest.mark.parametrize("size", [0, 1, 199_999, 200_000, 200_001])
def test_storage_character_boundary_accepts_full_body_and_marks_only_prefix(size):
    text, total = mime.stored_body_text("é" * size, is_html=False)
    assert len(text) == min(size, 200_000)
    assert total == size


def test_body_counts_include_embedded_null_and_unicode(tmp_path):
    from eom_email_watcher import body_state
    store, state, _ = followed(tmp_path)
    text, count = mime.stored_body_text("é\x00日🙂", is_html=False)
    body_state.record(store, "m", (text, count), fetched_at=NOW)
    with store.connection() as db:
        row = db.execute("SELECT text,stored_chars,source_chars FROM message_bodies").fetchone()
        assert tuple(row) == ("é\x00日🙂", 4, 4)
