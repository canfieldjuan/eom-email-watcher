from __future__ import annotations

import imaplib
import ssl
from pathlib import Path
from typing import Any

import pytest

from eom_email_watcher.imap import (
    MAX_INCREMENTAL_MESSAGE_IDS,
    MAX_MESSAGE_BYTES,
    ImapCredentials,
    ImapError,
    ImapGateway,
    credentials_from_connection,
    load_credentials,
    write_credentials,
)
from eom_email_watcher.mailbox import MailboxMessageInvalid, StaleMailboxCursor

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


def credentials() -> ImapCredentials:
    return ImapCredentials(
        email_address="owner@example.com",
        host="mail.example.com",
        port=993,
        security="tls",
        username="owner@example.com",
        password="private-password",
    )


class FakeImap:
    def __init__(
        self,
        *,
        uid_validity: int = 44,
        uid_next: int = 8,
        search: bytes = b"7",
        raw_message: bytes = RAW_MESSAGE,
    ) -> None:
        self.uid_validity = uid_validity
        self.uid_next = uid_next
        self.search = search
        self.raw_message = raw_message
        self.calls: list[tuple[Any, ...]] = []
        self.readonly: bool | None = None
        self.logged_out = False

    def login(self, username: str, password: str) -> tuple[str, list[bytes]]:
        self.calls.append(("login", username, password))
        return "OK", [b"authenticated"]

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.calls.append(("select", mailbox, readonly))
        self.readonly = readonly
        return "OK", [b"1"]

    def response(self, name: str) -> tuple[str, list[bytes]]:
        value = self.uid_validity if name == "UIDVALIDITY" else self.uid_next
        return name, [str(value).encode("ascii")]

    def uid(self, command: str, *args: object) -> tuple[str, list[Any]]:
        self.calls.append(("uid", command, *args))
        if command == "SEARCH":
            return "OK", [self.search]
        query = str(args[-1])
        uid = str(args[0])
        if "HEADER.FIELDS" in query:
            headers, _separator, _body = self.raw_message.partition(b"\r\n\r\n")
            metadata = (
                f'{uid} (UID {uid} INTERNALDATE "04-Sep-2026 10:16:00 -0500")'.encode()
            )
            return "OK", [(metadata, headers + b"\r\n\r\n"), b")"]
        if "RFC822.SIZE" in query:
            return "OK", [f"{uid} (UID {uid} RFC822.SIZE {len(self.raw_message)})".encode()]
        if "BODY.PEEK[]" in query:
            return "OK", [(f"{uid} (UID {uid})".encode(), self.raw_message), b")"]
        raise AssertionError(query)

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


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"host": "https://mail.example.com"}, "hostname"),
        ({"port": 0}, "between 1 and 65535"),
        ({"security": "plain"}, "TLS or STARTTLS"),
        ({"email_address": "not-an-email"}, "valid mailbox"),
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


def test_cursor_is_snapshotted_and_incremental_search_is_bounded() -> None:
    clients: list[FakeImap] = []
    search = b" ".join(str(uid).encode() for uid in range(8, 8 + MAX_INCREMENTAL_MESSAGE_IDS + 1))
    gateway = ImapGateway(
        credentials(),
        factory(
            clients,
            uid_next=8 + MAX_INCREMENTAL_MESSAGE_IDS + 1,
            search=search,
        ),
    )

    changes = gateway.changes_since("eom-imap-v1:44:7")

    assert len(changes.message_ids) == MAX_INCREMENTAL_MESSAGE_IDS
    assert changes.message_ids[0] == "8"
    assert changes.cursor == f"eom-imap-v1:44:{7 + MAX_INCREMENTAL_MESSAGE_IDS}"
    assert clients[0].readonly is True
    assert ("uid", "SEARCH", None, f"UID 8:{8 + MAX_INCREMENTAL_MESSAGE_IDS}") in clients[
        0
    ].calls
    assert clients[0].logged_out is True


def test_uid_validity_change_requires_recovery() -> None:
    gateway = ImapGateway(credentials(), factory([], uid_validity=45))

    with pytest.raises(StaleMailboxCursor):
        gateway.changes_since("eom-imap-v1:44:7")


def test_metadata_uses_headers_only_and_content_uses_peek() -> None:
    clients: list[FakeImap] = []
    gateway = ImapGateway(credentials(), factory(clients))

    metadata = gateway.metadata("7")
    content = gateway.content("7", 1000)

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
    assert any("BODY.PEEK[HEADER.FIELDS" in str(call[-1]) for call in fetches)
    assert any(call[-1] == "(UID BODY.PEEK[])" for call in fetches)
    assert all("RFC822" not in str(call[-1]) or call[-1] == "(UID RFC822.SIZE)" for call in fetches)


def test_attachment_bytes_reuses_stable_mime_position() -> None:
    gateway = ImapGateway(credentials(), factory([]))

    assert gateway.attachment_bytes("7", "mime-0", None) == b"PDFDATA"


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

    with pytest.raises(MailboxMessageInvalid) as raised:
        gateway.content("7", 1000)

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


def test_post_login_protocol_error_is_categorized_without_server_detail() -> None:
    client = FakeImap()

    def fail_uid(command: str, *args: object) -> tuple[str, list[Any]]:
        raise imaplib.IMAP4.error("private protocol detail")

    client.uid = fail_uid  # type: ignore[method-assign]
    gateway = ImapGateway(credentials(), lambda _credentials, _context: client)

    with pytest.raises(ImapError) as raised:
        gateway.changes_since("eom-imap-v1:44:6")

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
