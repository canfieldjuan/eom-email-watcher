from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from eom_email_watcher import microsoft365
from eom_email_watcher.mailbox import (
    MailboxMessageUnavailable,
    StaleMailboxCursor,
)
from eom_email_watcher.microsoft365 import (
    GRAPH_ROOT,
    Microsoft365Error,
    Microsoft365Gateway,
    MicrosoftAuthorizationRejected,
    MicrosoftConfigurationError,
    load_microsoft_public_client,
)
from eom_email_watcher.runtime import load_runtime, mail_account_token_file

CLIENT_ID = "11111111-2222-4333-8444-555555555555"
TENANT_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def write_public_client(path: Path, *, tenant: str = "organizations") -> None:
    path.write_text(
        json.dumps({"client_id": CLIENT_ID, "tenant": tenant}),
        encoding="utf-8",
    )


def write_config(path: Path) -> None:
    path.write_text(
        f'''gmail_credentials_file = "{path.parent / "google.json"}"
microsoft_credentials_file = "{path.parent / "microsoft.json"}"
gmail_token_file = "{path.parent / "gmail-token.json"}"
gmail_send_token_file = "{path.parent / "gmail-send-token.json"}"
database_file = "{path.parent / "watcher.sqlite3"}"
model_base_url = "http://127.0.0.1:1234/v1"
model_name = "local-model"
model_require_auth = false
''',
        encoding="utf-8",
    )


def graph_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_gateway_suppresses_transport_url_logging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    httpx_logger = logging.getLogger("httpx")
    httpcore_logger = logging.getLogger("httpcore")
    monkeypatch.setattr(httpx_logger, "level", logging.INFO)
    monkeypatch.setattr(httpcore_logger, "level", logging.DEBUG)

    Microsoft365Gateway(
        "private-access",
        "owner@example.com",
        graph_client(lambda request: httpx.Response(200, json={})),
    )

    assert httpx_logger.level == logging.WARNING
    assert httpcore_logger.level == logging.WARNING


def test_public_client_configuration_is_non_secret_and_tenant_bounded(tmp_path: Path) -> None:
    path = tmp_path / "microsoft.json"
    write_public_client(path, tenant=TENANT_ID.upper())

    configuration = load_microsoft_public_client(path)

    assert configuration.client_id == CLIENT_ID
    assert configuration.tenant == TENANT_ID
    assert configuration.authority == f"https://login.microsoftonline.com/{TENANT_ID}"


@pytest.mark.parametrize(
    "document",
    [
        {"client_id": CLIENT_ID, "tenant": "common"},
        {"client_id": CLIENT_ID, "tenant": "organizations", "client_secret": "secret"},
        {"client_id": "not-a-uuid", "tenant": "organizations"},
        {"client_id": "00000000-0000-0000-0000-000000000000"},
        [],
    ],
)
def test_public_client_configuration_rejects_unsafe_shapes(
    tmp_path: Path,
    document: object,
) -> None:
    path = tmp_path / "microsoft.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(MicrosoftConfigurationError):
        load_microsoft_public_client(path)


def test_public_client_configuration_resolves_packaged_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "bundle"
    bundled_file = bundle / "eom_email_watcher_data/microsoft-oauth-client.json"
    bundled_file.parent.mkdir(parents=True)
    write_public_client(bundled_file)
    monkeypatch.setattr(microsoft365.sys, "_MEIPASS", str(bundle), raising=False)

    configuration = load_microsoft_public_client(tmp_path / "missing.json")

    assert configuration.client_id == CLIENT_ID
    assert configuration.tenant == "organizations"


class FakeCache:
    def __init__(self) -> None:
        self.serialized = "{}"
        self.has_state_changed = False

    def deserialize(self, value: str) -> None:
        self.serialized = value
        self.has_state_changed = False

    def serialize(self) -> str:
        return self.serialized


def test_interactive_authorization_uses_only_mail_read_and_writes_private_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials = tmp_path / "microsoft.json"
    token_file = tmp_path / "account-cache.json"
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
                "id_token_claims": {"preferred_username": "OWNER@Example.com"},
            }

        def get_accounts(self):
            return [{"username": "owner@example.com"}]

    monkeypatch.setattr(microsoft365.msal, "SerializableTokenCache", lambda: cache)
    monkeypatch.setattr(
        microsoft365,
        "_new_public_client",
        lambda configuration, selected_cache: FakeApplication(),
    )

    gateway, changed = Microsoft365Gateway.authorize_with_status(
        credentials,
        token_file,
        force_reauthorize=True,
    )

    assert changed is True
    assert gateway.profile().email_address == "owner@example.com"
    assert calls == [
        (
            ["Mail.Read"],
            {"prompt": "select_account", "timeout": 300, "port": 0},
        )
    ]
    assert "private-refresh" in token_file.read_text(encoding="utf-8")
    assert token_file.stat().st_mode & 0o777 == 0o600


def test_silent_refresh_persists_updated_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials = tmp_path / "microsoft.json"
    token_file = tmp_path / "account-cache.json"
    write_public_client(credentials)
    token_file.write_text("old-cache", encoding="utf-8")
    cache = FakeCache()
    calls: list[list[str]] = []

    class FakeApplication:
        def get_accounts(self):
            return [{"username": "owner@example.com"}]

        def acquire_token_silent_with_error(self, scopes: list[str], *, account: object):
            calls.append(scopes)
            assert account == {"username": "owner@example.com"}
            cache.serialized = "refreshed-cache"
            cache.has_state_changed = True
            return {"access_token": "refreshed-access"}

    monkeypatch.setattr(microsoft365.msal, "SerializableTokenCache", lambda: cache)
    monkeypatch.setattr(
        microsoft365,
        "_new_public_client",
        lambda configuration, selected_cache: FakeApplication(),
    )

    gateway = Microsoft365Gateway.from_token(credentials, token_file)

    assert gateway.profile().email_address == "owner@example.com"
    assert calls == [["Mail.Read"]]
    assert token_file.read_text(encoding="utf-8") == "refreshed-cache"


@pytest.mark.parametrize(
    "cursor",
    [
        "http://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta?$deltatoken=x",
        "https://evil.example/v1.0/me/mailFolders/inbox/messages/delta?$deltatoken=x",
        "https://graph.microsoft.com.evil.example/v1.0/me/mailFolders/inbox/messages/delta?$deltatoken=x",
        "https://user@graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta?$deltatoken=x",
        "https://graph.microsoft.com:443/v1.0/me/mailFolders/inbox/messages/delta?$deltatoken=x",
        "https://graph.microsoft.com/v1.0/me/messages/delta?$deltatoken=x",
        "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta?$skiptoken=x",
        "https://graph.microsoft.com/v1.0/me/mailfolders('inbox')/messages/delta?$skiptoken=x",
        "https://graph.microsoft.com/v1.0/me/mailfolders('in/box')/messages/delta?$deltatoken=x",
    ],
)
def test_changes_reject_unsafe_persisted_cursor_without_network(
    cursor: str,
) -> None:
    def unexpected_request(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"Unsafe cursor reached the network: {request.url}")

    gateway = Microsoft365Gateway(
        "private-access",
        "owner@example.com",
        graph_client(unexpected_request),
    )

    with pytest.raises(Microsoft365Error, match="cursor|continuation"):
        gateway.changes_since(cursor)


def test_delta_changes_paginate_deduplicate_and_keep_immutable_ids() -> None:
    requests: list[httpx.Request] = []
    next_link = (
        f"{GRAPH_ROOT}/me/mailfolders('AQMk-folder-id')/messages/delta?%24skiptoken=next-token"
    )
    delta_link = (
        f"{GRAPH_ROOT}/me/mailfolders('AQMk-folder-id')/messages/delta?%24deltatoken=new-token"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer private-access"
        assert 'IdType="ImmutableId"' in request.headers["prefer"]
        if request.url.params.get("$deltatoken") == "old-token":
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "message-1"},
                        {"id": "message-1"},
                        {"id": "removed", "@removed": {"reason": "deleted"}},
                    ],
                    "@odata.nextLink": next_link,
                },
            )
        return httpx.Response(
            200,
            json={"value": [{"id": "message-2"}], "@odata.deltaLink": delta_link},
        )

    gateway = Microsoft365Gateway(
        "private-access",
        "owner@example.com",
        graph_client(handler),
    )
    result = gateway.changes_since(
        f"{GRAPH_ROOT}/me/mailFolders/inbox/messages/delta?%24deltatoken=old-token"
    )

    assert result.message_ids == ("message-1", "message-2")
    assert result.cursor == delta_link
    assert len(requests) == 2


def test_initial_cursor_defers_first_delta_round_and_recovery_is_retention_bounded() -> None:
    filters: list[str] = []
    change_types: list[str] = []
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        filters.append(request.url.params["$filter"])
        change_types.append(request.url.params["changeType"])
        return httpx.Response(
            200,
            json={
                "value": [{"id": f"message-{calls}"}],
                "@odata.deltaLink": (
                    f"{GRAPH_ROOT}/me/mailFolders/inbox/messages/delta?%24deltatoken=cursor-{calls}"
                ),
            },
        )

    gateway = Microsoft365Gateway(
        "private-access",
        "owner@example.com",
        graph_client(handler),
    )

    initial = gateway.initial_cursor()
    assert initial.startswith("microsoft365-initial:")
    assert calls == 0

    first_round = gateway.changes_since(initial)
    recovered = gateway.recover_since(
        frozenset({"trusted@example.com"}),
        datetime(2026, 9, 1, 12, 30, tzinfo=UTC),
    )

    assert first_round.message_ids == ("message-1",)
    assert first_round.cursor.endswith("%24deltatoken=cursor-1")
    assert recovered.message_ids == ("message-2",)
    assert filters[0].startswith("receivedDateTime ge ")
    assert filters[1] == "receivedDateTime ge 2026-09-01T12:30:00Z"
    assert change_types == ["created", "created"]


@pytest.mark.parametrize(
    "cursor",
    [
        "microsoft365-initial:",
        "microsoft365-initial:not-a-time",
        "microsoft365-initial:2026-09-02T12:00:00",
        "microsoft365-initial:2026-09-02T12:00:00+01:00",
    ],
)
def test_malformed_initial_cursor_is_rejected_without_network(cursor: str) -> None:
    def unexpected_request(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"Invalid initial cursor reached the network: {request.url}")

    gateway = Microsoft365Gateway(
        "private-access",
        "owner@example.com",
        graph_client(unexpected_request),
    )

    with pytest.raises(Microsoft365Error, match="cursor"):
        gateway.changes_since(cursor)


def test_expired_delta_cursor_raises_shared_recovery_signal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(410, json={"error": {"code": "syncStateNotFound"}})

    gateway = Microsoft365Gateway(
        "private-access",
        "owner@example.com",
        graph_client(handler),
    )

    with pytest.raises(StaleMailboxCursor):
        gateway.changes_since(
            f"{GRAPH_ROOT}/me/mailFolders/inbox/messages/delta?%24deltatoken=expired"
        )


def test_message_content_and_file_attachments_map_to_shared_contract() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if "/mailFolders/inbox/messages/" in path:
            return httpx.Response(
                200,
                json={
                    "id": "message-1",
                    "conversationId": "conversation-1",
                    "from": {
                        "emailAddress": {
                            "address": "TRUSTED@Example.com",
                            "name": "Trusted Person",
                        }
                    },
                    "subject": " Invoice ",
                    "receivedDateTime": "2026-09-02T12:00:00Z",
                },
            )
        if path.endswith("/attachments"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "@odata.type": "#microsoft.graph.fileAttachment",
                            "id": "attachment-1",
                            "name": "invoice.pdf",
                            "contentType": "application/PDF",
                            "size": 1234,
                        },
                        {
                            "@odata.type": "#microsoft.graph.itemAttachment",
                            "id": "ignored-item",
                            "name": "forwarded.eml",
                            "size": 10,
                        },
                    ]
                },
            )
        if path.endswith("/attachments/attachment-1/$value"):
            return httpx.Response(200, content=b"pdf-bytes")
        return httpx.Response(
            200,
            json={
                "body": {"contentType": "text", "content": " First line \n\n Second line "},
                "hasAttachments": True,
            },
        )

    gateway = Microsoft365Gateway(
        "private-access",
        "owner@example.com",
        graph_client(handler),
    )

    metadata = gateway.metadata("message-1")
    content = gateway.content("message-1", 100)
    attachment = content.attachments[0]
    payload = gateway.attachment_bytes(
        "message-1",
        attachment.part_id,
        attachment.attachment_id,
    )

    assert metadata.sender == "trusted@example.com"
    assert metadata.subject == "Invoice"
    assert metadata.labels == frozenset({"INBOX"})
    assert content.body == "First line\nSecond line"
    assert content.attachment_names == ("invoice.pdf",)
    assert attachment.media_type == "application/pdf"
    assert payload == b"pdf-bytes"
    assert all('IdType="ImmutableId"' in request.headers["prefer"] for request in requests)
    content_request = next(
        request
        for request in requests
        if request.url.path.endswith("/messages/message-1")
        and request.url.params.get("$select") == "body,hasAttachments"
    )
    assert 'outlook.body-content-type="text"' in content_request.headers["prefer"]


@pytest.mark.parametrize(
    ("status", "error", "expected"),
    [
        (302, "", Microsoft365Error),
        (401, "InvalidAuthenticationToken", MicrosoftAuthorizationRejected),
        (404, "ErrorItemNotFound", MailboxMessageUnavailable),
        (429, "TooManyRequests", Microsoft365Error),
        (503, "ServiceUnavailable", Microsoft365Error),
    ],
)
def test_message_request_maps_provider_failures_without_response_details(
    status: int,
    error: str,
    expected: type[Exception],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"code": error, "message": "private"}})

    gateway = Microsoft365Gateway(
        "private-access",
        "owner@example.com",
        graph_client(handler),
    )

    with pytest.raises(expected) as captured:
        gateway.metadata("message-1")
    assert "private" not in str(captured.value)


def test_runtime_dispatches_microsoft_account_to_microsoft_gateway(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    account = runtime.store.register_mail_account(
        "microsoft365",
        f"microsoft365-{'a' * 32}",
        display_name="Microsoft 365",
        address="owner@example.com",
        active=True,
    )
    token_file = mail_account_token_file(runtime.config, account)
    token_file.parent.mkdir(parents=True)
    token_file.write_text("private cache", encoding="utf-8")
    sentinel = SimpleNamespace(name="microsoft gateway")
    observed: list[tuple[Path, Path]] = []

    def from_token(credentials_file: Path, selected_token_file: Path):
        observed.append((credentials_file, selected_token_file))
        return sentinel

    monkeypatch.setattr(
        "eom_email_watcher.runtime.Microsoft365Gateway.from_token",
        from_token,
    )

    from eom_email_watcher.runtime import load_mailbox_account

    session = load_mailbox_account(
        runtime.config,
        runtime.store,
        account.provider,
        account.account_id,
    )

    assert session.gateway is sentinel
    assert observed == [(runtime.config.microsoft_credentials_file, token_file)]
