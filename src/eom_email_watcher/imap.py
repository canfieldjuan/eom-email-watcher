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
from datetime import UTC, date, datetime, timedelta
from email import policy
from email.errors import HeaderParseError
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import decode_rfc2231, parseaddr, parsedate_to_datetime
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote_to_bytes

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
from .mime import AttachmentDescriptor, html_to_text

IMAP_PROVIDER = "imap"
IMAP_CONNECTION_METHOD = "server_credentials"
IMAP_TIMEOUT_SECONDS = 30.0
MAX_CA_FILE_BYTES = 256 * 1024
MAX_CREDENTIAL_FILE_BYTES = MAX_CA_FILE_BYTES + 16 * 1024
MAX_MESSAGE_BYTES = 50 * 1024 * 1024
MAX_HEADER_BYTES = 64 * 1024
MAX_BODYSTRUCTURE_BYTES = 1024 * 1024
MAX_MIME_DEPTH = 100
MAX_MIME_PARTS = 1000
MAX_BODYSTRUCTURE_TOKENS = MAX_MIME_PARTS * 32
MAX_ATTACHMENT_FILENAME_BYTES = 1024
MAX_ATTACHMENT_FILENAME_TOTAL_BYTES = 64 * 1024
MAX_PERSISTED_ATTACHMENT_BYTES = (1 << 63) - 1
MAX_INCREMENTAL_MESSAGE_IDS = 200
MAX_UID_SEARCH_SPAN = 10_000
CURSOR_PREFIX = "eom-imap-v2:"
RECOVERY_CURSOR_PREFIX = "eom-imap-recovery-v1:"
MESSAGE_ID_PREFIX = "eom-imap-message-v2:"
_DOMAIN_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_UID = re.compile(r"[1-9][0-9]*\Z")
_MAILBOX_ID = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_ATTACHMENT_SUFFIX = re.compile(r"\.[A-Za-z0-9]{1,12}\Z")
_INTERNAL_DATE = re.compile(rb'INTERNALDATE "([^"]+)"', re.IGNORECASE)
_FETCH_UID = re.compile(rb"(?:^|[ (])UID ([1-9][0-9]*)(?:[ )]|$)", re.IGNORECASE)
_IMAP_LITERAL_SUFFIX = re.compile(rb"\{([0-9]+)\+?\}\r?\n?\Z")
_IMAP_SECTION = re.compile(r"(?:[1-9][0-9]*)(?:\.[1-9][0-9]*)*\Z")
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


def _canonical_host(host: str) -> str:
    try:
        return str(ip_address(host))
    except ValueError:
        return host.encode("idna").decode("ascii").casefold()


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
    return f"{local}@{_canonical_host(domain)}"


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
    host = _canonical_host(host)

    port = value.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65_535:
        raise ImapError("imap_configuration_error", "Mail server port must be between 1 and 65535")

    security = value.get("security")
    if not isinstance(security, str) or security not in {"tls", "starttls"}:
        raise ImapError("imap_configuration_error", "Choose TLS or STARTTLS security")

    username_value = value.get("username")
    username = username_value if isinstance(username_value, str) else ""
    if (
        not username.strip()
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


def imap_cursor_mailbox_identity(cursor: str) -> str:
    if cursor.startswith(RECOVERY_CURSOR_PREFIX):
        return _decode_recovery_cursor(cursor)[0]
    return _decode_cursor(cursor)[0]


def imap_cursor_epoch(cursor: str) -> tuple[str, int]:
    """Return the credential identity and UIDVALIDITY captured by a saved cursor."""
    decoded = (
        _decode_recovery_cursor(cursor)
        if cursor.startswith(RECOVERY_CURSOR_PREFIX)
        else _decode_cursor(cursor)
    )
    return decoded[0], decoded[1]


def _cursor(mailbox_id: str, uid_validity: int, last_uid: int) -> str:
    return f"{CURSOR_PREFIX}{mailbox_id}:{uid_validity}:{last_uid}"


def _recovery_cursor(
    mailbox_id: str,
    uid_validity: int,
    upper_uid: int,
    snapshot_uid: int,
    search_day: date,
) -> str:
    return (
        f"{RECOVERY_CURSOR_PREFIX}{mailbox_id}:{uid_validity}:{upper_uid}:"
        f"{snapshot_uid}:{search_day.toordinal()}"
    )


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


def _decode_recovery_cursor(value: str) -> tuple[str, int, int, int, date]:
    encoded = (
        value.removeprefix(RECOVERY_CURSOR_PREFIX)
        if value.startswith(RECOVERY_CURSOR_PREFIX)
        else ""
    )
    fields = encoded.split(":")
    if (
        len(fields) != 5
        or _MAILBOX_ID.fullmatch(fields[0]) is None
        or any(not field.isdecimal() for field in fields[1:])
    ):
        raise ImapError("imap_cursor_invalid", "Saved mail server cursor is invalid")
    mailbox_id, validity_text, upper_text, snapshot_text, ordinal_text = fields
    validity = int(validity_text)
    upper_uid = int(upper_text)
    snapshot_uid = int(snapshot_text)
    try:
        search_day = date.fromordinal(int(ordinal_text))
    except ValueError as exc:
        raise ImapError("imap_cursor_invalid", "Saved mail server cursor is invalid") from exc
    if validity <= 0 or upper_uid <= 0 or snapshot_uid <= 0 or upper_uid > snapshot_uid:
        raise ImapError("imap_cursor_invalid", "Saved mail server cursor is invalid")
    return mailbox_id, validity, upper_uid, snapshot_uid, search_day


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


def _quoted_imap_astring(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


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


def _fetch_sequence_uids(client: imaplib.IMAP4, sequences: list[int]) -> list[int]:
    if not sequences:
        return []
    sequence_set = ",".join(str(sequence) for sequence in sequences)
    status, response = client.fetch(sequence_set, "(UID)")
    if status != "OK":
        raise ImapError("imap_protocol_error", "Mail server UID page failed")
    values: list[int] = []
    for item in response or []:
        metadata = item[0] if isinstance(item, tuple) and item else item
        if isinstance(metadata, bytes):
            values.extend(int(match.group(1)) for match in _FETCH_UID.finditer(metadata))
    values = sorted(set(values))
    if len(values) != len(sequences):
        raise ImapError("imap_protocol_error", "Mail server UID page was incomplete")
    return values


def _uid_at_sequence(client: imaplib.IMAP4, sequence: int) -> int:
    return _fetch_sequence_uids(client, [sequence])[0]


def _first_sequence_after_uid(client: imaplib.IMAP4, message_count: int, saved_uid: int) -> int:
    low = 1
    high = message_count
    result = message_count + 1
    while low <= high:
        middle = (low + high) // 2
        if _uid_at_sequence(client, middle) > saved_uid:
            result = middle
            high = middle - 1
        else:
            low = middle + 1
    return result


def _last_sequence_at_or_before_uid(
    client: imaplib.IMAP4, message_count: int, upper_uid: int
) -> int:
    low = 1
    high = message_count
    result = 0
    while low <= high:
        middle = (low + high) // 2
        if _uid_at_sequence(client, middle) <= upper_uid:
            result = middle
            low = middle + 1
        else:
            high = middle - 1
    return result


def _reject_recovery_expunge(client: imaplib.IMAP4) -> None:
    _status, values = client.response("EXPUNGE")
    if any(value is not None for value in values or []):
        raise ImapError("imap_mailbox_changed", "Mail server changed during recovery; retry")


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


class _ImapQuoted(bytes):
    """Marker that distinguishes an IMAP quoted string from an atom."""


type _ImapValue = bytes | None | list[_ImapValue]


@dataclass(frozen=True)
class _ImapBodyPart:
    section: str
    media_type: str
    transfer_encoding: str
    charset: str | None
    byte_size: int


@dataclass(frozen=True)
class _ImapAttachment:
    section: str
    transfer_encoding: str
    is_multipart: bool
    descriptor: AttachmentDescriptor


@dataclass(frozen=True)
class _ImapCatalog:
    plain: tuple[_ImapBodyPart, ...]
    html: tuple[_ImapBodyPart, ...]
    attachments: tuple[_ImapAttachment, ...]


class _ImapValueParser:
    def __init__(self, data: bytes):
        self.data = data
        self.position = 0
        self.tokens = 0

    def parse(self) -> list[_ImapValue]:
        values: list[_ImapValue] = []
        self._skip_spaces()
        while self.position < len(self.data):
            values.append(self._value(0))
            self._skip_spaces()
        return values

    def _value(self, depth: int) -> _ImapValue:
        self.tokens += 1
        if self.tokens > MAX_BODYSTRUCTURE_TOKENS or depth > MAX_MIME_DEPTH + 8:
            raise ValueError("IMAP data exceeds structural limits")
        if self.position >= len(self.data):
            raise ValueError("IMAP data ended early")
        if self.data[self.position] == ord("("):
            self.position += 1
            values: list[_ImapValue] = []
            self._skip_spaces()
            while self.position < len(self.data) and self.data[self.position] != ord(")"):
                values.append(self._value(depth + 1))
                self._skip_spaces()
            if self.position >= len(self.data):
                raise ValueError("IMAP list ended early")
            self.position += 1
            return values
        if self.data[self.position] == ord('"'):
            return self._quoted()
        start = self.position
        while self.position < len(self.data) and self.data[self.position] not in b" ()\r\n\t":
            self.position += 1
        if start == self.position:
            raise ValueError("IMAP atom is invalid")
        atom = self.data[start : self.position]
        return None if atom.upper() == b"NIL" else atom

    def _quoted(self) -> _ImapQuoted:
        self.position += 1
        value = bytearray()
        while self.position < len(self.data):
            current = self.data[self.position]
            self.position += 1
            if current == ord('"'):
                return _ImapQuoted(value)
            if current == ord("\\"):
                if self.position >= len(self.data):
                    raise ValueError("IMAP quoted string ended early")
                current = self.data[self.position]
                self.position += 1
            value.append(current)
        raise ValueError("IMAP quoted string ended early")

    def _skip_spaces(self) -> None:
        while self.position < len(self.data) and self.data[self.position] in b" \r\n\t":
            self.position += 1


def _bodystructure_response_bytes(response: list[Any] | None) -> bytes:
    output = bytearray()
    for item in response or []:
        if item is None:
            continue
        if isinstance(item, tuple) and len(item) == 2:
            metadata, literal = item
            if not isinstance(metadata, bytes) or not isinstance(literal, bytes):
                raise ValueError("IMAP literal is invalid")
            match = _IMAP_LITERAL_SUFFIX.search(metadata)
            if match is None or int(match.group(1)) != len(literal):
                raise ValueError("IMAP literal length is invalid")
            output.extend(metadata[: match.start()])
            output.extend(b'"')
            output.extend(literal.replace(b"\\", b"\\\\").replace(b'"', b'\\"'))
            output.extend(b'"')
        elif isinstance(item, bytes):
            output.extend(item)
        else:
            raise ValueError("IMAP response item is invalid")
        output.extend(b" ")
        if len(output) > MAX_BODYSTRUCTURE_BYTES:
            raise ValueError("IMAP response exceeds the metadata limit")
    if not output:
        raise MailboxMessageUnavailable("The mail server message is no longer available")
    return bytes(output)


def _bodystructure(response: list[Any] | None, expected_uid: str) -> list[_ImapValue]:
    try:
        parsed = _ImapValueParser(_bodystructure_response_bytes(response)).parse()
        candidates = [
            value
            for value in parsed
            if isinstance(value, list)
            and any(isinstance(item, bytes) and item.upper() == b"BODYSTRUCTURE" for item in value)
        ]
        if len(candidates) != 1:
            raise ValueError("IMAP body structure response is ambiguous")
        fetch_data = candidates[0]
        uid_values = [
            fetch_data[index + 1]
            for index, value in enumerate(fetch_data[:-1])
            if isinstance(value, bytes) and value.upper() == b"UID"
        ]
        structures = [
            fetch_data[index + 1]
            for index, value in enumerate(fetch_data[:-1])
            if isinstance(value, bytes) and value.upper() == b"BODYSTRUCTURE"
        ]
        if uid_values != [expected_uid.encode("ascii")] or len(structures) != 1:
            raise ValueError("IMAP fetch identity is invalid")
        structure = structures[0]
        if not isinstance(structure, list):
            raise ValueError("IMAP body structure is invalid")
        return structure
    except MailboxMessageUnavailable:
        raise
    except (StopIteration, TypeError, ValueError) as exc:
        raise MailboxMessageInvalid(
            "imap_bodystructure_invalid", "Message MIME metadata is invalid"
        ) from exc


def _imap_text(value: _ImapValue) -> str | None:
    if not isinstance(value, bytes):
        return None
    return value.decode("utf-8", errors="replace")


def _imap_atom(value: _ImapValue, field: str) -> str:
    decoded = _imap_text(value)
    if decoded is None or not decoded or any(character.isspace() for character in decoded):
        raise ValueError(f"IMAP {field} is invalid")
    return decoded.casefold()


def _imap_size(value: _ImapValue) -> int:
    if not isinstance(value, bytes) or not value.isdigit():
        raise ValueError("IMAP body size is invalid")
    size = int(value)
    if size < 0 or size > MAX_PERSISTED_ATTACHMENT_BYTES:
        raise ValueError("IMAP body size is invalid")
    return size


def _section_payload(
    response: list[Any] | None,
    expected_uid: str,
    expected_section: str,
    byte_limit: int,
) -> bytes:
    expected_uid_bytes = expected_uid.encode("ascii")
    expected_selector = f"BODY[{expected_section}]".encode("ascii").upper()
    literal_items = [item for item in response or [] if isinstance(item, tuple) and len(item) == 2]
    if literal_items:
        if len(literal_items) != 1:
            raise MailboxMessageUnavailable("The mail server message is no longer available")
        metadata, payload = literal_items[0]
        if not isinstance(metadata, bytes) or not isinstance(payload, bytes):
            raise MailboxMessageUnavailable("The mail server message is no longer available")
        returned_uids = [match.group(1) for match in _FETCH_UID.finditer(metadata)]
        returned_section = re.compile(
            rb"BODY\["
            + re.escape(expected_section.encode("ascii"))
            + rb"\](?:<0>)?\s*\{([0-9]+)\+?\}\r?\n?\Z",
            re.IGNORECASE,
        )
        section_match = returned_section.search(metadata)
        if (
            returned_uids != [expected_uid_bytes]
            or section_match is None
            or int(section_match.group(1)) != len(payload)
        ):
            raise MailboxMessageUnavailable("The mail server message is no longer available")
        extra_section = re.compile(
            rb"BODY\[" + re.escape(expected_section.encode("ascii")) + rb"\]",
            re.IGNORECASE,
        )
        if any(
            isinstance(item, bytes) and extra_section.search(item) is not None
            for item in response or []
        ):
            raise MailboxMessageUnavailable("The mail server message is no longer available")
        selected = payload
    else:
        encoded = bytearray()
        for item in response or []:
            if item is None:
                continue
            if not isinstance(item, bytes):
                raise MailboxMessageUnavailable("The mail server message is no longer available")
            if len(encoded) + len(item) + 1 > byte_limit + MAX_HEADER_BYTES:
                raise MailboxMessageInvalid(
                    "imap_message_too_large", "Message content exceeds the safe size limit"
                )
            encoded.extend(item)
            encoded.extend(b" ")
        if not encoded:
            raise MailboxMessageUnavailable("The mail server message is no longer available")
        try:
            parsed = _ImapValueParser(bytes(encoded)).parse()
        except ValueError as exc:
            raise MailboxMessageUnavailable(
                "The mail server message is no longer available"
            ) from exc
        selected_values: list[bytes] = []
        for value in parsed:
            if not isinstance(value, list):
                continue
            returned_uids = [
                value[index + 1]
                for index, item in enumerate(value[:-1])
                if isinstance(item, bytes) and item.upper() == b"UID"
            ]
            returned_sections = [
                value[index + 1]
                for index, item in enumerate(value[:-1])
                if isinstance(item, bytes)
                and item.upper() in {expected_selector, expected_selector + b"<0>"}
            ]
            if not returned_sections:
                continue
            if (
                returned_uids != [expected_uid_bytes]
                or len(returned_sections) != 1
                or not isinstance(returned_sections[0], _ImapQuoted)
            ):
                raise MailboxMessageUnavailable("The mail server message is no longer available")
            selected_values.append(bytes(returned_sections[0]))
        if len(selected_values) != 1:
            raise MailboxMessageUnavailable("The mail server message is no longer available")
        selected = selected_values[0]
    if len(selected) > byte_limit:
        raise MailboxMessageInvalid(
            "imap_message_too_large", "Message content exceeds the safe size limit"
        )
    return selected


def _imap_params(value: _ImapValue) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, list) or len(value) % 2:
        raise ValueError("IMAP parameter list is invalid")
    params: dict[str, str] = {}
    for index in range(0, len(value), 2):
        name = _imap_atom(value[index], "parameter name")
        parameter_value = _imap_text(value[index + 1])
        if parameter_value is None:
            raise ValueError("IMAP parameter value is invalid")
        params.setdefault(name, parameter_value)
    return params


def _decode_mime_words(value: str) -> str:
    try:
        return str(make_header(decode_header(value)))
    except (HeaderParseError, LookupError, UnicodeError, ValueError):
        return value


def _decode_rfc2231_bytes(value: str) -> tuple[str | None, bytes]:
    charset, _language, encoded = decode_rfc2231(value)
    return charset, unquote_to_bytes(encoded)


def _decode_parameter_bytes(value: bytes, charset: str | None) -> str:
    try:
        return value.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return value.decode("utf-8", errors="replace")


def _extended_parameter(params: dict[str, str], name: str) -> tuple[bool, str | None]:
    exact = params.get(name)
    single_extended = params.get(f"{name}*")
    segment_pattern = re.compile(rf"{re.escape(name)}\*([0-9]+)(\*)?\Z")
    segments: dict[int, tuple[bool, str]] = {}
    marker = exact is not None or single_extended is not None
    ambiguous = False
    for key, value in params.items():
        match = segment_pattern.fullmatch(key)
        if match is None:
            continue
        marker = True
        try:
            index = int(match.group(1))
        except ValueError:
            ambiguous = True
            continue
        if index >= MAX_MIME_PARTS or index in segments:
            ambiguous = True
            continue
        segments[index] = (match.group(2) is not None, value)

    if segments:
        if single_extended is not None or set(segments) != set(range(len(segments))):
            ambiguous = True
        if not ambiguous:
            charset: str | None = None
            output = bytearray()
            for index in range(len(segments)):
                encoded, value = segments[index]
                if index == 0 and encoded:
                    charset, decoded = _decode_rfc2231_bytes(value)
                    output.extend(decoded)
                elif encoded:
                    output.extend(unquote_to_bytes(value))
                else:
                    output.extend(value.encode("utf-8", errors="replace"))
            return True, _decode_mime_words(_decode_parameter_bytes(bytes(output), charset))
        return True, None

    if single_extended is not None:
        charset, decoded = _decode_rfc2231_bytes(single_extended)
        return True, _decode_mime_words(_decode_parameter_bytes(decoded, charset))
    if exact is not None:
        return True, _decode_mime_words(exact)
    return marker, None


def _imap_disposition(value: _ImapValue) -> tuple[str | None, dict[str, str]]:
    if value is None:
        return None, {}
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("IMAP disposition is invalid")
    return _imap_atom(value[0], "disposition"), _imap_params(value[1])


def _catalog_from_bodystructure(structure: list[_ImapValue]) -> _ImapCatalog:
    plain: list[_ImapBodyPart] = []
    html: list[_ImapBodyPart] = []
    attachments: list[_ImapAttachment] = []
    filename_bytes = 0
    visited = 0

    def record_attachment(
        *,
        section: str,
        media_type: str,
        transfer_encoding: str,
        byte_size: int,
        filename: str | None,
        is_multipart: bool = False,
    ) -> None:
        nonlocal filename_bytes
        position = len(attachments)
        selected_name = filename or _synthesized_attachment_name(position, media_type)
        selected_bytes = len(selected_name.encode("utf-8", errors="replace"))
        filename_bytes += selected_bytes
        if (
            selected_bytes > MAX_ATTACHMENT_FILENAME_BYTES
            or filename_bytes > MAX_ATTACHMENT_FILENAME_TOTAL_BYTES
        ):
            raise MailboxMessageInvalid(
                "imap_attachment_metadata_too_large",
                "Message attachment metadata exceeds the safe size limit",
            )
        attachments.append(
            _ImapAttachment(
                section=section,
                transfer_encoding=transfer_encoding,
                is_multipart=is_multipart,
                descriptor=AttachmentDescriptor(
                    part_id=f"mime-{position}",
                    attachment_id=None,
                    filename=selected_name,
                    media_type=media_type,
                    byte_size=byte_size,
                    position=position,
                ),
            )
        )

    def visit(value: list[_ImapValue], section: str, depth: int) -> None:
        nonlocal visited
        visited += 1
        if visited > MAX_MIME_PARTS or depth > MAX_MIME_DEPTH or not value:
            raise ValueError("IMAP MIME structure exceeds limits")
        multipart = isinstance(value[0], list)
        if multipart:
            child_count = 0
            while child_count < len(value) and isinstance(value[child_count], list):
                child_count += 1
            if child_count == 0 or child_count >= len(value):
                raise ValueError("IMAP multipart structure is invalid")
            subtype = _imap_atom(value[child_count], "multipart subtype")
            params_index = child_count + 1
            params = _imap_params(value[params_index]) if params_index < len(value) else {}
            disposition_index = child_count + 2
            disposition, disposition_params = (
                _imap_disposition(value[disposition_index])
                if disposition_index < len(value)
                else (None, {})
            )
            disposition_marker, disposition_filename = _extended_parameter(
                disposition_params, "filename"
            )
            type_marker, type_filename = _extended_parameter(params, "name")
            filename = disposition_filename or type_filename
            attachment = disposition == "attachment" or disposition_marker or type_marker
            if attachment:
                record_attachment(
                    section=section,
                    media_type=f"multipart/{subtype}",
                    transfer_encoding="binary",
                    byte_size=0,
                    filename=filename,
                    is_multipart=True,
                )
                return
            for index, child in enumerate(value[:child_count], start=1):
                if not isinstance(child, list):
                    raise ValueError("IMAP multipart child is invalid")
                child_section = f"{section}.{index}" if section else str(index)
                visit(child, child_section, depth + 1)
            return

        if len(value) < 7:
            raise ValueError("IMAP single-part structure is incomplete")
        maintype = _imap_atom(value[0], "media type")
        subtype = _imap_atom(value[1], "media subtype")
        params = _imap_params(value[2])
        transfer_encoding = _imap_atom(value[5], "transfer encoding")
        if transfer_encoding not in {"7bit", "8bit", "binary", "base64", "quoted-printable"}:
            raise ValueError("IMAP transfer encoding is unsupported")
        byte_size = _imap_size(value[6])
        if maintype == "message" and subtype == "rfc822":
            disposition_index = 11
        elif maintype == "text":
            disposition_index = 9
        else:
            disposition_index = 8
        disposition, disposition_params = (
            _imap_disposition(value[disposition_index])
            if disposition_index < len(value)
            else (None, {})
        )
        disposition_marker, disposition_filename = _extended_parameter(
            disposition_params, "filename"
        )
        type_marker, type_filename = _extended_parameter(params, "name")
        filename = disposition_filename or type_filename
        media_type = f"{maintype}/{subtype}"
        attachment = disposition == "attachment" or disposition_marker or type_marker
        if attachment:
            record_attachment(
                section=section,
                media_type=media_type,
                transfer_encoding=transfer_encoding,
                byte_size=byte_size,
                filename=filename,
            )
            return
        if maintype == "message" and subtype == "rfc822":
            if len(value) < 10 or not isinstance(value[8], list):
                raise ValueError("IMAP message body structure is invalid")
            nested = value[8]
            nested_section = section if nested and isinstance(nested[0], list) else f"{section}.1"
            visit(nested, nested_section, depth + 1)
            return
        if maintype != "text" or subtype not in {"plain", "html"}:
            return
        part = _ImapBodyPart(
            section=section,
            media_type=media_type,
            transfer_encoding=transfer_encoding,
            charset=params.get("charset"),
            byte_size=byte_size,
        )
        (plain if subtype == "plain" else html).append(part)

    try:
        root_section = "" if structure and isinstance(structure[0], list) else "1"
        visit(structure, root_section, 0)
    except MailboxMessageInvalid:
        raise
    except (RecursionError, TypeError, ValueError) as exc:
        raise MailboxMessageInvalid(
            "imap_bodystructure_invalid", "Message MIME metadata is invalid"
        ) from exc
    return _ImapCatalog(tuple(plain), tuple(html), tuple(attachments))


def _decoded_section(payload: bytes, transfer_encoding: str) -> bytes:
    synthetic = f"Content-Transfer-Encoding: {transfer_encoding}\r\n\r\n".encode("ascii") + payload
    try:
        parsed = BytesParser(policy=policy.default).parsebytes(synthetic)
        decoded = parsed.get_payload(decode=True)
    except (RecursionError, TypeError, ValueError) as exc:
        raise MailboxMessageInvalid("imap_message_invalid", "Message content is invalid") from exc
    if not isinstance(decoded, bytes):
        raise MailboxMessageInvalid("imap_message_invalid", "Message content is invalid")
    return decoded


def _multipart_attachment_prefix(headers: bytes, expected_media_type: str) -> bytes:
    normalized = headers.rstrip(b"\r\n") + b"\r\n\r\n"
    try:
        parsed = BytesParser(policy=policy.default).parsebytes(normalized, headersonly=True)
        content_type = parsed.get_content_type().casefold()
        boundary = parsed.get_boundary()
    except (HeaderParseError, RecursionError, TypeError, UnicodeError, ValueError) as exc:
        raise MailboxMessageInvalid(
            "imap_message_invalid", "Message attachment headers are invalid"
        ) from exc
    if content_type != expected_media_type or not boundary:
        raise MailboxMessageInvalid(
            "imap_message_invalid", "Message attachment headers are invalid"
        )
    return normalized


def _message_date(metadata: bytes, message: Message) -> str:
    match = _INTERNAL_DATE.search(metadata)
    if match is not None:
        try:
            parsed = parsedate_to_datetime(match.group(1).decode("ascii"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC).isoformat()
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
            if part.get_content_maintype() == "multipart":
                return part.as_bytes(policy=policy.default)
            return b"\r\n".join(item.as_bytes(policy=policy.default) for item in nested)
        except RecursionError as exc:
            raise MailboxMessageInvalid(
                "imap_mime_too_complex", "Message MIME structure exceeds the safe limit"
            ) from exc
    return b""


def _validate_mime_tree(message: Message) -> None:
    pending: list[tuple[Message, int]] = [(message, 0)]
    visited = 0
    while pending:
        part, depth = pending.pop()
        visited += 1
        if visited > MAX_MIME_PARTS or depth > MAX_MIME_DEPTH:
            raise MailboxMessageInvalid(
                "imap_mime_too_complex", "Message MIME structure exceeds the safe limit"
            )
        children = part.get_payload()
        if isinstance(children, list):
            pending.extend((child, depth + 1) for child in reversed(children))


def _content_and_attachment_payloads(
    message: EmailMessage, body_char_limit: int
) -> tuple[MessageContent, tuple[bytes, ...]]:
    plain: list[str] = []
    html: list[str] = []
    attachments: list[AttachmentDescriptor] = []
    attachment_payloads: list[bytes] = []
    attachment_filename_bytes = 0

    try:
        _validate_mime_tree(message)
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
                position = len(attachments)
                media_type = part.get_content_type().casefold()
                filename = filename or _synthesized_attachment_name(position, media_type)
                encoded_filename_bytes = len(filename.encode("utf-8", errors="replace"))
                attachment_filename_bytes += encoded_filename_bytes
                if (
                    encoded_filename_bytes > MAX_ATTACHMENT_FILENAME_BYTES
                    or attachment_filename_bytes > MAX_ATTACHMENT_FILENAME_TOTAL_BYTES
                ):
                    raise MailboxMessageInvalid(
                        "imap_attachment_metadata_too_large",
                        "Message attachment metadata exceeds the safe size limit",
                    )
                payload = _attachment_payload(part)
                attachment_payloads.append(payload)
                attachments.append(
                    AttachmentDescriptor(
                        part_id=f"mime-{position}",
                        attachment_id=None,
                        filename=filename,
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
                html.append(html_to_text(_part_text(part)))
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


def _recovery_search_day(since: datetime) -> date:
    normalized = since.replace(tzinfo=UTC) if since.tzinfo is None else since.astimezone(UTC)
    search_date = normalized.date()
    with contextlib.suppress(OverflowError):
        search_date -= timedelta(days=1)
    return search_date


def _format_recovery_search_day(search_date: date) -> str:
    return f"{search_date.day:02d}-{_ENGLISH_MONTHS[search_date.month - 1]}-{search_date.year:04d}"


def _recovery_search_date(since: datetime) -> str:
    return _format_recovery_search_day(_recovery_search_day(since))


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
        except ssl.SSLError as exc:
            with contextlib.suppress(Exception):
                client.logout()
            raise ImapError("imap_tls_failed", "Mail server did not establish STARTTLS") from exc
        except imaplib.IMAP4.abort as exc:
            with contextlib.suppress(Exception):
                client.logout()
            raise ImapError(
                "imap_connection_failed", "Mail server connection failed; retry"
            ) from exc
        except (OSError, TimeoutError) as exc:
            with contextlib.suppress(Exception):
                client.logout()
            raise ImapError(
                "imap_connection_failed", "Mail server connection failed; retry"
            ) from exc
        except imaplib.IMAP4.error as exc:
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
            status, _response = client.login(
                _quoted_imap_astring(self.credentials.username),
                self.credentials.password,
            )
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

    def mailbox_epoch(self) -> tuple[str, int]:
        if self._active_client is None:
            raise ImapError(
                "imap_protocol_error",
                "Mail server identity requires an active polling session",
            )
        return self._mailbox_id, _selected_uid_validity(self._active_client)

    def mailbox_identity_key(self) -> str:
        mailbox_id, uid_validity = self.mailbox_epoch()
        value = "\0".join(("imap-mailbox-v2", mailbox_id, str(uid_validity))).encode("utf-8")
        return hashlib.sha256(value).hexdigest()

    def mailbox_address(self) -> str:
        return self.credentials.email_address

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
        if cursor.startswith(RECOVERY_CURSOR_PREFIX):
            (
                saved_mailbox,
                saved_validity,
                upper_uid,
                snapshot_uid,
                search_day,
            ) = _decode_recovery_cursor(cursor)
            if saved_mailbox != self._mailbox_id:
                raise StaleMailboxCursor("The configured mail server mailbox changed")
            with self._mailbox() as client:
                current_validity = _selected_uid_validity(client)
                if current_validity != saved_validity:
                    current_validity, current_snapshot_uid = self._snapshot(client)
                    _reject_recovery_expunge(client)
                    if current_snapshot_uid == 0:
                        return MailboxChanges((), _cursor(self._mailbox_id, current_validity, 0))
                    return self._recovery_page(
                        client,
                        uid_validity=current_validity,
                        upper_uid=current_snapshot_uid,
                        snapshot_uid=current_snapshot_uid,
                        search_day=search_day,
                    )
                return self._recovery_page(
                    client,
                    uid_validity=saved_validity,
                    upper_uid=upper_uid,
                    snapshot_uid=snapshot_uid,
                    search_day=search_day,
                )
        saved_mailbox, saved_validity, saved_uid = _decode_cursor(cursor)
        if saved_mailbox != self._mailbox_id:
            raise StaleMailboxCursor("The configured mail server mailbox changed")
        with self._mailbox() as client:
            current_validity, snapshot_uid = self._snapshot(client)
            if current_validity != saved_validity:
                raise StaleMailboxCursor("The mail server reset its INBOX message identifiers")
            if snapshot_uid <= saved_uid:
                return MailboxChanges((), _cursor(self._mailbox_id, current_validity, snapshot_uid))
            message_count = _selected_message_count(client)
            first_sequence = _first_sequence_after_uid(client, message_count, saved_uid)
            if first_sequence > message_count:
                return MailboxChanges((), _cursor(self._mailbox_id, current_validity, snapshot_uid))
            last_sequence = min(message_count, first_sequence + MAX_INCREMENTAL_MESSAGE_IDS - 1)
            candidates = _fetch_sequence_uids(
                client, list(range(first_sequence, last_sequence + 1))
            )
            if any(uid <= saved_uid or uid > snapshot_uid for uid in candidates):
                raise ImapError("imap_protocol_error", "Mail server UID page was invalid")
        next_uid = snapshot_uid if last_sequence == message_count else candidates[-1]
        return MailboxChanges(
            tuple(_message_id(self._mailbox_id, current_validity, uid) for uid in candidates),
            _cursor(self._mailbox_id, current_validity, next_uid),
        )

    def _recovery_page(
        self,
        client: imaplib.IMAP4,
        *,
        uid_validity: int,
        upper_uid: int,
        snapshot_uid: int,
        search_day: date,
    ) -> MailboxChanges:
        _reject_recovery_expunge(client)
        message_count = _selected_message_count(client)
        if message_count == 0:
            return MailboxChanges((), _cursor(self._mailbox_id, uid_validity, snapshot_uid))
        last_sequence = _last_sequence_at_or_before_uid(client, message_count, upper_uid)
        _reject_recovery_expunge(client)
        if last_sequence == 0:
            return MailboxChanges((), _cursor(self._mailbox_id, uid_validity, snapshot_uid))
        first_sequence = max(1, last_sequence - MAX_UID_SEARCH_SPAN + 1)
        status, response = client.search(
            None,
            f"{first_sequence}:{last_sequence}",
            "SINCE",
            _format_recovery_search_day(search_day),
        )
        if status != "OK":
            raise ImapError("imap_protocol_error", "Mail server recovery search failed")
        _reject_recovery_expunge(client)
        matching_sequences = [
            sequence for sequence in _uids(response) if first_sequence <= sequence <= last_sequence
        ]
        selected_sequences = matching_sequences[-MAX_INCREMENTAL_MESSAGE_IDS:]
        candidates = _fetch_sequence_uids(client, selected_sequences)
        if len(matching_sequences) > len(selected_sequences):
            next_upper_uid = candidates[0] - 1
        elif first_sequence == 1:
            next_upper_uid = 0
        else:
            next_upper_uid = _uid_at_sequence(client, first_sequence) - 1
        _reject_recovery_expunge(client)
        if any(uid > upper_uid or uid > snapshot_uid for uid in candidates):
            raise ImapError("imap_protocol_error", "Mail server UID page was invalid")
        if next_upper_uid <= 0:
            next_cursor = _cursor(self._mailbox_id, uid_validity, snapshot_uid)
        else:
            next_cursor = _recovery_cursor(
                self._mailbox_id,
                uid_validity,
                next_upper_uid,
                snapshot_uid,
                search_day,
            )
        return MailboxChanges(
            tuple(_message_id(self._mailbox_id, uid_validity, uid) for uid in candidates),
            next_cursor,
        )

    def recover_since(self, addresses: frozenset[str], since: datetime) -> MailboxChanges:
        del addresses
        with self._mailbox() as client:
            uid_validity, snapshot_uid = self._snapshot(client)
            _reject_recovery_expunge(client)
            if snapshot_uid == 0:
                return MailboxChanges((), _cursor(self._mailbox_id, uid_validity, 0))
            return self._recovery_page(
                client,
                uid_validity=uid_validity,
                upper_uid=snapshot_uid,
                snapshot_uid=snapshot_uid,
                search_day=_recovery_search_day(since),
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

    @staticmethod
    def _catalog(client: imaplib.IMAP4, uid: str) -> _ImapCatalog:
        status, response = client.uid("FETCH", uid, "(UID BODYSTRUCTURE)")
        if status != "OK":
            raise ImapError("imap_protocol_error", "Mail server MIME metadata fetch failed; retry")
        return _catalog_from_bodystructure(_bodystructure(response, uid))

    @staticmethod
    def _fetch_section_bytes(
        client: imaplib.IMAP4,
        uid: str,
        section: str,
        byte_limit: int,
    ) -> bytes:
        numbered_section = _IMAP_SECTION.fullmatch(section)
        mime_section = section.endswith(".MIME") and _IMAP_SECTION.fullmatch(section[:-5])
        if section and numbered_section is None and not mime_section:
            raise MailboxMessageInvalid(
                "imap_bodystructure_invalid", "Message MIME metadata is invalid"
            )
        if byte_limit < 0 or byte_limit > MAX_MESSAGE_BYTES:
            raise MailboxMessageInvalid(
                "imap_message_too_large", "Message content exceeds the safe size limit"
            )
        status, response = client.uid(
            "FETCH",
            uid,
            f"(UID BODY.PEEK[{section}]<0.{byte_limit + 1}>)",
        )
        if status != "OK":
            raise ImapError("imap_protocol_error", "Mail server content fetch failed; retry")
        return _section_payload(response, uid, section, byte_limit)

    @classmethod
    def _section_bytes(
        cls,
        client: imaplib.IMAP4,
        uid: str,
        part: _ImapBodyPart | _ImapAttachment,
        *,
        byte_limit: int = MAX_MESSAGE_BYTES,
    ) -> bytes:
        root_attachment = isinstance(part, _ImapAttachment) and not part.section
        if not root_attachment and _IMAP_SECTION.fullmatch(part.section) is None:
            raise MailboxMessageInvalid(
                "imap_bodystructure_invalid", "Message MIME metadata is invalid"
            )
        expected_size = (
            part.byte_size if isinstance(part, _ImapBodyPart) else part.descriptor.byte_size
        )
        if expected_size > MAX_MESSAGE_BYTES:
            raise MailboxMessageInvalid(
                "imap_message_too_large", "Message content exceeds the safe size limit"
            )
        if isinstance(part, _ImapAttachment) and part.is_multipart and part.section:
            headers = cls._fetch_section_bytes(
                client, uid, f"{part.section}.MIME", MAX_HEADER_BYTES
            )
            prefix = _multipart_attachment_prefix(headers, part.descriptor.media_type)
            remaining = MAX_MESSAGE_BYTES - len(prefix)
            body = cls._fetch_section_bytes(client, uid, part.section, remaining)
            return prefix + body
        return cls._fetch_section_bytes(client, uid, part.section, byte_limit)

    def content(self, message_id: str, body_char_limit: int) -> MessageContent:
        with self._mailbox() as client:
            uid = self._checked_uid(client, message_id)
            catalog = self._catalog(client, uid)
            selected_parts = catalog.plain if catalog.plain else catalog.html
            if sum(part.byte_size for part in selected_parts) > MAX_MESSAGE_BYTES:
                raise MailboxMessageInvalid(
                    "imap_message_too_large", "Message content exceeds the safe size limit"
                )
            rendered: list[str] = []
            remaining_bytes = MAX_MESSAGE_BYTES
            for part in selected_parts:
                section = self._section_bytes(
                    client,
                    uid,
                    part,
                    byte_limit=remaining_bytes,
                )
                remaining_bytes -= len(section)
                decoded = _decoded_section(section, part.transfer_encoding)
                try:
                    text = decoded.decode(part.charset or "utf-8", errors="replace")
                except LookupError:
                    text = decoded.decode("utf-8", errors="replace")
                rendered.append(html_to_text(text) if part.media_type == "text/html" else text)
        selected = "\n\n".join(rendered)
        body = "\n".join(line.strip() for line in selected.splitlines() if line.strip())
        descriptors = tuple(attachment.descriptor for attachment in catalog.attachments)
        names = tuple(dict.fromkeys(item.filename for item in descriptors))
        return MessageContent(body[:body_char_limit], names, descriptors)

    def attachment_bytes(self, message_id: str, part_id: str, attachment_id: str | None) -> bytes:
        if attachment_id is not None or not part_id.startswith("mime-"):
            raise MailboxMessageUnavailable("The mail server attachment identity is invalid")
        try:
            selected = int(part_id.removeprefix("mime-"))
        except ValueError as exc:
            raise MailboxMessageUnavailable(
                "The mail server attachment identity is invalid"
            ) from exc
        with self._mailbox() as client:
            uid = self._checked_uid(client, message_id)
            catalog = self._catalog(client, uid)
            if selected < 0 or selected >= len(catalog.attachments):
                raise MailboxMessageUnavailable("The mail server attachment is no longer available")
            attachment = catalog.attachments[selected]
            payload = self._section_bytes(client, uid, attachment)
        if attachment.is_multipart:
            return payload
        return _decoded_section(payload, attachment.transfer_encoding)
