import base64

import pytest

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
