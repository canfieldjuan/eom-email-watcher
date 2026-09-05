from __future__ import annotations

import contextlib
import hashlib
import imaplib
import json
import mimetypes
import re
import ssl
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Literal

from .config import normalize_address
from .mailbox import (
    MailboxChanges,
    MailboxError,
    MailboxMessageInvalid,
    MailboxMessageUnavailable,
    MessageContent,
    MessageMetadata,
    StaleMailboxCursor,
)
from .mime import AttachmentDescriptor

IMAP_PROVIDER = "imap"
IMAP_CONNECTION_METHOD = "server_credentials"
IMAP_TIMEOUT_SECONDS = 30.0
MAX_CA_FILE_BYTES = 256 * 1024
MAX_CREDENTIAL_FILE_BYTES = MAX_CA_FILE_BYTES + 16 * 1024
MAX_MESSAGE_BYTES = 50 * 1024 * 1024
MAX_HEADER_BYTES = 64 * 1024
MAX_MIME_DEPTH = 100
MAX_MIME_PARTS = 1000
MAX_INCREMENTAL_MESSAGE_IDS = 200
MAX_UID_SEARCH_SPAN = 10_000
CURSOR_PREFIX = "eom-imap-v2:"
MESSAGE_ID_PREFIX = "eom-imap-message-v2:"
_DOMAIN_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_UID = re.compile(r"[1-9][0-9]*\Z")
_MAILBOX_ID = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_ATTACHMENT_SUFFIX = re.compile(r"\.[A-Za-z0-9]{1,12}\Z")
_INTERNAL_DATE = re.compile(rb'INTERNALDATE "([^"]+)"', re.IGNORECASE)
_RFC822_SIZE = re.compile(rb"RFC822\.SIZE ([0-9]+)", re.IGNORECASE)
_FETCH_UID = re.compile(rb"(?:^|[ (])UID ([1-9][0-9]*)(?:[ )]|$)", re.IGNORECASE)
_ENGLISH_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


class ImapError(MailboxError):
    """A safe, categorized IMAP operation failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ImapCredentials:
    email_address: str
    host: str
    port: int
    security: Literal["tls", "starttls"]
    username: str
    password: str
    ca_pem: str | None = None


def _valid_host(host: str) -> bool:
    try:
        ip_address(host)
    except ValueError:
        try:
            encoded = host.encode("idna").decode("ascii")
        except UnicodeError:
            return False
        return len(encoded) <= 253 and all(
            _DOMAIN_LABEL.fullmatch(label) for label in encoded.split(".")
        )
    return True


def _valid_email(value: str) -> str:
    address = normalize_address(value)
    local, separator, domain = address.rpartition("@")
    if (
        separator != "@"
        or not local
        or not domain
        or "@" in local
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
        or not _valid_host(domain)
        or any(character.isspace() or not character.isprintable() for character in address)
    ):
        raise ImapError("imap_configuration_error", "Enter a valid mailbox email address")
    return address


def credentials_from_connection(value: object) -> ImapCredentials:
    """Validate a desktop request and copy any selected CA into private credentials."""
    if not isinstance(value, dict):
        raise ImapError("imap_configuration_error", "Enter the mail server connection details")
    allowed = {
        "email_address",
        "host",
        "port",
        "security",
        "username",
        "password",
        "ca_file",
    }
    if set(value) - allowed:
        raise ImapError("imap_configuration_error", "Unsupported mail server setting")

    raw_host = value.get("host")
    host = raw_host.strip().rstrip(".") if isinstance(raw_host, str) else ""
    if (
        not host
        or not _valid_host(host)
        or any(character.isspace() or not character.isprintable() for character in host)
    ):
        raise ImapError("imap_configuration_error", "Enter a valid mail server hostname")

    port = value.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65_535:
        raise ImapError("imap_configuration_error", "Mail server port must be between 1 and 65535")

    security = value.get("security")
    if security not in {"tls", "starttls"}:
        raise ImapError("imap_configuration_error", "Choose TLS or STARTTLS security")

    username_value = value.get("username")
    username = username_value.strip() if isinstance(username_value, str) else ""
    if (
        not username
        or len(username) > 320
        or not username.isascii()
        or any(not character.isprintable() for character in username)
    ):
        raise ImapError("imap_configuration_error", "Enter a valid mail server username")

    password = value.get("password")
    if (
        not isinstance(password, str)
        or not password
        or len(password) > 4096
        or not password.isascii()
        or any(not character.isprintable() for character in password)
    ):
        raise ImapError("imap_configuration_error", "Enter a valid mail server password")

    ca_pem: str | None = None
    ca_file = value.get("ca_file")
    if ca_file is not None:
        if not isinstance(ca_file, str) or not ca_file.strip():
            raise ImapError("imap_configuration_error", "Selected CA file is invalid")
        source = Path(ca_file).expanduser()
        if not source.is_absolute() or not source.is_file():
            raise ImapError("imap_configuration_error", "Selected CA file is unavailable")
        try:
            if source.stat().st_size > MAX_CA_FILE_BYTES:
                raise ImapError("imap_configuration_error", "Selected CA file is too large")
            ca_pem = source.read_text(encoding="utf-8")
            ssl.create_default_context(cadata=ca_pem)
        except ImapError:
            raise
        except (OSError, UnicodeError, ssl.SSLError) as exc:
            raise ImapError(
                "imap_configuration_error", "Selected CA file is not valid PEM"
            ) from exc

    return ImapCredentials(
        email_address=_valid_email(str(value.get("email_address", ""))),
        host=host,
        port=port,
        security=security,
        username=username,
        password=password,
        ca_pem=ca_pem,
    )


def write_credentials(path: Path, credentials: ImapCredentials) -> None:
    path.write_text(json.dumps(asdict(credentials), sort_keys=True), encoding="utf-8")
    path.chmod(0o600)


def load_credentials(path: Path) -> ImapCredentials:
    try:
        if path.stat().st_size > MAX_CREDENTIAL_FILE_BYTES:
            raise ImapError("imap_configuration_error", "Saved mail server credentials are invalid")
        value = json.loads(path.read_text(encoding="utf-8"))
    except ImapError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ImapError(
            "imap_configuration_error", "Saved mail server credentials are invalid"
        ) from exc
    if not isinstance(value, dict) or set(value) - {
        "email_address",
        "host",
        "port",
        "security",
        "username",
        "password",
        "ca_pem",
    }:
        raise ImapError("imap_configuration_error", "Saved mail server credentials are invalid")
    connection = dict(value)
    ca_pem = connection.pop("ca_pem", None)
    credentials = credentials_from_connection(connection)
    if ca_pem is not None:
        if not isinstance(ca_pem, str) or len(ca_pem.encode("utf-8")) > MAX_CA_FILE_BYTES:
            raise ImapError("imap_configuration_error", "Saved mail server credentials are invalid")
        try:
            ssl.create_default_context(cadata=ca_pem)
        except ssl.SSLError as exc:
            raise ImapError(
                "imap_configuration_error", "Saved mail server credentials are invalid"
            ) from exc
        credentials = ImapCredentials(**{**asdict(credentials), "ca_pem": ca_pem})
    return credentials


def imap_mailbox_identity(credentials: ImapCredentials) -> str:
    """Return a non-secret stable identity for one configured server mailbox."""
    value = json.dumps(
        [
            credentials.email_address,
            credentials.host.casefold(),
            credentials.port,
            credentials.security,
            credentials.username,
        ],
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(value).hexdigest()


def _cursor(mailbox_id: str, uid_validity: int, last_uid: int) -> str:
    return f"{CURSOR_PREFIX}{mailbox_id}:{uid_validity}:{last_uid}"


def _message_id(mailbox_id: str, uid_validity: int, uid: int) -> str:
    return f"{MESSAGE_ID_PREFIX}{mailbox_id}:{uid_validity}:{uid}"


def _decode_cursor(value: str) -> tuple[str, int, int]:
    encoded = value.removeprefix(CURSOR_PREFIX) if value.startswith(CURSOR_PREFIX) else ""
    mailbox_id, separator, remainder = encoded.partition(":")
    validity, uid_separator, uid = remainder.partition(":")
    if (
        not separator
        or not uid_separator
        or _MAILBOX_ID.fullmatch(mailbox_id) is None
        or not validity.isdecimal()
        or not uid.isdecimal()
    ):
        raise ImapError("imap_cursor_invalid", "Saved mail server cursor is invalid")
    parsed = mailbox_id, int(validity), int(uid)
    if parsed[1] <= 0 or parsed[2] < 0:
        raise ImapError("imap_cursor_invalid", "Saved mail server cursor is invalid")
    return parsed


def _decode_message_id(value: str) -> tuple[str, int, int]:
    encoded = value.removeprefix(MESSAGE_ID_PREFIX) if value.startswith(MESSAGE_ID_PREFIX) else ""
    mailbox_id, separator, remainder = encoded.partition(":")
    validity, uid_separator, uid = remainder.partition(":")
    if (
        not separator
        or not uid_separator
        or _MAILBOX_ID.fullmatch(mailbox_id) is None
        or not validity.isdecimal()
        or _UID.fullmatch(uid) is None
    ):
        raise ImapError("imap_protocol_error", "Mail server message identity is invalid")
    parsed = mailbox_id, int(validity), int(uid)
    if parsed[1] <= 0:
        raise ImapError("imap_protocol_error", "Mail server message identity is invalid")
    return parsed


def _synthesized_attachment_name(position: int, media_type: str) -> str:
    suffix = mimetypes.guess_extension(media_type, strict=False) or ""
    if _SAFE_ATTACHMENT_SUFFIX.fullmatch(suffix) is None:
        suffix = ""
    return f"attachment-{position + 1}{suffix.casefold()}"


def _response_number(client: imaplib.IMAP4, name: str) -> int:
    parsed = _optional_response_number(client, name)
    if parsed is None:
        raise ImapError("imap_protocol_error", "Mail server omitted required mailbox state")
    return parsed


def _optional_response_number(client: imaplib.IMAP4, name: str) -> int | None:
    _status, values = client.response(name)
    value = values[0] if values else None
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ImapError(
            "imap_protocol_error", "Mail server omitted required mailbox state"
        ) from exc
    if parsed <= 0:
        raise ImapError("imap_protocol_error", "Mail server omitted required mailbox state")
    return parsed


def _selected_uid_validity(client: imaplib.IMAP4) -> int:
    cached = getattr(client, "_eom_uid_validity", None)
    if isinstance(cached, int) and cached > 0:
        return cached
    value = _response_number(client, "UIDVALIDITY")
    client._eom_uid_validity = value  # type: ignore[attr-defined]
    return value


def _selected_message_count(client: imaplib.IMAP4) -> int:
    cached = getattr(client, "_eom_message_count", None)
    if isinstance(cached, int) and cached >= 0:
        return cached
    raise ImapError("imap_protocol_error", "Mail server omitted required mailbox state")


def _uids(response: list[Any] | None) -> list[int]:
    raw = response[0] if response else b""
    if not isinstance(raw, bytes):
        raise ImapError("imap_protocol_error", "Mail server returned an invalid message list")
    values: list[int] = []
    for item in raw.split():
        decoded = item.decode("ascii", errors="ignore")
        if _UID.fullmatch(decoded) is None:
            raise ImapError("imap_protocol_error", "Mail server returned an invalid message list")
        values.append(int(decoded))
    return sorted(set(values))


def _literal(response: list[Any] | None) -> tuple[bytes, bytes]:
    for item in response or []:
        if isinstance(item, tuple) and len(item) == 2:
            metadata, payload = item
            if isinstance(metadata, bytes) and isinstance(payload, bytes):
                return metadata, payload
    raise MailboxMessageUnavailable("The mail server message is no longer available")


def _response_metadata(response: list[Any] | None) -> bytes:
    for item in response or []:
        if isinstance(item, tuple) and item and isinstance(item[0], bytes):
            return item[0]
        if isinstance(item, bytes):
            return item
    raise MailboxMessageUnavailable("The mail server message is no longer available")


def _message_date(metadata: bytes, message: Message) -> str:
    match = _INTERNAL_DATE.search(metadata)
    if match is not None:
        try:
            return parsedate_to_datetime(match.group(1).decode("ascii")).astimezone(UTC).isoformat()
        except (OverflowError, UnicodeError, ValueError):
            pass
    try:
        parsed = parsedate_to_datetime(str(message.get("Date", "")))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat()
    except (OverflowError, TypeError, ValueError):
        return ""


def _part_text(part: Message) -> str:
    try:
        content = part.get_content()
    except (LookupError, UnicodeError, ValueError):
        payload = part.get_payload(decode=True)
        return payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else ""
    return content if isinstance(content, str) else ""


def _attachment_payload(part: Message) -> bytes:
    decoded = part.get_payload(decode=True)
    if isinstance(decoded, bytes):
        return decoded
    nested = part.get_payload()
    if isinstance(nested, list):
        try:
            return b"\r\n".join(item.as_bytes(policy=policy.default) for item in nested)
        except RecursionError as exc:
            raise MailboxMessageInvalid(
                "imap_mime_too_complex", "Message MIME structure exceeds the safe limit"
            ) from exc
    return b""


def _content_and_attachment_payloads(
    message: EmailMessage, body_char_limit: int
) -> tuple[MessageContent, tuple[bytes, ...]]:
    plain: list[str] = []
    html: list[str] = []
    attachments: list[AttachmentDescriptor] = []
    attachment_payloads: list[bytes] = []

    try:
        pending: list[tuple[Message, int, bool]] = [(message, 0, True)]
        visited = 0
        while pending:
            part, depth, root = pending.pop()
            visited += 1
            if visited > MAX_MIME_PARTS or depth > MAX_MIME_DEPTH:
                raise MailboxMessageInvalid(
                    "imap_mime_too_complex", "Message MIME structure exceeds the safe limit"
                )
            filename = part.get_filename()
            attachment_disposition = part.get_content_disposition() == "attachment"
            if filename or attachment_disposition:
                payload = _attachment_payload(part)
                position = len(attachments)
                media_type = part.get_content_type().casefold()
                attachment_payloads.append(payload)
                attachments.append(
                    AttachmentDescriptor(
                        part_id=f"mime-{position}",
                        attachment_id=None,
                        filename=filename or _synthesized_attachment_name(position, media_type),
                        media_type=media_type,
                        byte_size=len(payload),
                        position=position,
                    )
                )
                continue
            if part.is_multipart():
                children = part.get_payload()
                if isinstance(children, list):
                    pending.extend((child, depth + 1, False) for child in reversed(children))
                continue
            if part.get_content_type() == "text/plain":
                plain.append(_part_text(part))
            elif part.get_content_type() == "text/html":
                html.append(re.sub(r"<[^>]+>", " ", _part_text(part)))
    except RecursionError as exc:
        raise MailboxMessageInvalid(
            "imap_mime_too_complex", "Message MIME structure exceeds the safe limit"
        ) from exc
    selected = "\n\n".join(plain if plain else html)
    body = "\n".join(line.strip() for line in selected.splitlines() if line.strip())
    names = tuple(dict.fromkeys(item.filename for item in attachments))
    return (
        MessageContent(body[:body_char_limit], names, tuple(attachments)),
        tuple(attachment_payloads),
    )


def _content(message: EmailMessage, body_char_limit: int) -> MessageContent:
    return _content_and_attachment_payloads(message, body_char_limit)[0]


def _recovery_search_date(since: datetime) -> str:
    normalized = since.replace(tzinfo=UTC) if since.tzinfo is None else since.astimezone(UTC)
    search_date = normalized.date()
    with contextlib.suppress(OverflowError):
        search_date -= timedelta(days=1)
    return f"{search_date.day:02d}-{_ENGLISH_MONTHS[search_date.month - 1]}-{search_date.year:04d}"


class ImapGateway:
    def __init__(
        self,
        credentials: ImapCredentials,
        client_factory: Callable[[ImapCredentials, ssl.SSLContext], imaplib.IMAP4] | None = None,
    ):
        self.credentials = credentials
        self._mailbox_id = imap_mailbox_identity(credentials)
        self._client_factory = client_factory or self._default_client
        self._active_client: imaplib.IMAP4 | None = None

    @classmethod
    def from_credentials_file(cls, path: Path) -> ImapGateway:
        return cls(load_credentials(path))

    @staticmethod
    def _default_client(credentials: ImapCredentials, context: ssl.SSLContext) -> imaplib.IMAP4:
        if credentials.security == "tls":
            return imaplib.IMAP4_SSL(
                credentials.host,
                credentials.port,
                ssl_context=context,
                timeout=IMAP_TIMEOUT_SECONDS,
            )
        client = imaplib.IMAP4(
            credentials.host,
            credentials.port,
            timeout=IMAP_TIMEOUT_SECONDS,
        )
        try:
            status, _response = client.starttls(ssl_context=context)
        except (imaplib.IMAP4.error, ssl.SSLError, OSError, TimeoutError) as exc:
            with contextlib.suppress(Exception):
                client.logout()
            raise ImapError("imap_tls_failed", "Mail server did not establish STARTTLS") from exc
        if status != "OK":
            with contextlib.suppress(Exception):
                client.logout()
            raise ImapError("imap_tls_failed", "Mail server did not establish STARTTLS")
        return client

    @contextlib.contextmanager
    def _connected_mailbox(self) -> Iterator[imaplib.IMAP4]:
        try:
            context = ssl.create_default_context(cadata=self.credentials.ca_pem)
            client = self._client_factory(self.credentials, context)
        except ImapError:
            raise
        except imaplib.IMAP4.abort as exc:
            raise ImapError(
                "imap_connection_failed", "Mail server connection failed; retry"
            ) from exc
        except imaplib.IMAP4.error as exc:
            raise ImapError(
                "imap_protocol_error", "Mail server greeting was invalid; retry"
            ) from exc
        except (ssl.SSLError, ssl.CertificateError) as exc:
            raise ImapError("imap_tls_failed", "Mail server TLS verification failed") from exc
        except (OSError, TimeoutError) as exc:
            raise ImapError(
                "imap_connection_failed", "Mail server connection failed; retry"
            ) from exc

        try:
            status, _response = client.login(self.credentials.username, self.credentials.password)
            if status != "OK":
                raise ImapError(
                    "imap_authentication_failed", "Mail server rejected the credentials"
                )
        except ImapError:
            with contextlib.suppress(Exception):
                client.logout()
            raise
        except imaplib.IMAP4.abort as exc:
            with contextlib.suppress(Exception):
                client.logout()
            raise ImapError(
                "imap_connection_failed", "Mail server connection failed; retry"
            ) from exc
        except imaplib.IMAP4.error as exc:
            with contextlib.suppress(Exception):
                client.logout()
            raise ImapError(
                "imap_authentication_failed", "Mail server rejected the credentials"
            ) from exc
        except (OSError, TimeoutError) as exc:
            with contextlib.suppress(Exception):
                client.logout()
            raise ImapError(
                "imap_connection_failed", "Mail server connection failed; retry"
            ) from exc

        try:
            status, response = client.select("INBOX", readonly=True)
            if status != "OK":
                raise ImapError("imap_protocol_error", "Mail server INBOX is unavailable")
            raw_count = response[0] if response else None
            try:
                message_count = int(raw_count) if raw_count is not None else -1
            except (TypeError, ValueError) as exc:
                raise ImapError(
                    "imap_protocol_error", "Mail server omitted required mailbox state"
                ) from exc
            if message_count < 0:
                raise ImapError("imap_protocol_error", "Mail server omitted required mailbox state")
            client._eom_message_count = message_count  # type: ignore[attr-defined]
            yield client
        except ImapError:
            raise
        except imaplib.IMAP4.abort as exc:
            raise ImapError(
                "imap_connection_failed", "Mail server connection failed; retry"
            ) from exc
        except imaplib.IMAP4.error as exc:
            raise ImapError("imap_protocol_error", "Mail server operation failed; retry") from exc
        except (ssl.SSLError, ssl.CertificateError) as exc:
            raise ImapError("imap_tls_failed", "Mail server TLS verification failed") from exc
        except (OSError, TimeoutError) as exc:
            raise ImapError(
                "imap_connection_failed", "Mail server connection failed; retry"
            ) from exc
        finally:
            with contextlib.suppress(Exception):
                client.logout()

    @contextlib.contextmanager
    def polling_session(self) -> Iterator[None]:
        """Reuse one authenticated read-only session for a complete watcher check."""
        if self._active_client is not None:
            raise ImapError("imap_protocol_error", "Mail server polling session is already active")
        with self._connected_mailbox() as client:
            self._active_client = client
            try:
                yield
            finally:
                self._active_client = None

    @contextlib.contextmanager
    def _mailbox(self) -> Iterator[imaplib.IMAP4]:
        if self._active_client is not None:
            yield self._active_client
            return
        with self._connected_mailbox() as client:
            yield client

    @staticmethod
    def _snapshot(client: imaplib.IMAP4) -> tuple[int, int]:
        uid_validity = _selected_uid_validity(client)
        uid_next = _optional_response_number(client, "UIDNEXT")
        if uid_next is not None:
            return uid_validity, uid_next - 1
        message_count = _selected_message_count(client)
        if message_count == 0:
            return uid_validity, 0
        status, response = client.fetch(str(message_count), "(UID)")
        if status != "OK":
            raise ImapError("imap_protocol_error", "Mail server UID snapshot failed")
        match = _FETCH_UID.search(_response_metadata(response))
        if match is None:
            raise ImapError("imap_protocol_error", "Mail server UID snapshot failed")
        return uid_validity, int(match.group(1))

    def _checked_uid(self, client: imaplib.IMAP4, message_id: str) -> str:
        expected_mailbox, expected_validity, uid = _decode_message_id(message_id)
        if (
            expected_mailbox != self._mailbox_id
            or _selected_uid_validity(client) != expected_validity
        ):
            raise MailboxMessageUnavailable("The mail server message is no longer available")
        return str(uid)

    def initial_cursor(self) -> str:
        with self._mailbox() as client:
            uid_validity, last_uid = self._snapshot(client)
        return _cursor(self._mailbox_id, uid_validity, last_uid)

    def changes_since(self, cursor: str) -> MailboxChanges:
        saved_mailbox, saved_validity, saved_uid = _decode_cursor(cursor)
        if saved_mailbox != self._mailbox_id:
            raise StaleMailboxCursor("The configured mail server mailbox changed")
        with self._mailbox() as client:
            current_validity, snapshot_uid = self._snapshot(client)
            if current_validity != saved_validity:
                raise StaleMailboxCursor("The mail server reset its INBOX message identifiers")
            if snapshot_uid <= saved_uid:
                return MailboxChanges((), _cursor(self._mailbox_id, current_validity, snapshot_uid))
            search_end = min(snapshot_uid, saved_uid + MAX_UID_SEARCH_SPAN)
            status, response = client.uid("SEARCH", None, f"UID {saved_uid + 1}:{search_end}")
            if status != "OK":
                raise ImapError("imap_protocol_error", "Mail server change search failed")
            candidates = [uid for uid in _uids(response) if uid <= search_end]
        selected = candidates[:MAX_INCREMENTAL_MESSAGE_IDS]
        next_uid = selected[-1] if len(candidates) > len(selected) else search_end
        return MailboxChanges(
            tuple(_message_id(self._mailbox_id, current_validity, uid) for uid in selected),
            _cursor(self._mailbox_id, current_validity, next_uid),
        )

    def recover_since(self, addresses: frozenset[str], since: datetime) -> MailboxChanges:
        del addresses
        with self._mailbox() as client:
            uid_validity, snapshot_uid = self._snapshot(client)
            if snapshot_uid == 0:
                return MailboxChanges((), _cursor(self._mailbox_id, uid_validity, 0))
            search_end = min(snapshot_uid, MAX_UID_SEARCH_SPAN)
            status, response = client.uid(
                "SEARCH",
                None,
                f"UID 1:{search_end}",
                "SINCE",
                _recovery_search_date(since),
            )
            if status != "OK":
                raise ImapError("imap_protocol_error", "Mail server recovery search failed")
            candidates = [uid for uid in _uids(response) if uid <= search_end]
        selected = candidates[:MAX_INCREMENTAL_MESSAGE_IDS]
        next_uid = selected[-1] if len(candidates) > len(selected) else search_end
        return MailboxChanges(
            tuple(_message_id(self._mailbox_id, uid_validity, uid) for uid in selected),
            _cursor(self._mailbox_id, uid_validity, next_uid),
        )

    def metadata(self, message_id: str) -> MessageMetadata:
        with self._mailbox() as client:
            uid = self._checked_uid(client, message_id)
            status, response = client.uid(
                "FETCH",
                uid,
                "(UID INTERNALDATE "
                f"BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)]<0.{MAX_HEADER_BYTES}>)",
            )
            if status != "OK":
                raise ImapError("imap_protocol_error", "Mail server header fetch failed; retry")
            metadata, payload = _literal(response)
            if len(payload) >= MAX_HEADER_BYTES:
                raise MailboxMessageInvalid(
                    "imap_headers_too_large", "Message headers exceed the safe size limit"
                )
        try:
            parsed = BytesParser(policy=policy.default).parsebytes(payload, headersonly=True)
            raw_from = str(parsed.get("From", ""))
            sender_name, _address = parseaddr(raw_from)
            subject = str(parsed.get("Subject", "")).strip() or "(no subject)"
            thread_id = str(parsed.get("Message-ID", "")).strip() or None
        except RecursionError as exc:
            raise MailboxMessageInvalid(
                "imap_headers_too_complex", "Message headers exceed the safe complexity limit"
            ) from exc
        return MessageMetadata(
            message_id=message_id,
            thread_id=thread_id,
            sender=normalize_address(raw_from),
            sender_name=sender_name.strip() or None,
            subject=subject,
            received_at=_message_date(metadata, parsed),
            labels=frozenset({"INBOX"}),
        )

    def _raw_message(self, message_id: str) -> EmailMessage:
        with self._mailbox() as client:
            uid = self._checked_uid(client, message_id)
            status, size_response = client.uid("FETCH", uid, "(UID RFC822.SIZE)")
            if status != "OK":
                raise ImapError("imap_protocol_error", "Mail server size fetch failed; retry")
            size_metadata = _response_metadata(size_response)
            size_match = _RFC822_SIZE.search(size_metadata)
            if size_match is None:
                raise ImapError("imap_protocol_error", "Mail server omitted message size")
            if int(size_match.group(1)) > MAX_MESSAGE_BYTES:
                raise MailboxMessageInvalid(
                    "imap_message_too_large", "Message exceeds the safe size limit"
                )
            status, response = client.uid("FETCH", uid, "(UID BODY.PEEK[])")
            if status != "OK":
                raise ImapError("imap_protocol_error", "Mail server content fetch failed; retry")
            _metadata, payload = _literal(response)
        if len(payload) > MAX_MESSAGE_BYTES:
            raise MailboxMessageInvalid(
                "imap_message_too_large", "Message exceeds the safe size limit"
            )
        try:
            parsed = BytesParser(policy=policy.default).parsebytes(payload)
        except RecursionError as exc:
            raise MailboxMessageInvalid(
                "imap_mime_too_complex", "Message MIME structure exceeds the safe limit"
            ) from exc
        if not isinstance(parsed, EmailMessage):
            raise MailboxMessageInvalid("imap_message_invalid", "Message content is invalid")
        return parsed

    def content(self, message_id: str, body_char_limit: int) -> MessageContent:
        return _content(self._raw_message(message_id), body_char_limit)

    def attachment_bytes(self, message_id: str, part_id: str, attachment_id: str | None) -> bytes:
        if attachment_id is not None or not part_id.startswith("mime-"):
            raise MailboxMessageUnavailable("The mail server attachment identity is invalid")
        try:
            selected = int(part_id.removeprefix("mime-"))
        except ValueError as exc:
            raise MailboxMessageUnavailable(
                "The mail server attachment identity is invalid"
            ) from exc
        _message_content, attachments = _content_and_attachment_payloads(
            self._raw_message(message_id), 0
        )
        if selected < 0 or selected >= len(attachments):
            raise MailboxMessageUnavailable("The mail server attachment is no longer available")
        return attachments[selected]
