from __future__ import annotations

import json
from pathlib import Path

import pytest

from eom_email_watcher import microsoft_calendar
from eom_email_watcher.microsoft_calendar import (
    CALENDAR_READ_SCOPES,
    MicrosoftCalendarConsentPending,
    MicrosoftCalendarReadAuthorization,
    MicrosoftPrincipal,
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


def test_calendar_cache_validation_requests_only_calendar_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials = tmp_path / "microsoft.json"
    token_file = tmp_path / "calendar-read-cache.json"
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

    authorization = MicrosoftCalendarReadAuthorization.from_token(credentials, token_file)

    assert authorization.principal == principal()
    assert calls == [["Calendars.Read"]]


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
