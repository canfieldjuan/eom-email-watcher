"""Component obligations and bounded IMAP candidate-selection regressions."""

from datetime import timedelta

import pytest
from test_imap import FakeImap, credentials
from test_thread_reconcile import KEY, NOW, runner, seed
from test_thread_reconcile import reconcile_clock as reconcile_clock

from eom_email_watcher import db as database
from eom_email_watcher import reconcile
from eom_email_watcher.imap import MAX_HEADER_BYTES, ImapGateway, _message_id


def test_imap_merge_carries_late_follow_target(tmp_path, monkeypatch, reconcile_clock):
    watcher, state = runner(tmp_path, monkeypatch, object(), "imap")
    seed(watcher.store, "a", provider="imap", rfc_message_id="survivor@root")
    survivor = state.threads()[0]
    progress = state.load_or_start(NOW, 30, {"stage": "sync", "units": []})
    assert state.watermark(progress, survivor)
    later = NOW + timedelta(hours=1)
    reconcile_clock.current = later
    seed(watcher.store, "z", provider="imap", rfc_message_id="loser@root")
    loser = next(t for t in state.threads() if t["key"] != survivor["key"])
    assert state.catchup_targets(progress) == {loser["key"]: later.isoformat()}
    seed(
        watcher.store,
        "bridge",
        provider="imap",
        rfc_message_id="bridge@root",
        reply_ids=("survivor@root", "loser@root"),
    )
    assert state.catchup_targets(progress) == {survivor["key"]: later.isoformat()}
    assert state.watermark(progress, state.threads()[0])
    assert state.catchup_pending(progress)
    with pytest.raises(RuntimeError, match="catch-up remains pending"):
        state.complete(progress)


class Dates(FakeImap):
    def uid(self, command, *args):
        if command == "FETCH" and args[-1] == "(UID INTERNALDATE)":
            self.calls.append(("uid", command, *args))
            uid = int(args[0])
            stamp = (NOW - timedelta(days=1)).strftime("%d-%b-%Y %H:%M:%S +0000")
            return "OK", [f'{uid} (UID {uid} INTERNALDATE "{stamp}")'.encode()]
        if command == "SEARCH" and "CHARSET" in args:
            self.calls.append(("uid", command, *args))
            return "NO", [b"[BADCHARSET (US-ASCII)] not supported"]
        return super().uid(command, *args)


def gateway_unit(client, **selection):
    gateway = ImapGateway(credentials())
    gateway._active_client = client
    gateway._ensure_selected(client, "INBOX")
    return gateway, dict(
        folder="inbox", token=None, snapshot=gateway.reconcile_snapshot("inbox"), **selection
    )


def test_badcharset_address_fallback_rejects_unrelated_candidate():
    client = Dates(uid_next=2, search=b"1", raw_message=b"From: other@example.com\r\n\r\n")
    gateway, unit = gateway_unit(client, addresses=["caf\u00e9@example.com"])
    page = gateway.reconcile_page(unit, start=NOW, cutoff=NOW - timedelta(days=30))
    assert not page.items
    assert page.token is None
    assert any("HEADER.FIELDS (FROM TO CC)" in str(call) for call in client.calls)


def test_idless_imap_sync_uses_stored_sources_only():
    client = Dates(uid_next=3, search=b"1 2")
    gateway, unit = gateway_unit(client, ids=[])
    source = _message_id(gateway._mailbox_id, 44, 1)
    unit["sources"] = [source]
    page = gateway.reconcile_page(unit, start=NOW, cutoff=NOW - timedelta(days=30))
    assert page.token is None
    assert [item.message_id for item in page.items] == [source]
    assert not any(call[:2] == ("uid", "SEARCH") for call in client.calls)


@pytest.mark.parametrize("survivor_later", [False, True])
def test_merge_preserves_max_target_and_other_scope(
    tmp_path, monkeypatch, reconcile_clock, survivor_later
):
    watcher, state = runner(tmp_path, monkeypatch, object(), "imap")
    # Foreign-key enforcement requires the independent scope's real parent;
    # retain the original same-key/different-account isolation control.
    watcher.store.register_mail_account("imap", "other", display_name="Other mailbox")
    watcher.store.reconcile_mailbox_identity("imap", "other", KEY)
    seed(
        watcher.store, "other-message", provider="imap", account_id="other",
        rfc_message_id="other@root",
    )
    with watcher.store.connection() as db:
        other_thread = db.execute(
            "SELECT thread_key FROM thread_follow WHERE provider='imap' AND account_id='other' "
            "AND mailbox_identity_key=?", (KEY,),
        ).fetchone()[0]
    seed(watcher.store, "a", provider="imap", rfc_message_id="survivor@root")
    survivor = state.threads()[0]
    progress = state.load_or_start(NOW, 30, {"units": []})
    reconcile_clock.current = NOW + timedelta(hours=1)
    seed(watcher.store, "z", provider="imap", rfc_message_id="loser@root")
    later = NOW + timedelta(hours=2 if survivor_later else 0)
    with watcher.store.connection() as db:
        db.execute(
            "INSERT INTO reconcile_catchup VALUES (?,?,?,?,?,?)",
            (*state.scope, progress["generation"], survivor["key"], later.isoformat()),
        )
        db.execute(
            "INSERT INTO reconcile_catchup VALUES (?,?,?,?,?,?)",
            ("imap", "other", KEY, progress["generation"], other_thread, later.isoformat()),
        )
    seed(
        watcher.store,
        "bridge",
        provider="imap",
        rfc_message_id="bridge@root",
        reply_ids=("survivor@root", "loser@root"),
    )
    expected = max(later, NOW + timedelta(hours=1)).isoformat()
    assert state.catchup_targets(progress) == {survivor["key"]: expected}
    with watcher.store.connection() as db:
        assert (
            db.execute("SELECT target FROM reconcile_catchup WHERE account_id='other'").fetchone()[
                0
            ]
            == later.isoformat()
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM reconcile_catchup WHERE account_id='mailbox'"
            ).fetchone()[0]
            == 1
        )


def test_merge_target_rekey_rolls_back_with_component(tmp_path, monkeypatch, reconcile_clock):
    watcher, state = runner(tmp_path, monkeypatch, object(), "imap")
    seed(watcher.store, "a", provider="imap", rfc_message_id="survivor@root")
    progress = state.load_or_start(NOW, 30, {"units": []})
    reconcile_clock.current = NOW + timedelta(hours=1)
    seed(watcher.store, "z", provider="imap", rfc_message_id="loser@root")
    before = state.catchup_targets(progress)
    original = database._apply_imap_component

    def fail_after_rekey(*args, **kwargs):
        original(*args, **kwargs)
        if kwargs["merged"]:
            raise RuntimeError("abort merged capture")

    monkeypatch.setattr(database, "_apply_imap_component", fail_after_rekey)
    with pytest.raises(RuntimeError, match="abort merged capture"):
        seed(
            watcher.store,
            "bridge",
            provider="imap",
            rfc_message_id="bridge@root",
            reply_ids=("survivor@root", "loser@root"),
        )
    assert state.catchup_targets(progress) == before
    assert len(state.threads()) == 2


@pytest.mark.parametrize(
    "headers, admitted",
    [
        ("From: Vendor <CAFÉ@EXAMPLE.COM>", True),
        ("From: other@example.com\r\nTo: Vendor <café@example.com>", True),
        ("From: other@example.com\r\nCc: Vendor <café@example.com>", True),
        ('From: "café@example.com" <other@example.com>', False),
        ("From: other@example.com", False),
    ],
)
def test_badcharset_local_address_match_has_positive_and_negative_controls(headers, admitted):
    raw = (headers + "\r\n\r\n").encode()
    client = Dates(uid_next=2, raw_message=raw)
    gateway, unit = gateway_unit(client, addresses=["caf\u00e9@example.com"])
    page = gateway.reconcile_page(unit, start=NOW, cutoff=NOW - timedelta(days=30))
    assert bool(page.items) is admitted
    queries = [str(c[-1]) for c in client.calls if c[:2] == ("uid", "FETCH")]
    assert queries == ["(UID INTERNALDATE)", "(UID BODY.PEEK[HEADER.FIELDS (FROM TO CC)]<0.65536>)"]


@pytest.mark.parametrize(
    "kind", ["empty", "wrong_mailbox", "wrong_epoch", "wrong_folder", "above_snapshot"]
)
def test_idless_sources_do_not_widen_frozen_scope(kind):
    client = Dates(uid_next=3, search=b"1 2")
    gateway, unit = gateway_unit(client, ids=[])
    mailbox, epoch, uid, folder = gateway._mailbox_id, 44, 1, None
    if kind == "wrong_mailbox":
        mailbox = "0" * 64
    if kind == "wrong_epoch":
        epoch = 45
    if kind == "wrong_folder":
        folder = "a" * 64
    if kind == "above_snapshot":
        uid = 3
    unit["sources"] = [] if kind == "empty" else [_message_id(mailbox, epoch, uid, folder)]
    page = gateway.reconcile_page(unit, start=NOW, cutoff=NOW - timedelta(days=30))
    assert not page.items and page.token is None
    assert not any(c[:2] == ("uid", "SEARCH") or c[:2] == ("uid", "FETCH") for c in client.calls)


def test_idless_real_runner_uses_only_its_source(tmp_path, monkeypatch):
    client = Dates(uid_next=3, search=b"1 2", raw_message=b"From: vendor@example.com\r\n\r\n")
    gateway, _ = gateway_unit(client)
    watcher, state = runner(tmp_path, monkeypatch, gateway, "imap")
    source = _message_id(gateway._mailbox_id, 44, 1)
    seed(watcher.store, source, provider="imap", rfc_message_id=None)
    state.observe(active=True, retention_days=30, sent_folder="unavailable", now=NOW)
    progress = state.load_or_start(NOW, 30, {"units": []})
    from eom_email_watcher import body_state

    body_state.record(watcher.store, source, ("existing", 8), fetched_at=NOW)
    state.complete(progress)
    client.calls.clear()
    reconcile.run(watcher, KEY, "unavailable", label_selectors=(), now=NOW)
    assert not state.stale()
    assert not any(c[:2] == ("uid", "SEARCH") for c in client.calls)
    assert all(c[2] == "1" for c in client.calls if c[:2] == ("uid", "FETCH"))


@pytest.mark.parametrize("age, expected_date_read", [(31, False), (30, True), (0, False)])
def test_idless_known_sources_respect_window_before_provider_read(
    tmp_path, monkeypatch, age, expected_date_read
):
    client = Dates(uid_next=3, search=b"1 2", raw_message=b"From: vendor@example.com\r\n\r\n")
    gateway, _ = gateway_unit(client)
    watcher, state = runner(tmp_path, monkeypatch, gateway, "imap")
    source = _message_id(gateway._mailbox_id, 44, 1)
    seed(
        watcher.store,
        source,
        provider="imap",
        rfc_message_id=None,
        received_at=(NOW - timedelta(days=age)).isoformat(),
    )
    state.observe(active=True, retention_days=30, sent_folder="unavailable", now=NOW)
    progress = state.load_or_start(NOW, 30, {"units": []})
    from eom_email_watcher import body_state

    body_state.record(watcher.store, source, ("existing", 8), fetched_at=NOW)
    state.complete(progress)
    client.calls.clear()
    reconcile.run(watcher, KEY, "unavailable", label_selectors=(), now=NOW)
    assert not state.stale()
    assert any(c[:2] == ("uid", "FETCH") for c in client.calls) is expected_date_read
    assert not any(c[:2] == ("uid", "SEARCH") for c in client.calls)


@pytest.mark.parametrize(
    "size,admitted",
    [
        (0, False),
        (MAX_HEADER_BYTES - 1, True),
        (MAX_HEADER_BYTES, False),
        (MAX_HEADER_BYTES + 1, False),
    ],
)
def test_address_fallback_header_bound(size, admitted):
    base = "From: café@example.com\r\nX-Pad: ".encode()
    payload = (base + b"x" * max(0, size - len(base)))[:size]

    class Bounded(Dates):
        def uid(self, command, *args):
            if command == "FETCH" and "HEADER.FIELDS (FROM TO CC)" in str(args[-1]):
                return "OK", [(b"1 (UID 1 BODY[HEADER.FIELDS (FROM TO CC)]", payload)]
            return super().uid(command, *args)

    client = Bounded(uid_next=2)
    gateway, unit = gateway_unit(client, addresses=["café@example.com"])
    page = gateway.reconcile_page(unit, start=NOW, cutoff=NOW - timedelta(days=30))
    assert bool(page.items) is admitted


def test_address_fallback_ignores_unsolicited_header_uid():
    class Mixed(Dates):
        def uid(self, command, *args):
            if command == "FETCH" and "HEADER.FIELDS (FROM TO CC)" in str(args[-1]):
                return "OK", [
                    (
                        b"99 (UID 99 BODY[HEADER.FIELDS (FROM TO CC)]",
                        b"From: other@example.com\r\n",
                    ),
                    (
                        b"1 (UID 1 BODY[HEADER.FIELDS (FROM TO CC)]",
                        "From: café@example.com\r\n".encode(),
                    ),
                ]
            return super().uid(command, *args)

    gateway, unit = gateway_unit(Mixed(uid_next=2), addresses=["café@example.com"])
    page = gateway.reconcile_page(unit, start=NOW, cutoff=NOW - timedelta(days=30))
    assert len(page.items) == 1


def test_component_sources_include_recorded_copies_only_in_its_scope(tmp_path, monkeypatch):
    watcher, state = runner(tmp_path, monkeypatch, object(), "imap")
    seed(watcher.store, "primary", provider="imap", rfc_message_id="root@example.com")
    thread = state.threads()[0]
    with watcher.store.connection() as db:
        message = db.execute(
            "SELECT message_id FROM messages WHERE provider_message_id='primary'"
        ).fetchone()[0]
        db.execute(
            "INSERT INTO message_locations VALUES (?,?,?,?,?,?,?,?)",
            (message, *state.scope, "sent-copy", "sent", None, NOW.isoformat()),
        )
        db.execute(
            "INSERT INTO message_locations VALUES (?,?,?,?,?,?,?,?)",
            (message, "imap", "mailbox", "0" * 64, "other-identity", "sent", None, NOW.isoformat()),
        )
    assert set(state.sources(thread["key"])) == {"primary", "sent-copy"}
