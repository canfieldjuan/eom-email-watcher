import base64

import pytest
from filelock import FileLock

from eom_email_watcher import gmail as gmail_module
from eom_email_watcher.gmail import GmailError, GmailGateway, parse_metadata


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


class FakeRequest:
    def __init__(self, response: dict[str, object]):
        self.response = response

    def execute(self) -> dict[str, object]:
        return self.response


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


def test_attachment_bytes_uses_gmail_attachment_identity() -> None:
    attachments = FakeAttachments({"data": encoded(b"pdf bytes")})
    gateway = GmailGateway(FakeService(attachments))

    assert gateway.attachment_bytes("message-1", "2", "attachment-1") == b"pdf bytes"
    assert attachments.calls == [
        {"userId": "me", "messageId": "message-1", "id": "attachment-1"}
    ]


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


def test_recent_inbox_message_ids_is_watchlist_scoped_and_bounded() -> None:
    class SearchMessages:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def list(self, **kwargs: object) -> FakeRequest:
            self.calls.append(kwargs)
            return FakeRequest(
                {
                    "messages": [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}],
                    "nextPageToken": "must-not-be-followed",
                }
            )

    messages = SearchMessages()

    class SearchUsers:
        def messages(self) -> SearchMessages:
            return messages

    class SearchService:
        def users(self) -> SearchUsers:
            return SearchUsers()

    gateway = GmailGateway(SearchService())

    assert gateway.recent_inbox_message_ids(
        frozenset({"z@example.com", "a@example.com"}), limit=2
    ) == ["m1", "m2"]
    assert messages.calls == [
        {
            "userId": "me",
            "q": "in:inbox {from:a@example.com from:z@example.com}",
            "pageToken": None,
            "maxResults": 2,
        }
    ]


@pytest.mark.parametrize("limit", [0, 501])
def test_recent_inbox_message_ids_rejects_unsafe_limits(limit: int) -> None:
    with pytest.raises(ValueError, match="between 1 and 500"):
        GmailGateway(None).recent_inbox_message_ids(
            frozenset({"a@example.com"}), limit=limit
        )
