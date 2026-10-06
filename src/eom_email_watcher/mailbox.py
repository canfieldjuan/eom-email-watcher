from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .mime import AttachmentDescriptor

DEFAULT_MAIL_PROVIDER = "gmail"
DEFAULT_MAIL_ACCOUNT_ID = "gmail-default"
# RFC 5322 line limit; longer message ids are malformed and dropped.
MAX_MESSAGE_ID_CHARS = 998
MAX_REFERENCES_IDS = 64
_BRACKETED_ID_RE = re.compile(r"<([^<>\s]+)>")
# RFC 5322 msg-id without its brackets: left@right, no whitespace, controls, or brackets.
_MESSAGE_ID_RE = re.compile(r"[^\s<>\x00-\x1f\x7f]+@[^\s<>\x00-\x1f\x7f]+")


class MailboxError(RuntimeError):
    """A mailbox provider operation failed."""


class StaleMailboxCursor(MailboxError):
    """The provider can no longer continue from a saved change cursor."""


class MailboxMessageUnavailable(MailboxError):
    """A source message disappeared after it was discovered."""


class MailboxMessageInvalid(MailboxError):
    """A source message cannot be processed and must not retry forever."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class MailboxAccountUnavailable(MailboxError):
    """The selected local mailbox account cannot currently be opened."""


@dataclass(frozen=True)
class MailboxSession:
    provider: str
    account_id: str
    gateway: MailboxGateway
    identity_key: str | None = None


@dataclass(frozen=True)
class MailboxChanges:
    message_ids: tuple[str, ...]
    cursor: str


@dataclass(frozen=True)
class MessageMetadata:
    message_id: str
    thread_id: str | None
    sender: str
    sender_name: str | None
    subject: str
    received_at: str
    labels: frozenset[str]
    # RFC 5322 identity for thread keys (contract D-identity); providers that
    # supply their own thread id leave these empty.
    rfc_message_id: str | None = None
    reply_ids: tuple[str, ...] = ()


def normalize_message_id(value: object) -> str | None:
    """Return one RFC 5322 message id without angle brackets, or None if malformed."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.startswith("<") and text.endswith(">"):
        text = text[1:-1].strip()
    if len(text) > MAX_MESSAGE_ID_CHARS or _MESSAGE_ID_RE.fullmatch(text) is None:
        return None
    return text


def message_id_list(value: object, limit: int | None = None) -> tuple[str, ...]:
    """Return the distinct message ids of an In-Reply-To or References value, in order."""
    if not isinstance(value, str):
        return ()
    found = _BRACKETED_ID_RE.findall(value) or value.split()
    ids = tuple(dict.fromkeys(i for i in map(normalize_message_id, found) if i is not None))
    return ids if limit is None else ids[:limit]


@dataclass(frozen=True)
class MessageContent:
    body: str
    attachment_names: tuple[str, ...]
    attachments: tuple[AttachmentDescriptor, ...]
    body_source_chars: int


class MailboxGateway(Protocol):
    def set_operation_timeout(self, timeout_seconds: float) -> None: ...

    def mailbox_address(self) -> str: ...

    def mailbox_identity_key(self) -> str: ...

    def initial_cursor(self) -> str: ...

    def changes_since(self, cursor: str) -> MailboxChanges: ...

    def recover_since(self, addresses: frozenset[str], since: datetime) -> MailboxChanges: ...

    def metadata(self, message_id: str) -> MessageMetadata: ...

    def content(self, message_id: str, body_char_limit: int) -> MessageContent: ...

    def attachment_bytes(
        self, message_id: str, part_id: str, attachment_id: str | None
    ) -> bytes: ...


def validate_operation_timeout(timeout_seconds: float) -> float:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("Mailbox operation timeout must be a positive finite number")
    return float(timeout_seconds)


@contextmanager
def mailbox_polling_session(gateway: MailboxGateway) -> Iterator[None]:
    """Use a provider's optional poll-scoped transport without imposing it on all adapters."""
    provider_session = getattr(gateway, "polling_session", None)
    with provider_session() if callable(provider_session) else nullcontext():
        yield


def default_mailbox_session(gateway: MailboxGateway) -> MailboxSession:
    return MailboxSession(DEFAULT_MAIL_PROVIDER, DEFAULT_MAIL_ACCOUNT_ID, gateway)


def mailbox_session_identity_key(session: MailboxSession) -> str:
    """Resolve and validate the credential-backed identity of an open gateway."""
    key = session.identity_key
    if key is None:
        resolver = getattr(session.gateway, "mailbox_identity_key", None)
        if not callable(resolver):
            raise MailboxAccountUnavailable(
                "The selected email provider cannot prove its mailbox identity"
            )
        key = resolver()
    if (
        not isinstance(key, str)
        or len(key) != 64
        or any(character not in "0123456789abcdef" for character in key)
    ):
        raise MailboxAccountUnavailable(
            "The selected email provider returned an invalid mailbox identity"
        )
    return key


def mailbox_session_address(session: MailboxSession) -> str | None:
    """Return a supported adapter's authenticated mailbox address when exposed."""
    resolver = getattr(session.gateway, "mailbox_address", None)
    if not callable(resolver):
        return None
    address = resolver()
    if not isinstance(address, str) or not address.strip():
        raise MailboxAccountUnavailable(
            "The selected email provider returned an invalid mailbox address"
        )
    return address.strip().casefold()


def scoped_message_id(
    provider: str,
    account_id: str,
    provider_message_id: str,
    mailbox_identity_key: str | None = None,
) -> str:
    """Return a stable local identifier for a provider-owned message."""
    if not provider or not account_id or not provider_message_id:
        raise ValueError("Mailbox message identity must be complete")
    # Preserve the public/local identifiers already emitted by the single-account
    # Gmail application. Additional accounts receive a namespaced local identity.
    if (
        mailbox_identity_key is None
        and provider == DEFAULT_MAIL_PROVIDER
        and account_id == DEFAULT_MAIL_ACCOUNT_ID
    ):
        return provider_message_id
    identity = (
        (provider, account_id, mailbox_identity_key, provider_message_id)
        if mailbox_identity_key is not None
        else (provider, account_id, provider_message_id)
    )
    encoded = "\0".join(identity).encode("utf-8")
    return f"mail-{hashlib.sha256(encoded).hexdigest()}"
