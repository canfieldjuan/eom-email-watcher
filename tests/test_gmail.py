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
    credentials = SimpleNamespace(valid=True, to_json=lambda: "local account token")
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
    credentials = SimpleNamespace(valid=True, expired=False, refresh_token=None)
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

    replacement = SimpleNamespace(valid=True, to_json=lambda: "replacement token")
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
