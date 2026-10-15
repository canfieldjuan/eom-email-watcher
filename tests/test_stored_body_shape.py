"""Malformed provider trees never become a complete stored body."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from eom_email_watcher.gmail import GmailGateway
from eom_email_watcher.mailbox import MailboxMessageInvalid


def leaf():
    return {"mimeType": "text/plain", "body": {"data": "YWJj", "size": 3}}


def gateway(payload):
    return GmailGateway(SimpleNamespace(users=lambda: SimpleNamespace(messages=lambda:
        SimpleNamespace(get=lambda **_: SimpleNamespace(execute=lambda: {
            "labelIds": ["INBOX"], "payload": payload,
        }))
    )))


@pytest.mark.parametrize("bad", ["abc", {}, [leaf(), "bad"], [leaf(), None]])
def test_malformed_parts_are_terminal(bad):
    payload = {"mimeType": "multipart/mixed", "body": {}, "parts": deepcopy(bad)}
    with pytest.raises(MailboxMessageInvalid):
        gateway(payload).stored_body("m", scope=frozenset({"inbox"}))


@pytest.mark.parametrize("body", [None, [], "bad", {"data": 7}, {"size": True},
                                  {"size": -1}, {"size": 3}, {"attachmentId": 7}])
def test_malformed_body_is_terminal(body):
    payload = leaf()
    payload["body"] = body
    with pytest.raises(MailboxMessageInvalid):
        gateway(payload).stored_body("m", scope=frozenset({"inbox"}))


def test_malformed_mime_type_is_terminal():
    payload = leaf()
    payload["mimeType"] = 3
    with pytest.raises(MailboxMessageInvalid):
        gateway(payload).stored_body("m", scope=frozenset({"inbox"}))


@pytest.mark.parametrize("depth", [3, 101])
def test_nested_valid_twin_and_depth_bound(depth):
    payload = leaf()
    for _ in range(depth):
        payload = {"mimeType": "multipart/mixed", "body": {}, "parts": [payload]}
    if depth == 3:
        assert gateway(payload).stored_body("m", scope=frozenset({"inbox"})) == ("abc", 3)
    else:
        with pytest.raises(MailboxMessageInvalid):
            gateway(payload).stored_body("m", scope=frozenset({"inbox"}))


def test_part_count_is_bounded():
    payload = {"mimeType": "multipart/mixed", "body": {}, "parts": [leaf() for _ in range(1000)]}
    with pytest.raises(MailboxMessageInvalid):
        gateway(payload).stored_body("m", scope=frozenset({"inbox"}))


def test_large_binary_attachment_does_not_reject_valid_text():
    payload = {"mimeType": "multipart/mixed", "body": {}, "parts": [leaf(), {
        "mimeType": "application/pdf", "filename": "invoice.pdf",
        "body": {"size": 60 * 1024 * 1024, "attachmentId": "attachment"},
    }]}
    assert gateway(payload).stored_body("m", scope=frozenset({"inbox"})) == ("abc", 3)


@pytest.mark.parametrize("payload", [None, [], "", False, 0])
def test_invalid_root_does_not_default_to_empty(payload):
    with pytest.raises(MailboxMessageInvalid):
        gateway(payload).stored_body("m", scope=frozenset({"inbox"}))


@pytest.mark.parametrize("depth", [99, 100, 101])
def test_exact_depth_boundary(depth):
    payload = leaf()
    for _ in range(depth):
        payload = {"mimeType": "multipart/mixed", "body": {}, "parts": [payload]}
    if depth <= 100:
        assert gateway(payload).stored_body("m", scope=frozenset({"inbox"})) == ("abc", 3)
    else:
        with pytest.raises(MailboxMessageInvalid):
            gateway(payload).stored_body("m", scope=frozenset({"inbox"}))


@pytest.mark.parametrize("children", [998, 999, 1000])
def test_exact_part_count_boundary(children):
    payload = {"mimeType": "multipart/mixed", "body": {}, "parts": [leaf()] * children}
    if children < 1000:
        result, count = gateway(payload).stored_body("m", scope=frozenset({"inbox"}))
        assert result.splitlines() == ["abc"] * children
        assert count == len(result)
    else:
        with pytest.raises(MailboxMessageInvalid):
            gateway(payload).stored_body("m", scope=frozenset({"inbox"}))


@pytest.mark.parametrize("bad", [None, False, 0, "", {}, [None]])
def test_falsy_parts_never_default_to_empty(bad):
    payload = {"mimeType": "multipart/mixed", "body": {}, "parts": bad}
    with pytest.raises(MailboxMessageInvalid):
        gateway(payload).stored_body("m", scope=frozenset({"inbox"}))


@pytest.mark.parametrize("valid", [False, True])
def test_whole_tree_validation_settles_through_reconcile(tmp_path, monkeypatch, valid):
    from test_thread_bodies import body_runner, prepare_derived
    from test_thread_reconcile import KEY, NOW

    from eom_email_watcher import reconcile

    watcher, state, source = body_runner(tmp_path, monkeypatch, {})
    payload = {"mimeType": "multipart/mixed", "body": {}, "parts": [leaf()]}
    if not valid:
        payload["parts"].append("invalid sibling")
    source.stored_body = lambda message_id, *, scope: gateway(payload).stored_body(
        message_id, scope=scope,
    )
    prepare_derived(state)
    reconcile.run(watcher, KEY, "unavailable", label_selectors=(), now=NOW)
    assert not state.stale()
    with watcher.store.connection() as db:
        stored = db.execute("SELECT text FROM message_bodies WHERE message_id='m'").fetchone()
        invalid = db.execute(
            "SELECT reason FROM message_body_unavailable WHERE message_id='m'"
        ).fetchone()
    assert stored[0] == "abc" if valid else stored is None
    assert invalid is None if valid else invalid[0] == "source_gone"
