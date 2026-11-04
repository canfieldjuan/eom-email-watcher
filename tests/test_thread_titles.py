"""The open-thread title follows the shared visible-member rule, not its page."""

from datetime import timedelta

import pytest
from conftest import local_removal
from test_thread_bodies import followed
from test_thread_copy_reads import selection
from test_thread_message_reads import messages
from test_thread_reconcile import NOW, seed

from eom_email_watcher import body_state, thread_reads


@pytest.mark.parametrize("subject", ["Newest visible subject", ""])
def test_title_uses_newest_visible_tie_winner_outside_first_page(tmp_path, subject):
    store, _, vendor = followed(tmp_path)
    for source, title in [("z-new", "Losing tie"), ("a-new", subject)]:
        seed(store, source, subject=title, received_at=NOW.isoformat())
    seed(store, "invalid-date", subject="Invalid date cannot win")
    with store.connection() as db:
        db.execute("UPDATE messages SET received_at='invalid' WHERE message_id='invalid-date'")
    expected = subject or "No subject"
    listing = thread_reads.list_threads(store, vendor)["items"][0]
    first = messages(store, vendor, limit=1)
    assert listing["subject"] == first["thread"]["subject"] == expected
    assert first["items"][0]["message_id"] == "m"
    assert first["items"][0]["subject"] == "Quote" != expected
    cursor = first["next_cursor"]
    while cursor:
        page = messages(store, vendor, limit=1, cursor=cursor)
        assert page["thread"]["subject"] == expected
        cursor = page["next_cursor"]


def test_all_invalid_dates_use_the_same_canonical_source_title_tie(tmp_path):
    store, _, vendor = followed(tmp_path)
    seed(store, "a-first", subject="Canonical byte-order winner")
    with store.connection() as db:
        db.execute("UPDATE messages SET received_at='invalid'")
    assert (
        thread_reads.list_threads(store, vendor)["items"][0]["subject"]
        == (messages(store, vendor)["thread"]["subject"])
        == "Canonical byte-order winner"
    )


def test_title_uses_logical_max_date_but_canonical_subject(tmp_path):
    store, _, vendor = followed(tmp_path)
    seed(store, "middle", subject="Middle", received_at=NOW.isoformat())
    seed(
        store,
        "later-copy",
        subject="Physical copy",
        received_at=(NOW + timedelta(days=1)).isoformat(),
    )
    with store.connection() as db:
        db.execute("UPDATE messages SET logical_of='m' WHERE message_id='later-copy'")
    assert (
        thread_reads.list_threads(store, vendor)["items"][0]["subject"]
        == (messages(store, vendor, limit=1)["thread"]["subject"])
        == "Quote"
    )


def test_title_change_stales_cursor_and_removed_title_never_returns(tmp_path):
    store, _, vendor = followed(tmp_path)
    seed(store, "newest", subject="Old title", received_at=NOW.isoformat())
    ref = selection(store, vendor)
    first = messages(store, vendor, ref, limit=1)
    with store.connection() as db:
        db.execute("UPDATE messages SET subject='New title' WHERE message_id='newest'")
    with pytest.raises(thread_reads.ThreadReadError) as error:
        messages(store, vendor, ref, limit=1, cursor=first["next_cursor"])
    assert error.value.code == "stale_cursor"
    assert messages(store, vendor, ref, limit=1)["thread"]["subject"] == "New title"
    local_removal(store, "delete_message", "newest")
    assert messages(store, vendor, ref)["thread"]["subject"] == "Quote"
    assert thread_reads.list_threads(store, vendor)["items"][0]["subject"] == "Quote"


def test_physically_held_removal_excludes_the_newest_title(tmp_path, monkeypatch):
    from test_removal_refusal import held_runtime

    store = held_runtime(tmp_path, monkeypatch, small=True)
    vendor = store.list_vendors()[0]["vendor_id"]
    with store.connection() as db:
        db.execute("UPDATE messages SET subject='Hidden newest title' WHERE message_id='m'")
        db.execute(
            "UPDATE messages SET subject='Visible title',received_at=? WHERE message_id='small'",
            ((NOW - timedelta(days=3)).isoformat(),),
        )
    ref = selection(store, vendor)
    assert messages(store, vendor, ref)["thread"]["subject"] == "Hidden newest title"
    local_removal(store, "delete_message", "m")
    with store.connection() as db:
        assert db.execute("SELECT subject FROM messages WHERE message_id='m'").fetchone()[0] == (
            "Hidden newest title"
        )
    assert messages(store, vendor, ref)["thread"]["subject"] == "Visible title"
    assert thread_reads.list_threads(store, vendor)["items"][0]["subject"] == "Visible title"


def test_title_and_headers_share_one_snapshot_across_concurrent_commit(tmp_path, monkeypatch):
    store, _, vendor = followed(tmp_path)
    seed(store, "newest", subject="Before commit", received_at=NOW.isoformat())
    with store.connection() as db:
        db.execute("PRAGMA journal_mode=WAL")
    original, committed = body_state.project, False

    def commit_during_projection(db, *args, **kwargs):
        nonlocal committed
        if not committed:
            committed = True
            with store.connection() as writer:
                writer.execute(
                    "UPDATE messages SET subject='After commit' WHERE message_id='newest'"
                )
        return original(db, *args, **kwargs)

    monkeypatch.setattr(body_state, "project", commit_during_projection)
    first = messages(store, vendor)
    assert first["thread"]["subject"] == first["items"][-1]["subject"] == "Before commit"
    second = messages(store, vendor)
    assert second["thread"]["subject"] == second["items"][-1]["subject"] == "After commit"


def test_title_context_itself_participates_in_cursor_revision(tmp_path, monkeypatch):
    store, _, vendor = followed(tmp_path)
    seed(store, "second")
    first = messages(store, vendor, limit=1)
    original = thread_reads._thread_title

    def changed_title(members):
        _, order = original(members)
        return "Changed projection title", order

    monkeypatch.setattr(thread_reads, "_thread_title", changed_title)
    with pytest.raises(thread_reads.ThreadReadError) as error:
        messages(store, vendor, limit=1, cursor=first["next_cursor"])
    assert error.value.code == "stale_cursor"
