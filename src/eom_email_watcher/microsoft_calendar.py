from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Self
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import msal
from filelock import FileLock
from filelock import Timeout as FileLockTimeout

from .microsoft365 import (
    AUTHORIZATION_TIMEOUT_SECONDS,
    GRAPH_ROOT,
    GRAPH_TIMEOUT_SECONDS,
    MAX_GRAPH_URL_LENGTH,
    TOKEN_LOCK_TIMEOUT_SECONDS,
    Microsoft365Error,
    MicrosoftAuthorizationRejected,
    _authorization_result,
    _load_cache,
    _new_public_client,
    _profile_address,
    _write_private_cache,
    load_microsoft_public_client,
)
from .microsoft365 import (
    SCOPES as MAIL_READ_SCOPES,
)

CALENDAR_READ_PROFILE = "read"
CALENDAR_READ_SCOPES = ("Calendars.Read",)
CALENDAR_PROPOSAL_PROFILE = "proposal"
CALENDAR_PROPOSAL_SCOPES = ("Calendars.Read.Shared",)
CALENDAR_WRITE_PROFILE = "write"
CALENDAR_WRITE_SCOPES = ("Calendars.ReadWrite",)
CALENDAR_PAGE_SIZE = 100
MAX_CALENDAR_DELTA_PAGES = 64
MAX_CALENDAR_DELTA_ENTRIES = 3_200
MAX_CALENDAR_PAGE_BYTES = 2 * 1024 * 1024
MAX_CALENDAR_ROUND_BYTES = 16 * 1024 * 1024
MAX_CALENDAR_ROUND_SECONDS = 300.0
MAX_CALENDAR_EVENT_ID_BYTES = 512
MAX_CALENDAR_SUBJECT_BYTES = 512
MAX_CALENDAR_LOCATION_BYTES = 512
MAX_CALENDAR_DATETIME_BYTES = 64
MAX_CALENDAR_ZONE_BYTES = 128
_CONSENT_PENDING_ERROR_CODES = frozenset({65001, 90094, 90095})
_RFC3339_INSTANT = re.compile(
    r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})\Z"
)
_GRAPH_LOCAL_DATETIME = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,7})?\Z")


def _calendar_monotonic() -> float:
    return time.monotonic()


class MicrosoftCalendarConsentPending(Microsoft365Error):
    """The user or tenant administrator must finish calendar consent."""


class StaleCalendarCursor(Microsoft365Error):
    """Microsoft can no longer continue the saved calendar delta round."""


@dataclass(frozen=True)
class CalendarAuthorizationProfile:
    name: str
    scopes: tuple[str, ...]


CALENDAR_AUTHORIZATION_PROFILES = {
    "read": CalendarAuthorizationProfile("read", CALENDAR_READ_SCOPES),
    "proposal": CalendarAuthorizationProfile("proposal", CALENDAR_PROPOSAL_SCOPES),
    "write": CalendarAuthorizationProfile("write", CALENDAR_WRITE_SCOPES),
}


@dataclass(frozen=True)
class CalendarEvent:
    event_id: str
    subject: str
    start_date_time: str
    start_time_zone: str
    end_date_time: str
    end_time_zone: str
    is_all_day: bool
    location: str


@dataclass(frozen=True)
class CalendarDeltaChange:
    event_id: str
    event: CalendarEvent | None


@dataclass(frozen=True)
class CalendarDeltaRound:
    changes: tuple[CalendarDeltaChange, ...]
    cursor: str


def canonical_calendar_window(window_start: object, window_end: object) -> tuple[str, str]:
    canonical: list[datetime] = []
    for value in (window_start, window_end):
        if (
            not isinstance(value, str)
            or _RFC3339_INSTANT.fullmatch(value) is None
            or value.endswith("-00:00")
        ):
            raise ValueError("calendar window boundaries must be offset-bearing RFC 3339 instants")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(
                "calendar window boundaries must be offset-bearing RFC 3339 instants"
            ) from exc
        if parsed.utcoffset() is None:
            raise ValueError("calendar window boundaries must include an offset")
        try:
            canonical.append(parsed.astimezone(UTC))
        except (OverflowError, ValueError) as exc:
            raise ValueError(
                "calendar window boundaries must be offset-bearing RFC 3339 instants"
            ) from exc
    start, end = canonical
    if end <= start:
        raise ValueError("calendar window end must be after its start")
    if end - start > timedelta(days=366):
        raise ValueError("calendar window cannot exceed 366 days")
    return tuple(
        instant.isoformat(timespec="microseconds").replace("+00:00", "Z")
        for instant in (start, end)
    )


def _entra_error_codes(result: dict[str, Any]) -> set[int]:
    codes: set[int] = set()
    raw_codes = result.get("error_codes")
    if isinstance(raw_codes, list):
        codes.update(
            code for code in raw_codes if isinstance(code, int) and not isinstance(code, bool)
        )

    description = result.get("error_description")
    if isinstance(description, str):
        prefix = description.partition(":")[0].strip().casefold()
        if prefix.startswith("aadsts"):
            numeric_code = prefix.removeprefix("aadsts")
            if numeric_code.isascii() and numeric_code.isdigit():
                codes.add(int(numeric_code))
    return codes


@dataclass(frozen=True)
class MicrosoftPrincipal:
    home_account_id: str
    tenant_id: str
    object_id: str
    email_address: str

    @property
    def key(self) -> str:
        value = "\0".join((self.home_account_id, self.tenant_id, self.object_id))
        return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _identity_part(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or any(character.isspace() or not character.isprintable() for character in value)
    ):
        raise MicrosoftAuthorizationRejected(
            f"Microsoft authorization did not identify one immutable {name}"
        )
    return value


def _principal(result: dict[str, Any], account: object) -> MicrosoftPrincipal:
    if not isinstance(account, dict):
        raise MicrosoftAuthorizationRejected(
            "Microsoft authorization did not identify one immutable principal"
        )
    claims = result.get("id_token_claims")
    claim_values = claims if isinstance(claims, dict) else {}
    return MicrosoftPrincipal(
        home_account_id=_identity_part(account.get("home_account_id"), "home account"),
        tenant_id=_identity_part(
            claim_values.get("tid", account.get("realm")),
            "tenant",
        ),
        object_id=_identity_part(
            claim_values.get("oid", account.get("local_account_id")),
            "object",
        ),
        email_address=_profile_address(result, account),
    )


def _calendar_authorization_result(result: object) -> dict[str, Any]:
    if isinstance(result, dict) and not result.get("access_token"):
        error = str(result.get("error", "")).casefold()
        suberror = str(result.get("suberror", "")).casefold()
        pending_errors = {
            "authorization_pending",
            "consent_required",
            "interaction_required",
        }
        if (
            error in pending_errors
            or suberror in pending_errors
            or _entra_error_codes(result) & _CONSENT_PENDING_ERROR_CODES
        ):
            raise MicrosoftCalendarConsentPending(
                "Microsoft calendar consent requires user or administrator approval"
            )
    return _authorization_result(result)


def microsoft_mailbox_principal(
    credentials_file: Path,
    token_file: Path,
) -> MicrosoftPrincipal:
    """Resolve the immutable principal from the existing Mail.Read cache."""
    configuration = load_microsoft_public_client(credentials_file)
    if not token_file.is_file():
        raise MicrosoftAuthorizationRejected("Microsoft mailbox is not authorized")
    try:
        with FileLock(f"{token_file}.lock", timeout=TOKEN_LOCK_TIMEOUT_SECONDS):
            cache = _load_cache(token_file)
            application = _new_public_client(configuration, cache)
            accounts = application.get_accounts()
            if not isinstance(accounts, list) or len(accounts) != 1:
                raise MicrosoftAuthorizationRejected(
                    "Microsoft mailbox cache does not identify one principal"
                )
            result = _authorization_result(
                application.acquire_token_silent_with_error(
                    list(MAIL_READ_SCOPES),
                    account=accounts[0],
                )
            )
            principal = _principal(result, accounts[0])
            if cache.has_state_changed:
                _write_private_cache(token_file, cache)
    except FileLockTimeout as exc:
        raise Microsoft365Error("Microsoft mailbox cache is busy; retry") from exc
    except Microsoft365Error:
        raise
    except Exception as exc:
        raise Microsoft365Error("Microsoft mailbox authorization failed; retry") from exc
    return principal


def microsoft_cached_mailbox_principal(token_file: Path) -> MicrosoftPrincipal:
    """Read the immutable mailbox principal from the cache without token acquisition."""
    if not token_file.is_file():
        raise MicrosoftAuthorizationRejected("Microsoft mailbox is not authorized")
    try:
        with FileLock(f"{token_file}.lock", timeout=TOKEN_LOCK_TIMEOUT_SECONDS):
            cache = _load_cache(token_file)
            accounts = tuple(cache.search(msal.TokenCache.CredentialType.ACCOUNT))
            if len(accounts) != 1:
                raise MicrosoftAuthorizationRejected(
                    "Microsoft mailbox cache does not identify one principal"
                )
            return _principal({}, accounts[0])
    except FileLockTimeout as exc:
        raise Microsoft365Error("Microsoft mailbox cache is busy; retry") from exc
    except Microsoft365Error:
        raise
    except Exception as exc:
        raise Microsoft365Error("Microsoft mailbox cache inspection failed; retry") from exc


class MicrosoftCalendarAuthorization:
    profile = CALENDAR_AUTHORIZATION_PROFILES[CALENDAR_READ_PROFILE]

    def __init__(
        self,
        principal: MicrosoftPrincipal,
        access_token: str,
        *,
        http_client: httpx.AsyncClient | None = None,
    ):
        # httpx logs full request URLs at INFO; calendar delta URLs contain opaque tokens.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        self.principal = principal
        self._access_token = access_token
        self._http_client = http_client

    @classmethod
    def from_token(
        cls,
        credentials_file: Path,
        token_file: Path,
    ) -> Self:
        configuration = load_microsoft_public_client(credentials_file)
        if not token_file.is_file():
            raise MicrosoftAuthorizationRejected("Microsoft calendar is not authorized")
        try:
            with FileLock(f"{token_file}.lock", timeout=TOKEN_LOCK_TIMEOUT_SECONDS):
                cache = _load_cache(token_file)
                application = _new_public_client(configuration, cache)
                accounts = application.get_accounts()
                if not isinstance(accounts, list) or len(accounts) != 1:
                    raise MicrosoftAuthorizationRejected(
                        "Microsoft calendar cache does not identify one principal"
                    )
                result = _authorization_result(
                    application.acquire_token_silent_with_error(
                        list(cls.profile.scopes),
                        account=accounts[0],
                    )
                )
                principal = _principal(result, accounts[0])
                if cache.has_state_changed:
                    _write_private_cache(token_file, cache)
        except FileLockTimeout as exc:
            raise Microsoft365Error("Microsoft calendar cache is busy; retry") from exc
        except Microsoft365Error:
            raise
        except Exception as exc:
            raise Microsoft365Error("Microsoft calendar authorization failed; retry") from exc
        return cls(principal, str(result["access_token"]))

    @classmethod
    def from_matching_tokens(
        cls,
        credentials_file: Path,
        token_file: Path,
        mailbox_token_file: Path,
        expected_principal_key: str,
    ) -> Self:
        authorization = cls.from_token(credentials_file, token_file)
        if authorization.principal.key != expected_principal_key:
            raise MicrosoftAuthorizationRejected(
                "Microsoft calendar grant does not match its authorized principal"
            )
        mailbox_principal = microsoft_mailbox_principal(credentials_file, mailbox_token_file)
        if mailbox_principal.key != expected_principal_key:
            raise MicrosoftAuthorizationRejected(
                "Microsoft calendar grant does not match the mailbox principal"
            )
        return authorization

    @classmethod
    def authorize_with_status(
        cls,
        credentials_file: Path,
        token_file: Path,
    ) -> tuple[Self, bool]:
        configuration = load_microsoft_public_client(credentials_file)
        cache = msal.SerializableTokenCache()
        try:
            application = _new_public_client(configuration, cache)
            raw_result = application.acquire_token_interactive(
                list(cls.profile.scopes),
                prompt="select_account",
                timeout=AUTHORIZATION_TIMEOUT_SECONDS,
                port=0,
            )
            result = _calendar_authorization_result(raw_result)
            accounts = application.get_accounts()
            if not isinstance(accounts, list) or len(accounts) != 1:
                raise MicrosoftAuthorizationRejected(
                    "Microsoft calendar authorization did not identify one principal"
                )
            principal = _principal(result, accounts[0])
            with FileLock(f"{token_file}.lock", timeout=TOKEN_LOCK_TIMEOUT_SECONDS):
                _write_private_cache(token_file, cache)
        except FileLockTimeout as exc:
            raise Microsoft365Error("Microsoft calendar cache is busy; retry") from exc
        except Microsoft365Error:
            raise
        except Exception as exc:
            raise Microsoft365Error("Microsoft calendar authorization failed; retry") from exc
        return cls(principal, str(result["access_token"])), True


class MicrosoftCalendarReadAuthorization(MicrosoftCalendarAuthorization):
    profile = CALENDAR_AUTHORIZATION_PROFILES[CALENDAR_READ_PROFILE]


class MicrosoftCalendarProposalAuthorization(MicrosoftCalendarAuthorization):
    profile = CALENDAR_AUTHORIZATION_PROFILES[CALENDAR_PROPOSAL_PROFILE]


class MicrosoftCalendarWriteAuthorization(MicrosoftCalendarAuthorization):
    profile = CALENDAR_AUTHORIZATION_PROFILES[CALENDAR_WRITE_PROFILE]


def _calendar_delta_url(window_start: str, window_end: str) -> str:
    query = urlencode({"startDateTime": window_start, "endDateTime": window_end})
    return f"{GRAPH_ROOT}/me/calendarView/delta?{query}"


def _safe_calendar_continuation(url: object, token_name: str) -> str:
    if not isinstance(url, str) or not url or len(url.encode("utf-8")) > MAX_GRAPH_URL_LENGTH:
        raise Microsoft365Error("Microsoft Graph returned an invalid calendar cursor")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise Microsoft365Error("Microsoft Graph returned an invalid calendar cursor") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != "graph.microsoft.com"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
        or parsed.path.casefold() != "/v1.0/me/calendarview/delta"
    ):
        raise Microsoft365Error("Microsoft Graph returned an invalid calendar cursor")
    query = parse_qs(parsed.query, keep_blank_values=True)
    if set(query) != {token_name} or len(query[token_name]) != 1 or not query[token_name][0]:
        raise Microsoft365Error("Microsoft Graph returned an invalid calendar cursor")
    return url


def _calendar_round_time_remaining(deadline: float) -> float:
    remaining = deadline - _calendar_monotonic()
    if remaining <= 0:
        raise Microsoft365Error("Microsoft Graph calendar round exceeded its time limit")
    return remaining


async def _bounded_graph_document(
    response: httpx.Response,
    deadline: float,
    remaining_round_bytes: int,
) -> tuple[dict[str, Any], int]:
    content_encoding = response.headers.get("content-encoding")
    if content_encoding is not None and content_encoding.strip().casefold() not in {
        "",
        "identity",
    }:
        raise Microsoft365Error("Microsoft Graph returned a compressed calendar response")
    declared_length = response.headers.get("content-length")
    if declared_length is not None:
        try:
            parsed_length = int(declared_length)
        except ValueError as exc:
            raise Microsoft365Error("Microsoft Graph returned an invalid Content-Length") from exc
        if parsed_length < 0:
            raise Microsoft365Error("Microsoft Graph returned an invalid Content-Length")
        if parsed_length > MAX_CALENDAR_PAGE_BYTES:
            raise Microsoft365Error("Microsoft Graph calendar response exceeded its byte limit")
        if parsed_length > remaining_round_bytes:
            raise Microsoft365Error("Microsoft Graph calendar round exceeded its byte limit")
    content = bytearray()
    async for chunk in response.aiter_bytes():
        _calendar_round_time_remaining(deadline)
        if len(content) + len(chunk) > MAX_CALENDAR_PAGE_BYTES:
            raise Microsoft365Error("Microsoft Graph calendar response exceeded its byte limit")
        if len(content) + len(chunk) > remaining_round_bytes:
            raise Microsoft365Error("Microsoft Graph calendar round exceeded its byte limit")
        content.extend(chunk)
    _calendar_round_time_remaining(deadline)
    try:
        document = json.loads(content)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise Microsoft365Error("Microsoft Graph calendar response was not valid JSON") from exc
    if not isinstance(document, dict):
        raise Microsoft365Error("Microsoft Graph calendar response was not an object")
    return document, len(content)


async def _calendar_graph_page_before_deadline(
    client: httpx.AsyncClient,
    authorization: MicrosoftCalendarReadAuthorization,
    url: str,
    cursor_request: bool,
    deadline: float,
    remaining_round_bytes: int,
) -> tuple[dict[str, Any], int]:
    """Open and consume one page on the cancellable round task."""
    async with client.stream(
        "GET",
        url,
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Authorization": f"Bearer {authorization._access_token}",
            "Prefer": f'IdType="ImmutableId", odata.maxpagesize={CALENDAR_PAGE_SIZE}',
        },
        follow_redirects=False,
        timeout=min(GRAPH_TIMEOUT_SECONDS, _calendar_round_time_remaining(deadline)),
    ) as response:
        if response.status_code == 401:
            raise MicrosoftAuthorizationRejected(
                "Microsoft rejected the calendar read authorization"
            )
        if cursor_request and response.status_code == 410:
            raise StaleCalendarCursor("Saved Microsoft calendar cursor is unavailable")
        if response.status_code == 429 or response.status_code >= 500:
            raise Microsoft365Error(
                f"Microsoft Graph calendar is temporarily unavailable "
                f"(HTTP {response.status_code}); retry"
            )
        document = await _bounded_graph_document(
            response,
            deadline,
            remaining_round_bytes,
        )
        error_code = _calendar_error_code(document[0])
        if cursor_request and error_code in {
            "syncstatenotfound",
            "errorsyncstatenotfound",
        }:
            raise StaleCalendarCursor("Saved Microsoft calendar cursor is unavailable")
        if not response.is_success:
            raise Microsoft365Error(
                f"Microsoft Graph calendar request failed (HTTP {response.status_code})"
            )
    return document


def _calendar_error_code(document: dict[str, Any]) -> str:
    error = document.get("error")
    return str(error.get("code", "")).casefold() if isinstance(error, dict) else ""


def _bounded_graph_text(
    value: object,
    name: str,
    byte_limit: int,
    *,
    allow_empty: bool,
) -> str:
    if not isinstance(value, str) or (not value and not allow_empty):
        raise Microsoft365Error(f"Microsoft Graph returned an invalid calendar {name}")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise Microsoft365Error(f"Microsoft Graph returned an invalid calendar {name}") from exc
    if len(encoded) > byte_limit:
        raise Microsoft365Error(f"Microsoft Graph returned an oversized calendar {name}")
    if any(unicodedata.category(character) in {"Cc", "Cs"} for character in value):
        raise Microsoft365Error(f"Microsoft Graph returned an invalid calendar {name}")
    return value


def _calendar_event_id(value: object) -> str:
    event_id = _bounded_graph_text(
        value,
        "event id",
        MAX_CALENDAR_EVENT_ID_BYTES,
        allow_empty=False,
    )
    if any(character.isspace() for character in event_id):
        raise Microsoft365Error("Microsoft Graph returned an invalid calendar event id")
    return event_id


def _calendar_date_time(value: object, name: str) -> str:
    date_time = _bounded_graph_text(
        value,
        name,
        MAX_CALENDAR_DATETIME_BYTES,
        allow_empty=False,
    )
    if _GRAPH_LOCAL_DATETIME.fullmatch(date_time) is None:
        raise Microsoft365Error(f"Microsoft Graph returned an invalid calendar {name}")
    try:
        datetime.fromisoformat(date_time)
    except ValueError as exc:
        raise Microsoft365Error(f"Microsoft Graph returned an invalid calendar {name}") from exc
    return date_time


def _calendar_event(item: dict[str, Any], event_id: str) -> CalendarEvent:
    start = item.get("start")
    end = item.get("end")
    if not isinstance(start, dict) or not isinstance(end, dict):
        raise Microsoft365Error("Microsoft Graph calendar event omitted its time range")
    is_all_day = item.get("isAllDay")
    if not isinstance(is_all_day, bool):
        raise Microsoft365Error("Microsoft Graph calendar event omitted its all-day flag")
    subject_value = item.get("subject")
    subject = (
        ""
        if subject_value is None
        else _bounded_graph_text(
            subject_value,
            "subject",
            MAX_CALENDAR_SUBJECT_BYTES,
            allow_empty=True,
        )
    )
    location_value = item.get("location")
    if location_value is None:
        location = ""
    elif isinstance(location_value, dict):
        display_name = location_value.get("displayName")
        location = (
            ""
            if display_name is None
            else _bounded_graph_text(
                display_name,
                "location",
                MAX_CALENDAR_LOCATION_BYTES,
                allow_empty=True,
            )
        )
    else:
        raise Microsoft365Error("Microsoft Graph returned an invalid calendar location")
    return CalendarEvent(
        event_id=event_id,
        subject=subject,
        start_date_time=_calendar_date_time(
            start.get("dateTime"),
            "start date-time",
        ),
        start_time_zone=_bounded_graph_text(
            start.get("timeZone"),
            "start time zone",
            MAX_CALENDAR_ZONE_BYTES,
            allow_empty=False,
        ),
        end_date_time=_calendar_date_time(
            end.get("dateTime"),
            "end date-time",
        ),
        end_time_zone=_bounded_graph_text(
            end.get("timeZone"),
            "end time zone",
            MAX_CALENDAR_ZONE_BYTES,
            allow_empty=False,
        ),
        is_all_day=is_all_day,
        location=location,
    )


def _calendar_change(value: object) -> CalendarDeltaChange:
    if not isinstance(value, dict):
        raise Microsoft365Error("Microsoft Graph returned an invalid calendar entry")
    event_id = _calendar_event_id(value.get("id"))
    if "@removed" in value:
        if not isinstance(value["@removed"], dict):
            raise Microsoft365Error("Microsoft Graph returned an invalid calendar tombstone")
        return CalendarDeltaChange(event_id, None)
    return CalendarDeltaChange(event_id, _calendar_event(value, event_id))


async def _calendar_delta_round(
    authorization: MicrosoftCalendarReadAuthorization,
    window_start: str,
    window_end: str,
    *,
    cursor: str | None = None,
) -> CalendarDeltaRound:
    current = (
        _safe_calendar_continuation(cursor, "$deltatoken")
        if cursor is not None
        else _calendar_delta_url(window_start, window_end)
    )
    owned_client = authorization._http_client is None
    client = authorization._http_client or httpx.AsyncClient(timeout=GRAPH_TIMEOUT_SECONDS)
    changes: list[CalendarDeltaChange] = []
    total_bytes = 0
    pages = 0
    cursor_request = cursor is not None
    deadline = _calendar_monotonic() + MAX_CALENDAR_ROUND_SECONDS
    try:
        while True:
            if pages >= MAX_CALENDAR_DELTA_PAGES:
                raise Microsoft365Error("Microsoft Graph calendar round exceeded its page limit")
            pages += 1
            try:
                document, response_bytes = await _calendar_graph_page_before_deadline(
                    client,
                    authorization,
                    current,
                    cursor_request,
                    deadline,
                    MAX_CALENDAR_ROUND_BYTES - total_bytes,
                )
            except httpx.RequestError as exc:
                raise Microsoft365Error("Microsoft Graph calendar request failed; retry") from exc
            total_bytes += response_bytes
            if total_bytes > MAX_CALENDAR_ROUND_BYTES:
                raise Microsoft365Error("Microsoft Graph calendar round exceeded its byte limit")
            values = document.get("value")
            if not isinstance(values, list) or len(values) > CALENDAR_PAGE_SIZE:
                raise Microsoft365Error("Microsoft Graph calendar page exceeded its entry limit")
            if len(changes) + len(values) > MAX_CALENDAR_DELTA_ENTRIES:
                raise Microsoft365Error("Microsoft Graph calendar round exceeded its entry limit")
            changes.extend(_calendar_change(value) for value in values)
            has_next = "@odata.nextLink" in document
            has_delta = "@odata.deltaLink" in document
            if has_next == has_delta:
                raise Microsoft365Error(
                    "Microsoft Graph calendar response omitted a single continuation cursor"
                )
            next_link = document.get("@odata.nextLink")
            delta_link = document.get("@odata.deltaLink")
            if has_next and isinstance(next_link, str):
                current = _safe_calendar_continuation(next_link, "$skiptoken")
                cursor_request = True
                continue
            if has_delta and isinstance(delta_link, str):
                return CalendarDeltaRound(
                    tuple(changes),
                    _safe_calendar_continuation(delta_link, "$deltatoken"),
                )
            raise Microsoft365Error(
                "Microsoft Graph calendar response omitted a single continuation cursor"
            )
    finally:
        if owned_client:
            await client.aclose()


def calendar_delta_round(
    authorization: MicrosoftCalendarReadAuthorization,
    window_start: str,
    window_end: str,
    *,
    cursor: str | None = None,
) -> CalendarDeltaRound:
    try:
        return asyncio.run(
            asyncio.wait_for(
                _calendar_delta_round(
                    authorization,
                    window_start,
                    window_end,
                    cursor=cursor,
                ),
                timeout=MAX_CALENDAR_ROUND_SECONDS,
            )
        )
    except TimeoutError as exc:
        raise Microsoft365Error("Microsoft Graph calendar round exceeded its time limit") from exc
