from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import sys
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Literal

import httplib2
from filelock import FileLock
from filelock import Timeout as FileLockTimeout
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from google_auth_oauthlib.flow import InstalledAppFlow, WSGITimeoutError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import normalize_address
from .mailbox import (
    MailboxChanges,
    MailboxError,
    MailboxMessageUnavailable,
    MessageContent,
    MessageMetadata,
    StaleMailboxCursor,
    validate_operation_timeout,
)
from .mime import extract_body

SCOPES = ("https://www.googleapis.com/auth/gmail.readonly",)
TOKEN_LOCK_TIMEOUT_SECONDS = 30
GMAIL_AUTHORIZATION_TIMEOUT_SECONDS = 300
# Each unique history ID requires a metadata request before exact sender gating.
MAX_INCREMENTAL_MESSAGE_IDS = 200
MAX_HISTORY_CONTINUATION_BYTES = 4096
MAX_HISTORY_CONTINUATION_OFFSET = 50_000
HISTORY_CONTINUATION_PREFIX = "eom-gmail-history-v2:"
LEGACY_HISTORY_CONTINUATION_PREFIX = "eom-gmail-history-v1:"
MAX_GMAIL_LABEL_CATALOG_BYTES = 1_048_576
MAX_GMAIL_LABEL_COUNT = 10_000
MAX_GMAIL_LABEL_ID_BYTES = 512
MAX_GMAIL_LABEL_NAME_BYTES = 1_024
MAX_GMAIL_RECOVERY_PAGE_IDS = 200
MAX_GMAIL_PAGE_TOKEN_BYTES = 8_192
GMAIL_LABELS_URL = "https://gmail.googleapis.com/gmail/v1/users/me/labels"
BUNDLED_GOOGLE_OAUTH_CLIENT = Path("eom_email_watcher_data/google-oauth-client.json")


class GmailError(MailboxError):
    """Gmail operation failed."""


class GmailAuthorizationRejected(GmailError):
    """Gmail rejected credentials that appeared usable locally."""


class GmailLabelCatalogInvalid(GmailError):
    """Gmail returned a label catalog that cannot safely be used."""

    code = "gmail_label_catalog_invalid"


class GmailLabelCatalogUnavailable(GmailError):
    """The complete Gmail label catalog could not be fetched."""

    code = "gmail_label_catalog_unavailable"


class GmailRecoveryPageInvalid(GmailError):
    """Gmail returned a recovery page that cannot safely be persisted."""

    code = "gmail_recovery_page_invalid"


class GmailRecoveryPageTokenInvalid(GmailError):
    """Gmail rejected the stored recovery page token."""

    code = "gmail_recovery_page_token_invalid"


class StaleHistoryCursor(GmailError, StaleMailboxCursor):
    """The saved Gmail history cursor requires bounded durable recovery."""


class MessageUnavailable(GmailError, MailboxMessageUnavailable):
    """A specific message could not be fetched -- e.g. it was deleted or
    expunged after the history event that referenced it. Recoverable: the
    caller should skip this one message, not fail the whole run."""


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_non_json_numeric_constant(value: str) -> object:
    raise ValueError(f"non-JSON numeric constant: {value}")


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _contains_control(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def _bounded_text(
    value: object,
    *,
    maximum_bytes: int,
    field: str,
    error_type: type[GmailError],
) -> str:
    if not isinstance(value, str) or not value or _contains_control(value):
        raise error_type(f"{field} is invalid")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise error_type(f"{field} is invalid") from exc
    if len(encoded) > maximum_bytes:
        raise error_type(f"{field} is invalid")
    return value


def _history_prefix_digest(message_ids: list[str]) -> str:
    return hashlib.sha256(_canonical_json_bytes(message_ids)).hexdigest()


def _decode_history_cursor(cursor: str) -> tuple[str, int, str | None]:
    if not isinstance(cursor, str) or not cursor:
        raise StaleHistoryCursor("Saved Gmail history cursor is invalid")
    if cursor.startswith(LEGACY_HISTORY_CONTINUATION_PREFIX):
        raise StaleHistoryCursor("Saved Gmail history continuation uses the retired stream")
    if not cursor.startswith(HISTORY_CONTINUATION_PREFIX):
        if not cursor.isdecimal() or len(cursor.encode("utf-8")) > MAX_HISTORY_CONTINUATION_BYTES:
            raise StaleHistoryCursor("Saved Gmail history cursor is invalid")
        return cursor, 0, None
    if len(cursor.encode("utf-8")) > MAX_HISTORY_CONTINUATION_BYTES:
        raise StaleHistoryCursor("Saved Gmail history continuation cursor is invalid")
    encoded = cursor.removeprefix(HISTORY_CONTINUATION_PREFIX)
    try:
        payload = json.loads(encoded, object_pairs_hook=_reject_duplicate_json_keys)
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise StaleHistoryCursor("Saved Gmail history continuation cursor is invalid") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "prefix_sha256",
        "returned",
        "start_history_id",
    }:
        raise StaleHistoryCursor("Saved Gmail history continuation cursor is invalid")
    start_history_id = payload["start_history_id"]
    offset = payload["returned"]
    digest = payload["prefix_sha256"]
    if (
        not isinstance(start_history_id, str)
        or not start_history_id.isdecimal()
        or isinstance(offset, bool)
        or not isinstance(offset, int)
        or not 0 <= offset <= MAX_HISTORY_CONTINUATION_OFFSET
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise StaleHistoryCursor("Saved Gmail history continuation cursor is invalid")
    if offset == 0 and digest != _history_prefix_digest([]):
        raise StaleHistoryCursor("Saved Gmail history continuation prefix changed")
    return start_history_id, offset, digest


def _history_continuation_cursor(start_history_id: str, message_ids: list[str]) -> str:
    offset = len(message_ids)
    if offset > MAX_HISTORY_CONTINUATION_OFFSET:
        raise StaleHistoryCursor("Saved Gmail history continuation exceeded replay bounds")
    payload = {
        "prefix_sha256": _history_prefix_digest(message_ids),
        "returned": offset,
        "start_history_id": start_history_id,
    }
    cursor = HISTORY_CONTINUATION_PREFIX + _canonical_json_bytes(payload).decode("utf-8")
    if len(cursor.encode("utf-8")) > MAX_HISTORY_CONTINUATION_BYTES:
        raise StaleHistoryCursor("Saved Gmail history continuation exceeded transport bounds")
    return cursor


@dataclass(frozen=True)
class GmailProfile:
    email_address: str
    history_id: str


@dataclass(frozen=True)
class GmailLabel:
    label_id: str
    display_name: str
    label_type: Literal["user", "system"]


def decode_gmail_label_catalog(
    body: bytes,
    *,
    content_length: object = None,
) -> tuple[GmailLabel, ...]:
    """Decode one complete bounded Gmail labels response without partial results."""
    string_length_over_cap = (
        isinstance(content_length, str)
        and content_length.strip().isdecimal()
        and int(content_length.strip()) > MAX_GMAIL_LABEL_CATALOG_BYTES
    )
    integer_length_over_cap = (
        isinstance(content_length, int)
        and not isinstance(content_length, bool)
        and content_length > MAX_GMAIL_LABEL_CATALOG_BYTES
    )
    if string_length_over_cap or integer_length_over_cap:
        raise GmailLabelCatalogInvalid("gmail_label_catalog_invalid: response is too large")
    if not isinstance(body, bytes) or len(body) > MAX_GMAIL_LABEL_CATALOG_BYTES:
        raise GmailLabelCatalogInvalid("gmail_label_catalog_invalid: response is too large")
    try:
        document = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_non_json_numeric_constant,
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise GmailLabelCatalogInvalid("gmail_label_catalog_invalid: malformed JSON") from exc
    if not isinstance(document, dict) or not isinstance(document.get("labels"), list):
        raise GmailLabelCatalogInvalid("gmail_label_catalog_invalid: labels are missing")
    raw_labels = document["labels"]
    if len(raw_labels) > MAX_GMAIL_LABEL_COUNT:
        raise GmailLabelCatalogInvalid("gmail_label_catalog_invalid: too many labels")
    labels: list[GmailLabel] = []
    seen_ids: set[str] = set()
    for item in raw_labels:
        if not isinstance(item, dict):
            raise GmailLabelCatalogInvalid("gmail_label_catalog_invalid: malformed label")
        label_id = _bounded_text(
            item.get("id"),
            maximum_bytes=MAX_GMAIL_LABEL_ID_BYTES,
            field="Gmail label ID",
            error_type=GmailLabelCatalogInvalid,
        )
        display_name = _bounded_text(
            item.get("name"),
            maximum_bytes=MAX_GMAIL_LABEL_NAME_BYTES,
            field="Gmail label name",
            error_type=GmailLabelCatalogInvalid,
        )
        label_type = item.get("type")
        if label_type not in ("user", "system"):
            raise GmailLabelCatalogInvalid("gmail_label_catalog_invalid: unknown label type")
        if label_id in seen_ids:
            raise GmailLabelCatalogInvalid("gmail_label_catalog_invalid: duplicate label ID")
        seen_ids.add(label_id)
        labels.append(GmailLabel(label_id, display_name, label_type))
    labels.sort(key=lambda label: label.label_id)
    normalized = {
        "labels": [
            {"id": label.label_id, "name": label.display_name, "type": label.label_type}
            for label in labels
        ]
    }
    if len(_canonical_json_bytes(normalized)) > MAX_GMAIL_LABEL_CATALOG_BYTES:
        raise GmailLabelCatalogInvalid(
            "gmail_label_catalog_invalid: normalized response is too large"
        )
    return tuple(labels)


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
    except (OverflowError, TypeError, ValueError):
        # Keep malformed source time invalid so retention admission rejects it.
        return ""


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
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
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
    def __init__(
        self,
        service: Any,
        mailbox_identity_key: str | None = None,
        credentials: Credentials | None = None,
    ):
        self.service = service
        self._mailbox_identity_key = mailbox_identity_key
        self._credentials = credentials

    def _execute_request(
        self,
        request: Any,
        *,
        timeout_seconds: float | None = None,
    ) -> Any:
        if timeout_seconds is None:
            return request.execute()
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("Gmail request timeout must be a positive finite number")
        if self._credentials is None:
            return request.execute()
        transport = AuthorizedHttp(
            self._credentials,
            http=httplib2.Http(timeout=float(timeout_seconds)),
        )
        return request.execute(http=transport)

    @staticmethod
    def _credential_identity(credentials: Credentials) -> str:
        refresh_token = credentials.refresh_token
        if not isinstance(refresh_token, str) or not refresh_token:
            raise GmailAuthorizationRejected(
                "Gmail authorization does not contain a durable refresh token"
            )
        value = "\0".join(("gmail-credential-v1", refresh_token)).encode("utf-8")
        return hashlib.sha256(value).hexdigest()

    def mailbox_identity_key(self) -> str:
        if self._mailbox_identity_key is None:
            raise GmailAuthorizationRejected("Gmail mailbox identity is unavailable")
        return self._mailbox_identity_key

    def set_operation_timeout(self, timeout_seconds: float) -> None:
        timeout = validate_operation_timeout(timeout_seconds)
        authorized_http = getattr(self.service, "_http", None)
        transport = getattr(authorized_http, "http", authorized_http)
        if transport is None or not hasattr(transport, "timeout"):
            raise GmailError("Gmail transport timeout cannot be configured")
        transport.timeout = timeout

    def mailbox_address(self) -> str:
        return self.profile().email_address

    @classmethod
    def from_token(
        cls,
        credentials_file: Path,
        token_file: Path,
        remaining_timeout: Callable[[], float] | None = None,
    ) -> GmailGateway:
        operation_timeout = (
            validate_operation_timeout(remaining_timeout())
            if remaining_timeout is not None
            else None
        )
        token_lock_timeout = (
            min(float(TOKEN_LOCK_TIMEOUT_SECONDS), operation_timeout)
            if operation_timeout is not None
            else TOKEN_LOCK_TIMEOUT_SECONDS
        )
        resolved_credentials_file = resolve_gmail_credentials_file(credentials_file)
        if not resolved_credentials_file.is_file():
            raise GmailError(
                f"OAuth desktop credentials not found: {credentials_file}. "
                "Download them from Google Cloud Console after enabling Gmail API."
            )
        token_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            with FileLock(f"{token_file}.lock", timeout=token_lock_timeout):
                credentials: Credentials | None = None
                if token_file.exists():
                    try:
                        credentials = Credentials.from_authorized_user_file(str(token_file), SCOPES)
                    except (ValueError, json.JSONDecodeError) as exc:
                        raise GmailAuthorizationRejected(
                            f"Invalid OAuth token file: {token_file}"
                        ) from exc
                if credentials and credentials.expired and credentials.refresh_token:
                    try:
                        refresh_request = Request()
                        if remaining_timeout is not None:
                            refresh_timeout = validate_operation_timeout(remaining_timeout())
                            unbounded_request = refresh_request

                            def refresh_request(*args: Any, **kwargs: Any) -> Any:
                                try:
                                    requested_timeout = validate_operation_timeout(
                                        kwargs.get("timeout")
                                    )
                                except ValueError:
                                    requested_timeout = refresh_timeout
                                kwargs["timeout"] = min(
                                    requested_timeout,
                                    refresh_timeout,
                                )
                                return unbounded_request(*args, **kwargs)

                        credentials.refresh(refresh_request)
                    except RefreshError as exc:
                        if exc.retryable:
                            raise GmailError("Gmail authorization refresh failed; retry") from exc
                        raise GmailAuthorizationRejected(
                            "Gmail rejected the configured authorization"
                        ) from exc
                    token_file.write_text(credentials.to_json(), encoding="utf-8")
                    token_file.chmod(0o600)
                if not credentials or not credentials.valid:
                    raise GmailAuthorizationRejected(
                        "Gmail is not authorized. Run: eom-mail-watch setup"
                    )
        except FileLockTimeout as exc:
            raise GmailError("Gmail token is busy; retry the operation") from exc
        assert credentials is not None
        identity_key = cls._credential_identity(credentials)
        gateway = cls(
            build("gmail", "v1", credentials=credentials, cache_discovery=False),
            identity_key,
            credentials,
        )
        if remaining_timeout is not None:
            gateway.set_operation_timeout(
                validate_operation_timeout(remaining_timeout())
            )
        return gateway

    @classmethod
    def authorize(cls, credentials_file: Path, token_file: Path) -> GmailGateway:
        gateway, _authorization_changed = cls.authorize_with_status(credentials_file, token_file)
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
                        credentials = Credentials.from_authorized_user_file(str(token_file), SCOPES)
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
                        raise GmailError("Gmail authorization timed out; retry setup") from exc
                    token_file.write_text(credentials.to_json(), encoding="utf-8")
                    token_file.chmod(0o600)
        except FileLockTimeout as exc:
            raise GmailError("Gmail token is busy; retry setup") from exc
        assert credentials is not None
        identity_key = cls._credential_identity(credentials)
        return (
            cls(
                build("gmail", "v1", credentials=credentials, cache_discovery=False),
                identity_key,
                credentials,
            ),
            authorization_changed,
        )

    def profile(self) -> GmailProfile:
        try:
            result = self.service.users().getProfile(userId="me").execute()
        except HttpError as exc:
            if getattr(exc.resp, "status", None) == 401:
                raise GmailAuthorizationRejected(
                    "Gmail rejected the configured authorization"
                ) from exc
            raise GmailError(f"Gmail profile request failed (HTTP {exc.resp.status})") from exc
        email_address = normalize_address(str(result.get("emailAddress", "")))
        local, separator, domain = email_address.rpartition("@")
        if (
            separator != "@"
            or not local
            or not domain
            or "@" in local
            or any(
                character.isspace() or not character.isprintable() for character in email_address
            )
        ):
            raise GmailError("Gmail profile response did not contain an email address")
        history_id = str(result.get("historyId", "")).strip()
        if (
            not history_id.isdecimal()
            or len(history_id.encode("utf-8")) > MAX_HISTORY_CONTINUATION_BYTES
        ):
            raise GmailError("Gmail profile response did not contain a history cursor")
        return GmailProfile(email_address=email_address, history_id=history_id)

    def profile_history_id(self) -> str:
        return self.profile().history_id

    def initial_cursor(self) -> str:
        return self.profile_history_id()

    def label_catalog(self) -> tuple[GmailLabel, ...]:
        if self._credentials is None:
            raise GmailLabelCatalogUnavailable(
                "gmail_label_catalog_unavailable: authenticated transport is unavailable"
            )
        try:
            with AuthorizedSession(self._credentials) as session:
                response = session.get(GMAIL_LABELS_URL, stream=True, timeout=120)
                if response.status_code in (401, 403):
                    raise GmailAuthorizationRejected(
                        "Gmail rejected the configured authorization"
                    )
                if response.status_code >= 300:
                    raise GmailLabelCatalogUnavailable(
                        "gmail_label_catalog_unavailable: "
                        f"Gmail labels request failed (HTTP {response.status_code})"
                    )
                content_length = response.headers.get("Content-Length")
                if (
                    isinstance(content_length, str)
                    and content_length.strip().isdecimal()
                    and int(content_length.strip()) > MAX_GMAIL_LABEL_CATALOG_BYTES
                ):
                    raise GmailLabelCatalogInvalid(
                        "gmail_label_catalog_invalid: response is too large"
                    )
                body = bytearray()
                for chunk in response.iter_content(chunk_size=65_536):
                    if not chunk:
                        continue
                    remaining = MAX_GMAIL_LABEL_CATALOG_BYTES + 1 - len(body)
                    body.extend(chunk[:remaining])
                    if len(body) > MAX_GMAIL_LABEL_CATALOG_BYTES:
                        raise GmailLabelCatalogInvalid(
                            "gmail_label_catalog_invalid: response is too large"
                        )
                return decode_gmail_label_catalog(
                    bytes(body),
                    content_length=content_length,
                )
        except (GmailAuthorizationRejected, GmailLabelCatalogInvalid):
            raise
        except GmailLabelCatalogUnavailable:
            raise
        except Exception as exc:
            raise GmailLabelCatalogUnavailable(
                "gmail_label_catalog_unavailable: Gmail labels request failed"
            ) from exc

    def history_message_ids(self, start_history_id: str) -> tuple[list[str], str]:
        request_start_history_id, skip_unique_ids, expected_prefix_digest = (
            _decode_history_cursor(start_history_id)
        )
        ids: list[str] = []
        seen_ids: set[str] = set()
        ordered_unique_ids: list[str] = []
        page_token: str | None = None
        newest = request_start_history_id
        prefix_verified = skip_unique_ids == 0
        try:
            while True:
                request = (
                    self.service.users()
                    .history()
                    .list(
                        userId="me",
                        startHistoryId=request_start_history_id,
                        historyTypes=["messageAdded", "labelAdded"],
                        pageToken=page_token,
                        maxResults=500,
                    )
                )
                response = request.execute()
                if not isinstance(response, dict):
                    raise GmailError("Gmail history response is invalid")
                candidate_newest = response.get("historyId", newest)
                if not isinstance(candidate_newest, str) or not candidate_newest.isdecimal():
                    raise GmailError("Gmail history response contained an invalid cursor")
                newest = candidate_newest
                events = response.get("history", [])
                if not isinstance(events, list):
                    raise GmailError("Gmail history response is invalid")
                for event in events:
                    if not isinstance(event, dict):
                        raise GmailError("Gmail history response is invalid")
                    for event_field in ("messagesAdded", "labelsAdded"):
                        additions = event.get(event_field, [])
                        if not isinstance(additions, list):
                            raise GmailError("Gmail history response is invalid")
                        for added in additions:
                            message = added.get("message") if isinstance(added, dict) else None
                            message_id = _bounded_text(
                                message.get("id") if isinstance(message, dict) else None,
                                maximum_bytes=MAX_GMAIL_LABEL_ID_BYTES,
                                field="Gmail history message ID",
                                error_type=GmailError,
                            )
                            if message_id in seen_ids:
                                continue
                            seen_ids.add(message_id)
                            ordered_unique_ids.append(message_id)
                            unique_ids_seen = len(ordered_unique_ids)
                            if unique_ids_seen == skip_unique_ids:
                                actual_prefix_digest = _history_prefix_digest(ordered_unique_ids)
                                if actual_prefix_digest != expected_prefix_digest:
                                    raise StaleHistoryCursor(
                                        "Saved Gmail history continuation prefix changed"
                                    )
                                prefix_verified = True
                                continue
                            if unique_ids_seen < skip_unique_ids:
                                continue
                            if len(ids) == MAX_INCREMENTAL_MESSAGE_IDS:
                                prefix = ordered_unique_ids[: skip_unique_ids + len(ids)]
                                return ids, _history_continuation_cursor(
                                    request_start_history_id,
                                    prefix,
                                )
                            ids.append(message_id)
                page_token = response.get("nextPageToken")
                if page_token is not None:
                    page_token = _bounded_text(
                        page_token,
                        maximum_bytes=MAX_GMAIL_PAGE_TOKEN_BYTES,
                        field="Gmail history page token",
                        error_type=GmailError,
                    )
                if not page_token:
                    break
        except HttpError as exc:
            if getattr(exc.resp, "status", None) == 404:
                raise StaleHistoryCursor(
                    "Saved Gmail history cursor is no longer available"
                ) from exc
            raise GmailError(f"Gmail history request failed (HTTP {exc.resp.status})") from exc
        if not prefix_verified:
            raise StaleHistoryCursor("Saved Gmail history continuation cursor cannot be resumed")
        return ids, newest

    def changes_since(self, cursor: str) -> MailboxChanges:
        message_ids, newest = self.history_message_ids(cursor)
        return MailboxChanges(tuple(message_ids), newest)

    def metadata(
        self,
        message_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> MessageMetadata:
        try:
            request = (
                self.service.users()
                .messages()
                .get(
                    userId="me",
                    id=message_id,
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                )
            )
            message = self._execute_request(request, timeout_seconds=timeout_seconds)
        except HttpError as exc:
            if getattr(exc.resp, "status", None) == 404:
                raise MessageUnavailable(
                    f"Gmail message {message_id} unavailable (HTTP 404)"
                ) from exc
            raise GmailError(f"Gmail metadata fetch failed (HTTP {exc.resp.status})") from exc
        except (TimeoutError, OSError, httplib2.HttpLib2Error) as exc:
            raise GmailError("Gmail metadata fetch timed out or failed") from exc
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

    def content(self, message_id: str, body_char_limit: int) -> MessageContent:
        body, attachment_names, attachments = extract_body(
            self.full_payload(message_id), body_char_limit
        )
        return MessageContent(body, attachment_names, attachments)

    def attachment_bytes(self, message_id: str, part_id: str, attachment_id: str | None) -> bytes:
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
                raise GmailError(f"Gmail attachment fetch failed (HTTP {exc.resp.status})") from exc
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
            return self.attachment_bytes(message_id, part_id, current_attachment_id.strip())
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

    def recovery_page(
        self,
        page_token: str | None,
        after_exclusive_epoch: int,
        before_exclusive_epoch: int,
        max_results: int = MAX_GMAIL_RECOVERY_PAGE_IDS,
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[tuple[str, ...], str | None]:
        if (
            isinstance(after_exclusive_epoch, bool)
            or not isinstance(after_exclusive_epoch, int)
            or after_exclusive_epoch < 0
            or isinstance(before_exclusive_epoch, bool)
            or not isinstance(before_exclusive_epoch, int)
            or before_exclusive_epoch <= after_exclusive_epoch
            or isinstance(max_results, bool)
            or max_results != MAX_GMAIL_RECOVERY_PAGE_IDS
        ):
            raise GmailRecoveryPageInvalid("gmail_recovery_page_invalid: invalid request bounds")
        if page_token is not None:
            _bounded_text(
                page_token,
                maximum_bytes=MAX_GMAIL_PAGE_TOKEN_BYTES,
                field="Gmail recovery page token",
                error_type=GmailRecoveryPageInvalid,
            )
        query = (
            f"in:inbox after:{after_exclusive_epoch} "
            f"before:{before_exclusive_epoch}"
        )
        try:
            request = (
                self.service.users()
                .messages()
                .list(
                    userId="me",
                    q=query,
                    pageToken=page_token,
                    maxResults=MAX_GMAIL_RECOVERY_PAGE_IDS,
                )
            )
            response = self._execute_request(request, timeout_seconds=timeout_seconds)
        except HttpError as exc:
            if page_token is not None and getattr(exc.resp, "status", None) == 400:
                raise GmailRecoveryPageTokenInvalid(
                    "gmail_recovery_page_token_invalid: Gmail rejected the page token"
                ) from exc
            raise GmailError(
                f"Gmail recovery search failed (HTTP {exc.resp.status})"
            ) from exc
        except (TimeoutError, OSError, httplib2.HttpLib2Error) as exc:
            raise GmailError("Gmail recovery search timed out or failed") from exc
        if not isinstance(response, dict):
            raise GmailRecoveryPageInvalid("gmail_recovery_page_invalid: malformed response")
        messages = response.get("messages", [])
        if not isinstance(messages, list) or len(messages) > MAX_GMAIL_RECOVERY_PAGE_IDS:
            raise GmailRecoveryPageInvalid("gmail_recovery_page_invalid: malformed response")
        ids: list[str] = []
        seen_ids: set[str] = set()
        for item in messages:
            message_id = _bounded_text(
                item.get("id") if isinstance(item, dict) else None,
                maximum_bytes=MAX_GMAIL_LABEL_ID_BYTES,
                field="Gmail recovery message ID",
                error_type=GmailRecoveryPageInvalid,
            )
            if message_id in seen_ids:
                raise GmailRecoveryPageInvalid(
                    "gmail_recovery_page_invalid: duplicate message ID"
                )
            seen_ids.add(message_id)
            ids.append(message_id)
        next_page_token = response.get("nextPageToken")
        if next_page_token is not None:
            next_page_token = _bounded_text(
                next_page_token,
                maximum_bytes=MAX_GMAIL_PAGE_TOKEN_BYTES,
                field="Gmail recovery next page token",
                error_type=GmailRecoveryPageInvalid,
            )
        return tuple(ids), next_page_token

    def recover_since(self, addresses: frozenset[str], since: datetime) -> MailboxChanges:
        recovery_cursor = self.initial_cursor()
        return MailboxChanges(tuple(self.search_since(addresses, since)), recovery_cursor)
