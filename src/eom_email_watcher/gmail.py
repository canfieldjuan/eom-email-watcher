from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import normalize_address

SCOPES = ("https://www.googleapis.com/auth/gmail.readonly",)


class GmailError(RuntimeError):
    """Gmail operation failed."""


class StaleHistoryCursor(GmailError):
    """The saved Gmail history cursor has expired."""


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


class GmailGateway:
    def __init__(self, service: Any):
        self.service = service

    @classmethod
    def from_token(cls, credentials_file: Path, token_file: Path) -> GmailGateway:
        if not credentials_file.exists():
            raise GmailError(
                f"OAuth desktop credentials not found: {credentials_file}. "
                "Download them from Google Cloud Console after enabling Gmail API."
            )
        credentials: Credentials | None = None
        if token_file.exists():
            try:
                credentials = Credentials.from_authorized_user_file(str(token_file), SCOPES)
            except (ValueError, json.JSONDecodeError) as exc:
                raise GmailError(f"Invalid OAuth token file: {token_file}") from exc
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            token_file.write_text(credentials.to_json(), encoding="utf-8")
            token_file.chmod(0o600)
        if not credentials or not credentials.valid:
            raise GmailError("Gmail is not authorized. Run: eom-mail-watch setup")
        return cls(build("gmail", "v1", credentials=credentials, cache_discovery=False))

    @classmethod
    def authorize(cls, credentials_file: Path, token_file: Path) -> GmailGateway:
        if not credentials_file.exists():
            raise GmailError(
                f"OAuth desktop credentials not found: {credentials_file}. "
                "Enable Gmail API and place the downloaded JSON at that path."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), SCOPES)
        credentials = flow.run_local_server(host="127.0.0.1", port=0, open_browser=True)
        token_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        token_file.write_text(credentials.to_json(), encoding="utf-8")
        token_file.chmod(0o600)
        return cls(build("gmail", "v1", credentials=credentials, cache_discovery=False))

    def profile_history_id(self) -> str:
        result = self.service.users().getProfile(userId="me").execute()
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
            raise GmailError(f"Gmail body fetch failed (HTTP {exc.resp.status})") from exc
        return message.get("payload") or {}

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
