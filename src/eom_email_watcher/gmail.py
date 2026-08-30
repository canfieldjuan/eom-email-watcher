from __future__ import annotations

import base64
import binascii
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from filelock import FileLock
from filelock import Timeout as FileLockTimeout
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow, WSGITimeoutError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import normalize_address

SCOPES = ("https://www.googleapis.com/auth/gmail.readonly",)
TOKEN_LOCK_TIMEOUT_SECONDS = 30
GMAIL_AUTHORIZATION_TIMEOUT_SECONDS = 300
BUNDLED_GOOGLE_OAUTH_CLIENT = Path("eom_email_watcher_data/google-oauth-client.json")


class GmailError(RuntimeError):
    """Gmail operation failed."""


class GmailAuthorizationRejected(GmailError):
    """Gmail rejected credentials that appeared usable locally."""


class StaleHistoryCursor(GmailError):
    """The saved Gmail history cursor has expired."""


class MessageUnavailable(GmailError):
    """A specific message could not be fetched -- e.g. it was deleted or
    expunged after the history event that referenced it. Recoverable: the
    caller should skip this one message, not fail the whole run."""


def resolve_gmail_credentials_file(configured_file: Path) -> Path:
    """Prefer an explicit operator file, then a packaged desktop client."""
    if configured_file.is_file():
        return configured_file
    bundle_root = getattr(sys, "_MEIPASS", None)
    if isinstance(bundle_root, str):
        bundled_file = Path(bundle_root) / BUNDLED_GOOGLE_OAUTH_CLIENT
        if bundled_file.is_file():
            return bundled_file
    return configured_file


def gmail_credentials_configured(configured_file: Path) -> bool:
    return resolve_gmail_credentials_file(configured_file).is_file()


@dataclass(frozen=True)
class MessageMetadata:
    message_id: str
    thread_id: str | None
    sender: str
    sender_name: str | None
    subject: str
    received_at: str
    labels: frozenset[str]


def _headers(payload: dict[str, Any]) -> dict[str, str]:
    return {
        str(item.get("name", "")).casefold(): str(item.get("value", ""))
        for item in payload.get("headers") or []
        if isinstance(item, dict)
    }


def _received_at(message: dict[str, Any], headers: dict[str, str]) -> str:
    millis = message.get("internalDate")
    if millis:
        try:
            return datetime.fromtimestamp(int(millis) / 1000, tz=UTC).isoformat()
        except (TypeError, ValueError, OSError):
            pass
    try:
        parsed = parsedate_to_datetime(headers.get("date", ""))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat()
    except (TypeError, ValueError):
        return datetime.now(UTC).isoformat()


def parse_metadata(message: dict[str, Any]) -> MessageMetadata:
    payload = message.get("payload") or {}
    headers = _headers(payload)
    raw_from = headers.get("from", "")
    from email.utils import parseaddr

    sender_name, _address = parseaddr(raw_from)
    return MessageMetadata(
        message_id=str(message["id"]),
        thread_id=str(message["threadId"]) if message.get("threadId") else None,
        sender=normalize_address(raw_from),
        sender_name=sender_name.strip() or None,
        subject=headers.get("subject", "(no subject)").strip() or "(no subject)",
        received_at=_received_at(message, headers),
        labels=frozenset(str(label) for label in message.get("labelIds") or []),
    )


def _decode_attachment_data(value: object) -> bytes:
    if not isinstance(value, str):
        raise GmailError("Gmail attachment response did not contain data")
    try:
        return base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
    except (binascii.Error, ValueError) as exc:
        raise GmailError("Gmail attachment response contained invalid data") from exc


def _find_part(payload: dict[str, Any], part_id: str) -> dict[str, Any] | None:
    candidate = payload.get("partId")
    if isinstance(candidate, str) and candidate.strip() == part_id:
        return payload
    for child in payload.get("parts") or []:
        if isinstance(child, dict):
            found = _find_part(child, part_id)
            if found is not None:
                return found
    return None


class GmailGateway:
    def __init__(self, service: Any):
        self.service = service

    @classmethod
    def from_token(cls, credentials_file: Path, token_file: Path) -> GmailGateway:
        resolved_credentials_file = resolve_gmail_credentials_file(credentials_file)
        if not resolved_credentials_file.is_file():
            raise GmailError(
                f"OAuth desktop credentials not found: {credentials_file}. "
                "Download them from Google Cloud Console after enabling Gmail API."
            )
        token_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            with FileLock(f"{token_file}.lock", timeout=TOKEN_LOCK_TIMEOUT_SECONDS):
                credentials: Credentials | None = None
                if token_file.exists():
                    try:
                        credentials = Credentials.from_authorized_user_file(
                            str(token_file), SCOPES
                        )
                    except (ValueError, json.JSONDecodeError) as exc:
                        raise GmailError(f"Invalid OAuth token file: {token_file}") from exc
                if credentials and credentials.expired and credentials.refresh_token:
                    credentials.refresh(Request())
                    token_file.write_text(credentials.to_json(), encoding="utf-8")
                    token_file.chmod(0o600)
                if not credentials or not credentials.valid:
                    raise GmailError("Gmail is not authorized. Run: eom-mail-watch setup")
        except FileLockTimeout as exc:
            raise GmailError("Gmail token is busy; retry the operation") from exc
        return cls(build("gmail", "v1", credentials=credentials, cache_discovery=False))

    @classmethod
    def authorize(cls, credentials_file: Path, token_file: Path) -> GmailGateway:
        gateway, _authorization_changed = cls.authorize_with_status(
            credentials_file, token_file
        )
        return gateway

    @classmethod
    def authorize_with_status(
        cls,
        credentials_file: Path,
        token_file: Path,
        *,
        force_reauthorize: bool = False,
    ) -> tuple[GmailGateway, bool]:
        resolved_credentials_file = resolve_gmail_credentials_file(credentials_file)
        if not resolved_credentials_file.is_file():
            raise GmailError(
                f"OAuth desktop credentials not found: {credentials_file}. "
                "Enable Gmail API and place the downloaded JSON at that path."
            )
        token_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            with FileLock(f"{token_file}.lock", timeout=TOKEN_LOCK_TIMEOUT_SECONDS):
                credentials: Credentials | None = None
                if token_file.exists() and not force_reauthorize:
                    try:
                        credentials = Credentials.from_authorized_user_file(
                            str(token_file), SCOPES
                        )
                    except (ValueError, json.JSONDecodeError):
                        credentials = None
                    if credentials and credentials.expired and credentials.refresh_token:
                        try:
                            credentials.refresh(Request())
                        except RefreshError:
                            credentials = None
                        else:
                            token_file.write_text(credentials.to_json(), encoding="utf-8")
                            token_file.chmod(0o600)
                authorization_changed = not credentials or not credentials.valid
                if authorization_changed:
                    flow = InstalledAppFlow.from_client_secrets_file(
                        str(resolved_credentials_file), SCOPES
                    )
                    try:
                        credentials = flow.run_local_server(
                            host="127.0.0.1",
                            port=0,
                            authorization_prompt_message=None,
                            open_browser=True,
                            prompt="consent",
                            timeout_seconds=GMAIL_AUTHORIZATION_TIMEOUT_SECONDS,
                        )
                    except WSGITimeoutError as exc:
                        raise GmailError(
                            "Gmail authorization timed out; retry setup"
                        ) from exc
                    token_file.write_text(credentials.to_json(), encoding="utf-8")
                    token_file.chmod(0o600)
        except FileLockTimeout as exc:
            raise GmailError("Gmail token is busy; retry setup") from exc
        return (
            cls(build("gmail", "v1", credentials=credentials, cache_discovery=False)),
            authorization_changed,
        )

    def profile_history_id(self) -> str:
        try:
            result = self.service.users().getProfile(userId="me").execute()
        except HttpError as exc:
            if getattr(exc.resp, "status", None) == 401:
                raise GmailAuthorizationRejected(
                    "Gmail rejected the configured authorization"
                ) from exc
            raise GmailError(f"Gmail profile request failed (HTTP {exc.resp.status})") from exc
        return str(result["historyId"])

    def history_message_ids(self, start_history_id: str) -> tuple[list[str], str]:
        ids: list[str] = []
        page_token: str | None = None
        newest = start_history_id
        try:
            while True:
                request = (
                    self.service.users()
                    .history()
                    .list(
                        userId="me",
                        startHistoryId=start_history_id,
                        historyTypes=["messageAdded"],
                        labelId="INBOX",
                        pageToken=page_token,
                        maxResults=500,
                    )
                )
                response = request.execute()
                newest = str(response.get("historyId", newest))
                for event in response.get("history") or []:
                    for added in event.get("messagesAdded") or []:
                        message = added.get("message") or {}
                        if message.get("id"):
                            ids.append(str(message["id"]))
                page_token = response.get("nextPageToken")
                if not page_token:
                    break
        except HttpError as exc:
            if getattr(exc.resp, "status", None) == 404:
                raise StaleHistoryCursor(
                    "Saved Gmail history cursor is no longer available"
                ) from exc
            raise GmailError(f"Gmail history request failed (HTTP {exc.resp.status})") from exc
        return list(dict.fromkeys(ids)), newest

    def metadata(self, message_id: str) -> MessageMetadata:
        try:
            message = (
                self.service.users()
                .messages()
                .get(
                    userId="me",
                    id=message_id,
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                )
                .execute()
            )
        except HttpError as exc:
            if getattr(exc.resp, "status", None) == 404:
                raise MessageUnavailable(
                    f"Gmail message {message_id} unavailable (HTTP 404)"
                ) from exc
            raise GmailError(f"Gmail metadata fetch failed (HTTP {exc.resp.status})") from exc
        return parse_metadata(message)

    def full_payload(self, message_id: str) -> dict[str, Any]:
        try:
            message = (
                self.service.users()
                .messages()
                .get(userId="me", id=message_id, format="full")
                .execute()
            )
        except HttpError as exc:
            if getattr(exc.resp, "status", None) == 404:
                raise MessageUnavailable(
                    f"Gmail message {message_id} unavailable (HTTP 404)"
                ) from exc
            raise GmailError(f"Gmail body fetch failed (HTTP {exc.resp.status})") from exc
        return message.get("payload") or {}

    def attachment_bytes(
        self, message_id: str, part_id: str, attachment_id: str | None
    ) -> bytes:
        if attachment_id:
            try:
                response = (
                    self.service.users()
                    .messages()
                    .attachments()
                    .get(userId="me", messageId=message_id, id=attachment_id)
                    .execute()
                )
            except HttpError as exc:
                if getattr(exc.resp, "status", None) == 404:
                    raise MessageUnavailable(
                        f"Gmail message {message_id} attachment unavailable (HTTP 404)"
                    ) from exc
                raise GmailError(
                    f"Gmail attachment fetch failed (HTTP {exc.resp.status})"
                ) from exc
            return _decode_attachment_data(response.get("data"))

        part = _find_part(self.full_payload(message_id), part_id)
        if part is None:
            raise MessageUnavailable("Gmail attachment part is no longer available")
        body = part.get("body") or {}
        if not isinstance(body, dict):
            raise GmailError("Gmail attachment part contained an invalid body")
        inline_data = body.get("data")
        if isinstance(inline_data, str):
            return _decode_attachment_data(inline_data)
        current_attachment_id = body.get("attachmentId")
        if isinstance(current_attachment_id, str) and current_attachment_id.strip():
            return self.attachment_bytes(
                message_id, part_id, current_attachment_id.strip()
            )
        raise GmailError("Gmail attachment part did not contain retrievable data")

    def search_since(self, addresses: frozenset[str], since: datetime) -> list[str]:
        sender_terms = " ".join(f"from:{address}" for address in sorted(addresses))
        query = f"in:inbox {{{sender_terms}}} after:{int(since.timestamp())}"
        ids: list[str] = []
        page_token: str | None = None
        while True:
            try:
                response = (
                    self.service.users()
                    .messages()
                    .list(userId="me", q=query, pageToken=page_token, maxResults=500)
                    .execute()
                )
            except HttpError as exc:
                raise GmailError(f"Gmail recovery search failed (HTTP {exc.resp.status})") from exc
            ids.extend(str(item["id"]) for item in response.get("messages") or [])
            page_token = response.get("nextPageToken")
            if not page_token:
                return list(dict.fromkeys(ids))
