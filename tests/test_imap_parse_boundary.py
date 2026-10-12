import json
from datetime import timedelta
from email import policy
from email.errors import HeaderParseError
from email.parser import BytesParser

import pytest
from test_imap import FakeImap, credentials, message_id
from test_thread_reconcile_round2 import NOW, Dates, gateway_unit

from eom_email_watcher import imap
from eom_email_watcher.mailbox import MailboxMessageInvalid


@pytest.mark.parametrize("field", ["From", "To", "Cc"])
def test_lone_quote_fallback_skips_bad_and_processes_next_uid(field):
    class Mixed(Dates):
        def uid(self, command, *args):
            if command == "FETCH" and "HEADER.FIELDS" in str(args[-1]):
                self.raw_message = (
                    (
                        ("" if field == "From" else "From: café@example.com\r\n")
                        + f'{field}: "\r\n\r\n'
                    ).encode()
                    if args[0] == "1"
                    else "From: café@example.com\r\n\r\n".encode()
                )
            return super().uid(command, *args)

    gateway, unit = gateway_unit(Mixed(uid_next=3, search=b"1 2"), addresses=["café@example.com"])
    first = gateway.reconcile_page(unit, start=NOW, cutoff=NOW - timedelta(days=30))
    assert not first.items
    # Durable unit state is sufficient after the bad message; a fresh gateway
    # must resume at the good UID without re-reading the poison message.
    unit = json.loads(json.dumps(unit))
    client = Mixed(uid_next=3, search=b"1 2")
    gateway, _ = gateway_unit(client)
    second = gateway.reconcile_page(unit, start=NOW, cutoff=NOW - timedelta(days=30))
    assert [item.message_id for item in second.items] == [message_id(2)]
    assert second.token is None
    assert all(call[2] == "2" for call in client.calls if call[:2] == ("uid", "FETCH"))


@pytest.mark.parametrize("field", ["From", "To", "Cc"])
def test_lone_quote_metadata_is_item_failure(field):
    gateway = imap.ImapGateway(
        credentials(), lambda *_: FakeImap(raw_message=f'{field}: "\r\n\r\n'.encode())
    )
    with pytest.raises(MailboxMessageInvalid) as raised:
        gateway.metadata(message_id())
    assert raised.value.code == "imap_message_invalid"


ERRORS = [IndexError, ValueError, TypeError, HeaderParseError, RecursionError, UnicodeError]


@pytest.mark.parametrize("error", ERRORS)
@pytest.mark.parametrize("path", ["content", "attachment"])
def test_lazy_mime_failure_is_item_failure(error, path, monkeypatch):
    message = BytesParser(policy=policy.default).parsebytes(b"Content-Type: text/plain\r\n\r\nbody")

    def fail(*_args, **_kwargs):
        raise error("private parser details")

    if path == "content":
        monkeypatch.setattr(message, "get_filename", fail)

        def operation():
            return imap._content_and_attachment_payloads(message, 100)
    else:
        message.set_type("message/rfc822")
        child = BytesParser(policy=policy.default).parsebytes(b"\r\nchild")
        monkeypatch.setattr(child, "as_bytes", fail)
        message.set_payload([child])

        def operation():
            return imap._attachment_payload(message)

    with pytest.raises(MailboxMessageInvalid) as raised:
        operation()
    assert raised.value.code == (
        "imap_mime_too_complex" if error is RecursionError else "imap_message_invalid"
    )
    assert "private parser details" not in str(raised.value)


@pytest.mark.parametrize(
    "raw", [b"\r\n", b"From: vendor@example.com\r\nTo: user@example.com\r\n\r\n"]
)
def test_valid_and_empty_headers_remain_readable(raw):
    gateway = imap.ImapGateway(credentials(), lambda *_: FakeImap(raw_message=raw))
    metadata = gateway.metadata(message_id())
    assert metadata.sender == ("vendor@example.com" if b"From:" in raw else "")
    assert metadata.to == (("user@example.com",) if b"To:" in raw else ())


@pytest.mark.parametrize("error", ERRORS)
@pytest.mark.parametrize("path", ["metadata", "fallback", "thread", "reply", "multipart", "decode"])
def test_header_parser_family_has_one_failure_mapping(error, path, monkeypatch):
    class BadHeaders:
        def get(self, *_args):
            raise error("private parser details")

        get_content_type = get_boundary = get_payload = get

    class Parser:
        def __init__(self, **_kwargs):
            pass

        def parsebytes(self, *_args, **_kwargs):
            return BadHeaders()

    monkeypatch.setattr(imap, "BytesParser", Parser)
    if path in {"fallback", "thread"}:
        selection = (
            {"addresses": ["café@example.com"]}
            if path == "fallback"
            else {"ids": ["café@example.com"]}
        )
        gateway, unit = gateway_unit(Dates(uid_next=2, search=b"1"), **selection)
        page = gateway.reconcile_page(unit, start=NOW, cutoff=NOW - timedelta(days=30))
        assert not page.items
        assert page.token is None
    elif path == "reply":
        assert imap._reply_ids(b"References: <valid@example.com>\r\n") == ()
    else:
        with pytest.raises(MailboxMessageInvalid) as raised:
            if path == "metadata":
                imap.ImapGateway(credentials(), lambda *_: FakeImap()).metadata(message_id())
            elif path == "multipart":
                imap._multipart_attachment_prefix(
                    b"Content-Type: multipart/mixed\r\n", "multipart/mixed"
                )
            else:
                imap._decoded_section(b"body", "7bit")
        expected = (
            "imap_headers_too_complex"
            if path == "metadata" and error is RecursionError
            else "imap_message_invalid"
        )
        assert raised.value.code == expected
        assert "private parser details" not in str(raised.value)


def test_optional_reply_header_positive_and_empty_controls():
    assert imap._reply_ids(None) == ()
    assert imap._reply_ids(b"\r\n") == ()
    assert imap._reply_ids(b"References: <valid@example.com>\r\n") == ("valid@example.com",)


def test_transport_errors_are_not_message_parse_errors():
    with pytest.raises(OSError, match="transport"), imap._message_parse_boundary():
        raise OSError("transport")
