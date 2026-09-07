from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from eom_email_watcher import microsoft_calendar
from eom_email_watcher.microsoft365 import MicrosoftAuthorizationRejected
from eom_email_watcher.microsoft_calendar import (
    CALENDAR_PAGE_SIZE,
    CALENDAR_READ_SCOPES,
    MAX_CALENDAR_PAGE_BYTES,
    MicrosoftCalendarConsentPending,
    MicrosoftCalendarProposalAuthorization,
    MicrosoftCalendarReadAuthorization,
    MicrosoftCalendarWriteAuthorization,
    MicrosoftPrincipal,
    StaleCalendarCursor,
    calendar_delta_round,
    canonical_calendar_window,
    microsoft_cached_mailbox_principal,
    microsoft_mailbox_principal,
)

CLIENT_ID = "11111111-2222-4333-8444-555555555555"
TENANT_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
OBJECT_ID = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"


def write_public_client(path: Path) -> None:
    path.write_text(
        json.dumps({"client_id": CLIENT_ID, "tenant": TENANT_ID}),
        encoding="utf-8",
    )


def principal() -> MicrosoftPrincipal:
    return MicrosoftPrincipal(
        home_account_id=f"{OBJECT_ID}.{TENANT_ID}",
        tenant_id=TENANT_ID,
        object_id=OBJECT_ID,
        email_address="owner@example.com",
    )


class FakeCache:
    def __init__(self) -> None:
        self.serialized = "{}"
        self.has_state_changed = False

    def deserialize(self, value: str) -> None:
        self.serialized = value
        self.has_state_changed = False

    def serialize(self) -> str:
        return self.serialized


def test_calendar_authorization_requests_only_read_and_records_immutable_principal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials = tmp_path / "microsoft.json"
    token_file = tmp_path / "calendar-read-cache.json"
    write_public_client(credentials)
    cache = FakeCache()
    calls: list[tuple[list[str], dict[str, object]]] = []

    class FakeApplication:
        def acquire_token_interactive(self, scopes: list[str], **kwargs):
            calls.append((scopes, kwargs))
            cache.serialized = '{"RefreshToken":{"secret":"private-refresh"}}'
            cache.has_state_changed = True
            return {
                "access_token": "private-access",
                "id_token_claims": {
                    "preferred_username": "OWNER@Example.com",
                    "tid": TENANT_ID,
                    "oid": OBJECT_ID,
                },
            }

        def get_accounts(self):
            return [
                {
                    "home_account_id": f"{OBJECT_ID}.{TENANT_ID}",
                    "local_account_id": OBJECT_ID,
                    "realm": TENANT_ID,
                    "username": "owner@example.com",
                }
            ]

    monkeypatch.setattr(microsoft_calendar.msal, "SerializableTokenCache", lambda: cache)
    monkeypatch.setattr(
        microsoft_calendar,
        "_new_public_client",
        lambda configuration, selected_cache: FakeApplication(),
    )

    authorization, changed = MicrosoftCalendarReadAuthorization.authorize_with_status(
        credentials,
        token_file,
    )

    assert changed is True
    assert authorization.principal == principal()
    assert calls == [
        (
            ["Calendars.Read"],
            {"prompt": "select_account", "timeout": 300, "port": 0},
        )
    ]
    assert CALENDAR_READ_SCOPES == ("Calendars.Read",)
    assert "private-refresh" in token_file.read_text(encoding="utf-8")
    assert token_file.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    ("authorization_type", "scope"),
    [
        (MicrosoftCalendarReadAuthorization, "Calendars.Read"),
        (MicrosoftCalendarProposalAuthorization, "Calendars.Read.Shared"),
        (MicrosoftCalendarWriteAuthorization, "Calendars.ReadWrite"),
    ],
)
def test_calendar_cache_validation_requests_only_its_exact_scope(
    authorization_type,
    scope: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials = tmp_path / "microsoft.json"
    token_file = tmp_path / "calendar-cache.json"
    write_public_client(credentials)
    token_file.write_text("private-cache", encoding="utf-8")
    cache = FakeCache()
    calls: list[list[str]] = []

    class FakeApplication:
        def acquire_token_silent_with_error(self, scopes: list[str], *, account: object):
            calls.append(scopes)
            return {
                "access_token": "private-access",
                "id_token_claims": {
                    "preferred_username": "owner@example.com",
                    "tid": TENANT_ID,
                    "oid": OBJECT_ID,
                },
            }

        def get_accounts(self):
            return [
                {
                    "home_account_id": f"{OBJECT_ID}.{TENANT_ID}",
                    "local_account_id": OBJECT_ID,
                    "realm": TENANT_ID,
                    "username": "owner@example.com",
                }
            ]

    monkeypatch.setattr(microsoft_calendar, "_load_cache", lambda path: cache)
    monkeypatch.setattr(
        microsoft_calendar,
        "_new_public_client",
        lambda configuration, selected_cache: FakeApplication(),
    )

    authorization = authorization_type.from_token(credentials, token_file)

    assert authorization.principal == principal()
    assert calls == [[scope]]


def test_mailbox_principal_lookup_requests_only_mail_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials = tmp_path / "microsoft.json"
    token_file = tmp_path / "mail-read-cache.json"
    write_public_client(credentials)
    token_file.write_text("private-cache", encoding="utf-8")
    cache = FakeCache()
    calls: list[list[str]] = []

    class FakeApplication:
        def acquire_token_silent_with_error(self, scopes: list[str], *, account: object):
            calls.append(scopes)
            return {
                "access_token": "private-access",
                "id_token_claims": {
                    "preferred_username": "owner@example.com",
                    "tid": TENANT_ID,
                    "oid": OBJECT_ID,
                },
            }

        def get_accounts(self):
            return [
                {
                    "home_account_id": f"{OBJECT_ID}.{TENANT_ID}",
                    "local_account_id": OBJECT_ID,
                    "realm": TENANT_ID,
                    "username": "owner@example.com",
                }
            ]

    monkeypatch.setattr(microsoft_calendar, "_load_cache", lambda path: cache)
    monkeypatch.setattr(
        microsoft_calendar,
        "_new_public_client",
        lambda configuration, selected_cache: FakeApplication(),
    )

    selected = microsoft_mailbox_principal(credentials, token_file)

    assert selected == principal()
    assert calls == [["Mail.Read"]]


@pytest.mark.parametrize(
    ("calendar_object_id", "mailbox_object_id", "accepted"),
    [
        (OBJECT_ID, OBJECT_ID, True),
        ("cccccccc-dddd-4eee-8fff-000000000000", OBJECT_ID, False),
        (OBJECT_ID, "cccccccc-dddd-4eee-8fff-000000000000", False),
    ],
)
def test_calendar_authorization_requires_matching_grant_and_mailbox_principals(
    calendar_object_id: str,
    mailbox_object_id: str,
    accepted: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials = tmp_path / "microsoft.json"
    calendar_token = tmp_path / "calendar-cache.json"
    mailbox_token = tmp_path / "mailbox-cache.json"

    def selected_principal(object_id: str) -> MicrosoftPrincipal:
        return MicrosoftPrincipal(
            home_account_id=f"{object_id}.{TENANT_ID}",
            tenant_id=TENANT_ID,
            object_id=object_id,
            email_address="owner@example.com",
        )

    calendar_authorization = MicrosoftCalendarProposalAuthorization(
        selected_principal(calendar_object_id),
        "private-access-token",
    )
    monkeypatch.setattr(
        MicrosoftCalendarProposalAuthorization,
        "from_token",
        classmethod(lambda cls, credentials_file, token_file: calendar_authorization),
    )
    monkeypatch.setattr(
        microsoft_calendar,
        "microsoft_mailbox_principal",
        lambda credentials_file, token_file: selected_principal(mailbox_object_id),
    )

    if accepted:
        authorization = MicrosoftCalendarProposalAuthorization.from_matching_tokens(
            credentials,
            calendar_token,
            mailbox_token,
            principal().key,
        )
        assert authorization is calendar_authorization
    else:
        with pytest.raises(MicrosoftAuthorizationRejected, match="principal"):
            MicrosoftCalendarProposalAuthorization.from_matching_tokens(
                credentials,
                calendar_token,
                mailbox_token,
                principal().key,
            )


def test_cached_mailbox_principal_lookup_does_not_construct_a_token_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_file = tmp_path / "mail-read-cache.json"
    token_file.write_text("private-cache", encoding="utf-8")
    searches: list[str] = []

    class LocalCache:
        def search(self, credential_type: str):
            searches.append(credential_type)
            return [
                {
                    "home_account_id": f"{OBJECT_ID}.{TENANT_ID}",
                    "local_account_id": OBJECT_ID,
                    "realm": TENANT_ID,
                    "username": "owner@example.com",
                }
            ]

    monkeypatch.setattr(microsoft_calendar, "_load_cache", lambda path: LocalCache())
    monkeypatch.setattr(
        microsoft_calendar,
        "_new_public_client",
        lambda *args: pytest.fail("Offline principal lookup constructed a token client"),
    )

    selected = microsoft_cached_mailbox_principal(token_file)

    assert selected == principal()
    assert searches == ["Account"]


@pytest.mark.parametrize(
    "error",
    ["authorization_pending", "consent_required", "interaction_required"],
)
def test_calendar_authorization_reports_consent_pending(
    error: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials = tmp_path / "microsoft.json"
    write_public_client(credentials)

    class PendingApplication:
        def acquire_token_interactive(self, scopes: list[str], **kwargs):
            return {"error": error}

    monkeypatch.setattr(
        microsoft_calendar,
        "_new_public_client",
        lambda configuration, cache: PendingApplication(),
    )

    with pytest.raises(MicrosoftCalendarConsentPending):
        MicrosoftCalendarReadAuthorization.authorize_with_status(
            credentials,
            tmp_path / "calendar-read-cache.json",
        )


@pytest.mark.parametrize(
    "result",
    [
        {
            "error": "access_denied",
            "error_description": "AADSTS65001: User or administrator consent is missing.",
        },
        {
            "error": "access_denied",
            "error_description": "AADSTS90094: Administrator consent is required.",
        },
        {"error": "access_denied", "error_codes": [90095]},
    ],
)
def test_calendar_authorization_reports_documented_consent_codes_as_pending(
    result: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials = tmp_path / "microsoft.json"
    write_public_client(credentials)

    class PendingApplication:
        def acquire_token_interactive(self, scopes: list[str], **kwargs):
            return result

    monkeypatch.setattr(
        microsoft_calendar,
        "_new_public_client",
        lambda configuration, cache: PendingApplication(),
    )

    with pytest.raises(MicrosoftCalendarConsentPending):
        MicrosoftCalendarReadAuthorization.authorize_with_status(
            credentials,
            tmp_path / "calendar-read-cache.json",
        )


@pytest.mark.parametrize(
    "result",
    [
        {
            "error": "access_denied",
            "error_description": "AADSTS65004: The user declined consent.",
            "error_codes": [65004],
        },
        {
            "error": "access_denied",
            "error_description": "AADSTS99999: An unrecognized failure.",
        },
    ],
)
def test_calendar_authorization_does_not_treat_other_codes_as_pending(
    result: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials = tmp_path / "microsoft.json"
    write_public_client(credentials)

    class RejectedApplication:
        def acquire_token_interactive(self, scopes: list[str], **kwargs):
            return result

    monkeypatch.setattr(
        microsoft_calendar,
        "_new_public_client",
        lambda configuration, cache: RejectedApplication(),
    )

    with pytest.raises(MicrosoftAuthorizationRejected):
        MicrosoftCalendarReadAuthorization.authorize_with_status(
            credentials,
            tmp_path / "calendar-read-cache.json",
        )


@pytest.mark.parametrize(
    ("authorization_type", "scope"),
    [
        (MicrosoftCalendarReadAuthorization, "Calendars.Read"),
        (MicrosoftCalendarProposalAuthorization, "Calendars.Read.Shared"),
        (MicrosoftCalendarWriteAuthorization, "Calendars.ReadWrite"),
    ],
)
def test_calendar_profiles_request_only_their_exact_scope(
    authorization_type,
    scope: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials = tmp_path / "microsoft.json"
    write_public_client(credentials)
    cache = FakeCache()
    calls: list[list[str]] = []

    class FakeApplication:
        def acquire_token_interactive(self, scopes: list[str], **kwargs):
            calls.append(scopes)
            cache.has_state_changed = True
            return {
                "access_token": "private-access",
                "id_token_claims": {"tid": TENANT_ID, "oid": OBJECT_ID},
            }

        def get_accounts(self):
            return [
                {
                    "home_account_id": f"{OBJECT_ID}.{TENANT_ID}",
                    "local_account_id": OBJECT_ID,
                    "realm": TENANT_ID,
                    "username": "owner@example.com",
                }
            ]

    monkeypatch.setattr(microsoft_calendar.msal, "SerializableTokenCache", lambda: cache)
    monkeypatch.setattr(
        microsoft_calendar,
        "_new_public_client",
        lambda configuration, selected_cache: FakeApplication(),
    )

    authorization_type.authorize_with_status(credentials, tmp_path / f"{scope}.json")

    assert calls == [[scope]]


def test_calendar_window_validation_pins_both_sides_of_duration_and_offsets() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    exact_end = start + timedelta(days=366)
    canonical = canonical_calendar_window(start.isoformat(), exact_end.isoformat())

    assert canonical == ("2026-01-01T00:00:00.000000Z", "2027-01-02T00:00:00.000000Z")
    with pytest.raises(ValueError, match="cannot exceed"):
        canonical_calendar_window(
            start.isoformat(),
            (exact_end + timedelta(microseconds=1)).isoformat(),
        )
    with pytest.raises(ValueError, match="offset-bearing"):
        canonical_calendar_window("2026-01-01T00:00:00", exact_end.isoformat())
    with pytest.raises(ValueError, match="offset-bearing"):
        canonical_calendar_window("2026-01-01T00:00:00-00:00", exact_end.isoformat())
    with pytest.raises(ValueError, match="after its start"):
        canonical_calendar_window(start.isoformat(), start.isoformat())
    with pytest.raises(ValueError, match="offset-bearing"):
        canonical_calendar_window("2026-01-01T00:00:00.1234567Z", exact_end.isoformat())
    with pytest.raises(ValueError, match="offset-bearing"):
        canonical_calendar_window("0001-01-01T00:00:00+14:00", exact_end.isoformat())
    with pytest.raises(ValueError, match="offset-bearing"):
        canonical_calendar_window(start.isoformat(), "9999-12-31T23:59:59-14:00")


def calendar_event(event_id: str, subject: str = "Planning") -> dict[str, object]:
    return {
        "id": event_id,
        "subject": subject,
        "start": {"dateTime": "2026-09-07T09:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-09-07T10:00:00.0000000", "timeZone": "UTC"},
        "isAllDay": False,
        "location": {"displayName": "Office"},
    }


def test_calendar_delta_round_paginates_and_preserves_ordered_changes() -> None:
    next_link = f"{microsoft_calendar.GRAPH_ROOT}/me/calendarView/delta?%24skiptoken=next"
    delta_link = f"{microsoft_calendar.GRAPH_ROOT}/me/calendarView/delta?%24deltatoken=done"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.params.get("$skiptoken") == "next":
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "event-1", "@removed": {"reason": "deleted"}},
                        calendar_event("event-2"),
                    ],
                    "@odata.deltaLink": delta_link,
                },
            )
        return httpx.Response(
            200,
            json={"value": [calendar_event("event-1")], "@odata.nextLink": next_link},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    authorization = MicrosoftCalendarReadAuthorization(
        principal(),
        "private-access",
        http_client=client,
    )

    result = calendar_delta_round(
        authorization,
        "2026-09-01T00:00:00.000000Z",
        "2026-10-01T00:00:00.000000Z",
    )

    assert [change.event_id for change in result.changes] == ["event-1", "event-1", "event-2"]
    assert [change.event is None for change in result.changes] == [False, True, False]
    assert result.cursor == delta_link
    assert len(requests) == 2
    assert requests[0].url.params.get("startDateTime") == "2026-09-01T00:00:00.000000Z"
    assert requests[0].url.params.get("endDateTime") == "2026-10-01T00:00:00.000000Z"
    assert "$select" not in requests[0].url.params
    assert requests[0].headers["prefer"] == (
        f'IdType="ImmutableId", odata.maxpagesize={CALENDAR_PAGE_SIZE}'
    )
    assert requests[0].headers["accept-encoding"] == "identity"
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("subject", "Planning\nsecret"),
        ("start", {"dateTime": "2026-02-30T09:00:00", "timeZone": "UTC"}),
        ("end", {"dateTime": "not-a-date", "timeZone": "UTC"}),
    ],
)
def test_calendar_delta_rejects_malformed_event_projection_fields(
    field: str,
    value: object,
) -> None:
    event = calendar_event("event-1")
    event[field] = value
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "value": [event],
                    "@odata.deltaLink": (
                        f"{microsoft_calendar.GRAPH_ROOT}/me/calendarView/delta?%24deltatoken=done"
                    ),
                },
            )
        )
    )
    authorization = MicrosoftCalendarReadAuthorization(
        principal(),
        "private-access",
        http_client=client,
    )

    with pytest.raises(microsoft_calendar.Microsoft365Error, match="invalid calendar"):
        calendar_delta_round(
            authorization,
            "2026-09-01T00:00:00.000000Z",
            "2026-10-01T00:00:00.000000Z",
        )


def test_calendar_text_accepts_unicode_formatting_and_rejects_unsafe_controls() -> None:
    family_emoji = "Family 👨\u200d👩\u200d👧\u200d👦 review"

    assert (
        microsoft_calendar._bounded_graph_text(
            family_emoji,
            "subject",
            microsoft_calendar.MAX_CALENDAR_SUBJECT_BYTES,
            allow_empty=False,
        )
        == family_emoji
    )
    for rejected in ("line\nfeed", "surrogate-\ud800"):
        with pytest.raises(microsoft_calendar.Microsoft365Error, match="invalid calendar subject"):
            microsoft_calendar._bounded_graph_text(
                rejected,
                "subject",
                microsoft_calendar.MAX_CALENDAR_SUBJECT_BYTES,
                allow_empty=False,
            )


@pytest.mark.parametrize(
    "cursor",
    [
        "http://graph.microsoft.com/v1.0/me/calendarView/delta?$deltatoken=x",
        "https://evil.example/v1.0/me/calendarView/delta?$deltatoken=x",
        "https://user@graph.microsoft.com/v1.0/me/calendarView/delta?$deltatoken=x",
        "https://graph.microsoft.com:443/v1.0/me/calendarView/delta?$deltatoken=x",
        "https://graph.microsoft.com/v1.0/me/events/delta?$deltatoken=x",
        "https://graph.microsoft.com/v1.0/me/calendarView/delta?$skiptoken=x",
        "https://graph.microsoft.com/v1.0/me/calendarView/delta?$deltatoken=x&extra=y",
    ],
)
def test_calendar_delta_rejects_hostile_cursor_before_http(cursor: str) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: pytest.fail("HTTP request was reached"))
    )
    authorization = MicrosoftCalendarReadAuthorization(
        principal(),
        "private-access",
        http_client=client,
    )

    with pytest.raises(microsoft_calendar.Microsoft365Error, match="invalid calendar cursor"):
        calendar_delta_round(
            authorization,
            "2026-09-01T00:00:00.000000Z",
            "2026-10-01T00:00:00.000000Z",
            cursor=cursor,
        )


def test_calendar_delta_reports_stale_cursor() -> None:
    cursor = f"{microsoft_calendar.GRAPH_ROOT}/me/calendarView/delta?%24deltatoken=old"
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                410,
                json={"error": {"code": "syncStateNotFound"}},
            )
        )
    )
    authorization = MicrosoftCalendarReadAuthorization(
        principal(),
        "private-access",
        http_client=client,
    )

    with pytest.raises(StaleCalendarCursor):
        calendar_delta_round(
            authorization,
            "2026-09-01T00:00:00.000000Z",
            "2026-10-01T00:00:00.000000Z",
            cursor=cursor,
        )


@pytest.mark.parametrize(
    ("status", "error_type", "message"),
    [
        (401, MicrosoftAuthorizationRejected, "rejected"),
        (410, StaleCalendarCursor, "cursor is unavailable"),
        (429, microsoft_calendar.Microsoft365Error, "temporarily unavailable"),
        (503, microsoft_calendar.Microsoft365Error, "temporarily unavailable"),
    ],
)
def test_calendar_delta_classifies_bounded_error_status_before_json(
    status: int,
    error_type: type[Exception],
    message: str,
) -> None:
    cursor = f"{microsoft_calendar.GRAPH_ROOT}/me/calendarView/delta?%24deltatoken=old"
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, content=b"not-json"))
    )
    authorization = MicrosoftCalendarReadAuthorization(
        principal(),
        "private-access",
        http_client=client,
    )

    with pytest.raises(error_type, match=message):
        calendar_delta_round(
            authorization,
            "2026-09-01T00:00:00.000000Z",
            "2026-10-01T00:00:00.000000Z",
            cursor=cursor,
        )


@pytest.mark.parametrize(
    "document",
    [
        {"value": []},
        {
            "value": [],
            "@odata.nextLink": (
                f"{microsoft_calendar.GRAPH_ROOT}/me/calendarView/delta?%24skiptoken=next"
            ),
            "@odata.deltaLink": 7,
        },
    ],
)
def test_calendar_delta_requires_exactly_one_well_typed_continuation(
    document: dict[str, object],
) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=document))
    )
    authorization = MicrosoftCalendarReadAuthorization(
        principal(),
        "private-access",
        http_client=client,
    )

    with pytest.raises(microsoft_calendar.Microsoft365Error, match="single continuation"):
        calendar_delta_round(
            authorization,
            "2026-09-01T00:00:00.000000Z",
            "2026-10-01T00:00:00.000000Z",
        )


def test_calendar_page_byte_limit_checks_exact_boundary_before_json() -> None:
    at_limit = httpx.Response(200, content=b" " * MAX_CALENDAR_PAGE_BYTES)
    over_limit = httpx.Response(200, content=b" " * (MAX_CALENDAR_PAGE_BYTES + 1))

    with pytest.raises(microsoft_calendar.Microsoft365Error, match="not valid JSON"):
        asyncio.run(
            microsoft_calendar._bounded_graph_document(
                at_limit,
                float("inf"),
                microsoft_calendar.MAX_CALENDAR_ROUND_BYTES,
            )
        )
    with pytest.raises(microsoft_calendar.Microsoft365Error, match="exceeded its byte limit"):
        asyncio.run(
            microsoft_calendar._bounded_graph_document(
                over_limit,
                float("inf"),
                microsoft_calendar.MAX_CALENDAR_ROUND_BYTES,
            )
        )


def test_calendar_page_stream_enforces_absolute_round_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SlowResponse:
        headers: dict[str, str] = {}

        @staticmethod
        async def aiter_bytes():
            yield b'{"value":'
            yield b"[]}"

    clock = iter((0.0, microsoft_calendar.MAX_CALENDAR_ROUND_SECONDS + 1.0))
    monkeypatch.setattr(microsoft_calendar, "_calendar_monotonic", lambda: next(clock))

    with pytest.raises(microsoft_calendar.Microsoft365Error, match="time limit"):
        asyncio.run(
            microsoft_calendar._bounded_graph_document(  # type: ignore[arg-type]
                SlowResponse(),
                microsoft_calendar.MAX_CALENDAR_ROUND_SECONDS,
                microsoft_calendar.MAX_CALENDAR_ROUND_BYTES,
            )
        )


def test_calendar_page_stream_enforces_remaining_round_byte_limit() -> None:
    class StreamingResponse:
        headers: dict[str, str] = {}

        @staticmethod
        async def aiter_bytes():
            yield b'{"value":'
            yield b"[]}"

    with pytest.raises(microsoft_calendar.Microsoft365Error, match="round exceeded its byte"):
        asyncio.run(
            microsoft_calendar._bounded_graph_document(  # type: ignore[arg-type]
                StreamingResponse(),
                float("inf"),
                len(b'{"value":[]') - 1,
            )
        )


def test_calendar_round_deadline_interrupts_wait_for_response_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancelled = threading.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    monkeypatch.setattr(microsoft_calendar, "MAX_CALENDAR_ROUND_SECONDS", 0.05)
    authorization = MicrosoftCalendarReadAuthorization(
        principal(),
        "private-access",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    started_at = time.monotonic()
    with pytest.raises(microsoft_calendar.Microsoft365Error, match="time limit"):
        calendar_delta_round(
            authorization,
            "2026-09-01T00:00:00.000000Z",
            "2026-10-01T00:00:00.000000Z",
        )

    assert time.monotonic() - started_at < 1.0
    assert cancelled.is_set()


def test_calendar_round_deadline_interrupts_wait_for_next_body_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancelled = threading.Event()

    class BlockingBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"value":'
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.set()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=BlockingBody())

    monkeypatch.setattr(microsoft_calendar, "MAX_CALENDAR_ROUND_SECONDS", 0.05)
    authorization = MicrosoftCalendarReadAuthorization(
        principal(),
        "private-access",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    started_at = time.monotonic()
    with pytest.raises(microsoft_calendar.Microsoft365Error, match="time limit"):
        calendar_delta_round(
            authorization,
            "2026-09-01T00:00:00.000000Z",
            "2026-10-01T00:00:00.000000Z",
        )

    assert time.monotonic() - started_at < 1.0
    assert cancelled.is_set()


def test_calendar_delta_disables_and_rejects_response_compression() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"Content-Encoding": "gzip"},
            stream=httpx.ByteStream(b"compressed-body-is-not-materialized"),
        )

    authorization = MicrosoftCalendarReadAuthorization(
        principal(),
        "private-access",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(microsoft_calendar.Microsoft365Error, match="compressed calendar response"):
        calendar_delta_round(
            authorization,
            "2026-09-01T00:00:00.000000Z",
            "2026-10-01T00:00:00.000000Z",
        )

    assert requests[0].headers["accept-encoding"] == "identity"


def test_calendar_delta_page_limit_accepts_64_and_rejects_page_65() -> None:
    def run(final_page: int):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == final_page:
                return httpx.Response(
                    200,
                    json={
                        "value": [],
                        "@odata.deltaLink": (
                            f"{microsoft_calendar.GRAPH_ROOT}/me/calendarView/delta"
                            "?%24deltatoken=done"
                        ),
                    },
                )
            return httpx.Response(
                200,
                json={
                    "value": [],
                    "@odata.nextLink": (
                        f"{microsoft_calendar.GRAPH_ROOT}/me/calendarView/delta"
                        f"?%24skiptoken=page-{calls + 1}"
                    ),
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        authorization = MicrosoftCalendarReadAuthorization(
            principal(),
            "private-access",
            http_client=client,
        )
        return authorization, lambda: calls

    accepted, accepted_calls = run(64)
    result = calendar_delta_round(
        accepted,
        "2026-09-01T00:00:00.000000Z",
        "2026-10-01T00:00:00.000000Z",
    )
    assert result.changes == ()
    assert accepted_calls() == 64

    rejected, rejected_calls = run(65)
    with pytest.raises(microsoft_calendar.Microsoft365Error, match="page limit"):
        calendar_delta_round(
            rejected,
            "2026-09-01T00:00:00.000000Z",
            "2026-10-01T00:00:00.000000Z",
        )
    assert rejected_calls() == 64


def test_calendar_delta_entry_limit_accepts_3200_and_rejects_3201() -> None:
    def run(extra_entry: bool):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            final = calls == 33 if extra_entry else calls == 32
            entry_count = 1 if calls == 33 else 100
            continuation = (
                {
                    "@odata.deltaLink": (
                        f"{microsoft_calendar.GRAPH_ROOT}/me/calendarView/delta?%24deltatoken=done"
                    )
                }
                if final
                else {
                    "@odata.nextLink": (
                        f"{microsoft_calendar.GRAPH_ROOT}/me/calendarView/delta"
                        f"?%24skiptoken=page-{calls + 1}"
                    )
                }
            )
            values = [calendar_event(f"event-{calls}-{index}") for index in range(entry_count)]
            return httpx.Response(200, json={"value": values, **continuation})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return MicrosoftCalendarReadAuthorization(
            principal(),
            "private-access",
            http_client=client,
        )

    accepted = calendar_delta_round(
        run(False),
        "2026-09-01T00:00:00.000000Z",
        "2026-10-01T00:00:00.000000Z",
    )
    assert len(accepted.changes) == 3_200

    with pytest.raises(microsoft_calendar.Microsoft365Error, match="entry limit"):
        calendar_delta_round(
            run(True),
            "2026-09-01T00:00:00.000000Z",
            "2026-10-01T00:00:00.000000Z",
        )


def test_calendar_cursor_byte_limit_accepts_exactly_32_kib() -> None:
    prefix = f"{microsoft_calendar.GRAPH_ROOT}/me/calendarView/delta?%24deltatoken="
    exact = prefix + "x" * (microsoft_calendar.MAX_GRAPH_URL_LENGTH - len(prefix.encode()))

    assert microsoft_calendar._safe_calendar_continuation(exact, "$deltatoken") == exact
    with pytest.raises(microsoft_calendar.Microsoft365Error, match="invalid calendar cursor"):
        microsoft_calendar._safe_calendar_continuation(exact + "x", "$deltatoken")


def test_calendar_round_byte_limit_accepts_16_mib_and_rejects_one_page_more(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run(final_page: int) -> MicrosoftCalendarReadAuthorization:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"{}")

        async def bounded(
            response: httpx.Response,
            deadline: float,
            remaining_round_bytes: int,
        ):
            del deadline, response
            nonlocal calls
            calls += 1
            if remaining_round_bytes < MAX_CALENDAR_PAGE_BYTES:
                raise microsoft_calendar.Microsoft365Error(
                    "Microsoft Graph calendar round exceeded its byte limit"
                )
            continuation = (
                {
                    "@odata.deltaLink": (
                        f"{microsoft_calendar.GRAPH_ROOT}/me/calendarView/delta?%24deltatoken=done"
                    )
                }
                if calls == final_page
                else {
                    "@odata.nextLink": (
                        f"{microsoft_calendar.GRAPH_ROOT}/me/calendarView/delta"
                        f"?%24skiptoken=page-{calls + 1}"
                    )
                }
            )
            return {"value": [], **continuation}, MAX_CALENDAR_PAGE_BYTES

        monkeypatch.setattr(microsoft_calendar, "_bounded_graph_document", bounded)
        return MicrosoftCalendarReadAuthorization(
            principal(),
            "private-access",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    accepted = calendar_delta_round(
        run(8),
        "2026-09-01T00:00:00.000000Z",
        "2026-10-01T00:00:00.000000Z",
    )
    assert accepted.changes == ()

    with pytest.raises(microsoft_calendar.Microsoft365Error, match="round exceeded its byte"):
        calendar_delta_round(
            run(9),
            "2026-09-01T00:00:00.000000Z",
            "2026-10-01T00:00:00.000000Z",
        )


def test_calendar_round_deadline_stops_pagination_before_another_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "value": [],
                "@odata.nextLink": (
                    f"{microsoft_calendar.GRAPH_ROOT}/me/calendarView/delta?%24skiptoken=next"
                ),
            },
        )

    clock = iter(
        (
            0.0,
            0.0,
            0.0,
            0.0,
            microsoft_calendar.MAX_CALENDAR_ROUND_SECONDS + 1.0,
        )
    )
    monkeypatch.setattr(microsoft_calendar, "_calendar_monotonic", lambda: next(clock))
    authorization = MicrosoftCalendarReadAuthorization(
        principal(),
        "private-access",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(microsoft_calendar.Microsoft365Error, match="time limit"):
        calendar_delta_round(
            authorization,
            "2026-09-01T00:00:00.000000Z",
            "2026-10-01T00:00:00.000000Z",
        )
    assert calls == 1
