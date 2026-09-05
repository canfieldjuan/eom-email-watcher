from __future__ import annotations

import imaplib
import ssl
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path
from typing import Any

import pytest

from eom_email_watcher.imap import (
    CURSOR_PREFIX,
    MAX_ATTACHMENT_FILENAME_BYTES,
    MAX_ATTACHMENT_FILENAME_TOTAL_BYTES,
    MAX_HEADER_BYTES,
    MAX_INCREMENTAL_MESSAGE_IDS,
    MAX_MESSAGE_BYTES,
    MAX_MIME_DEPTH,
    MAX_MIME_PARTS,
    MESSAGE_ID_PREFIX,
    ImapCredentials,
    ImapError,
    ImapGateway,
    _content,
    _content_and_attachment_payloads,
    _synthesized_attachment_name,
    credentials_from_connection,
    imap_mailbox_identity,
    load_credentials,
    write_credentials,
)
from eom_email_watcher.mailbox import (
    MailboxMessageInvalid,
    MailboxMessageUnavailable,
    StaleMailboxCursor,
)

RAW_MESSAGE = b"""From: Sender Name <WATCHED@Example.com>\r
Subject: Invoice received\r
Date: Fri, 04 Sep 2026 10:15:00 -0500\r
Message-ID: <message@example.com>\r
MIME-Version: 1.0\r
Content-Type: multipart/mixed; boundary=boundary\r
\r
--boundary\r
Content-Type: text/plain; charset=utf-8\r
\r
The invoice is attached.\r
--boundary\r
Content-Type: application/pdf\r
Content-Disposition: attachment; filename=invoice.pdf\r
Content-Transfer-Encoding: base64\r
\r
UERGREFUQQ==\r
--boundary--\r
"""

FORWARDED_MESSAGE = b"""From: Sender Name <WATCHED@Example.com>\r
Subject: Forwarded message\r
Date: Fri, 04 Sep 2026 10:15:00 -0500\r
Message-ID: <forwarded@example.com>\r
MIME-Version: 1.0\r
Content-Type: multipart/mixed; boundary=outer\r
\r
--outer\r
Content-Type: text/plain; charset=utf-8\r
\r
Outer body only.\r
--outer\r
Content-Type: message/rfc822\r
Content-Disposition: attachment; filename=forwarded.eml\r
\r
From: Inner Sender <inner@example.com>\r
Subject: Private forwarded content\r
Date: Thu, 03 Sep 2026 10:15:00 -0500\r
\r
Inner body must not join the outer body.\r
--outer--\r
"""

FILENAMELESS_ATTACHMENTS = b"""From: Sender Name <WATCHED@Example.com>\r
Subject: Filename-less attachments\r
Date: Fri, 04 Sep 2026 10:15:00 -0500\r
Message-ID: <filename-less@example.com>\r
MIME-Version: 1.0\r
Content-Type: multipart/mixed; boundary=outer\r
\r
--outer\r
Content-Type: text/plain; charset=utf-8\r
\r
Outer body only.\r
--outer\r
Content-Type: message/rfc822\r
Content-Disposition: attachment\r
\r
From: Inner Sender <inner@example.com>\r
Subject: Private forwarded content\r
\r
Embedded private body.\r
--outer\r
Content-Type: text/plain; charset=utf-8\r
Content-Disposition: attachment\r
\r
Private text attachment.\r
--outer--\r
"""


def credentials() -> ImapCredentials:
    return ImapCredentials(
        email_address="owner@example.com",
        host="mail.example.com",
        port=993,
        security="tls",
        username="owner@example.com",
        password="private-password",
    )


def cursor(uid: int = 7, *, uid_validity: int = 44, values: ImapCredentials | None = None) -> str:
    mailbox_id = imap_mailbox_identity(values or credentials())
    return f"{CURSOR_PREFIX}{mailbox_id}:{uid_validity}:{uid}"


def message_id(
    uid: int = 7, *, uid_validity: int = 44, values: ImapCredentials | None = None
) -> str:
    mailbox_id = imap_mailbox_identity(values or credentials())
    return f"{MESSAGE_ID_PREFIX}{mailbox_id}:{uid_validity}:{uid}"


class FakeImap:
    def __init__(
        self,
        *,
        uid_validity: int = 44,
        uid_next: int = 8,
        search: bytes = b"1",
        raw_message: bytes = RAW_MESSAGE,
        sequence_uids: list[int] | None = None,
    ) -> None:
        self.uid_validity = uid_validity
        self.uid_next = uid_next
        self.search_response = search
        self.raw_message = raw_message
        self.sequence_uids = sequence_uids or [7]
        self.calls: list[tuple[Any, ...]] = []
        self.readonly: bool | None = None
        self.logged_out = False

    def login(self, username: str, password: str) -> tuple[str, list[bytes]]:
        self.calls.append(("login", username, password))
        return "OK", [b"authenticated"]

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.calls.append(("select", mailbox, readonly))
        self.readonly = readonly
        return "OK", [str(len(self.sequence_uids)).encode("ascii")]

    def response(self, name: str) -> tuple[str, list[bytes]]:
        value = self.uid_validity if name == "UIDVALIDITY" else self.uid_next
        return name, [str(value).encode("ascii")]

    def uid(self, command: str, *args: object) -> tuple[str, list[Any]]:
        self.calls.append(("uid", command, *args))
        if command == "SEARCH":
            return "OK", [self.search_response]
        query = str(args[-1])
        uid = str(args[0])
        if "HEADER.FIELDS" in query:
            headers, _separator, _body = self.raw_message.partition(b"\r\n\r\n")
            metadata = (
                f"{uid} (UID {uid} RFC822.SIZE {len(self.raw_message)} "
                'INTERNALDATE "04-Sep-2026 10:16:00 -0500")'.encode()
            )
            return "OK", [(metadata, headers + b"\r\n\r\n"), b")"]
        if "RFC822.SIZE" in query:
            return "OK", [f"{uid} (UID {uid} RFC822.SIZE {len(self.raw_message)})".encode()]
        if "BODY.PEEK[]" in query:
            return "OK", [(f"{uid} (UID {uid})".encode(), self.raw_message), b")"]
        raise AssertionError(query)

    def search(self, charset: str | None, *criteria: str) -> tuple[str, list[bytes]]:
        self.calls.append(("search", charset, *criteria))
        return "OK", [self.search_response]

    def fetch(self, sequence: str, query: str) -> tuple[str, list[bytes]]:
        self.calls.append(("fetch", sequence, query))
        selected: list[int] = []
        for item in sequence.split(","):
            if ":" in item:
                start, end = (int(value) for value in item.split(":", 1))
                selected.extend(range(start, end + 1))
            else:
                selected.append(int(item))
        return "OK", [
            f"{position} (UID {self.sequence_uids[position - 1]})".encode()
            for position in selected
            if 1 <= position <= len(self.sequence_uids)
        ]

    def logout(self) -> tuple[str, list[bytes]]:
        self.logged_out = True
        return "BYE", [b"logout"]


def factory(clients: list[FakeImap], **options: object):
    def create(
        _credentials: ImapCredentials,
        _context: ssl.SSLContext,
    ) -> FakeImap:
        client = FakeImap(**options)
        clients.append(client)
        return client

    return create


def connection(**updates: object) -> dict[str, object]:
    values: dict[str, object] = {
        "email_address": "OWNER@Example.com",
        "host": "mail.example.com",
        "port": 993,
        "security": "tls",
        "username": "owner@example.com",
        "password": "private-password",
    }
    values.update(updates)
    return values


def test_connection_validation_normalizes_identity_and_copies_ca(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ca_file = tmp_path / "private-ca.pem"
    ca_file.write_text("test trust root", encoding="utf-8")
    requested_ca: list[str | None] = []

    def context(*, cadata: str | None = None) -> ssl.SSLContext:
        requested_ca.append(cadata)
        return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    monkeypatch.setattr("eom_email_watcher.imap.ssl.create_default_context", context)

    result = credentials_from_connection(connection(ca_file=str(ca_file)))

    assert result.email_address == "owner@example.com"
    assert result.ca_pem == "test trust root"
    assert requested_ca == ["test trust root"]


def test_connection_validation_canonicalizes_idna_hostname() -> None:
    result = credentials_from_connection(connection(host="MÁIL.example."))

    assert result.host == "xn--mil-ela.example"


def test_connection_validation_canonicalizes_idna_mailbox_domain() -> None:
    result = credentials_from_connection(connection(email_address="owner@MÁIL.example"))

    assert result.email_address == "owner@xn--mil-ela.example"


def test_connection_validation_preserves_significant_username_whitespace() -> None:
    result = credentials_from_connection(connection(username=" owner@example.com "))

    assert result.username == " owner@example.com "


def test_login_quotes_username_as_an_imap_astring() -> None:
    clients: list[FakeImap] = []
    values = ImapCredentials(**{**credentials().__dict__, "username": 'owner name"\\account'})
    gateway = ImapGateway(values, factory(clients))

    gateway.initial_cursor()

    assert clients[0].calls[0] == (
        "login",
        '"owner name\\"\\\\account"',
        "private-password",
    )


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"host": "https://mail.example.com"}, "hostname"),
        ({"port": 0}, "between 1 and 65535"),
        ({"security": "plain"}, "TLS or STARTTLS"),
        ({"security": ["tls"]}, "TLS or STARTTLS"),
        ({"security": {"mode": "tls"}}, "TLS or STARTTLS"),
        ({"email_address": "not-an-email"}, "valid mailbox"),
        ({"username": "owñer@example.com"}, "valid mail server username"),
        ({"password": "pässword"}, "valid mail server password"),
        ({"password": ""}, "valid mail server password"),
        ({"unexpected": True}, "Unsupported"),
    ],
)
def test_connection_validation_rejects_unsafe_boundaries(
    updates: dict[str, object], message: str
) -> None:
    with pytest.raises(ImapError, match=message):
        credentials_from_connection(connection(**updates))


def test_private_credentials_round_trip_without_source_ca_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "credentials.json"
    values = ImapCredentials(**{**credentials().__dict__, "ca_pem": "private trust root"})
    monkeypatch.setattr(
        "eom_email_watcher.imap.ssl.create_default_context",
        lambda *, cadata=None: ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
    )

    write_credentials(path, values)

    assert load_credentials(path) == values
    assert path.stat().st_mode & 0o777 == 0o600
    assert "ca_file" not in path.read_text(encoding="utf-8")


def test_cursor_is_snapshotted_and_incremental_sequence_page_is_bounded() -> None:
    clients: list[FakeImap] = []
    new_uids = list(range(8, 8 + MAX_INCREMENTAL_MESSAGE_IDS + 1))
    gateway = ImapGateway(
        credentials(),
        factory(
            clients,
            uid_next=8 + MAX_INCREMENTAL_MESSAGE_IDS + 1,
            sequence_uids=[7, *new_uids],
        ),
    )

    changes = gateway.changes_since(cursor())

    assert len(changes.message_ids) == MAX_INCREMENTAL_MESSAGE_IDS
    assert changes.message_ids[0] == message_id(8)
    assert changes.cursor == cursor(7 + MAX_INCREMENTAL_MESSAGE_IDS)
    assert clients[0].readonly is True
    page_calls = [call for call in clients[0].calls if call[0] == "fetch" and "," in call[1]]
    assert len(page_calls) == 1
    assert len(page_calls[0][1].split(",")) == MAX_INCREMENTAL_MESSAGE_IDS
    assert not [call for call in clients[0].calls if call[:2] == ("uid", "SEARCH")]
    assert clients[0].logged_out is True


def test_uid_validity_change_requires_recovery() -> None:
    gateway = ImapGateway(credentials(), factory([], uid_validity=45))

    with pytest.raises(StaleMailboxCursor):
        gateway.changes_since(cursor())


def test_incremental_page_crosses_large_sparse_uid_gap_in_one_poll() -> None:
    clients: list[FakeImap] = []
    sparse_uid = 1_000_000_000
    gateway = ImapGateway(
        credentials(),
        factory(clients, uid_next=sparse_uid + 1, sequence_uids=[7, sparse_uid]),
    )

    changes = gateway.changes_since(cursor())

    assert changes.message_ids == (message_id(sparse_uid),)
    assert changes.cursor == cursor(sparse_uid)
    assert ("fetch", "2", "(UID)") in clients[0].calls
    assert not [call for call in clients[0].calls if call[:2] == ("uid", "SEARCH")]


def test_snapshot_falls_back_to_highest_uid_when_uidnext_is_absent() -> None:
    class MissingUidNext(FakeImap):
        def response(self, name: str) -> tuple[str, list[Any]]:
            if name == "UIDNEXT":
                return name, []
            return super().response(name)

    client = MissingUidNext()
    gateway = ImapGateway(credentials(), lambda _credentials, _context: client)

    assert gateway.initial_cursor() == cursor()
    assert ("fetch", "1", "(UID)") in client.calls


def test_snapshot_uidnext_fallback_handles_empty_mailbox_without_fetch() -> None:
    class EmptyMissingUidNext(FakeImap):
        def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
            super().select(mailbox, readonly)
            return "OK", [b"0"]

        def response(self, name: str) -> tuple[str, list[Any]]:
            if name == "UIDNEXT":
                return name, []
            return super().response(name)

    client = EmptyMissingUidNext()
    gateway = ImapGateway(credentials(), lambda _credentials, _context: client)

    assert gateway.initial_cursor() == cursor(0)
    assert not [call for call in client.calls if call[0] == "fetch"]


def test_snapshot_uidnext_fallback_rejects_missing_uid() -> None:
    class MissingUid(FakeImap):
        def response(self, name: str) -> tuple[str, list[Any]]:
            if name == "UIDNEXT":
                return name, []
            return super().response(name)

        def fetch(self, sequence: str, query: str) -> tuple[str, list[bytes]]:
            return "OK", [f"{sequence} ()".encode()]

    gateway = ImapGateway(credentials(), lambda _credentials, _context: MissingUid())

    with pytest.raises(ImapError, match="UID snapshot") as error:
        gateway.initial_cursor()

    assert error.value.code == "imap_protocol_error"


def test_metadata_uses_headers_only_and_content_uses_peek() -> None:
    clients: list[FakeImap] = []
    gateway = ImapGateway(credentials(), factory(clients))

    with gateway.polling_session():
        metadata = gateway.metadata(message_id())
        content = gateway.content(message_id(), 1000)

    assert metadata.sender == "watched@example.com"
    assert metadata.sender_name == "Sender Name"
    assert metadata.subject == "Invoice received"
    assert metadata.received_at == "2026-09-04T15:16:00+00:00"
    assert metadata.labels == frozenset({"INBOX"})
    assert content.body == "The invoice is attached."
    assert content.attachment_names == ("invoice.pdf",)
    assert content.attachments[0].part_id == "mime-0"
    assert content.attachments[0].byte_size == 7
    fetches = [call for client in clients for call in client.calls if call[:2] == ("uid", "FETCH")]
    assert len(clients) == 1
    assert clients[0].logged_out is True
    assert any("BODY.PEEK[HEADER.FIELDS" in str(call[-1]) for call in fetches)
    assert any(f"<0.{MAX_HEADER_BYTES}>" in str(call[-1]) for call in fetches)
    assert any(call[-1] == "(UID BODY.PEEK[])" for call in fetches)


def test_zone_less_internaldate_is_interpreted_as_utc() -> None:
    class ZoneLessInternalDate(FakeImap):
        def uid(self, command: str, *args: object) -> tuple[str, list[Any]]:
            status, response = super().uid(command, *args)
            if command == "FETCH" and "HEADER.FIELDS" in str(args[-1]):
                metadata, payload = response[0]
                response[0] = (metadata.replace(b"-0500", b"-0000"), payload)
            return status, response

    gateway = ImapGateway(credentials(), lambda _credentials, _context: ZoneLessInternalDate())

    assert gateway.metadata(message_id()).received_at == "2026-09-04T10:16:00+00:00"


def test_polling_session_reuses_consumed_uidvalidity_response() -> None:
    class ConsumingSelectResponses(FakeImap):
        def __init__(self) -> None:
            super().__init__()
            self.consumed: set[str] = set()

        def response(self, name: str) -> tuple[str, list[bytes]]:
            if name in self.consumed:
                return name, []
            self.consumed.add(name)
            return super().response(name)

    client = ConsumingSelectResponses()
    gateway = ImapGateway(credentials(), lambda _credentials, _context: client)

    with gateway.polling_session():
        changes = gateway.changes_since(cursor(6))
        gateway.metadata(changes.message_ids[0])
        gateway.content(changes.message_ids[0], 1000)

    assert client.consumed == {"UIDVALIDITY", "UIDNEXT"}


def test_message_identity_rejects_stale_uidvalidity_before_fetch() -> None:
    client = FakeImap(uid_validity=45)
    gateway = ImapGateway(credentials(), lambda _credentials, _context: client)

    with pytest.raises(MailboxMessageUnavailable):
        gateway.metadata(message_id(uid_validity=44))

    assert not [call for call in client.calls if call[:2] == ("uid", "FETCH")]


def test_mailbox_binding_rejects_old_cursor_and_message_identity() -> None:
    changed = ImapCredentials(**{**credentials().__dict__, "host": "replacement.example.com"})
    clients: list[FakeImap] = []
    gateway = ImapGateway(changed, factory(clients))

    with pytest.raises(StaleMailboxCursor):
        gateway.changes_since(cursor())
    with pytest.raises(MailboxMessageUnavailable):
        gateway.metadata(message_id())

    assert len(clients) == 1
    assert not [call for call in clients[0].calls if call[:2] == ("uid", "FETCH")]


def test_attachment_bytes_reuses_stable_mime_position() -> None:
    gateway = ImapGateway(credentials(), factory([]))

    assert gateway.attachment_bytes(message_id(), "mime-0", None) == b"PDFDATA"


def test_forwarded_message_is_an_attachment_not_outer_body() -> None:
    gateway = ImapGateway(credentials(), factory([], raw_message=FORWARDED_MESSAGE))

    content = gateway.content(message_id(), 1000)

    assert content.body == "Outer body only."
    assert content.attachment_names == ("forwarded.eml",)
    assert content.attachments[0].media_type == "message/rfc822"
    forwarded = gateway.attachment_bytes(message_id(), "mime-0", None)
    assert b"Private forwarded content" in forwarded
    assert b"Inner body must not join the outer body" in forwarded


def test_filename_less_attachment_dispositions_never_join_outer_body() -> None:
    gateway = ImapGateway(credentials(), factory([], raw_message=FILENAMELESS_ATTACHMENTS))

    content = gateway.content(message_id(), 1000)

    assert content.body == "Outer body only."
    assert content.attachment_names == ("attachment-1.eml", "attachment-2.txt")
    assert b"Embedded private body" in gateway.attachment_bytes(message_id(), "mime-0", None)
    assert b"Private text attachment" in gateway.attachment_bytes(message_id(), "mime-1", None)


def test_root_attachment_disposition_never_becomes_analysis_body() -> None:
    message = EmailMessage()
    message.set_content("Private attachment body")
    message["Content-Disposition"] = "attachment"

    content = _content(message, 1000)

    assert content.body == ""
    assert content.attachment_names == ("attachment-1.txt",)


def test_multipart_attachment_export_preserves_wrapper_and_boundaries() -> None:
    message = EmailMessage()
    message.make_mixed()
    attachment = EmailMessage()
    attachment.set_content("Plain alternative")
    attachment.add_alternative("<p>HTML alternative</p>", subtype="html")
    attachment["Content-Disposition"] = "attachment"
    message.attach(attachment)

    content, payloads = _content_and_attachment_payloads(message, 1000)
    exported = BytesParser(policy=policy.default).parsebytes(payloads[0])

    assert content.body == ""
    assert exported.get_content_type() == "multipart/alternative"
    assert exported.get_boundary()
    assert len(exported.get_payload()) == 2


def test_attachment_container_descendants_obey_mime_depth_limit() -> None:
    allowed = _nested_message(MAX_MIME_DEPTH)
    allowed["Content-Disposition"] = "attachment"

    assert len(_content(allowed, 1000).attachments) == 1

    excessive = _nested_message(MAX_MIME_DEPTH + 1)
    excessive["Content-Disposition"] = "attachment"

    with pytest.raises(MailboxMessageInvalid) as raised:
        _content(excessive, 1000)

    assert raised.value.code == "imap_mime_too_complex"


def test_synthesized_attachment_names_use_only_safe_known_suffixes() -> None:
    assert _synthesized_attachment_name(0, "application/pdf") == "attachment-1.pdf"
    assert _synthesized_attachment_name(0, "application/x-unregistered") == "attachment-1"


def test_html_body_uses_structured_text_extraction() -> None:
    message = EmailMessage()
    message.set_content('<p title="x > y">Visible x &gt; y</p>', subtype="html")

    assert _content(message, 1000).body == "Visible x > y"


def _message_with_attachment_names(names: list[str]) -> EmailMessage:
    message = EmailMessage()
    message.make_mixed()
    for name in names:
        part = EmailMessage()
        part.set_content("attachment")
        part.add_header("Content-Disposition", "attachment", filename=name)
        message.attach(part)
    return message


def test_attachment_filename_metadata_is_bounded_per_name_and_in_total() -> None:
    maximum_name = "a" * MAX_ATTACHMENT_FILENAME_BYTES
    allowed_count = MAX_ATTACHMENT_FILENAME_TOTAL_BYTES // MAX_ATTACHMENT_FILENAME_BYTES

    assert len(_content(_message_with_attachment_names([maximum_name]), 1000).attachments) == 1
    assert (
        len(
            _content(
                _message_with_attachment_names([maximum_name] * allowed_count),
                1000,
            ).attachments
        )
        == allowed_count
    )
    with pytest.raises(MailboxMessageInvalid) as per_name:
        _content(_message_with_attachment_names([maximum_name + "b"]), 1000)
    with pytest.raises(MailboxMessageInvalid) as cumulative:
        _content(
            _message_with_attachment_names([maximum_name] * (allowed_count + 1)),
            1000,
        )

    assert per_name.value.code == "imap_attachment_metadata_too_large"
    assert cumulative.value.code == "imap_attachment_metadata_too_large"


def _nested_message(depth: int) -> EmailMessage:
    message = EmailMessage()
    message.set_content("Safe body")
    for _index in range(depth):
        parent = EmailMessage()
        parent.make_mixed()
        parent.attach(message)
        message = parent
    return message


def test_mime_depth_limit_accepts_maximum_and_rejects_next_level() -> None:
    assert _content(_nested_message(MAX_MIME_DEPTH), 1000).body == "Safe body"

    with pytest.raises(MailboxMessageInvalid) as raised:
        _content(_nested_message(MAX_MIME_DEPTH + 1), 1000)

    assert raised.value.code == "imap_mime_too_complex"


def test_mime_part_limit_accepts_maximum_and_rejects_next_part() -> None:
    message = EmailMessage()
    message.make_mixed()
    for _index in range(MAX_MIME_PARTS - 1):
        part = EmailMessage()
        part.set_content("Safe body")
        message.attach(part)

    assert _content(message, 1000).body
    extra = EmailMessage()
    extra.set_content("One part too many")
    message.attach(extra)

    with pytest.raises(MailboxMessageInvalid) as raised:
        _content(message, 1000)

    assert raised.value.code == "imap_mime_too_complex"


def test_attachment_container_descendants_obey_mime_part_limit() -> None:
    message = EmailMessage()
    message.make_mixed()
    message["Content-Disposition"] = "attachment"
    for _index in range(MAX_MIME_PARTS - 1):
        part = EmailMessage()
        part.set_content("Attachment content")
        message.attach(part)

    assert len(_content(message, 1000).attachments) == 1

    extra = EmailMessage()
    extra.set_content("One part too many")
    message.attach(extra)

    with pytest.raises(MailboxMessageInvalid) as raised:
        _content(message, 1000)

    assert raised.value.code == "imap_mime_too_complex"


def test_parser_recursion_is_a_nonretryable_message_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecursiveParser:
        def __init__(self, **_options: object) -> None:
            pass

        def parsebytes(self, _payload: bytes) -> EmailMessage:
            raise RecursionError("private parser detail")

    monkeypatch.setattr("eom_email_watcher.imap.BytesParser", RecursiveParser)
    gateway = ImapGateway(credentials(), factory([]))

    with pytest.raises(MailboxMessageInvalid) as raised:
        gateway.content(message_id(), 1000)

    assert raised.value.code == "imap_mime_too_complex"
    assert "private parser detail" not in str(raised.value)


def test_recursive_attachment_header_is_a_nonretryable_message_failure() -> None:
    message = EmailMessage()
    message.make_mixed()
    part = EmailMessage()
    part.set_content("Attachment body")

    def recursive_filename() -> str:
        raise RecursionError("private header detail")

    part.get_filename = recursive_filename  # type: ignore[method-assign]
    message.attach(part)

    with pytest.raises(MailboxMessageInvalid) as raised:
        _content(message, 1000)

    assert raised.value.code == "imap_mime_too_complex"
    assert "private header detail" not in str(raised.value)


def test_recovery_uses_previous_utc_day_with_protocol_month_name() -> None:
    clients: list[FakeImap] = []
    gateway = ImapGateway(credentials(), factory(clients))

    gateway.recover_since(frozenset(), datetime(2026, 9, 4, 0, 5, tzinfo=UTC))

    assert ("search", None, "1:1", "SINCE", "03-Sep-2026") in clients[0].calls


def test_recovery_crosses_sparse_uid_gap_by_message_sequence() -> None:
    sparse_uid = 1_000_000_000
    gateway = ImapGateway(
        credentials(),
        factory([], uid_next=sparse_uid + 1, sequence_uids=[sparse_uid]),
    )

    changes = gateway.recover_since(frozenset(), datetime(2026, 9, 4, 0, 5, tzinfo=UTC))

    assert changes.message_ids == (message_id(sparse_uid),)
    assert changes.cursor == cursor(sparse_uid)


@pytest.mark.parametrize("status", ["NO", "BAD"])
def test_fetch_rejection_is_retryable_protocol_failure(status: str) -> None:
    class RejectedFetch(FakeImap):
        def uid(self, command: str, *args: object) -> tuple[str, list[Any]]:
            if command == "FETCH":
                return status, [b"private server detail"]
            return super().uid(command, *args)

    gateway = ImapGateway(credentials(), lambda _credentials, _context: RejectedFetch())

    with pytest.raises(ImapError) as raised:
        gateway.metadata(message_id())

    assert raised.value.code == "imap_protocol_error"
    assert "private server detail" not in str(raised.value)


@pytest.mark.parametrize("rejected_query", ["RFC822.SIZE", "BODY.PEEK[]"])
def test_content_fetch_rejection_is_retryable_protocol_failure(rejected_query: str) -> None:
    class RejectedFetch(FakeImap):
        def uid(self, command: str, *args: object) -> tuple[str, list[Any]]:
            if command == "FETCH" and rejected_query in str(args[-1]):
                return "NO", [b"private server detail"]
            return super().uid(command, *args)

    gateway = ImapGateway(credentials(), lambda _credentials, _context: RejectedFetch())

    with pytest.raises(ImapError) as raised:
        gateway.content(message_id(), 1000)

    assert raised.value.code == "imap_protocol_error"
    assert "private server detail" not in str(raised.value)


def test_ok_fetch_without_the_uid_is_message_unavailable() -> None:
    class MissingFetch(FakeImap):
        def uid(self, command: str, *args: object) -> tuple[str, list[Any]]:
            if command == "FETCH":
                return "OK", []
            return super().uid(command, *args)

    gateway = ImapGateway(credentials(), lambda _credentials, _context: MissingFetch())

    with pytest.raises(MailboxMessageUnavailable):
        gateway.metadata(message_id())


def test_bounded_header_fetch_rejects_truncated_admission_headers() -> None:
    class OversizedHeaders(FakeImap):
        def uid(self, command: str, *args: object) -> tuple[str, list[Any]]:
            query = str(args[-1])
            if command == "FETCH" and "HEADER.FIELDS" in query:
                return "OK", [
                    (
                        b'7 (UID 7 RFC822.SIZE 65536 INTERNALDATE "04-Sep-2026 10:16:00 -0500")',
                        b"X" * MAX_HEADER_BYTES,
                    ),
                    b")",
                ]
            return super().uid(command, *args)

    gateway = ImapGateway(credentials(), lambda _credentials, _context: OversizedHeaders())

    with pytest.raises(MailboxMessageInvalid) as raised:
        gateway.metadata(message_id())

    assert raised.value.code == "imap_headers_too_large"


def test_recursive_sender_header_is_permanently_unsafe_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecursiveHeader:
        def __str__(self) -> str:
            raise RecursionError("private header detail")

    class ParsedHeaders:
        def get(self, name: str, default: str = "") -> object:
            return RecursiveHeader() if name == "From" else default

    class RecursiveHeaderParser:
        def __init__(self, **_options: object) -> None:
            pass

        def parsebytes(self, _payload: bytes, *, headersonly: bool = False) -> ParsedHeaders:
            assert headersonly is True
            return ParsedHeaders()

    monkeypatch.setattr("eom_email_watcher.imap.BytesParser", RecursiveHeaderParser)
    gateway = ImapGateway(credentials(), factory([]))

    with pytest.raises(MailboxMessageInvalid) as raised:
        gateway.metadata(message_id())

    assert raised.value.code == "imap_headers_too_complex"
    assert "private header detail" not in str(raised.value)


def test_oversized_message_is_a_permanent_message_failure() -> None:
    class Oversized(FakeImap):
        def uid(self, command: str, *args: object) -> tuple[str, list[Any]]:
            query = str(args[-1])
            if command == "FETCH" and "RFC822.SIZE" in query:
                uid = str(args[0])
                return "OK", [f"{uid} (UID {uid} RFC822.SIZE {MAX_MESSAGE_BYTES + 1})".encode()]
            return super().uid(command, *args)

    gateway = ImapGateway(
        credentials(),
        lambda _credentials, _context: Oversized(),
    )

    assert gateway.metadata(message_id()).sender == "watched@example.com"
    with pytest.raises(MailboxMessageInvalid) as raised:
        gateway.content(message_id(), 1000)

    assert raised.value.code == "imap_message_too_large"


def test_login_rejection_is_categorized_and_connection_is_closed() -> None:
    client = FakeImap()

    def reject(_username: str, _password: str) -> tuple[str, list[bytes]]:
        raise imaplib.IMAP4.error("private server detail")

    client.login = reject  # type: ignore[method-assign]
    gateway = ImapGateway(credentials(), lambda _credentials, _context: client)

    with pytest.raises(ImapError) as raised:
        gateway.initial_cursor()

    assert raised.value.code == "imap_authentication_failed"
    assert "private server detail" not in str(raised.value)
    assert client.logged_out is True


def test_login_abort_is_a_retryable_connection_failure() -> None:
    client = FakeImap()

    def abort(_username: str, _password: str) -> tuple[str, list[bytes]]:
        raise imaplib.IMAP4.abort("private disconnect detail")

    client.login = abort  # type: ignore[method-assign]
    gateway = ImapGateway(credentials(), lambda _credentials, _context: client)

    with pytest.raises(ImapError) as raised:
        gateway.initial_cursor()

    assert raised.value.code == "imap_connection_failed"
    assert "private disconnect detail" not in str(raised.value)
    assert client.logged_out is True


@pytest.mark.parametrize(
    ("exception", "code"),
    [
        (imaplib.IMAP4.abort("private greeting detail"), "imap_connection_failed"),
        (imaplib.IMAP4.error("private greeting detail"), "imap_protocol_error"),
    ],
)
def test_greeting_failure_is_categorized_without_server_detail(
    exception: Exception, code: str
) -> None:
    def reject_greeting(_credentials: ImapCredentials, _context: ssl.SSLContext) -> FakeImap:
        raise exception

    gateway = ImapGateway(credentials(), reject_greeting)

    with pytest.raises(ImapError) as raised:
        gateway.initial_cursor()

    assert raised.value.code == code
    assert "private greeting detail" not in str(raised.value)


def test_post_login_protocol_error_is_categorized_without_server_detail() -> None:
    client = FakeImap()

    def fail_fetch(sequence: str, query: str) -> tuple[str, list[bytes]]:
        raise imaplib.IMAP4.error("private protocol detail")

    client.fetch = fail_fetch  # type: ignore[method-assign]
    gateway = ImapGateway(credentials(), lambda _credentials, _context: client)

    with pytest.raises(ImapError) as raised:
        gateway.changes_since(cursor(6))

    assert raised.value.code == "imap_protocol_error"
    assert "private protocol detail" not in str(raised.value)
    assert client.logged_out is True


def test_inbox_selection_error_is_protocol_not_authentication() -> None:
    client = FakeImap()

    def reject_select(mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        raise imaplib.IMAP4.error("private selection detail")

    client.select = reject_select  # type: ignore[method-assign]
    gateway = ImapGateway(credentials(), lambda _credentials, _context: client)

    with pytest.raises(ImapError) as raised:
        gateway.initial_cursor()

    assert raised.value.code == "imap_protocol_error"
    assert "private selection detail" not in str(raised.value)
    assert client.logged_out is True


def test_tls_failure_is_categorized_without_endpoint_detail() -> None:
    def reject_tls(_credentials: ImapCredentials, _context: ssl.SSLContext) -> FakeImap:
        raise ssl.SSLCertVerificationError("private endpoint detail")

    gateway = ImapGateway(credentials(), reject_tls)

    with pytest.raises(ImapError) as raised:
        gateway.initial_cursor()

    assert raised.value.code == "imap_tls_failed"
    assert "private endpoint detail" not in str(raised.value)
