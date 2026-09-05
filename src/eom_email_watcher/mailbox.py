from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .mime import AttachmentDescriptor

DEFAULT_MAIL_PROVIDER = "gmail"
DEFAULT_MAIL_ACCOUNT_ID = "gmail-default"


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


@dataclass(frozen=True)
class MessageContent:
    body: str
    attachment_names: tuple[str, ...]
    attachments: tuple[AttachmentDescriptor, ...]


class MailboxGateway(Protocol):
    def initial_cursor(self) -> str: ...

    def changes_since(self, cursor: str) -> MailboxChanges: ...

    def recover_since(self, addresses: frozenset[str], since: datetime) -> MailboxChanges: ...

    def metadata(self, message_id: str) -> MessageMetadata: ...

    def content(self, message_id: str, body_char_limit: int) -> MessageContent: ...

    def attachment_bytes(
        self, message_id: str, part_id: str, attachment_id: str | None
    ) -> bytes: ...


def default_mailbox_session(gateway: MailboxGateway) -> MailboxSession:
    return MailboxSession(DEFAULT_MAIL_PROVIDER, DEFAULT_MAIL_ACCOUNT_ID, gateway)


def scoped_message_id(provider: str, account_id: str, provider_message_id: str) -> str:
    """Return a stable local identifier for a provider-owned message."""
    if not provider or not account_id or not provider_message_id:
        raise ValueError("Mailbox message identity must be complete")
    # Preserve the public/local identifiers already emitted by the single-account
    # Gmail application. Additional accounts receive a namespaced local identity.
    if provider == DEFAULT_MAIL_PROVIDER and account_id == DEFAULT_MAIL_ACCOUNT_ID:
        return provider_message_id
    encoded = "\0".join((provider, account_id, provider_message_id)).encode("utf-8")
    return f"mail-{hashlib.sha256(encoded).hexdigest()}"
