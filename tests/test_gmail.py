import base64
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from filelock import FileLock
from googleapiclient.errors import HttpError

from eom_email_watcher import gmail as gmail_module
from eom_email_watcher.gmail import (
    GmailAuthorizationRejected,
    GmailError,
    GmailGateway,
    parse_metadata,
    resolve_gmail_credentials_file,
)


def test_gmail_operation_timeout_reaches_authorized_transport() -> None:
    transport = SimpleNamespace(timeout=30.0)
    gateway = GmailGateway(SimpleNamespace(_http=SimpleNamespace(http=transport)))

    gateway.set_operation_timeout(0.25)

    assert transport.timeout == 0.25
    with pytest.raises(ValueError, match="positive finite"):
        gateway.set_operation_timeout(0)


def test_from_token_bounds_refresh_and_authorized_transport(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials_file = tmp_path / "credentials.json"
    token_file = tmp_path / "token.json"
    credentials_file.write_text("{}", encoding="utf-8")
    token_file.write_text("existing token", encoding="utf-8")
    refresh_timeouts: list[float] = []
    lock_timeouts: list[float] = []
    remaining_timeouts = iter((0.25, 0.2, 0.15))
    transport = SimpleNamespace(timeout=30.0)

    class CapturingLock:
        def __init__(self, path: str, *, timeout: float) -> None:
            lock_timeouts.append(timeout)

        def __enter__(self) -> None:
            return None

        def __exit__(self, *args: object) -> None:
            return None

    def authorized_request(*args, **kwargs):
        refresh_timeouts.append(kwargs["timeout"])
        return object()

    class FakeCredentials:
        valid = False
        expired = True
        refresh_token = "refresh-token"

        def refresh(self, request) -> None:
            request(
                method="POST",
                url="https://oauth2.googleapis.com/token",
                headers={},
                body=None,
                timeout=120,
            )
            self.valid = True
            self.expired = False

        def to_json(self) -> str:
            return "refreshed token"

    monkeypatch.setattr(
        gmail_module.Credentials,
        "from_authorized_user_file",
        lambda path, scopes: FakeCredentials(),
    )
    monkeypatch.setattr(gmail_module, "Request", lambda: authorized_request)
    monkeypatch.setattr(gmail_module, "FileLock", CapturingLock)
    monkeypatch.setattr(
        gmail_module,
        "build",
        lambda *args, **kwargs: SimpleNamespace(_http=SimpleNamespace(http=transport)),
    )

    gateway = GmailGateway.from_token(
        credentials_file,
        token_file,
        lambda: next(remaining_timeouts),
    )

    assert gateway.service is not None
    assert lock_timeouts == [0.25]
    assert refresh_timeouts == [0.2]
    assert transport.timeout == 0.15


def test_parse_metadata_uses_internal_date_and_normalized_from() -> None:
    parsed = parse_metadata(
        {
            "id": "m1",
            "threadId": "t1",
            "internalDate": "1784383200000",
            "labelIds": ["INBOX", "UNREAD"],
            "payload": {
                "headers": [
                    {"name": "From", "value": "Person <TRUSTED@Example.com>"},
                    {"name": "Subject", "value": "Test"},
                ]
            },
        }
    )
    assert parsed.sender == "trusted@example.com"
    assert parsed.sender_name == "Person"
    assert parsed.subject == "Test"
    assert parsed.labels == frozenset({"INBOX", "UNREAD"})


def test_parse_metadata_keeps_missing_source_time_invalid() -> None:
    parsed = parse_metadata(
        {
            "id": "m1",
            "internalDate": "not-a-timestamp",
            "labelIds": ["INBOX"],
            "payload": {
                "headers": [
                    {"name": "From", "value": "trusted@example.com"},
                    {"name": "Date", "value": "not-a-date"},
                ]
            },
        }
    )

    assert parsed.received_at == ""


def test_gmail_implements_normalized_mailbox_change_and_content_contract() -> None:
    gateway = GmailGateway(None)
    gateway.history_message_ids = lambda cursor: (["m1", "m2"], "next-cursor")
    gateway.search_since = lambda addresses, since: ["recovered"]
    gateway.profile_history_id = lambda: "recovery-cursor"
    gateway.full_payload = lambda message_id: {
        "mimeType": "text/plain",
        "body": {"data": base64.urlsafe_b64encode(b"hello").decode()},
    }

    changes = gateway.changes_since("cursor")
    recovered = gateway.recover_since(
        frozenset({"a@example.com"}), datetime(2026, 9, 1, tzinfo=UTC)
    )
    content = gateway.content("m1", 100)

    assert changes.message_ids == ("m1", "m2")
    assert changes.cursor == "next-cursor"
    assert recovered.message_ids == ("recovered",)
    assert recovered.cursor == "recovery-cursor"
    assert content.body == "hello"
    assert content.attachment_names == ()
    assert content.attachments == ()


def test_mailbox_identity_tracks_refresh_token_without_exposing_it() -> None:
    first_token = "first-private-refresh-token"
    second_token = "second-private-refresh-token"

    first_key = GmailGateway._credential_identity(SimpleNamespace(refresh_token=first_token))
    second_key = GmailGateway._credential_identity(SimpleNamespace(refresh_token=second_token))

    assert len(first_key) == 64
    assert first_key != second_key
    assert first_token not in first_key
    assert second_token not in second_key


class FakeRequest:
    def __init__(self, response: dict[str, object]):
        self.response = response

    def execute(self) -> dict[str, object]:
        return self.response


class FakeHistory:
    def __init__(self, response: dict[str, object]):
        self.response = response
        self.calls: list[dict[str, object]] = []

    def list(self, **kwargs: object) -> FakeRequest:
        self.calls.append(kwargs)
        return FakeRequest(self.response)


class FakeHistoryUsers:
    def __init__(self, history: FakeHistory):
        self._history = history

    def history(self) -> FakeHistory:
        return self._history


class FakeHistoryService:
    def __init__(self, response: dict[str, object]):
        self.history = FakeHistory(response)
        self._users = FakeHistoryUsers(self.history)

    def users(self) -> FakeHistoryUsers:
        return self._users


def history_response(message_count: int) -> dict[str, object]:
    return {
        "historyId": "99999",
        "history": [
            {
                "messagesAdded": [
                    {"message": {"id": f"message-{index}"}} for index in range(message_count)
                ]
            }
        ],
    }


def test_gmail_history_accepts_exact_incremental_metadata_limit() -> None:
    gateway = GmailGateway(
        FakeHistoryService(history_response(gmail_module.MAX_INCREMENTAL_MESSAGE_IDS))
    )

    message_ids, cursor = gateway.history_message_ids("12345")

    assert len(message_ids) == gmail_module.MAX_INCREMENTAL_MESSAGE_IDS
    assert cursor == "99999"


def test_gmail_history_request_is_broad_and_identical_for_sender_label_and_union_configs() -> None:
    calls: list[dict[str, object]] = []
    for _configuration in ("sender", "label", "sender-and-label"):
        service = FakeHistoryService(history_response(1))
        GmailGateway(service).history_message_ids("12345")
        calls.append(service.history.calls[0])

    assert calls[0] == calls[1] == calls[2]
    assert calls[0]["historyTypes"] == ["messageAdded", "labelAdded"]
    assert "labelId" not in calls[0]


def test_gmail_history_v2_resumes_more_than_200_unique_ids_without_skip_or_duplicate() -> None:
    service = FakeHistoryService(history_response(gmail_module.MAX_INCREMENTAL_MESSAGE_IDS + 1))
    gateway = GmailGateway(service)

    first_ids, continuation = gateway.history_message_ids("12345")
    remaining_ids, cursor = gateway.history_message_ids(continuation)

    assert len(first_ids) == gmail_module.MAX_INCREMENTAL_MESSAGE_IDS
    assert continuation.startswith(gmail_module.HISTORY_CONTINUATION_PREFIX)
    assert len(continuation.encode("utf-8")) <= gmail_module.MAX_HISTORY_CONTINUATION_BYTES
    assert remaining_ids == [f"message-{gmail_module.MAX_INCREMENTAL_MESSAGE_IDS}"]
    assert cursor == "99999"
    assert [call["startHistoryId"] for call in service.history.calls] == [
        "12345",
        "12345",
    ]


def test_gmail_history_deduplicates_before_enforcing_limit() -> None:
    response = history_response(gmail_module.MAX_INCREMENTAL_MESSAGE_IDS)
    history = response["history"]
    assert isinstance(history, list)
    first_event = history[0]
    assert isinstance(first_event, dict)
    messages = first_event["messagesAdded"]
    assert isinstance(messages, list)
    messages.append({"message": {"id": "message-0"}})
    gateway = GmailGateway(FakeHistoryService(response))

    message_ids, _ = gateway.history_message_ids("12345")

    assert len(message_ids) == gmail_module.MAX_INCREMENTAL_MESSAGE_IDS


@pytest.mark.parametrize(
    "cursor",
    [
        gmail_module.HISTORY_CONTINUATION_PREFIX,
        f"{gmail_module.HISTORY_CONTINUATION_PREFIX}{{}}",
        f"{gmail_module.HISTORY_CONTINUATION_PREFIX}not-json",
    ],
)
def test_gmail_history_rejects_invalid_continuation_cursor(cursor: str) -> None:
    gateway = GmailGateway(FakeHistoryService(history_response(1)))

    with pytest.raises(gmail_module.StaleHistoryCursor, match="continuation cursor is invalid"):
        gateway.history_message_ids(cursor)


def test_gmail_history_rejects_continuation_past_available_ids() -> None:
    gateway = GmailGateway(FakeHistoryService(history_response(1)))
    cursor = gmail_module._history_continuation_cursor(
        "12345", ["message-0", "message-1"]
    )

    with pytest.raises(gmail_module.StaleHistoryCursor, match="cannot be resumed"):
        gateway.history_message_ids(cursor)


def test_gmail_history_deduplicates_message_and_label_added_in_canonical_order() -> None:
    response = {
        "historyId": "99999",
        "history": [
            {
                "messagesAdded": [
                    {"message": {"id": "delivered"}},
                    {"message": {"id": "overlap"}},
                ],
                "labelsAdded": [
                    {"message": {"id": "overlap"}},
                    {"message": {"id": "labeled-later"}},
                ],
            }
        ],
    }

    message_ids, cursor = GmailGateway(FakeHistoryService(response)).history_message_ids("12345")

    assert message_ids == ["delivered", "overlap", "labeled-later"]
    assert cursor == "99999"


def test_gmail_history_v2_rejects_changed_prefix_digest_and_legacy_v1_token_as_stale() -> None:
    response = history_response(gmail_module.MAX_INCREMENTAL_MESSAGE_IDS + 1)
    gateway = GmailGateway(FakeHistoryService(response))
    _, continuation = gateway.history_message_ids("12345")
    changed = history_response(gmail_module.MAX_INCREMENTAL_MESSAGE_IDS + 1)
    history = changed["history"]
    assert isinstance(history, list)
    first = history[0]
    assert isinstance(first, dict)
    additions = first["messagesAdded"]
    assert isinstance(additions, list)
    additions[0] = {"message": {"id": "changed-prefix"}}

    with pytest.raises(gmail_module.StaleHistoryCursor, match="prefix changed"):
        GmailGateway(FakeHistoryService(changed)).history_message_ids(continuation)
    with pytest.raises(gmail_module.StaleHistoryCursor, match="retired stream"):
        gateway.history_message_ids("eom-gmail-history-v1:12345:200")

    empty_continuation = gmail_module._history_continuation_cursor("12345", [])
    corrupted_empty = empty_continuation.replace(
        gmail_module._history_prefix_digest([]),
        "0" * 64,
    )
    with pytest.raises(gmail_module.StaleHistoryCursor, match="prefix changed"):
        gateway.history_message_ids(corrupted_empty)


def test_gmail_label_added_after_delivery_is_returned_without_inbox_history_filter() -> None:
    service = FakeHistoryService(
        {
            "historyId": "99999",
            "history": [
                {"labelsAdded": [{"message": {"id": "labeled-after-delivery"}}]}
            ],
        }
    )

    message_ids, _ = GmailGateway(service).history_message_ids("12345")

    assert message_ids == ["labeled-after-delivery"]
    assert "labelId" not in service.history.calls[0]


def test_gmail_recovery_captures_cursor_before_search() -> None:
    gateway = GmailGateway(None)
    calls: list[str] = []
    gateway.profile_history_id = lambda: calls.append("cursor") or "recovery-cursor"
    gateway.search_since = lambda addresses, since: calls.append("search") or ["message-1"]

    recovered = gateway.recover_since(
        frozenset({"trusted@example.com"}), datetime(2026, 9, 1, tzinfo=UTC)
    )

    assert calls == ["cursor", "search"]
    assert recovered == gmail_module.MailboxChanges(("message-1",), "recovery-cursor")


class FakeRecoveryMessages:
    def __init__(self, response: dict[str, object]):
        self.response = response
        self.calls: list[dict[str, object]] = []

    def list(self, **kwargs: object) -> FakeRequest:
        self.calls.append(kwargs)
        return FakeRequest(self.response)


class FakeRecoveryUsers:
    def __init__(self, messages: FakeRecoveryMessages):
        self._messages = messages

    def messages(self) -> FakeRecoveryMessages:
        return self._messages


class FakeRecoveryService:
    def __init__(self, response: dict[str, object]):
        self.messages = FakeRecoveryMessages(response)
        self._users = FakeRecoveryUsers(self.messages)

    def users(self) -> FakeRecoveryUsers:
        return self._users


def test_gmail_recovery_page_is_one_broad_inbox_window_query_without_rule_filters() -> None:
    service = FakeRecoveryService(
        {"messages": [{"id": "second"}, {"id": "first"}], "nextPageToken": "next"}
    )

    ids, next_page_token = GmailGateway(service).recovery_page("current", 100, 200)

    assert ids == ("second", "first")
    assert next_page_token == "next"
    assert service.messages.calls == [
        {
            "userId": "me",
            "q": "in:inbox after:100 before:200",
            "pageToken": "current",
            "maxResults": 200,
        }
    ]


def test_gmail_recovery_page_applies_caller_timeout_to_http_transport() -> None:
    class TimeoutRequest:
        def __init__(self) -> None:
            self.http = None

        def execute(self, http=None):
            self.http = http
            return {"messages": []}

    request = TimeoutRequest()
    messages = SimpleNamespace(list=lambda **_kwargs: request)
    service = SimpleNamespace(users=lambda: SimpleNamespace(messages=lambda: messages))
    gateway = GmailGateway(service, credentials=SimpleNamespace())

    gateway.recovery_page(None, 100, 200, timeout_seconds=12.5)

    assert request.http is not None
    assert request.http.http.timeout == 12.5


@pytest.mark.parametrize(
    "messages",
    [
        [{"id": f"message-{index}"} for index in range(201)],
        [{"id": ""}],
        [{"id": "a"}, {"id": "a"}],
        [{"id": "x" * 513}],
        [{"missing": "id"}],
    ],
)
def test_gmail_recovery_page_rejects_more_than_200_or_malformed_ids(
    messages: list[dict[str, str]],
) -> None:
    gateway = GmailGateway(FakeRecoveryService({"messages": messages}))

    with pytest.raises(gmail_module.GmailRecoveryPageInvalid):
        gateway.recovery_page(None, 100, 200)


@pytest.mark.parametrize("control", ["\x00", "\x1f", "\x7f", "\x85", "\x9f"])
def test_gmail_recovery_page_rejects_nul_c0_del_and_c1_ids_before_persistence(
    control: str,
) -> None:
    gateway = GmailGateway(FakeRecoveryService({"messages": [{"id": f"a{control}b"}]}))

    with pytest.raises(gmail_module.GmailRecoveryPageInvalid):
        gateway.recovery_page(None, 100, 200)


def catalog_body(labels: list[dict[str, object]]) -> bytes:
    return gmail_module._canonical_json_bytes({"labels": labels})


def test_gmail_label_catalog_returns_complete_sorted_known_type_snapshot() -> None:
    decoded = gmail_module.decode_gmail_label_catalog(
        catalog_body(
            [
                {"id": "Label_b", "name": "Bills", "type": "user", "ignored": True},
                {"id": "INBOX", "name": "Inbox", "type": "system"},
                {"id": "Label_a", "name": "Accounts", "type": "user"},
            ]
        )
    )

    assert decoded == (
        gmail_module.GmailLabel("INBOX", "Inbox", "system"),
        gmail_module.GmailLabel("Label_a", "Accounts", "user"),
        gmail_module.GmailLabel("Label_b", "Bills", "user"),
    )


def test_gmail_catalog_content_length_over_one_mib_rejects_before_body_parse() -> None:
    with pytest.raises(gmail_module.GmailLabelCatalogInvalid, match="too large"):
        gmail_module.decode_gmail_label_catalog(
            b"not-json",
            content_length=str(gmail_module.MAX_GMAIL_LABEL_CATALOG_BYTES + 1),
        )


class FakeCatalogResponse:
    def __init__(
        self,
        body: bytes,
        content_length: str | None = None,
        *,
        status_code: int = 200,
        content_encoding: str | None = None,
    ):
        self.status_code = status_code
        self.headers = {} if content_length is None else {"Content-Length": content_length}
        if content_encoding is not None:
            self.headers["Content-Encoding"] = content_encoding
        self.body = body
        self.read_started = False

    def iter_content(self, chunk_size: int):
        self.read_started = True
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start : start + chunk_size]


class InterruptedCatalogResponse(FakeCatalogResponse):
    def iter_content(self, chunk_size: int):
        yield from super().iter_content(chunk_size)
        raise OSError("private provider stream failure")


class FakeCatalogSession:
    def __init__(self, response: FakeCatalogResponse):
        self.response = response
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def get(self, url: str, **kwargs: object) -> FakeCatalogResponse:
        self.calls.append((url, kwargs))
        return self.response


def test_gmail_catalog_content_length_over_one_mib_rejects_before_body_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = FakeCatalogResponse(
        b"body must not be read",
        str(gmail_module.MAX_GMAIL_LABEL_CATALOG_BYTES + 1),
    )
    session = FakeCatalogSession(response)
    monkeypatch.setattr(gmail_module, "AuthorizedSession", lambda credentials: session)
    gateway = GmailGateway(None, credentials=SimpleNamespace())

    with pytest.raises(gmail_module.GmailLabelCatalogInvalid, match="too large"):
        gateway.label_catalog()

    assert response.read_started is False
    assert session.calls == [
        (
            gmail_module.GMAIL_LABELS_URL,
            {"stream": True, "timeout": 120},
        )
    ]


def test_gmail_catalog_stream_reader_rejects_decoded_cap_plus_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = FakeCatalogResponse(
        b"x" * (gmail_module.MAX_GMAIL_LABEL_CATALOG_BYTES + 1)
    )
    session = FakeCatalogSession(response)
    monkeypatch.setattr(gmail_module, "AuthorizedSession", lambda credentials: session)
    gateway = GmailGateway(None, credentials=SimpleNamespace())

    with pytest.raises(gmail_module.GmailLabelCatalogInvalid, match="too large"):
        gateway.label_catalog()

    assert response.read_started is True


@pytest.mark.parametrize(
    ("status_code", "body", "error_type"),
    [
        (401, b'{"error":{"errors":[{"reason":"rateLimitExceeded"}]}}', GmailAuthorizationRejected),
        (
            403,
            b'{"error":{"errors":[{"reason":"quotaExceeded"}]}}',
            GmailAuthorizationRejected,
        ),
        (
            403,
            b'{"error":{"errors":[{"reason":"rateLimitExceeded"}]}}',
            gmail_module.GmailLabelCatalogUnavailable,
        ),
        (
            403,
            b'{"error":{"errors":[{"reason":"userRateLimitExceeded"}]}}',
            gmail_module.GmailLabelCatalogUnavailable,
        ),
        (
            403,
            b'{"error":{"errors":[{"reason":"quotaExceeded"},{"reason":"rateLimitExceeded"}]}}',
            GmailAuthorizationRejected,
        ),
        (
            403,
            b'{"error":{"errors":[{"reason":"rateLimitExceeded"},{"reason":"userRateLimitExceeded"}]}}',
            gmail_module.GmailLabelCatalogUnavailable,
        ),
        (
            403,
            b'{"error":{"message":"private provider body","errors":[{"reason":"forbidden"}]}}',
            GmailAuthorizationRejected,
        ),
        (403, b'{"error":{"errors":[]}}', GmailAuthorizationRejected),
        (
            403,
            b'{"error":{"errors":[{"reason":"insufficientPermissions"}]}}',
            GmailAuthorizationRejected,
        ),
        (403, b'{"error":{"errors":[{"reason":"unknownReason"}]}}', GmailAuthorizationRejected),
        (
            403,
            b'{"error":{"errors":[{"reason":"rateLimitExceeded"},{"reason":"forbidden"}]}}',
            GmailAuthorizationRejected,
        ),
        (403, b'not-json', GmailAuthorizationRejected),
    ],
)
def test_gmail_catalog_classifies_http_auth_and_quota_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    body: bytes,
    error_type: type[Exception],
) -> None:
    response = FakeCatalogResponse(body, status_code=status_code)
    session = FakeCatalogSession(response)
    monkeypatch.setattr(gmail_module, "AuthorizedSession", lambda credentials: session)
    gateway = GmailGateway(None, credentials=SimpleNamespace())

    with pytest.raises(error_type) as raised:
        gateway.label_catalog()

    assert response.read_started is (status_code == 403)
    assert "private provider body" not in str(raised.value)


@pytest.mark.parametrize(
    "response",
    [
        InterruptedCatalogResponse(
            b'{"error":{"errors":[{"reason":"rateLimitExceeded"}]}}',
            status_code=403,
        ),
        InterruptedCatalogResponse(
            b'{"error":{"errors":[{"reason":"rateLimitExceeded"}]}}',
            content_length="17",
            status_code=403,
            content_encoding="gzip",
        ),
        FakeCatalogResponse(
            b'{"error":{"errors":[{"reason":"rateLimitExceeded"}]}}',
            content_length="999",
            status_code=403,
            content_encoding="identity",
        ),
    ],
)
def test_gmail_catalog_403_requires_a_complete_error_document(
    monkeypatch: pytest.MonkeyPatch,
    response: FakeCatalogResponse,
) -> None:
    session = FakeCatalogSession(response)
    monkeypatch.setattr(gmail_module, "AuthorizedSession", lambda credentials: session)
    gateway = GmailGateway(None, credentials=SimpleNamespace())

    with pytest.raises(GmailAuthorizationRejected) as raised:
        gateway.label_catalog()

    assert "private provider stream failure" not in str(raised.value)


@pytest.mark.parametrize("content_encoding", ["gzip", "deflate"])
def test_gmail_catalog_compressed_rate_limit_uses_decoded_body_length(
    monkeypatch: pytest.MonkeyPatch,
    content_encoding: str,
) -> None:
    body = b'{"error":{"errors":[{"reason":"rateLimitExceeded"}]}}'
    response = FakeCatalogResponse(
        body,
        content_length="17",
        status_code=403,
        content_encoding=content_encoding,
    )
    session = FakeCatalogSession(response)
    monkeypatch.setattr(gmail_module, "AuthorizedSession", lambda credentials: session)
    gateway = GmailGateway(None, credentials=SimpleNamespace())

    with pytest.raises(gmail_module.GmailLabelCatalogUnavailable, match="throttled"):
        gateway.label_catalog()


def test_gmail_catalog_compressed_success_uses_decoded_size_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = catalog_body([])
    response = FakeCatalogResponse(
        body,
        content_length=str(gmail_module.MAX_GMAIL_LABEL_CATALOG_BYTES + 1),
        content_encoding="gzip",
    )
    session = FakeCatalogSession(response)
    monkeypatch.setattr(gmail_module, "AuthorizedSession", lambda credentials: session)
    gateway = GmailGateway(None, credentials=SimpleNamespace())

    assert gateway.label_catalog() == ()
    assert response.read_started is True


def test_gmail_catalog_bounded_reader_accepts_exact_one_mib_and_rejects_plus_one() -> None:
    base = catalog_body([])
    exact = base + b" " * (gmail_module.MAX_GMAIL_LABEL_CATALOG_BYTES - len(base))

    assert gmail_module.decode_gmail_label_catalog(exact) == ()
    with pytest.raises(gmail_module.GmailLabelCatalogInvalid, match="too large"):
        gmail_module.decode_gmail_label_catalog(exact + b" ")


def test_gmail_catalog_accepts_10000_items_and_rejects_10001_whole() -> None:
    accepted = [
        {"id": f"Label_{index:05d}", "name": "A", "type": "user"}
        for index in range(gmail_module.MAX_GMAIL_LABEL_COUNT)
    ]

    assert (
        len(gmail_module.decode_gmail_label_catalog(catalog_body(accepted)))
        == gmail_module.MAX_GMAIL_LABEL_COUNT
    )
    with pytest.raises(gmail_module.GmailLabelCatalogInvalid, match="too many"):
        gmail_module.decode_gmail_label_catalog(
            catalog_body(
                [
                    *accepted,
                    {"id": "Label_overflow", "name": "C", "type": "user"},
                ]
            )
        )


@pytest.mark.parametrize(
    "label",
    [
        {"id": "", "name": "A", "type": "user"},
        {"id": "x" * 513, "name": "A", "type": "user"},
        {"id": "Label_a", "name": "", "type": "user"},
        {"id": "Label_a", "name": "x" * 1025, "type": "user"},
        {"id": "Label_a", "name": "A", "type": "unknown"},
        {"id": "Label_\x00", "name": "A", "type": "user"},
        {"id": "Label_a", "name": "A\x9f", "type": "user"},
    ],
)
def test_gmail_catalog_rejects_invalid_item_fields_whole(label: dict[str, object]) -> None:
    with pytest.raises(gmail_module.GmailLabelCatalogInvalid):
        gmail_module.decode_gmail_label_catalog(catalog_body([label]))


def test_gmail_catalog_accepts_exact_item_utf8_byte_limits() -> None:
    decoded = gmail_module.decode_gmail_label_catalog(
        catalog_body(
            [
                {
                    "id": "i" * gmail_module.MAX_GMAIL_LABEL_ID_BYTES,
                    "name": "n" * gmail_module.MAX_GMAIL_LABEL_NAME_BYTES,
                    "type": "user",
                }
            ]
        )
    )

    assert len(decoded[0].label_id.encode("utf-8")) == gmail_module.MAX_GMAIL_LABEL_ID_BYTES
    assert (
        len(decoded[0].display_name.encode("utf-8"))
        == gmail_module.MAX_GMAIL_LABEL_NAME_BYTES
    )


def test_gmail_recovery_page_accepts_200_maximum_length_ids() -> None:
    messages = [
        {"id": f"{index:03d}" + "x" * (gmail_module.MAX_GMAIL_LABEL_ID_BYTES - 3)}
        for index in range(gmail_module.MAX_GMAIL_RECOVERY_PAGE_IDS)
    ]

    ids, next_page_token = GmailGateway(
        FakeRecoveryService({"messages": messages})
    ).recovery_page(None, 100, 200)

    assert len(ids) == gmail_module.MAX_GMAIL_RECOVERY_PAGE_IDS
    assert all(len(message_id.encode("utf-8")) == 512 for message_id in ids)
    assert next_page_token is None


def test_gmail_catalog_rejects_duplicate_id_and_duplicate_json_keys() -> None:
    duplicate_ids = [
        {"id": "same", "name": "A", "type": "user"},
        {"id": "same", "name": "B", "type": "system"},
    ]

    with pytest.raises(gmail_module.GmailLabelCatalogInvalid, match="duplicate label ID"):
        gmail_module.decode_gmail_label_catalog(catalog_body(duplicate_ids))
    with pytest.raises(gmail_module.GmailLabelCatalogInvalid, match="malformed JSON"):
        gmail_module.decode_gmail_label_catalog(
            b'{"labels":[],"labels":[{"id":"x","name":"X","type":"user"}]}'
        )


@pytest.mark.parametrize("constant", [b"NaN", b"Infinity", b"-Infinity"])
def test_gmail_catalog_rejects_non_json_numeric_constants(constant: bytes) -> None:
    body = (
        b'{"labels":[{"id":"Label_a","name":"A","type":"user",'
        b'"ignored":'
        + constant
        + b'}]}'
    )

    with pytest.raises(
        gmail_module.GmailLabelCatalogInvalid,
        match="gmail_label_catalog_invalid: malformed JSON",
    ) as error:
        gmail_module.decode_gmail_label_catalog(body)

    assert error.value.code == "gmail_label_catalog_invalid"


def test_gmail_catalog_rejects_normalized_document_over_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_encoder = gmail_module._canonical_json_bytes
    body = original_encoder({"labels": [{"id": "x", "name": "A", "type": "user"}]})
    monkeypatch.setattr(gmail_module, "MAX_GMAIL_LABEL_CATALOG_BYTES", len(body))

    def oversized_normalized(value: object) -> bytes:
        return original_encoder(value) + b"x"

    monkeypatch.setattr(gmail_module, "_canonical_json_bytes", oversized_normalized)

    with pytest.raises(gmail_module.GmailLabelCatalogInvalid, match="normalized response"):
        gmail_module.decode_gmail_label_catalog(body)


def test_gmail_catalog_canonical_one_mib_accepts_exact_and_rejects_plus_one_whole() -> None:
    labels = [
        {"id": f"Label_{index:04d}", "name": "n", "type": "user"}
        for index in range(1_000)
    ]
    base_size = len(catalog_body(labels))
    remaining = gmail_module.MAX_GMAIL_LABEL_CATALOG_BYTES - base_size
    for label in labels:
        added = min(remaining, gmail_module.MAX_GMAIL_LABEL_NAME_BYTES - 1)
        label["name"] = "n" * (1 + added)
        remaining -= added
        if remaining == 0:
            break
    exact = catalog_body(labels)
    assert len(exact) == gmail_module.MAX_GMAIL_LABEL_CATALOG_BYTES

    assert len(gmail_module.decode_gmail_label_catalog(exact)) == len(labels)
    with pytest.raises(gmail_module.GmailLabelCatalogInvalid, match="too large"):
        gmail_module.decode_gmail_label_catalog(exact + b" ")


@pytest.mark.parametrize(
    ("page_token", "after_epoch", "before_epoch", "max_results"),
    [
        ("x" * (gmail_module.MAX_GMAIL_PAGE_TOKEN_BYTES + 1), 100, 200, 200),
        (None, -1, 200, 200),
        (None, 100, 100, 200),
        (None, 100, 200, 199),
        (None, True, 200, 200),
    ],
)
def test_gmail_recovery_page_rejects_outbound_boundary_violations(
    page_token: str | None,
    after_epoch: int,
    before_epoch: int,
    max_results: int,
) -> None:
    service = FakeRecoveryService({"messages": []})

    with pytest.raises(gmail_module.GmailRecoveryPageInvalid):
        GmailGateway(service).recovery_page(
            page_token,
            after_epoch,
            before_epoch,
            max_results,
        )

    assert service.messages.calls == []


class FakeAttachments:
    def __init__(self, response: dict[str, object]):
        self.response = response
        self.calls: list[dict[str, str]] = []

    def get(self, **kwargs: str) -> FakeRequest:
        self.calls.append(kwargs)
        return FakeRequest(self.response)


class FakeMessages:
    def __init__(self, attachments: FakeAttachments):
        self._attachments = attachments

    def attachments(self) -> FakeAttachments:
        return self._attachments


class FakeUsers:
    def __init__(self, messages: FakeMessages):
        self._messages = messages

    def messages(self) -> FakeMessages:
        return self._messages


class FakeService:
    def __init__(self, attachments: FakeAttachments):
        self._users = FakeUsers(FakeMessages(attachments))

    def users(self) -> FakeUsers:
        return self._users


def encoded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def test_explicit_gmail_credentials_override_bundled_client(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured_file = tmp_path / "configured.json"
    configured_file.write_text("{}", encoding="utf-8")
    bundle_root = tmp_path / "bundle"
    bundled_file = bundle_root / gmail_module.BUNDLED_GOOGLE_OAUTH_CLIENT
    bundled_file.parent.mkdir(parents=True)
    bundled_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(gmail_module.sys, "_MEIPASS", str(bundle_root), raising=False)

    assert resolve_gmail_credentials_file(configured_file) == configured_file


def test_missing_explicit_gmail_credentials_use_bundled_client(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured_file = tmp_path / "missing.json"
    bundle_root = tmp_path / "bundle"
    bundled_file = bundle_root / gmail_module.BUNDLED_GOOGLE_OAUTH_CLIENT
    bundled_file.parent.mkdir(parents=True)
    bundled_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(gmail_module.sys, "_MEIPASS", str(bundle_root), raising=False)

    assert resolve_gmail_credentials_file(configured_file) == bundled_file


def test_missing_explicit_and_bundled_credentials_preserve_configured_path(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured_file = tmp_path / "missing.json"
    monkeypatch.delattr(gmail_module.sys, "_MEIPASS", raising=False)

    assert resolve_gmail_credentials_file(configured_file) == configured_file


def test_authorize_uses_bundled_client_without_copying_it_to_local_state(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured_file = tmp_path / "missing.json"
    token_file = tmp_path / "token.json"
    bundle_root = tmp_path / "bundle"
    bundled_file = bundle_root / gmail_module.BUNDLED_GOOGLE_OAUTH_CLIENT
    bundled_file.parent.mkdir(parents=True)
    bundled_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(gmail_module.sys, "_MEIPASS", str(bundle_root), raising=False)
    credentials = SimpleNamespace(
        valid=True,
        refresh_token="browser-flow-refresh-token",
        to_json=lambda: "local account token",
    )
    flow = SimpleNamespace(run_local_server=lambda **kwargs: credentials)
    opened_client_files: list[str] = []

    def open_client_file(path: str, scopes: tuple[str, ...]):
        opened_client_files.append(path)
        assert scopes == gmail_module.SCOPES
        return flow

    monkeypatch.setattr(
        gmail_module.InstalledAppFlow,
        "from_client_secrets_file",
        open_client_file,
    )
    monkeypatch.setattr(gmail_module, "build", lambda *args, **kwargs: object())

    GmailGateway.authorize(configured_file, token_file)

    assert opened_client_files == [str(bundled_file)]
    assert not configured_file.exists()
    assert token_file.read_text(encoding="utf-8") == "local account token"


def test_attachment_bytes_uses_gmail_attachment_identity() -> None:
    attachments = FakeAttachments({"data": encoded(b"pdf bytes")})
    gateway = GmailGateway(FakeService(attachments))

    assert gateway.attachment_bytes("message-1", "2", "attachment-1") == b"pdf bytes"
    assert attachments.calls == [{"userId": "me", "messageId": "message-1", "id": "attachment-1"}]


def test_attachment_bytes_finds_inline_root_part_data() -> None:
    gateway = GmailGateway(None)
    gateway.full_payload = lambda message_id: {
        "partId": "",
        "filename": "root.pdf",
        "body": {"data": encoded(b"root bytes")},
    }

    assert gateway.attachment_bytes("message-1", "", None) == b"root bytes"


def test_attachment_bytes_rejects_invalid_encoded_data() -> None:
    attachments = FakeAttachments({"data": "%%%"})
    gateway = GmailGateway(FakeService(attachments))

    with pytest.raises(GmailError, match="invalid data"):
        gateway.attachment_bytes("message-1", "2", "attachment-1")


def test_attachment_bytes_accepts_an_empty_attachment() -> None:
    attachments = FakeAttachments({"data": ""})
    gateway = GmailGateway(FakeService(attachments))

    assert gateway.attachment_bytes("message-1", "2", "attachment-1") == b""


def test_from_token_fails_cleanly_while_another_process_owns_token_lock(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials_file = tmp_path / "credentials.json"
    token_file = tmp_path / "token.json"
    credentials_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(gmail_module, "TOKEN_LOCK_TIMEOUT_SECONDS", 0)

    with (
        FileLock(f"{token_file}.lock"),
        pytest.raises(GmailError, match="token is busy"),
    ):
        GmailGateway.from_token(credentials_file, token_file)


@pytest.mark.parametrize("retryable", [False, True])
def test_from_token_classifies_refresh_failures(
    tmp_path, monkeypatch: pytest.MonkeyPatch, retryable: bool
) -> None:
    credentials_file = tmp_path / "credentials.json"
    token_file = tmp_path / "token.json"
    credentials_file.write_text("{}", encoding="utf-8")
    token_file.write_text("existing token", encoding="utf-8")

    def refresh(_request) -> None:
        raise gmail_module.RefreshError("refresh failed", retryable=retryable)

    credentials = SimpleNamespace(
        valid=False,
        expired=True,
        refresh_token="refresh-token",
        refresh=refresh,
    )
    monkeypatch.setattr(
        gmail_module.Credentials,
        "from_authorized_user_file",
        lambda path, scopes: credentials,
    )
    monkeypatch.setattr(
        gmail_module,
        "build",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("failed refresh must not build a Gmail service")
        ),
    )

    with pytest.raises(GmailError) as raised:
        GmailGateway.from_token(credentials_file, token_file)

    assert isinstance(raised.value, GmailAuthorizationRejected) is not retryable
    assert token_file.read_text(encoding="utf-8") == "existing token"


@pytest.mark.parametrize("stored_token", ["malformed", "unusable"])
def test_from_token_classifies_unusable_local_authorization_as_rejected(
    tmp_path, monkeypatch: pytest.MonkeyPatch, stored_token: str
) -> None:
    credentials_file = tmp_path / "credentials.json"
    token_file = tmp_path / "token.json"
    credentials_file.write_text("{}", encoding="utf-8")
    token_file.write_text("existing token", encoding="utf-8")

    if stored_token == "malformed":

        def load_credentials(path, scopes):
            raise ValueError("malformed token")

    else:

        def load_credentials(path, scopes):
            return SimpleNamespace(valid=False, expired=False, refresh_token=None)

    monkeypatch.setattr(
        gmail_module.Credentials,
        "from_authorized_user_file",
        load_credentials,
    )

    with pytest.raises(GmailAuthorizationRejected):
        GmailGateway.from_token(credentials_file, token_file)


def test_authorize_serializes_the_entire_browser_flow(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials_file = tmp_path / "credentials.json"
    token_file = tmp_path / "token.json"
    credentials_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(gmail_module, "TOKEN_LOCK_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(
        gmail_module.InstalledAppFlow,
        "from_client_secrets_file",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("contended authorization must not open a browser")
        ),
    )

    with (
        FileLock(f"{token_file}.lock"),
        pytest.raises(GmailError, match="token is busy"),
    ):
        GmailGateway.authorize(credentials_file, token_file)


def test_authorize_reuses_a_token_found_after_lock_acquisition(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials_file = tmp_path / "credentials.json"
    token_file = tmp_path / "token.json"
    credentials_file.write_text("{}", encoding="utf-8")
    token_file.write_text("existing token", encoding="utf-8")
    credentials = SimpleNamespace(
        valid=True,
        expired=False,
        refresh_token="stored-refresh-token",
    )
    service = object()
    monkeypatch.setattr(
        gmail_module.Credentials,
        "from_authorized_user_file",
        lambda path, scopes: credentials,
    )
    monkeypatch.setattr(
        gmail_module.InstalledAppFlow,
        "from_client_secrets_file",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("an existing token must prevent a second browser flow")
        ),
    )
    monkeypatch.setattr(gmail_module, "build", lambda *args, **kwargs: service)

    gateway = GmailGateway.authorize(credentials_file, token_file)

    assert gateway.service is service


@pytest.mark.parametrize("stored_token", ["malformed", "unusable", "revoked"])
def test_authorize_replaces_an_unusable_existing_token(
    tmp_path, monkeypatch: pytest.MonkeyPatch, stored_token: str
) -> None:
    credentials_file = tmp_path / "credentials.json"
    token_file = tmp_path / "token.json"
    credentials_file.write_text("{}", encoding="utf-8")
    token_file.write_text("unusable token", encoding="utf-8")

    if stored_token == "malformed":
        stored_credentials = None

        def load_credentials(path, scopes):
            raise ValueError("malformed token")

    elif stored_token == "unusable":
        stored_credentials = SimpleNamespace(valid=False, expired=False, refresh_token=None)

        def load_credentials(path, scopes):
            return stored_credentials

    else:

        def rejected_refresh(request):
            raise gmail_module.RefreshError("revoked token")

        stored_credentials = SimpleNamespace(
            valid=False,
            expired=True,
            refresh_token="refresh-token",
            refresh=rejected_refresh,
        )

        def load_credentials(path, scopes):
            return stored_credentials

    replacement = SimpleNamespace(
        valid=True,
        refresh_token="replacement-refresh-token",
        to_json=lambda: "replacement token",
    )
    browser_calls: list[dict[str, object]] = []

    def run_local_server(**kwargs):
        browser_calls.append(kwargs)
        return replacement

    flow = SimpleNamespace(run_local_server=run_local_server)
    service = object()
    monkeypatch.setattr(
        gmail_module.Credentials,
        "from_authorized_user_file",
        load_credentials,
    )
    monkeypatch.setattr(
        gmail_module.InstalledAppFlow,
        "from_client_secrets_file",
        lambda *args: flow,
    )
    monkeypatch.setattr(gmail_module, "build", lambda *args, **kwargs: service)

    gateway, authorization_changed = GmailGateway.authorize_with_status(
        credentials_file, token_file
    )

    assert gateway.service is service
    assert authorization_changed is True
    assert token_file.read_text(encoding="utf-8") == "replacement token"
    assert browser_calls[0]["authorization_prompt_message"] is None
    assert browser_calls[0]["timeout_seconds"] == gmail_module.GMAIL_AUTHORIZATION_TIMEOUT_SECONDS


def test_authorization_timeout_does_not_persist_a_token(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials_file = tmp_path / "credentials.json"
    token_file = tmp_path / "token.json"
    credentials_file.write_text("{}", encoding="utf-8")

    def run_local_server(**kwargs):
        assert kwargs["timeout_seconds"] == gmail_module.GMAIL_AUTHORIZATION_TIMEOUT_SECONDS
        raise gmail_module.WSGITimeoutError("browser flow timed out")

    flow = SimpleNamespace(run_local_server=run_local_server)
    monkeypatch.setattr(
        gmail_module.InstalledAppFlow,
        "from_client_secrets_file",
        lambda *args: flow,
    )

    with pytest.raises(GmailError, match="authorization timed out"):
        GmailGateway.authorize_with_status(credentials_file, token_file)

    assert not token_file.exists()


def test_profile_history_id_classifies_http_401_as_rejected_authorization() -> None:
    response = SimpleNamespace(status=401, reason="Unauthorized")
    error = HttpError(response, b"{}")

    def reject():
        raise error

    request = SimpleNamespace(execute=reject)
    users = SimpleNamespace(getProfile=lambda **kwargs: request)
    gateway = GmailGateway(SimpleNamespace(users=lambda: users))

    with pytest.raises(GmailAuthorizationRejected, match="rejected"):
        gateway.profile_history_id()


def test_profile_returns_normalized_mailbox_identity_and_cursor() -> None:
    request = SimpleNamespace(
        execute=lambda: {"emailAddress": "Owner@Example.COM", "historyId": "12345"}
    )
    users = SimpleNamespace(getProfile=lambda **kwargs: request)
    gateway = GmailGateway(SimpleNamespace(users=lambda: users))

    profile = gateway.profile()

    assert profile.email_address == "owner@example.com"
    assert profile.history_id == "12345"
    assert gateway.mailbox_address() == "owner@example.com"
    assert gateway.profile_history_id() == "12345"


@pytest.mark.parametrize(
    "response",
    [
        {"emailAddress": "", "historyId": "12345"},
        {"emailAddress": "@", "historyId": "12345"},
        {"emailAddress": "owner@example.com", "historyId": ""},
    ],
)
def test_profile_rejects_incomplete_identity(response: dict[str, str]) -> None:
    request = SimpleNamespace(execute=lambda: response)
    users = SimpleNamespace(getProfile=lambda **kwargs: request)
    gateway = GmailGateway(SimpleNamespace(users=lambda: users))

    with pytest.raises(GmailError, match="profile response"):
        gateway.profile()
