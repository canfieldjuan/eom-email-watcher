from __future__ import annotations

import html
import json
import logging
import os
import re
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import httpx
import msal
from filelock import FileLock
from filelock import Timeout as FileLockTimeout

from .config import normalize_address
from .mailbox import (
    MailboxChanges,
    MailboxError,
    MailboxMessageUnavailable,
    MessageContent,
    MessageMetadata,
    StaleMailboxCursor,
)
from .mime import AttachmentDescriptor

MICROSOFT365_PROVIDER = "microsoft365"
SCOPES = ("Mail.Read",)
GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
AUTHORITY_ROOT = "https://login.microsoftonline.com"
TOKEN_LOCK_TIMEOUT_SECONDS = 30
AUTHORIZATION_TIMEOUT_SECONDS = 300
GRAPH_TIMEOUT_SECONDS = 30.0
MAX_CLIENT_CONFIG_BYTES = 32 * 1024
MAX_GRAPH_URL_LENGTH = 32 * 1024
BUNDLED_MICROSOFT_OAUTH_CLIENT = Path("eom_email_watcher_data/microsoft-oauth-client.json")
_INITIAL_CURSOR_PREFIX = "microsoft365-initial:"
_DELTA_PATH = re.compile(
    r"\A/v1\.0/me/mailFolders"
    r"(?:/[A-Za-z0-9_~.%=+-]+|\('[A-Za-z0-9_~.%=+-]+'\))"
    r"/messages/delta\Z",
    re.IGNORECASE,
)
_TRANSIENT_AUTH_ERRORS = frozenset(
    {"temporarily_unavailable", "server_error", "service_not_available"}
)


class Microsoft365Error(MailboxError):
    """A Microsoft 365 mailbox operation failed."""


class MicrosoftConfigurationError(Microsoft365Error):
    """The Microsoft public-client configuration is missing or invalid."""


class MicrosoftAuthorizationRejected(Microsoft365Error):
    """Microsoft rejected or can no longer refresh the delegated authorization."""


@dataclass(frozen=True)
class MicrosoftPublicClient:
    client_id: str
    tenant: str

    @property
    def authority(self) -> str:
        return f"{AUTHORITY_ROOT}/{self.tenant}"


@dataclass(frozen=True)
class Microsoft365Profile:
    email_address: str


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def resolve_microsoft_credentials_file(configured_file: Path) -> Path:
    """Prefer an explicit public-client file, then the packaged desktop client."""
    if configured_file.is_file():
        return configured_file
    bundle_root = getattr(sys, "_MEIPASS", None)
    if isinstance(bundle_root, str):
        bundled_file = Path(bundle_root) / BUNDLED_MICROSOFT_OAUTH_CLIENT
        if bundled_file.is_file():
            return bundled_file
    return configured_file


def _normalized_uuid(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MicrosoftConfigurationError(f"Microsoft OAuth {field} must be a UUID")
    try:
        parsed = uuid.UUID(value.strip())
    except (ValueError, AttributeError) as exc:
        raise MicrosoftConfigurationError(f"Microsoft OAuth {field} must be a UUID") from exc
    if parsed.int == 0:
        raise MicrosoftConfigurationError(f"Microsoft OAuth {field} must not be empty")
    return str(parsed)


def load_microsoft_public_client(configured_file: Path) -> MicrosoftPublicClient:
    path = resolve_microsoft_credentials_file(configured_file)
    if not path.is_file():
        raise MicrosoftConfigurationError(
            "Microsoft OAuth public-client configuration is not installed"
        )
    try:
        if path.stat().st_size > MAX_CLIENT_CONFIG_BYTES:
            raise MicrosoftConfigurationError(
                "Microsoft OAuth public-client configuration is too large"
            )
        document = json.loads(path.read_text(encoding="utf-8"))
    except MicrosoftConfigurationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MicrosoftConfigurationError(
            "Microsoft OAuth public-client configuration is not valid JSON"
        ) from exc
    if not isinstance(document, dict) or set(document) - {"client_id", "tenant"}:
        raise MicrosoftConfigurationError(
            "Microsoft OAuth public-client configuration must contain only client_id and tenant"
        )
    client_id = _normalized_uuid(document.get("client_id"), "client_id")
    tenant_value = document.get("tenant", "organizations")
    if tenant_value == "organizations":
        tenant = "organizations"
    else:
        tenant = _normalized_uuid(tenant_value, "tenant")
    return MicrosoftPublicClient(client_id=client_id, tenant=tenant)


def microsoft_credentials_configured(configured_file: Path) -> bool:
    try:
        load_microsoft_public_client(configured_file)
    except MicrosoftConfigurationError:
        return False
    return True


def _new_public_client(
    configuration: MicrosoftPublicClient,
    cache: msal.SerializableTokenCache,
) -> Any:
    return msal.PublicClientApplication(
        configuration.client_id,
        authority=configuration.authority,
        token_cache=cache,
    )


def _authorization_result(result: object) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise MicrosoftAuthorizationRejected("Microsoft authorization did not return a token")
    access_token = result.get("access_token")
    if isinstance(access_token, str) and access_token:
        return result
    error = str(result.get("error", "")).casefold()
    if error in _TRANSIENT_AUTH_ERRORS:
        raise Microsoft365Error("Microsoft authorization is temporarily unavailable; retry")
    raise MicrosoftAuthorizationRejected("Microsoft authorization was not completed")


def _profile_address(result: dict[str, Any], account: object) -> str:
    claims = result.get("id_token_claims")
    candidates: list[object] = []
    if isinstance(claims, dict):
        candidates.extend((claims.get("preferred_username"), claims.get("email")))
    if isinstance(account, dict):
        candidates.append(account.get("username"))
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        address = normalize_address(candidate)
        local, separator, domain = address.rpartition("@")
        if (
            separator == "@"
            and local
            and domain
            and "@" not in local
            and not any(character.isspace() or not character.isprintable() for character in address)
        ):
            return address
    raise MicrosoftAuthorizationRejected(
        "Microsoft authorization did not identify an email mailbox"
    )


def _load_cache(path: Path) -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    try:
        serialized = path.read_text(encoding="utf-8")
        if not serialized:
            raise ValueError("empty cache")
        cache.deserialize(serialized)
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise MicrosoftAuthorizationRejected("Microsoft authorization cache is invalid") from exc
    return cache


def _write_private_cache(path: Path, cache: msal.SerializableTokenCache) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(cache.serialize())
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _safe_graph_url(url: object, *, delta_token: str | None = None) -> str:
    if not isinstance(url, str) or not url or len(url) > MAX_GRAPH_URL_LENGTH:
        raise Microsoft365Error("Microsoft Graph returned an invalid continuation URL")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise Microsoft365Error("Microsoft Graph returned an invalid continuation URL") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != "graph.microsoft.com"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
    ):
        raise Microsoft365Error("Microsoft Graph returned an invalid continuation URL")
    if delta_token is not None:
        if _DELTA_PATH.fullmatch(parsed.path) is None:
            raise Microsoft365Error("Saved Microsoft 365 mailbox cursor is invalid")
        query = parse_qs(parsed.query, keep_blank_values=True)
        if delta_token not in query or any(not item for item in query[delta_token]):
            raise Microsoft365Error("Saved Microsoft 365 mailbox cursor is invalid")
        other_token = "$skiptoken" if delta_token == "$deltatoken" else "$deltatoken"
        if other_token in query:
            raise Microsoft365Error("Saved Microsoft 365 mailbox cursor is invalid")
    return url


def _delta_url(since: datetime) -> str:
    stamp = since.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    query = urlencode(
        {
            "$select": "id",
            "$filter": f"receivedDateTime ge {stamp}",
            "changeType": "created",
        }
    )
    return f"{GRAPH_ROOT}/me/mailFolders/inbox/messages/delta?{query}"


def _initial_cursor(since: datetime) -> str:
    stamp = since.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    return f"{_INITIAL_CURSOR_PREFIX}{stamp}"


def _initial_cursor_time(cursor: str) -> datetime | None:
    if not cursor.startswith(_INITIAL_CURSOR_PREFIX):
        return None
    stamp = cursor.removeprefix(_INITIAL_CURSOR_PREFIX)
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Microsoft365Error("Saved Microsoft 365 mailbox cursor is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise Microsoft365Error("Saved Microsoft 365 mailbox cursor is invalid")
    if _initial_cursor(parsed) != cursor:
        raise Microsoft365Error("Saved Microsoft 365 mailbox cursor is invalid")
    return parsed


def _graph_id(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or any(character.isspace() or not character.isprintable() for character in value)
    ):
        raise Microsoft365Error(f"Microsoft Graph returned an invalid {name}")
    return value


def _response_error_code(response: httpx.Response) -> str:
    try:
        document = response.json()
    except ValueError:
        return ""
    error = document.get("error") if isinstance(document, dict) else None
    return str(error.get("code", "")) if isinstance(error, dict) else ""


def _response_document(response: httpx.Response, context: str) -> dict[str, Any]:
    try:
        document = response.json()
    except ValueError as exc:
        raise Microsoft365Error(f"Microsoft Graph {context} returned invalid JSON") from exc
    if not isinstance(document, dict):
        raise Microsoft365Error(f"Microsoft Graph {context} returned an invalid response")
    return document


def _message_body_text(body: object, limit: int) -> str:
    if not isinstance(body, dict):
        return ""
    content = body.get("content")
    if not isinstance(content, str):
        return ""
    if str(body.get("contentType", "")).casefold() == "html":
        parser = _TextExtractor()
        parser.feed(content)
        content = html.unescape(" ".join(parser.parts))
    normalized = "\n".join(line.strip() for line in content.splitlines() if line.strip())
    return normalized[:limit]


class Microsoft365Gateway:
    def __init__(
        self,
        access_token: str,
        email_address: str,
        client: httpx.Client | None = None,
    ):
        # These clients log full URLs at INFO/DEBUG; Graph cursor URLs contain opaque
        # mailbox tokens. Scope suppression to processes that actually open Graph.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        self._access_token = access_token
        self._email_address = email_address
        self._client = client or httpx.Client(
            timeout=GRAPH_TIMEOUT_SECONDS,
            follow_redirects=False,
        )

    @classmethod
    def from_token(
        cls,
        credentials_file: Path,
        token_file: Path,
        *,
        client: httpx.Client | None = None,
    ) -> Microsoft365Gateway:
        configuration = load_microsoft_public_client(credentials_file)
        if not token_file.is_file():
            raise MicrosoftAuthorizationRejected("Microsoft 365 is not authorized")
        try:
            with FileLock(f"{token_file}.lock", timeout=TOKEN_LOCK_TIMEOUT_SECONDS):
                cache = _load_cache(token_file)
                try:
                    application = _new_public_client(configuration, cache)
                except Exception as exc:
                    raise Microsoft365Error(
                        "Microsoft authorization service is unavailable; retry"
                    ) from exc
                accounts = application.get_accounts()
                if not isinstance(accounts, list) or len(accounts) != 1:
                    raise MicrosoftAuthorizationRejected(
                        "Microsoft authorization cache does not identify one mailbox"
                    )
                try:
                    raw_result = application.acquire_token_silent_with_error(
                        list(SCOPES),
                        account=accounts[0],
                    )
                except Exception as exc:
                    raise Microsoft365Error(
                        "Microsoft authorization refresh failed; retry"
                    ) from exc
                result = _authorization_result(raw_result)
                email_address = _profile_address(result, accounts[0])
                if cache.has_state_changed:
                    _write_private_cache(token_file, cache)
        except FileLockTimeout as exc:
            raise Microsoft365Error("Microsoft authorization cache is busy; retry") from exc
        return cls(str(result["access_token"]), email_address, client)

    @classmethod
    def authorize_with_status(
        cls,
        credentials_file: Path,
        token_file: Path,
        *,
        force_reauthorize: bool = False,
        client: httpx.Client | None = None,
    ) -> tuple[Microsoft365Gateway, bool]:
        if token_file.is_file() and not force_reauthorize:
            return cls.from_token(credentials_file, token_file, client=client), False
        configuration = load_microsoft_public_client(credentials_file)
        cache = msal.SerializableTokenCache()
        try:
            application = _new_public_client(configuration, cache)
        except Exception as exc:
            raise Microsoft365Error(
                "Microsoft authorization service is unavailable; retry"
            ) from exc
        try:
            raw_result = application.acquire_token_interactive(
                list(SCOPES),
                prompt="select_account",
                timeout=AUTHORIZATION_TIMEOUT_SECONDS,
                port=0,
            )
        except Exception as exc:
            raise Microsoft365Error("Microsoft authorization failed; retry") from exc
        result = _authorization_result(raw_result)
        accounts = application.get_accounts()
        if not isinstance(accounts, list) or len(accounts) != 1:
            raise MicrosoftAuthorizationRejected(
                "Microsoft authorization did not identify one mailbox"
            )
        email_address = _profile_address(result, accounts[0])
        try:
            with FileLock(f"{token_file}.lock", timeout=TOKEN_LOCK_TIMEOUT_SECONDS):
                _write_private_cache(token_file, cache)
        except FileLockTimeout as exc:
            raise Microsoft365Error("Microsoft authorization cache is busy; retry") from exc
        return cls(str(result["access_token"]), email_address, client), True

    def profile(self) -> Microsoft365Profile:
        return Microsoft365Profile(email_address=self._email_address)

    def _request(
        self,
        url: str,
        *,
        prefer_text: bool = False,
        cursor_request: bool = False,
        missing_is_message: bool = False,
    ) -> httpx.Response:
        safe_url = _safe_graph_url(url)
        preferences = ['IdType="ImmutableId"']
        if prefer_text:
            preferences.append('outlook.body-content-type="text"')
        try:
            response = self._client.get(
                safe_url,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self._access_token}",
                    "Prefer": ", ".join(preferences),
                },
                follow_redirects=False,
            )
        except httpx.RequestError as exc:
            raise Microsoft365Error("Microsoft Graph request failed; retry") from exc
        error_code = _response_error_code(response).casefold()
        if response.status_code == 401:
            raise MicrosoftAuthorizationRejected("Microsoft rejected the configured authorization")
        if cursor_request and (
            response.status_code == 410
            or error_code in {"syncstatenotfound", "errorsyncstatenotfound"}
        ):
            raise StaleMailboxCursor("Saved Microsoft 365 mailbox cursor is no longer available")
        if missing_is_message and response.status_code == 404:
            raise MailboxMessageUnavailable("Microsoft 365 message is no longer available")
        if response.status_code == 429 or response.status_code >= 500:
            raise Microsoft365Error(
                f"Microsoft Graph is temporarily unavailable (HTTP {response.status_code}); retry"
            )
        if not response.is_success:
            raise Microsoft365Error(f"Microsoft Graph request failed (HTTP {response.status_code})")
        return response

    def _delta_round(self, url: str, *, continuation: bool) -> MailboxChanges:
        current = _safe_graph_url(url, delta_token="$deltatoken") if continuation else url
        ids: list[str] = []
        while True:
            response = self._request(current, cursor_request=continuation)
            document = _response_document(response, "mail delta")
            values = document.get("value")
            if not isinstance(values, list):
                raise Microsoft365Error("Microsoft Graph mail delta omitted its message list")
            for item in values:
                if not isinstance(item, dict) or "@removed" in item:
                    continue
                message_id = item.get("id")
                if isinstance(message_id, str) and message_id:
                    ids.append(_graph_id(message_id, "message id"))
            next_link = document.get("@odata.nextLink")
            delta_link = document.get("@odata.deltaLink")
            if isinstance(next_link, str) and not isinstance(delta_link, str):
                current = _safe_graph_url(next_link, delta_token="$skiptoken")
                continuation = True
                continue
            if isinstance(delta_link, str) and not isinstance(next_link, str):
                cursor = _safe_graph_url(delta_link, delta_token="$deltatoken")
                return MailboxChanges(tuple(dict.fromkeys(ids)), cursor)
            raise Microsoft365Error(
                "Microsoft Graph mail delta omitted a single continuation cursor"
            )

    def initial_cursor(self) -> str:
        # Persist the boundary before the first Graph request. The first watcher check
        # completes the initial delta round so arrivals during setup cannot be discarded.
        return _initial_cursor(datetime.now(UTC))

    def changes_since(self, cursor: str) -> MailboxChanges:
        initial_since = _initial_cursor_time(cursor)
        if initial_since is not None:
            return self._delta_round(_delta_url(initial_since), continuation=False)
        return self._delta_round(cursor, continuation=True)

    def recover_since(
        self,
        addresses: frozenset[str],
        since: datetime,
    ) -> MailboxChanges:
        del addresses
        return self._delta_round(_delta_url(since), continuation=False)

    def metadata(self, message_id: str) -> MessageMetadata:
        encoded_id = quote(_graph_id(message_id, "message id"), safe="")
        query = urlencode(
            {
                "$select": "id,conversationId,from,subject,receivedDateTime",
            }
        )
        response = self._request(
            f"{GRAPH_ROOT}/me/mailFolders/inbox/messages/{encoded_id}?{query}",
            missing_is_message=True,
        )
        document = _response_document(response, "message metadata")
        response_id = _graph_id(document.get("id"), "message id")
        if response_id != message_id:
            raise Microsoft365Error("Microsoft Graph returned metadata for another message")
        sender_object = document.get("from")
        email_object = (
            sender_object.get("emailAddress") if isinstance(sender_object, dict) else None
        )
        raw_sender = email_object.get("address") if isinstance(email_object, dict) else ""
        raw_name = email_object.get("name") if isinstance(email_object, dict) else None
        subject = document.get("subject")
        received_at = document.get("receivedDateTime")
        conversation_id = document.get("conversationId")
        return MessageMetadata(
            message_id=message_id,
            thread_id=conversation_id if isinstance(conversation_id, str) else None,
            sender=normalize_address(raw_sender if isinstance(raw_sender, str) else ""),
            sender_name=(
                raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else None
            ),
            subject=(
                subject.strip() if isinstance(subject, str) and subject.strip() else "(no subject)"
            ),
            received_at=received_at if isinstance(received_at, str) else "",
            labels=frozenset({"INBOX"}),
        )

    def _attachments(self, message_id: str) -> tuple[AttachmentDescriptor, ...]:
        encoded_id = quote(_graph_id(message_id, "message id"), safe="")
        query = urlencode({"$select": "id,name,contentType,size,isInline"})
        current = f"{GRAPH_ROOT}/me/messages/{encoded_id}/attachments?{query}"
        descriptors: list[AttachmentDescriptor] = []
        while True:
            response = self._request(current, missing_is_message=True)
            document = _response_document(response, "attachment list")
            values = document.get("value")
            if not isinstance(values, list):
                raise Microsoft365Error("Microsoft Graph attachment list was invalid")
            for item in values:
                if not isinstance(item, dict):
                    continue
                attachment_type = str(item.get("@odata.type", "")).casefold()
                if attachment_type not in {
                    "#microsoft.graph.fileattachment",
                    "microsoft.graph.fileattachment",
                }:
                    continue
                attachment_id = _graph_id(item.get("id"), "attachment id")
                filename = item.get("name")
                if not isinstance(filename, str) or not filename.strip():
                    continue
                raw_size = item.get("size", 0)
                byte_size = (
                    raw_size
                    if isinstance(raw_size, int)
                    and not isinstance(raw_size, bool)
                    and raw_size >= 0
                    else 0
                )
                descriptors.append(
                    AttachmentDescriptor(
                        part_id=attachment_id,
                        attachment_id=attachment_id,
                        filename=filename.strip(),
                        media_type=str(item.get("contentType", "")).casefold(),
                        byte_size=byte_size,
                        position=len(descriptors),
                    )
                )
            next_link = document.get("@odata.nextLink")
            if next_link is None:
                return tuple(descriptors)
            current = _safe_graph_url(next_link)

    def content(self, message_id: str, body_char_limit: int) -> MessageContent:
        encoded_id = quote(_graph_id(message_id, "message id"), safe="")
        query = urlencode({"$select": "body,hasAttachments"})
        response = self._request(
            f"{GRAPH_ROOT}/me/messages/{encoded_id}?{query}",
            prefer_text=True,
            missing_is_message=True,
        )
        document = _response_document(response, "message content")
        attachments = (
            self._attachments(message_id) if document.get("hasAttachments") is True else ()
        )
        names = tuple(dict.fromkeys(item.filename for item in attachments))
        return MessageContent(
            body=_message_body_text(document.get("body"), body_char_limit),
            attachment_names=names,
            attachments=attachments,
        )

    def attachment_bytes(
        self,
        message_id: str,
        part_id: str,
        attachment_id: str | None,
    ) -> bytes:
        selected_id = attachment_id or part_id
        if attachment_id is not None and attachment_id != part_id:
            raise MailboxMessageUnavailable(
                "Microsoft 365 attachment identity no longer matches the message"
            )
        encoded_message = quote(_graph_id(message_id, "message id"), safe="")
        encoded_attachment = quote(_graph_id(selected_id, "attachment id"), safe="")
        response = self._request(
            f"{GRAPH_ROOT}/me/messages/{encoded_message}/attachments/{encoded_attachment}/$value",
            missing_is_message=True,
        )
        return response.content
