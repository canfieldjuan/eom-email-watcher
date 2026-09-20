import hashlib
import io
import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from connect_automate import connect

from eom_email_watcher import engine_api
from eom_email_watcher.automation.rules import MAX_AUTOMATION_RULES
from eom_email_watcher.config import load_config
from eom_email_watcher.db import AdmissionProvenance, GmailLabelStoreError, Store
from eom_email_watcher.gmail import (
    GmailAuthorizationRejected,
    GmailError,
    GmailLabel,
    GmailLabelCatalogInvalid,
    GmailLabelCatalogUnavailable,
    GmailProfile,
    GmailRecoveryPageInvalid,
    GmailRecoveryPageTokenInvalid,
    MessageMetadata,
)
from eom_email_watcher.imap import (
    ImapCredentials,
    ImapError,
    imap_mailbox_identity,
    write_credentials,
)
from eom_email_watcher.mailbox import (
    DEFAULT_MAIL_ACCOUNT_ID,
    DEFAULT_MAIL_PROVIDER,
    MailboxChanges,
    MailboxSession,
    MessageContent,
    scoped_message_id,
)
from eom_email_watcher.microsoft365 import (
    Microsoft365Error,
    Microsoft365Profile,
    MicrosoftAuthorizationRejected,
)
from eom_email_watcher.microsoft_calendar import (
    CalendarDeltaChange,
    CalendarDeltaRound,
    CalendarEvent,
    MicrosoftPrincipal,
    StaleCalendarCursor,
)
from eom_email_watcher.mime import AttachmentDescriptor, extract_body
from eom_email_watcher.model import Analysis
from eom_email_watcher.runtime import (
    Runtime,
    load_runtime,
    mail_account_token_file,
    microsoft_calendar_read_token_file,
    microsoft_calendar_token_file,
)

IMAP_CURSOR = f"eom-imap-v2:{'a' * 64}:44:7"
REPLACEMENT_IMAP_CURSOR = f"eom-imap-v2:{'b' * 64}:55:99"
GMAIL_IDENTITY = "a" * 64
REPLACEMENT_GMAIL_IDENTITY = "b" * 64


def _test_mailbox_identity(provider: str, account_id: str) -> str:
    return hashlib.sha256(f"test-mailbox\0{provider}\0{account_id}".encode()).hexdigest()


def _bind_test_mailbox(store: Store, provider: str, account_id: str) -> str:
    account = store.mail_account(provider, account_id)
    if account is None:
        account = store.register_mail_account(
            provider,
            account_id,
            display_name=provider,
            address="owner@example.com",
        )
    identity_key = account.mailbox_identity_key or _test_mailbox_identity(provider, account_id)
    if account.mailbox_identity_key is None:
        store.reconcile_mailbox_identity(
            provider,
            account_id,
            identity_key,
            legacy_status="replacement",
            preserve_cursor=True,
        )
    return identity_key


def _add_test_message(store: Store, **values: object) -> bool:
    provider = str(values.get("provider", DEFAULT_MAIL_PROVIDER))
    account_id = str(values.get("account_id", DEFAULT_MAIL_ACCOUNT_ID))
    values.setdefault(
        "mailbox_identity_key",
        _bind_test_mailbox(store, provider, account_id),
    )
    values.setdefault(
        "admission",
        AdmissionProvenance(
            kind="exact_sender",
            selector_id=f"sender:{values.get('sender', 'sender@example.com')}",
            display_name=(
                str(values["sender_name"])
                if values.get("sender_name") is not None
                else None
            ),
            mailbox_identity_key=str(values["mailbox_identity_key"]),
            admitted_at="2026-09-19T12:00:00+00:00",
        ),
    )
    return store.add_message(**values)  # type: ignore[arg-type]


def _mark_test_analyzed(
    store: Store,
    message_id: str,
    result: dict[str, object],
    **values: object,
) -> None:
    source = store.message_source(message_id)
    assert source.mailbox_identity_key is not None
    values.setdefault("mailbox_identity_key", source.mailbox_identity_key)
    store.mark_analyzed(message_id, result, **values)  # type: ignore[arg-type]


def write_config(
    path: Path,
    *,
    ntfy_topic: str | None = None,
    notifications_enabled: bool = True,
    extra_settings: str = "",
    timezone: str = "America/Chicago",
    include_senders: bool = True,
) -> None:
    ntfy_setting = (
        f'ntfy_topic = "{ntfy_topic}"\nntfy_content_disclosure_acknowledged = true\n'
        if ntfy_topic
        else ""
    )
    notifications_setting = str(notifications_enabled).lower()
    credentials_file = (path.parent / "credentials.json").as_posix()
    microsoft_credentials_file = (path.parent / "microsoft-oauth-client.json").as_posix()
    token_file = (path.parent / "token.json").as_posix()
    send_token_file = (path.parent / "send-token.json").as_posix()
    database_file = (path.parent / "watcher.sqlite3").as_posix()
    senders = (
        """[[senders]]
email = "z@example.com"
name = "Zed"

[[senders]]
email = "A@Example.com"
name = "Trusted A"
"""
        if include_senders
        else ""
    )
    path.write_text(
        f'''timezone = "{timezone}"
gmail_credentials_file = "{credentials_file}"
microsoft_credentials_file = "{microsoft_credentials_file}"
gmail_token_file = "{token_file}"
gmail_send_token_file = "{send_token_file}"
database_file = "{database_file}"
model_base_url = "http://127.0.0.1:1234/v1"
model_name = "local-model"
model_require_auth = false
notifications_enabled = {notifications_setting}
{ntfy_setting}
{extra_settings}
{senders}
''',
        encoding="utf-8",
    )


def request(config_path: Path, operation: str, payload: dict[str, object] | None = None):
    return {
        "protocol": 1,
        "operation": operation,
        "config_path": str(config_path),
        "payload": payload or {},
    }


def test_gmail_label_engine_operations_enforce_payload_shape_before_runtime(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"

    with pytest.raises(engine_api.ApiError, match="Unsupported payload fields: secret"):
        engine_api.dispatch(
            request(
                config_path,
                "gmail.labels.catalog",
                {
                    "provider": "gmail",
                    "account_id": "gmail-default",
                    "secret": "must-not-be-accepted",
                },
            )
        )

    response = engine_api._response(
        request(
            config_path,
            "gmail.labels.catalog",
            {"provider": "GMAIL", "account_id": "gmail-default"},
        )
    )
    assert response["error"]["code"] == "unsupported_provider"


@pytest.mark.parametrize("revision", [False, -1, 1.5, "0", None])
def test_gmail_label_mutations_require_exact_non_negative_integer_revision(
    tmp_path: Path,
    revision: object,
) -> None:
    config_path = tmp_path / "config.toml"

    with pytest.raises(
        engine_api.ApiError,
        match="expected_revision must be a non-negative integer",
    ):
        engine_api.dispatch(
            request(
                config_path,
                "gmail.label_selectors.add",
                {
                    "provider": "gmail",
                    "account_id": "gmail-default",
                    "label_id": "Label_123",
                    "expected_revision": revision,
                },
            )
        )


class FakeGmailLabelGateway:
    def __init__(self, identity_key: str, labels: tuple[GmailLabel, ...]):
        self.identity_key = identity_key
        self.labels = labels
        self.catalog_calls = 0
        self.identity_calls = 0

    def mailbox_identity_key(self) -> str:
        self.identity_calls += 1
        return self.identity_key

    def label_catalog(self) -> tuple[GmailLabel, ...]:
        self.catalog_calls += 1
        return self.labels


def test_gmail_label_catalog_add_list_remove_are_account_and_revision_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    identity_key = _bind_test_mailbox(runtime.store, "gmail", "gmail-default")
    gateway = FakeGmailLabelGateway(
        identity_key,
        (
            GmailLabel("Label_456", "Receipts", "user"),
            GmailLabel("INBOX", "Inbox", "system"),
            GmailLabel("Label_123", "Invoices", "user"),
        ),
    )
    mailbox = MailboxSession("gmail", "gmail-default", gateway)
    monkeypatch.setattr(engine_api, "load_runtime", lambda _path: runtime)
    monkeypatch.setattr(engine_api, "mail_account_connected", lambda *_args: True)
    monkeypatch.setattr(engine_api, "load_mailbox_account", lambda *_args: mailbox)

    catalog = engine_api.dispatch(
        request(
            config_path,
            "gmail.labels.catalog",
            {"provider": "gmail", "account_id": "gmail-default"},
        )
    )
    assert catalog == {
        "provider": "gmail",
        "account_id": "gmail-default",
        "revision": 0,
        "items": [
            {
                "label_id": "Label_123",
                "display_name": "Invoices",
                "selected": False,
                "selector_id": None,
            },
            {
                "label_id": "Label_456",
                "display_name": "Receipts",
                "selected": False,
                "selector_id": None,
            },
        ],
    }
    assert gateway.identity_calls == 1
    assert gateway.catalog_calls == 1

    system_label = engine_api._response(
        request(
            config_path,
            "gmail.label_selectors.add",
            {
                "provider": "gmail",
                "account_id": "gmail-default",
                "label_id": "INBOX",
                "expected_revision": 0,
            },
        )
    )
    assert system_label["error"]["code"] == "label_not_user"
    assert runtime.store.gmail_label_selector_set("gmail-default").revision == 0

    added = engine_api.dispatch(
        request(
            config_path,
            "gmail.label_selectors.add",
            {
                "provider": "gmail",
                "account_id": "gmail-default",
                "label_id": "Label_123",
                "expected_revision": 0,
            },
        )
    )
    assert added["revision"] == 1
    assert added["item"] == {
        "selector_id": added["item"]["selector_id"],
        "label_id": "Label_123",
        "display_name": "Invoices",
        "status": "active",
        "admission_active": True,
    }

    listed = engine_api.dispatch(
        request(
            config_path,
            "gmail.label_selectors.list",
            {"provider": "gmail", "account_id": "gmail-default"},
        )
    )
    assert listed["revision"] == 1
    assert listed["catalog_state"] == "unavailable"
    assert listed["items"] == [
        {
            **added["item"],
            "status": "validation_unavailable",
            "admission_active": False,
        }
    ]

    removed = engine_api.dispatch(
        request(
            config_path,
            "gmail.label_selectors.remove",
            {
                "provider": "gmail",
                "account_id": "gmail-default",
                "selector_id": added["item"]["selector_id"],
                "expected_revision": 1,
            },
        )
    )
    assert removed == {
        "revision": 2,
        "removed_selector_id": added["item"]["selector_id"],
    }


def test_gmail_label_add_reopens_identity_and_fails_closed_on_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    identity_x = _bind_test_mailbox(runtime.store, "gmail", "gmail-default")
    identity_y = "f" * 64
    first = MailboxSession(
        "gmail",
        "gmail-default",
        FakeGmailLabelGateway(
            identity_x,
            (GmailLabel("Label_123", "Invoices", "user"),),
        ),
    )
    second = MailboxSession(
        "gmail",
        "gmail-default",
        FakeGmailLabelGateway(identity_y, ()),
    )
    sessions = iter((first, second))
    monkeypatch.setattr(engine_api, "load_runtime", lambda _path: runtime)
    monkeypatch.setattr(engine_api, "mail_account_connected", lambda *_args: True)
    monkeypatch.setattr(engine_api, "load_mailbox_account", lambda *_args: next(sessions))

    response = engine_api._response(
        request(
            config_path,
            "gmail.label_selectors.add",
            {
                "provider": "gmail",
                "account_id": "gmail-default",
                "label_id": "Label_123",
                "expected_revision": 0,
            },
        )
    )
    assert response["error"]["code"] == "mailbox_identity_changed"
    assert runtime.store.gmail_label_selectors("gmail-default") == ()
    assert runtime.store.gmail_label_selector_set("gmail-default").revision == 0


def test_gmail_label_list_uses_durable_identity_without_provider_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    identity_key = _bind_test_mailbox(runtime.store, "gmail", "gmail-default")
    revision, selector = runtime.store.add_gmail_label_selector(
        "gmail-default",
        identity_key,
        "Label_123",
        "Invoices",
        0,
    )

    monkeypatch.setattr(engine_api, "load_runtime", lambda _path: runtime)
    monkeypatch.setattr(engine_api, "mail_account_connected", lambda *_args: False)

    def reject_provider_access(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Selector listing attempted Gmail provider access")

    monkeypatch.setattr(engine_api, "load_mailbox_account", reject_provider_access)

    listed = engine_api.dispatch(
        request(
            config_path,
            "gmail.label_selectors.list",
            {"provider": "gmail", "account_id": "gmail-default"},
        )
    )
    assert listed == {
        "provider": "gmail",
        "account_id": "gmail-default",
        "revision": revision,
        "catalog_state": "unavailable",
        "items": [
            {
                "selector_id": selector.selector_id,
                "label_id": "Label_123",
                "display_name": "Invoices",
                "status": "validation_unavailable",
                "admission_active": False,
            }
        ],
    }


def test_gmail_catalog_validation_survives_restart_and_tracks_rename_delete_and_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    identity_key = _bind_test_mailbox(runtime.store, "gmail", "gmail-default")
    revision, selector = runtime.store.add_gmail_label_selector(
        "gmail-default",
        identity_key,
        "Label_123",
        "Invoices",
        0,
    )
    runtime.store.add_message(
        message_id="message-before-rename",
        provider_message_id="provider-before-rename",
        mailbox_identity_key=identity_key,
        thread_id=None,
        sender="sender@example.com",
        sender_name=None,
        subject="Original admission",
        received_at="2026-09-19T11:59:00+00:00",
        admission=AdmissionProvenance(
            kind="gmail_user_label",
            selector_id=selector.selector_id,
            display_name="Invoices",
            mailbox_identity_key=identity_key,
            admitted_at="2026-09-19T12:00:00+00:00",
        ),
    )
    gateway = FakeGmailLabelGateway(
        identity_key,
        (GmailLabel("Label_123", "Renamed invoices", "user"),),
    )
    mailbox = MailboxSession("gmail", "gmail-default", gateway)
    monkeypatch.setattr(engine_api, "load_runtime", lambda _path: runtime)
    monkeypatch.setattr(engine_api, "mail_account_connected", lambda *_args: True)
    monkeypatch.setattr(engine_api, "load_mailbox_account", lambda *_args: mailbox)

    renamed_catalog = engine_api.dispatch(
        request(
            config_path,
            "gmail.labels.catalog",
            {"provider": "gmail", "account_id": "gmail-default"},
        )
    )
    assert renamed_catalog["revision"] == revision
    assert renamed_catalog["items"][0]["display_name"] == "Renamed invoices"
    assert renamed_catalog["items"][0]["selected"] is True

    restarted = load_runtime(config_path)
    monkeypatch.setattr(engine_api, "load_runtime", lambda _path: restarted)
    monkeypatch.setattr(engine_api, "mail_account_connected", lambda *_args: False)

    def reject_provider_access(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Offline selector listing attempted Gmail provider access")

    monkeypatch.setattr(engine_api, "load_mailbox_account", reject_provider_access)
    renamed = engine_api.dispatch(
        request(
            config_path,
            "gmail.label_selectors.list",
            {"provider": "gmail", "account_id": "gmail-default"},
        )
    )
    assert renamed == {
        "provider": "gmail",
        "account_id": "gmail-default",
        "revision": revision,
        "catalog_state": "current",
        "items": [
            {
                "selector_id": selector.selector_id,
                "label_id": "Label_123",
                "display_name": "Renamed invoices",
                "status": "active",
                "admission_active": True,
            }
        ],
    }
    assert restarted.store.recent(1)[0]["admission"] == {
        "kind": "gmail_user_label",
        "selector_id": selector.selector_id,
        "display_name": "Invoices",
        "admitted_at": "2026-09-19T12:00:00+00:00",
    }

    for labels, expected_status, expected_name in (
        ((), "deleted", "Renamed invoices"),
        ((GmailLabel("Label_123", "System invoices", "system"),), "not_user", "System invoices"),
    ):
        gateway.labels = labels
        monkeypatch.setattr(engine_api, "mail_account_connected", lambda *_args: True)
        monkeypatch.setattr(engine_api, "load_mailbox_account", lambda *_args: mailbox)
        engine_api.dispatch(
            request(
                config_path,
                "gmail.labels.catalog",
                {"provider": "gmail", "account_id": "gmail-default"},
            )
        )
        monkeypatch.setattr(engine_api, "mail_account_connected", lambda *_args: False)
        monkeypatch.setattr(engine_api, "load_mailbox_account", reject_provider_access)
        listed = engine_api.dispatch(
            request(
                config_path,
                "gmail.label_selectors.list",
                {"provider": "gmail", "account_id": "gmail-default"},
            )
        )
        assert listed["catalog_state"] == "current"
        assert listed["items"] == [
            {
                "selector_id": selector.selector_id,
                "label_id": "Label_123",
                "display_name": expected_name,
                "status": expected_status,
                "admission_active": False,
            }
        ]


def test_gmail_catalog_revision_race_does_not_validate_newer_selector_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    identity_key = _bind_test_mailbox(runtime.store, "gmail", "gmail-default")
    revision, _selector = runtime.store.add_gmail_label_selector(
        "gmail-default", identity_key, "Label_1", "First", 0
    )
    gateway = FakeGmailLabelGateway(identity_key, ())

    def race_catalog() -> tuple[GmailLabel, ...]:
        runtime.store.add_gmail_label_selector(
            "gmail-default",
            identity_key,
            "Label_2",
            "Second",
            revision,
        )
        return (
            GmailLabel("Label_1", "First", "user"),
            GmailLabel("Label_2", "Second", "user"),
        )

    gateway.label_catalog = race_catalog  # type: ignore[method-assign]
    mailbox = MailboxSession("gmail", "gmail-default", gateway)
    monkeypatch.setattr(engine_api, "load_runtime", lambda _path: runtime)
    monkeypatch.setattr(engine_api, "mail_account_connected", lambda *_args: True)
    monkeypatch.setattr(engine_api, "load_mailbox_account", lambda *_args: mailbox)

    response = engine_api._response(
        request(
            config_path,
            "gmail.labels.catalog",
            {"provider": "gmail", "account_id": "gmail-default"},
        )
    )
    assert response["error"]["code"] == "stale_revision"


def test_gmail_catalog_failure_invalidates_only_matching_durable_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    identity_key = _bind_test_mailbox(runtime.store, "gmail", "gmail-default")
    revision, selector = runtime.store.add_gmail_label_selector(
        "gmail-default", identity_key, "Label_1", "Invoices", 0
    )
    gateway = FakeGmailLabelGateway(
        identity_key,
        (GmailLabel("Label_1", "Invoices", "user"),),
    )
    mailbox = MailboxSession("gmail", "gmail-default", gateway)
    monkeypatch.setattr(engine_api, "load_runtime", lambda _path: runtime)
    monkeypatch.setattr(engine_api, "mail_account_connected", lambda *_args: True)
    monkeypatch.setattr(engine_api, "load_mailbox_account", lambda *_args: mailbox)
    engine_api.dispatch(
        request(
            config_path,
            "gmail.labels.catalog",
            {"provider": "gmail", "account_id": "gmail-default"},
        )
    )

    def unavailable() -> tuple[GmailLabel, ...]:
        raise GmailLabelCatalogUnavailable("offline")

    gateway.label_catalog = unavailable  # type: ignore[method-assign]
    response = engine_api._response(
        request(
            config_path,
            "gmail.labels.catalog",
            {"provider": "gmail", "account_id": "gmail-default"},
        )
    )
    assert response["error"]["code"] == "gmail_label_catalog_unavailable"

    def reject_provider_access(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Offline selector listing attempted Gmail provider access")

    monkeypatch.setattr(engine_api, "mail_account_connected", lambda *_args: False)
    monkeypatch.setattr(engine_api, "load_mailbox_account", reject_provider_access)
    listed = engine_api.dispatch(
        request(
            config_path,
            "gmail.label_selectors.list",
            {"provider": "gmail", "account_id": "gmail-default"},
        )
    )
    assert listed == {
        "provider": "gmail",
        "account_id": "gmail-default",
        "revision": revision,
        "catalog_state": "unavailable",
        "items": [
            {
                "selector_id": selector.selector_id,
                "label_id": "Label_1",
                "display_name": "Invoices",
                "status": "validation_unavailable",
                "admission_active": False,
            }
        ],
    }


def test_invalid_gmail_catalog_classification_survives_restart_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    identity_key = _bind_test_mailbox(runtime.store, "gmail", "gmail-default")
    revision, selector = runtime.store.add_gmail_label_selector(
        "gmail-default", identity_key, "Label_1", "Invoices", 0
    )
    gateway = FakeGmailLabelGateway(
        identity_key,
        (GmailLabel("Label_1", "Invoices", "unknown"),),
    )
    mailbox = MailboxSession("gmail", "gmail-default", gateway)
    monkeypatch.setattr(engine_api, "load_runtime", lambda _path: runtime)
    monkeypatch.setattr(engine_api, "mail_account_connected", lambda *_args: True)
    monkeypatch.setattr(engine_api, "load_mailbox_account", lambda *_args: mailbox)

    response = engine_api._response(
        request(
            config_path,
            "gmail.labels.catalog",
            {"provider": "gmail", "account_id": "gmail-default"},
        )
    )
    assert response["error"] == {
        "code": "gmail_label_catalog_invalid",
        "message": "Gmail returned an invalid label catalog",
        "retryable": False,
    }

    restarted = load_runtime(config_path)
    monkeypatch.setattr(engine_api, "load_runtime", lambda _path: restarted)
    monkeypatch.setattr(engine_api, "mail_account_connected", lambda *_args: False)

    def reject_provider_access(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Offline selector listing attempted Gmail provider access")

    monkeypatch.setattr(engine_api, "load_mailbox_account", reject_provider_access)
    listed = engine_api.dispatch(
        request(
            config_path,
            "gmail.label_selectors.list",
            {"provider": "gmail", "account_id": "gmail-default"},
        )
    )
    assert listed == {
        "provider": "gmail",
        "account_id": "gmail-default",
        "revision": revision,
        "catalog_state": "invalid_catalog",
        "items": [
            {
                "selector_id": selector.selector_id,
                "label_id": "Label_1",
                "display_name": "Invoices",
                "status": "validation_unavailable",
                "admission_active": False,
            }
        ],
    }

    gateway.labels = (GmailLabel("Label_1", "Renamed invoices", "user"),)
    monkeypatch.setattr(engine_api, "mail_account_connected", lambda *_args: True)
    monkeypatch.setattr(engine_api, "load_mailbox_account", lambda *_args: mailbox)
    refreshed = engine_api.dispatch(
        request(
            config_path,
            "gmail.labels.catalog",
            {"provider": "gmail", "account_id": "gmail-default"},
        )
    )
    assert refreshed["revision"] == revision
    current = engine_api.dispatch(
        request(
            config_path,
            "gmail.label_selectors.list",
            {"provider": "gmail", "account_id": "gmail-default"},
        )
    )
    assert current["catalog_state"] == "current"
    assert current["items"][0]["display_name"] == "Renamed invoices"
    assert current["items"][0]["admission_active"] is True


def test_declared_invalid_gmail_catalog_is_persisted_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    identity_key = _bind_test_mailbox(runtime.store, "gmail", "gmail-default")
    revision, _selector = runtime.store.add_gmail_label_selector(
        "gmail-default", identity_key, "Label_1", "Invoices", 0
    )
    gateway = FakeGmailLabelGateway(identity_key, ())

    def invalid_catalog() -> tuple[GmailLabel, ...]:
        raise GmailLabelCatalogInvalid("malformed")

    gateway.label_catalog = invalid_catalog  # type: ignore[method-assign]
    mailbox = MailboxSession("gmail", "gmail-default", gateway)
    monkeypatch.setattr(engine_api, "load_runtime", lambda _path: runtime)
    monkeypatch.setattr(engine_api, "mail_account_connected", lambda *_args: True)
    monkeypatch.setattr(engine_api, "load_mailbox_account", lambda *_args: mailbox)

    response = engine_api._response(
        request(
            config_path,
            "gmail.labels.catalog",
            {"provider": "gmail", "account_id": "gmail-default"},
        )
    )
    assert response["error"]["code"] == "gmail_label_catalog_invalid"
    snapshot = runtime.store.gmail_label_validation_snapshot(
        "gmail-default", identity_key, revision
    )
    assert snapshot is not None
    assert snapshot.catalog_state == "invalid_catalog"
    assert snapshot.selectors == ()


def test_invalid_gmail_catalog_revision_race_cannot_publish_stale_classification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    identity_key = _bind_test_mailbox(runtime.store, "gmail", "gmail-default")
    revision, _selector = runtime.store.add_gmail_label_selector(
        "gmail-default", identity_key, "Label_1", "First", 0
    )
    gateway = FakeGmailLabelGateway(identity_key, ())

    def race_invalid_catalog() -> tuple[GmailLabel, ...]:
        runtime.store.add_gmail_label_selector(
            "gmail-default", identity_key, "Label_2", "Second", revision
        )
        raise GmailLabelCatalogInvalid("invalid")

    gateway.label_catalog = race_invalid_catalog  # type: ignore[method-assign]
    mailbox = MailboxSession("gmail", "gmail-default", gateway)
    monkeypatch.setattr(engine_api, "load_runtime", lambda _path: runtime)
    monkeypatch.setattr(engine_api, "mail_account_connected", lambda *_args: True)
    monkeypatch.setattr(engine_api, "load_mailbox_account", lambda *_args: mailbox)

    response = engine_api._response(
        request(
            config_path,
            "gmail.labels.catalog",
            {"provider": "gmail", "account_id": "gmail-default"},
        )
    )
    assert response["error"]["code"] == "gmail_label_catalog_invalid"
    current = runtime.store.gmail_label_selector_set("gmail-default")
    assert current is not None
    assert current.revision == revision + 1
    assert runtime.store.gmail_label_validation_snapshot(
        "gmail-default", identity_key, current.revision
    ) is None


def test_watchlist_rejects_sender_name_over_utf8_byte_limit(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path, include_senders=False)
    original = config_path.read_bytes()

    accepted = engine_api._response(
        request(
            config_path,
            "watchlist.add",
            {"email": "accepted@example.com", "name": "é" * 512},
        )
    )
    assert accepted["ok"] is True

    before_rejected = config_path.read_bytes()
    rejected = engine_api._response(
        request(
            config_path,
            "watchlist.add",
            {"email": "rejected@example.com", "name": ("é" * 512) + "a"},
        )
    )
    assert rejected["error"] == {
        "code": "invalid_request",
        "message": "sender name must be at most 1024 UTF-8 bytes",
    }
    assert config_path.read_bytes() == before_rejected
    assert config_path.read_bytes() != original


def test_existing_overlong_sender_name_fails_before_watcher_polling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    text = config_path.read_text(encoding="utf-8").replace(
        'name = "Zed"',
        f'name = "{("é" * 512) + "a"}"',
    )
    config_path.write_text(text, encoding="utf-8")

    def reject_poll(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Watcher polling started before configuration validation")

    monkeypatch.setattr(engine_api, "run_watcher_check", reject_poll)
    response = engine_api._response(request(config_path, "watcher.check", {"dry_run": True}))
    assert response["error"] == {
        "code": "configuration_error",
        "message": "sender name must be at most 1024 UTF-8 bytes",
    }


def test_gmail_label_list_marks_superseded_identity_inert_without_provider_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    original_identity = _bind_test_mailbox(runtime.store, "gmail", "gmail-default")
    revision, selector = runtime.store.add_gmail_label_selector(
        "gmail-default",
        original_identity,
        "Label_123",
        "Invoices",
        0,
    )
    replacement_identity = "f" * 64
    runtime.store.reconcile_mailbox_identity(
        "gmail",
        "gmail-default",
        replacement_identity,
    )
    monkeypatch.setattr(engine_api, "load_runtime", lambda _path: runtime)
    monkeypatch.setattr(engine_api, "mail_account_connected", lambda *_args: False)

    def reject_provider_access(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Selector listing attempted Gmail provider access")

    monkeypatch.setattr(engine_api, "load_mailbox_account", reject_provider_access)

    listed = engine_api.dispatch(
        request(
            config_path,
            "gmail.label_selectors.list",
            {"provider": "gmail", "account_id": "gmail-default"},
        )
    )
    assert listed == {
        "provider": "gmail",
        "account_id": "gmail-default",
        "revision": revision + 1,
        "catalog_state": "unavailable",
        "items": [
            {
                "selector_id": selector.selector_id,
                "label_id": "Label_123",
                "display_name": "Invoices",
                "status": "identity_mismatch",
                "admission_active": False,
            }
        ],
    }


def test_gmail_label_list_requires_durable_account_identity_without_provider_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    monkeypatch.setattr(engine_api, "load_runtime", lambda _path: runtime)

    def reject_provider_access(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Selector listing attempted Gmail provider access")

    monkeypatch.setattr(engine_api, "load_mailbox_account", reject_provider_access)
    response = engine_api._response(
        request(
            config_path,
            "gmail.label_selectors.list",
            {"provider": "gmail", "account_id": "gmail-default"},
        )
    )
    assert response["error"]["code"] == "account_unavailable"


def test_gmail_label_list_rejects_missing_account_without_provider_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    monkeypatch.setattr(engine_api, "load_runtime", lambda _path: runtime)

    def reject_provider_access(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Selector listing attempted Gmail provider access")

    monkeypatch.setattr(engine_api, "load_mailbox_account", reject_provider_access)
    response = engine_api._response(
        request(
            config_path,
            "gmail.label_selectors.list",
            {"provider": "gmail", "account_id": "gmail-missing"},
        )
    )
    assert response["error"]["code"] == "not_found"


def test_gmail_label_remove_is_provider_network_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    identity_key = _bind_test_mailbox(runtime.store, "gmail", "gmail-default")
    revision, selector = runtime.store.add_gmail_label_selector(
        "gmail-default",
        identity_key,
        "Label_123",
        "Invoices",
        0,
    )
    monkeypatch.setattr(engine_api, "load_runtime", lambda _path: runtime)
    monkeypatch.setattr(engine_api, "mail_account_connected", lambda *_args: True)

    def reject_provider_access(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Remove attempted Gmail provider access")

    monkeypatch.setattr(engine_api, "load_mailbox_account", reject_provider_access)
    removed = engine_api.dispatch(
        request(
            config_path,
            "gmail.label_selectors.remove",
            {
                "provider": "gmail",
                "account_id": "gmail-default",
                "selector_id": selector.selector_id,
                "expected_revision": revision,
            },
        )
    )
    assert removed == {"revision": 2, "removed_selector_id": selector.selector_id}


@pytest.mark.parametrize("label_id", ["x" * 513, "Label_123\u0000"])
def test_gmail_label_add_rejects_oversized_or_control_character_ids(
    tmp_path: Path,
    label_id: str,
) -> None:
    with pytest.raises(engine_api.ApiError, match="label_id is invalid"):
        engine_api.dispatch(
            request(
                tmp_path / "config.toml",
                "gmail.label_selectors.add",
                {
                    "provider": "gmail",
                    "account_id": "gmail-default",
                    "label_id": label_id,
                    "expected_revision": 0,
                },
            )
        )


def test_gmail_label_operations_reject_non_active_account_before_provider_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.store.register_mail_account(
        "gmail",
        "gmail-retained",
        display_name="Gmail",
        address="retained@example.com",
        active=False,
    )
    monkeypatch.setattr(engine_api, "load_runtime", lambda _path: runtime)
    monkeypatch.setattr(engine_api, "mail_account_connected", lambda *_args: True)

    def reject_provider_access(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Non-active account reached provider access")

    monkeypatch.setattr(engine_api, "load_mailbox_account", reject_provider_access)
    response = engine_api._response(
        request(
            config_path,
            "gmail.labels.catalog",
            {"provider": "gmail", "account_id": "gmail-retained"},
        )
    )
    assert response["error"]["code"] == "account_not_active"


def microsoft_principal(*, object_id: str = "object-1") -> MicrosoftPrincipal:
    return MicrosoftPrincipal(
        home_account_id=f"{object_id}.tenant-1",
        tenant_id="tenant-1",
        object_id=object_id,
        email_address="owner@example.com",
    )


def imap_credentials(
    *, host: str = "mail.example.com", password: str = "private-password"
) -> ImapCredentials:
    return ImapCredentials(
        email_address="owner@example.com",
        host=host,
        port=993,
        security="tls",
        username="owner@example.com",
        password=password,
    )


def test_read_operations_are_versioned_and_do_not_expose_token_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    monkeypatch.setattr(
        "eom_email_watcher.model.LocalModel.health", lambda self: (True, "HTTP 200")
    )

    watchlist = engine_api._response(request(config_path, "watchlist.list"))
    assert watchlist == {
        "protocol": 1,
        "ok": True,
        "operation": "watchlist.list",
        "data": {
            "items": [
                {"email": "a@example.com", "name": "Trusted A"},
                {"email": "z@example.com", "name": "Zed"},
            ]
        },
    }

    settings = engine_api._response(request(config_path, "settings.get"))
    assert settings["ok"] is True
    assert settings["data"]["poll_interval_minutes"] == 120
    assert settings["data"]["polling_supported"] is True
    encoded = json.dumps(settings)
    assert "model_api_token_file" not in encoded
    assert "send-token.json" not in encoded

    health = engine_api._response(request(config_path, "health.get"))
    assert health["data"]["local_model"]["ok"] is True
    assert health["data"]["notifications"] == {
        "delivery": "host",
        "enabled": True,
        "host_delivery_ready": True,
        "ntfy_configured": False,
    }
    assert health["data"]["production_check_supported"] is True

    runtime = load_runtime(config_path)
    _add_test_message(
        runtime.store,
        message_id="m1",
        thread_id=None,
        sender="a@example.com",
        sender_name=None,
        subject="Subject",
        received_at="2026-07-18T14:00:00+00:00",
    )
    inbox = engine_api._response(request(config_path, "inbox.recent", {"limit": 1}))
    assert inbox["data"]["items"][0]["message_id"] == "m1"
    assert inbox["data"]["items"][0]["provider"] == "gmail"
    assert inbox["data"]["items"][0]["account_id"] == "gmail-default"
    assert inbox["data"]["items"][0]["attachments"] == []
    assert inbox["data"]["items"][0]["admission"] == {
        "kind": "exact_sender",
        "selector_id": "sender:a@example.com",
        "display_name": None,
        "admitted_at": "2026-09-19T12:00:00+00:00",
    }
    assert "mailbox_identity" not in json.dumps(inbox)


def test_inbox_query_returns_opaque_cursor_and_uses_only_local_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    for message_id in ("message-1", "message-2"):
        _add_test_message(
            runtime.store,
            message_id=message_id,
            thread_id=None,
            sender="billing@example.com",
            sender_name="Billing",
            subject="Invoice status",
            received_at="2026-08-31T12:00:00+00:00",
        )
    _mark_test_analyzed(
        runtime.store,
        "message-2",
        {
            "category": "invoice",
            "priority": "high",
            "summary": "Invoice needs review.",
            "action_required": True,
            "suggested_action": "Review it.",
            "deadline_text": None,
            "deadline_iso": None,
            "confidence": 0.9,
        },
    )
    monkeypatch.setattr(engine_api, "load_runtime", lambda _path: runtime)

    def reject_external_access(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Inbox query attempted external provider access")

    monkeypatch.setattr(engine_api.GmailGateway, "from_token", reject_external_access)
    monkeypatch.setattr(runtime.model, "analyze", reject_external_access)
    monkeypatch.setattr(engine_api.connect, "discover_capabilities", reject_external_access)
    monkeypatch.setattr(engine_api.connect, "discover_summary_capability", reject_external_access)
    first = engine_api._response(
        request(
            config_path,
            "inbox.query",
            {
                "account_id": "gmail-default",
                "category": "invoice",
                "limit": 1,
                "provider": "gmail",
                "sender_query": "BILL",
            },
        )
    )
    assert first["ok"] is True
    assert first["data"]["items"][0]["message_id"] == "message-2"
    assert first["data"]["items"][0]["category"] == "invoice"
    assert first["data"]["next_cursor"] is None

    unfiltered = engine_api._response(request(config_path, "inbox.query", {"limit": 1}))
    cursor = unfiltered["data"]["next_cursor"]
    assert isinstance(cursor, str) and cursor
    second = engine_api._response(
        request(config_path, "inbox.query", {"cursor": cursor, "limit": 1})
    )
    assert [item["message_id"] for item in unfiltered["data"]["items"]] == ["message-2"]
    assert [item["message_id"] for item in second["data"]["items"]] == ["message-1"]
    assert second["data"]["next_cursor"] is None


@pytest.mark.parametrize("entitled", [False, True])
@pytest.mark.parametrize("operation", ["inbox.query", "inbox.recent"])
def test_inbox_exposes_calendar_proposal_only_with_automation_entitlement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entitled: bool,
    operation: str,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    proposal = {
        "run_id": "run-1",
        "state": "awaiting_confirmation",
        "proposal_version": 1,
    }
    monkeypatch.setattr(engine_api, "load_runtime", lambda _path: runtime)
    monkeypatch.setattr(
        runtime.store,
        "query_inbox",
        lambda **kwargs: ([{"message_id": "message-1", "calendar_proposal": proposal}], None),
    )
    monkeypatch.setattr(
        runtime.store,
        "recent",
        lambda limit: [{"message_id": "message-1", "calendar_proposal": proposal}],
    )
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: entitled)

    response = engine_api._response(request(config_path, operation, {"limit": 25}))

    assert response["ok"] is True
    assert response["data"]["items"][0]["calendar_proposal"] == (proposal if entitled else None)


@pytest.mark.parametrize("operation", ["inbox.query", "inbox.recent"])
@pytest.mark.parametrize(
    "state",
    ["write_authorized", "writing", "unresolved", "reconciling", "completed", "failed"],
)
def test_inbox_preserves_durable_calendar_write_outcomes_after_entitlement_expiry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    state: str,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    proposal = {"run_id": "run-1", "state": state, "proposal_version": 1}
    monkeypatch.setattr(engine_api, "load_runtime", lambda _path: runtime)
    monkeypatch.setattr(
        runtime.store,
        "query_inbox",
        lambda **kwargs: ([{"message_id": "message-1", "calendar_proposal": proposal}], None),
    )
    monkeypatch.setattr(
        runtime.store,
        "recent",
        lambda limit: [{"message_id": "message-1", "calendar_proposal": proposal}],
    )
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: False)

    response = engine_api._response(request(config_path, operation, {"limit": 25}))

    assert response["ok"] is True
    assert response["data"]["items"][0]["calendar_proposal"] == proposal


def test_calendar_proposal_decision_binds_message_and_exact_proposal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    run_id = "77d9c691-1c91-4e23-8f03-92973e12c385"
    monkeypatch.setattr(engine_api, "load_runtime", lambda _path: runtime)
    monkeypatch.setattr(
        runtime.store,
        "automation_run_for_message",
        lambda message_id: SimpleNamespace(run_id=run_id),
    )
    calls: list[dict[str, object]] = []

    def decide(config, store, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            run=SimpleNamespace(
                run_id=run_id,
                state="completed",
                state_version=8,
                failure_code=None,
            ),
            write=SimpleNamespace(graph_event_id="immutable-event-id"),
        )

    monkeypatch.setattr(engine_api, "decide_scheduling_proposal", decide)
    monkeypatch.setattr(engine_api, "operation_lock_supported", lambda path: True)

    @contextmanager
    def lock(path, message):
        yield

    monkeypatch.setattr(engine_api, "operation_lock", lock)
    response = engine_api._response(
        request(
            config_path,
            "calendar.automation.decide",
            {
                "decision": "confirm",
                "message_id": "message-1",
                "proposal_sha256": "a" * 64,
                "proposal_version": 2,
                "run_id": run_id,
                "state_version": 7,
            },
        )
    )

    assert response["ok"] is True
    assert response["data"] == {
        "run_id": run_id,
        "state": "completed",
        "state_version": 8,
        "failure_code": None,
        "graph_event_id": "immutable-event-id",
    }
    assert calls == [
        {
            "run_id": run_id,
            "expected_state_version": 7,
            "proposal_version": 2,
            "proposal_sha256": "a" * 64,
            "decision": "confirm",
        }
    ]


def test_calendar_proposal_decision_rejects_cross_message_run_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    monkeypatch.setattr(engine_api, "load_runtime", lambda _path: runtime)
    monkeypatch.setattr(
        runtime.store,
        "automation_run_for_message",
        lambda message_id: SimpleNamespace(run_id="11111111-1111-4111-8111-111111111111"),
    )
    monkeypatch.setattr(
        engine_api,
        "decide_scheduling_proposal",
        lambda *args, **kwargs: pytest.fail("cross-message decision reached service"),
    )
    monkeypatch.setattr(engine_api, "operation_lock_supported", lambda path: True)

    @contextmanager
    def lock(path, message):
        yield

    monkeypatch.setattr(engine_api, "operation_lock", lock)
    response = engine_api._response(
        request(
            config_path,
            "calendar.automation.decide",
            {
                "decision": "decline",
                "message_id": "message-1",
                "proposal_sha256": "a" * 64,
                "proposal_version": 1,
                "run_id": "77d9c691-1c91-4e23-8f03-92973e12c385",
                "state_version": 7,
            },
        )
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "not_found"


@pytest.mark.parametrize(
    "override",
    [
        {"decision": "approve"},
        {"run_id": "not-a-uuid"},
        {"proposal_sha256": "g" * 64},
        {"proposal_version": False},
        {"proposal_version": 2**63},
        {"state_version": 0},
        {"state_version": 2**63},
        {"unexpected": True},
    ],
)
def test_calendar_proposal_decision_rejects_ambiguous_identity_before_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    override: dict[str, object],
) -> None:
    payload: dict[str, object] = {
        "decision": "decline",
        "message_id": "message-1",
        "proposal_sha256": "a" * 64,
        "proposal_version": 1,
        "run_id": "77d9c691-1c91-4e23-8f03-92973e12c385",
        "state_version": 7,
    }
    payload.update(override)
    monkeypatch.setattr(
        engine_api,
        "load_runtime",
        lambda path: pytest.fail("invalid proposal decision reached runtime"),
    )

    response = engine_api._response(
        request(tmp_path / "unused.toml", "calendar.automation.decide", payload)
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


@pytest.mark.parametrize(
    "payload",
    [
        {"limit": False},
        {"limit": 0},
        {"limit": 101},
        {"cursor": "not-valid-base64!"},
        {"cursor": "eyJtZXNzYWdlX2lkIjoibTEiLCJyZWNlaXZlZF9hdCI6InQiLCJ2Ijp0cnVlfQ"},
        {"sender_query": ""},
        {"keyword": "x" * 201},
        {"priority": "critical"},
        {"category": "payments"},
        {"status": "deleted"},
    ],
)
def test_inbox_query_rejects_invalid_bounds_and_cursors(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    response = engine_api._response(request(tmp_path / "unused.toml", "inbox.query", payload))

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


def test_inbox_delete_and_clear_are_local_only_and_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.store.set_state("100", datetime.now(UTC))
    for message_id in ("message-1", "message-2"):
        _add_test_message(
            runtime.store,
            message_id=message_id,
            thread_id=None,
            sender="billing@example.com",
            sender_name="Billing",
            subject="Invoice status",
            received_at=datetime.now(UTC).isoformat(),
        )
    monkeypatch.setattr(engine_api, "load_runtime", lambda _path: runtime)

    def reject_external_access(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Local inbox mutation attempted external provider access")

    monkeypatch.setattr(engine_api.GmailGateway, "from_token", reject_external_access)
    monkeypatch.setattr(runtime.model, "analyze", reject_external_access)
    monkeypatch.setattr(engine_api.connect, "discover_capabilities", reject_external_access)

    deleted = engine_api._response(
        request(config_path, "inbox.delete", {"message_id": "message-1"})
    )
    missing = engine_api._response(request(config_path, "inbox.delete", {"message_id": "missing"}))
    cleared = engine_api._response(request(config_path, "inbox.clear"))

    assert deleted["data"] == {"deleted": True, "message_id": "message-1"}
    assert missing["error"]["code"] == "not_found"
    assert cleared["data"] == {"deleted": 1}
    assert runtime.store.recent(10) == []
    assert runtime.store.state()[0] == "100"


@pytest.mark.parametrize("message_id", [None, "", "x" * 513])
def test_inbox_delete_rejects_invalid_message_identity(tmp_path: Path, message_id: object) -> None:
    response = engine_api._response(
        request(tmp_path / "unused.toml", "inbox.delete", {"message_id": message_id})
    )

    assert response["error"]["code"] == "invalid_request"


def test_health_recognizes_bundled_desktop_oauth_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    bundle_root = tmp_path / "bundle"
    bundled_file = bundle_root / "eom_email_watcher_data/google-oauth-client.json"
    bundled_file.parent.mkdir(parents=True)
    bundled_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("eom_email_watcher.gmail.sys._MEIPASS", str(bundle_root), raising=False)
    monkeypatch.setattr(
        "eom_email_watcher.model.LocalModel.health", lambda self: (True, "HTTP 200")
    )

    health = engine_api._response(request(config_path, "health.get"))

    assert health["data"]["gmail"]["credentials_configured"] is True


def test_legacy_gmail_health_reports_only_the_active_account_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.config.gmail_token_file.write_text("retained token", encoding="utf-8")
    disconnected = runtime.store.register_mail_account(
        "gmail",
        f"gmail-{'b' * 32}",
        display_name="Gmail",
        address="active@example.com",
        active=True,
    )
    monkeypatch.setattr(engine_api, "load_runtime", lambda _path: runtime)
    monkeypatch.setattr(
        "eom_email_watcher.model.LocalModel.health", lambda self: (True, "HTTP 200")
    )

    health = engine_api._response(request(config_path, "health.get"))

    assert runtime.store.active_mail_account() == disconnected
    assert health["data"]["gmail"]["connected"] is False
    assert [
        account["connected"]
        for account in health["data"]["mail"]["accounts"]
        if account["account_id"] == "gmail-default"
    ] == [True]


def test_health_reports_only_current_identity_gmail_label_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path, include_senders=False)
    runtime = load_runtime(config_path)
    identity = _bind_test_mailbox(runtime.store, "gmail", "gmail-default")
    runtime.store.add_gmail_label_selector(
        "gmail-default", identity, "Label_123", "Invoices", 0
    )
    monkeypatch.setattr(engine_api, "load_runtime", lambda _path: runtime)
    monkeypatch.setattr(
        "eom_email_watcher.model.LocalModel.health", lambda self: (True, "HTTP 200")
    )

    current = engine_api._response(request(config_path, "health.get"))

    assert current["data"]["gmail"]["label_watch_configured"] is True

    runtime.store.reconcile_mailbox_identity(
        "gmail", "gmail-default", "f" * 64, preserve_cursor=True
    )
    replaced = engine_api._response(request(config_path, "health.get"))

    assert replaced["data"]["gmail"]["label_watch_configured"] is False


def test_health_reports_current_identity_gmail_recovery_without_selectors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path, include_senders=False)
    runtime = load_runtime(config_path)
    identity = _bind_test_mailbox(runtime.store, "gmail", "gmail-default")
    runtime.store.create_gmail_recovery_state(
        "gmail-default", identity, 0, [], [], 10, 20, "replacement-history"
    )
    monkeypatch.setattr(engine_api, "load_runtime", lambda _path: runtime)
    monkeypatch.setattr(
        "eom_email_watcher.model.LocalModel.health", lambda self: (True, "HTTP 200")
    )

    health = engine_api._response(request(config_path, "health.get"))

    assert health["data"]["gmail"]["label_watch_configured"] is True


def test_config_initialize_creates_safe_first_run_contract(tmp_path: Path) -> None:
    config_path = tmp_path / "new" / "config.toml"

    response = engine_api._response(
        request(
            config_path,
            "config.initialize",
            {
                "model_base_url": "http://127.0.0.1:8080/v1",
                "model_name": "local-model",
                "timezone": "UTC",
            },
        )
    )

    assert response["ok"] is True
    assert response["data"]["created"] is True
    assert response["data"]["settings"]["timezone"] == "UTC"
    assert response["data"]["settings"]["local_model"] == {
        "authentication_required": False,
        "editable": True,
        "endpoint": "http://127.0.0.1:8080/v1",
        "model": "local-model",
        "timeout_seconds": 60.0,
        "token_configured": False,
    }
    assert load_config(config_path).senders == ()
    encoded = json.dumps(response)
    assert "token.json" not in encoded
    assert "send-token.json" not in encoded


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"model_base_url": "http://127.0.0.1:8080/v1", "model_name": "model"},
        {
            "model_base_url": "http://127.0.0.1:8080/v1",
            "model_name": "model",
            "timezone": "UTC",
            "unknown": True,
        },
        {
            "model_base_url": "https://models.example.com/v1",
            "model_name": "model",
            "timezone": "UTC",
        },
    ],
)
def test_config_initialize_rejects_invalid_payload_without_creating_file(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    config_path = tmp_path / "config.toml"

    response = engine_api._response(request(config_path, "config.initialize", payload))

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert not config_path.exists()


def test_config_initialize_reports_conflict_without_reading_or_replacing(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    original = b"operator-owned config\n"
    config_path.write_bytes(original)

    response = engine_api._response(
        request(
            config_path,
            "config.initialize",
            {
                "model_base_url": "http://127.0.0.1:8080/v1",
                "model_name": "local-model",
                "timezone": "UTC",
            },
        )
    )

    assert response["ok"] is False
    assert response["error"] == {
        "code": "conflict",
        "message": "Configuration already exists",
    }
    assert config_path.read_bytes() == original


def test_gmail_authorize_creates_current_baseline_without_exposing_identifiers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)

    class AuthorizedGmail:
        def mailbox_identity_key(self) -> str:
            return GMAIL_IDENTITY

        def profile(self) -> GmailProfile:
            return GmailProfile("owner@example.com", "private-history-id")

    def authorize_with_status(
        credentials_file: Path,
        token_file: Path,
        *,
        force_reauthorize: bool,
    ) -> tuple[AuthorizedGmail, bool]:
        assert force_reauthorize is True
        token_file.write_text("readonly token", encoding="utf-8")
        return AuthorizedGmail(), True

    monkeypatch.setattr(engine_api.GmailGateway, "authorize_with_status", authorize_with_status)

    response = engine_api._response(request(config_path, "gmail.authorize"))

    assert response == {
        "data": {"baseline_initialized": True, "connected": True},
        "ok": True,
        "operation": "gmail.authorize",
        "protocol": 1,
    }
    assert "private-history-id" not in json.dumps(response)
    assert load_runtime(config_path).store.state()[0] == "private-history-id"


def test_gmail_authorize_compatibility_targets_the_active_generated_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.config.gmail_token_file.write_text("retained legacy token", encoding="utf-8")
    active = runtime.store.register_mail_account(
        "gmail",
        f"gmail-{'c' * 32}",
        display_name="Gmail",
        address="active@example.com",
        active=True,
    )

    class AuthorizedGmail:
        def mailbox_identity_key(self) -> str:
            return GMAIL_IDENTITY

        def profile(self) -> GmailProfile:
            return GmailProfile("active@example.com", "active-history-id")

    def authorize_with_status(
        credentials_file: Path,
        token_file: Path,
        *,
        force_reauthorize: bool,
    ) -> tuple[AuthorizedGmail, bool]:
        assert force_reauthorize is True
        token_file.write_text("active account token", encoding="utf-8")
        return AuthorizedGmail(), True

    monkeypatch.setattr(engine_api.GmailGateway, "authorize_with_status", authorize_with_status)

    response = engine_api._response(request(config_path, "gmail.authorize"))

    assert response["data"] == {"baseline_initialized": True, "connected": True}
    assert mail_account_token_file(runtime.config, active).read_text(encoding="utf-8") == (
        "active account token"
    )
    assert runtime.config.gmail_token_file.read_text(encoding="utf-8") == "retained legacy token"
    assert runtime.store.state(provider=active.provider, account_id=active.account_id)[0] == (
        "active-history-id"
    )


def test_gmail_authorize_preserves_existing_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.store.set_state("preserved-history-id", datetime(2026, 7, 18, tzinfo=UTC))
    runtime.config.gmail_token_file.write_text("existing token", encoding="utf-8")

    class ExistingGmail:
        def mailbox_identity_key(self) -> str:
            return GMAIL_IDENTITY

        def profile(self) -> GmailProfile:
            return GmailProfile("owner@example.com", "current-probe-history-id")

    monkeypatch.setattr(
        engine_api.GmailGateway,
        "from_token",
        lambda credentials_file, token_file: ExistingGmail(),
    )

    response = engine_api._response(request(config_path, "gmail.authorize"))

    assert response["data"] == {"baseline_initialized": False, "connected": True}
    assert load_runtime(config_path).store.state()[0] == "preserved-history-id"


def test_gmail_authorize_resets_baseline_for_an_already_installed_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.store.update_mail_account_identity(
        "gmail",
        "gmail-default",
        display_name="Gmail",
        address="owner@example.com",
    )
    runtime.store.reconcile_mailbox_identity(
        "gmail",
        "gmail-default",
        GMAIL_IDENTITY,
        legacy_status="replacement",
    )
    runtime.store.set_state(
        "old-history-id",
        mailbox_identity_key=GMAIL_IDENTITY,
    )
    runtime.config.gmail_token_file.write_text("replacement token", encoding="utf-8")

    class ExistingReplacementGmail:
        def mailbox_identity_key(self) -> str:
            return REPLACEMENT_GMAIL_IDENTITY

        def profile(self) -> GmailProfile:
            return GmailProfile("owner@example.com", "replacement-history-id")

    monkeypatch.setattr(
        engine_api.GmailGateway,
        "from_token",
        lambda credentials_file, token_file: ExistingReplacementGmail(),
    )

    response = engine_api._response(request(config_path, "gmail.authorize"))

    assert response["data"] == {"baseline_initialized": True, "connected": True}
    account = runtime.store.active_mail_account()
    assert account is not None
    assert account.mailbox_identity_key == REPLACEMENT_GMAIL_IDENTITY
    assert runtime.store.state()[0] == "replacement-history-id"


def test_gmail_authorize_replaces_a_token_rejected_by_gmail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.store.set_state("old-history-id", datetime(2026, 7, 18, tzinfo=UTC))
    runtime.store.update_mail_account_identity(
        "gmail",
        "gmail-default",
        display_name="Gmail",
        address="owner@example.com",
    )
    runtime.config.gmail_token_file.write_text("rejected token", encoding="utf-8")
    authorization_calls: list[bool] = []

    class RejectedGmail:
        def profile(self) -> GmailProfile:
            raise GmailAuthorizationRejected("rejected")

    class ReauthorizedGmail:
        def mailbox_identity_key(self) -> str:
            return REPLACEMENT_GMAIL_IDENTITY

        def profile(self) -> GmailProfile:
            return GmailProfile("owner@example.com", "new-history-id")

    def authorize_with_status(credentials_file, token_file, *, force_reauthorize: bool = False):
        authorization_calls.append(force_reauthorize)
        token_file.write_text("replacement token", encoding="utf-8")
        return ReauthorizedGmail(), True

    monkeypatch.setattr(
        engine_api.GmailGateway,
        "from_token",
        lambda credentials_file, token_file: RejectedGmail(),
    )
    monkeypatch.setattr(
        engine_api.GmailGateway,
        "authorize_with_status",
        authorize_with_status,
    )

    response = engine_api._response(request(config_path, "gmail.authorize"))

    assert response["data"] == {"baseline_initialized": True, "connected": True}
    assert authorization_calls == [True]
    assert load_runtime(config_path).store.state()[0] == "new-history-id"


def test_gmail_authorize_preserves_token_on_transient_profile_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.store.update_mail_account_identity(
        "gmail",
        "gmail-default",
        display_name="Gmail",
        address="owner@example.com",
    )
    runtime.config.gmail_token_file.write_text("valid token", encoding="utf-8")

    class UnavailableGmail:
        def profile(self) -> GmailProfile:
            raise GmailError("Gmail profile request failed (HTTP 429)")

    authorization_calls = 0

    def authorize_with_status(*args, **kwargs):
        nonlocal authorization_calls
        authorization_calls += 1
        raise AssertionError("transient Gmail errors must not start browser authorization")

    monkeypatch.setattr(
        engine_api.GmailGateway,
        "from_token",
        lambda credentials_file, token_file: UnavailableGmail(),
    )
    monkeypatch.setattr(
        engine_api.GmailGateway,
        "authorize_with_status",
        authorize_with_status,
    )

    response = engine_api._response(request(config_path, "gmail.authorize"))

    assert response["error"] == {
        "code": "gmail_error",
        "message": "Gmail operation failed; see stderr for details",
    }
    assert authorization_calls == 0
    assert runtime.config.gmail_token_file.read_text(encoding="utf-8") == "valid token"


def test_gmail_authorize_initializes_missing_baseline_with_existing_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    token_file = tmp_path / "token.json"
    token_file.write_text("existing token", encoding="utf-8")

    class ExistingGmail:
        def mailbox_identity_key(self) -> str:
            return GMAIL_IDENTITY

        def profile(self) -> GmailProfile:
            return GmailProfile("owner@example.com", "current-history-id")

    monkeypatch.setattr(
        engine_api.GmailGateway,
        "from_token",
        lambda credentials_file, configured_token_file: ExistingGmail(),
    )

    response = engine_api._response(request(config_path, "gmail.authorize"))

    assert response["data"] == {"baseline_initialized": True, "connected": True}
    assert load_runtime(config_path).store.state()[0] == "current-history-id"


def test_gmail_authorize_holds_operation_lock_through_baseline_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    lock_held = False

    @contextmanager
    def account_operation_lock(path: Path, busy_message: str):
        nonlocal lock_held
        assert path.name == "watcher.sqlite3.check.lock"
        assert busy_message == "Another mailbox operation is already running"
        lock_held = True
        try:
            yield
        finally:
            lock_held = False

    class AuthorizedGmail:
        def mailbox_identity_key(self) -> str:
            assert lock_held
            return GMAIL_IDENTITY

        def profile(self) -> GmailProfile:
            assert lock_held
            return GmailProfile("owner@example.com", "serialized-history-id")

    original_state = runtime.store.state
    original_set_state = runtime.store.set_state

    def state(*, provider: str, account_id: str):
        assert lock_held
        assert (provider, account_id) == (DEFAULT_MAIL_PROVIDER, DEFAULT_MAIL_ACCOUNT_ID)
        return original_state(provider=provider, account_id=account_id)

    def set_state(history_id: str, *, provider: str, account_id: str):
        assert lock_held
        assert (provider, account_id) == (DEFAULT_MAIL_PROVIDER, DEFAULT_MAIL_ACCOUNT_ID)
        original_set_state(history_id, provider=provider, account_id=account_id)

    monkeypatch.setattr(engine_api, "operation_lock_supported", lambda path: True)
    monkeypatch.setattr(engine_api, "operation_lock", account_operation_lock)
    monkeypatch.setattr(engine_api, "_runtime", lambda request: runtime)
    monkeypatch.setattr(runtime.store, "state", state)
    monkeypatch.setattr(runtime.store, "set_state", set_state)

    def authorize_with_status(
        credentials_file: Path,
        token_file: Path,
        *,
        force_reauthorize: bool,
    ) -> tuple[AuthorizedGmail, bool]:
        assert lock_held
        token_file.write_text("readonly token", encoding="utf-8")
        return AuthorizedGmail(), force_reauthorize

    monkeypatch.setattr(engine_api.GmailGateway, "authorize_with_status", authorize_with_status)

    response = engine_api._response(request(config_path, "gmail.authorize"))

    assert response["data"] == {"baseline_initialized": True, "connected": True}
    assert lock_held is False
    assert original_state()[0] == "serialized-history-id"


def test_mail_account_list_adopts_existing_gmail_token_without_exposing_paths(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    (tmp_path / "credentials.json").write_text("{}", encoding="utf-8")
    (tmp_path / "token.json").write_text("private readonly token", encoding="utf-8")

    response = engine_api._response(request(config_path, "mail.accounts.list"))

    assert response["ok"] is True
    assert response["data"] == {
        "accounts": [
            {
                "account_id": "gmail-default",
                "active": True,
                "address": None,
                "connected": True,
                "display_name": "Gmail",
                "last_check": None,
                "provider": "gmail",
            }
        ],
        "providers": [
            {
                "connection_available": True,
                "connection_method": "browser_oauth",
                "display_name": "Gmail",
                "multiple_accounts": True,
                "provider": "gmail",
            },
            {
                "connection_available": True,
                "connection_method": "server_credentials",
                "display_name": "Other mail server",
                "multiple_accounts": True,
                "provider": "imap",
            },
            {
                "connection_available": False,
                "connection_method": "browser_oauth",
                "display_name": "Microsoft 365",
                "multiple_accounts": True,
                "provider": "microsoft365",
            },
        ],
    }
    encoded = json.dumps(response)
    assert "private readonly token" not in encoded
    assert "token.json" not in encoded
    assert str(tmp_path) not in encoded


def test_mail_account_connect_installs_private_token_and_initializes_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)

    class AuthorizedGmail:
        def mailbox_identity_key(self) -> str:
            return GMAIL_IDENTITY

        def profile(self) -> GmailProfile:
            return GmailProfile("owner@example.com", "current-cursor")

    def authorize_with_status(
        credentials_file: Path,
        token_file: Path,
        *,
        force_reauthorize: bool,
    ) -> tuple[AuthorizedGmail, bool]:
        assert force_reauthorize is True
        token_file.write_text("private readonly token", encoding="utf-8")
        return AuthorizedGmail(), True

    monkeypatch.setattr(engine_api.GmailGateway, "authorize_with_status", authorize_with_status)

    response = engine_api._response(
        request(config_path, "mail.accounts.connect", {"provider": "GMAIL"})
    )

    assert response["ok"] is True
    assert response["data"]["baseline_initialized"] is True
    account_response = response["data"]["account"]
    assert isinstance(account_response["last_check"], str)
    assert {**account_response, "last_check": "<checked>"} == {
        "account_id": "gmail-default",
        "active": True,
        "address": "owner@example.com",
        "connected": True,
        "display_name": "Gmail",
        "last_check": "<checked>",
        "provider": "gmail",
    }
    runtime = load_runtime(config_path)
    assert runtime.config.gmail_token_file.read_text(encoding="utf-8") == "private readonly token"
    assert runtime.config.gmail_token_file.stat().st_mode & 0o777 == 0o600
    account = runtime.store.active_mail_account()
    assert account is not None
    assert account.address == "owner@example.com"
    assert account.mailbox_identity_key == GMAIL_IDENTITY
    assert runtime.store.state()[0] == "current-cursor"
    assert "private readonly token" not in json.dumps(response)


def test_mail_account_list_advertises_valid_microsoft_public_client_without_exposing_it(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    public_client = tmp_path / "microsoft-oauth-client.json"
    public_client.write_text(
        json.dumps(
            {
                "client_id": "11111111-2222-4333-8444-555555555555",
                "tenant": "organizations",
            }
        ),
        encoding="utf-8",
    )

    response = engine_api._response(request(config_path, "mail.accounts.list"))

    microsoft = next(
        item for item in response["data"]["providers"] if item["provider"] == "microsoft365"
    )
    assert microsoft == {
        "connection_available": True,
        "connection_method": "browser_oauth",
        "display_name": "Microsoft 365",
        "multiple_accounts": True,
        "provider": "microsoft365",
    }
    encoded = json.dumps(response)
    assert "11111111-2222-4333-8444-555555555555" not in encoded
    assert "microsoft-oauth-client.json" not in encoded
    assert str(tmp_path) not in encoded


def test_mail_account_connect_authorizes_microsoft_and_initializes_delta_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)

    class AuthorizedMicrosoft:
        def profile(self) -> Microsoft365Profile:
            return Microsoft365Profile("owner@example.com")

        def initial_cursor(self) -> str:
            return "https://graph.microsoft.com/private-delta-cursor"

    def authorize_with_status(
        credentials_file: Path,
        token_file: Path,
        *,
        force_reauthorize: bool,
    ) -> tuple[AuthorizedMicrosoft, bool]:
        assert force_reauthorize is True
        token_file.write_text("private Microsoft cache", encoding="utf-8")
        return AuthorizedMicrosoft(), True

    monkeypatch.setattr(
        engine_api.Microsoft365Gateway,
        "authorize_with_status",
        authorize_with_status,
    )

    response = engine_api._response(
        request(config_path, "mail.accounts.connect", {"provider": "microsoft365"})
    )

    assert response["ok"] is True
    assert response["data"]["baseline_initialized"] is True
    account_response = response["data"]["account"]
    assert account_response["provider"] == "microsoft365"
    assert account_response["address"] == "owner@example.com"
    assert account_response["connected"] is True
    assert account_response["active"] is True
    runtime = load_runtime(config_path)
    account = runtime.store.active_mail_account()
    assert account is not None
    assert account.provider == "microsoft365"
    assert account.account_id.startswith("microsoft365-")
    token_file = mail_account_token_file(runtime.config, account)
    assert token_file.read_text(encoding="utf-8") == "private Microsoft cache"
    assert token_file.stat().st_mode & 0o777 == 0o600
    assert runtime.store.state(provider=account.provider, account_id=account.account_id)[0] == (
        "https://graph.microsoft.com/private-delta-cursor"
    )
    encoded = json.dumps(response)
    assert "private Microsoft cache" not in encoded
    assert "private-delta-cursor" not in encoded


def test_calendar_read_connect_is_entitled_and_uses_separate_private_cache(
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
    mail_token = mail_account_token_file(runtime.config, account)
    mail_token.parent.mkdir(parents=True)
    mail_token.write_text("mail-read-cache", encoding="utf-8")
    monkeypatch.setattr(engine_api, "_calendar_entitlement_active", lambda: True)
    monkeypatch.setattr(
        engine_api, "microsoft_mailbox_principal", lambda *args: microsoft_principal()
    )

    class AuthorizedCalendar:
        principal = microsoft_principal()

    def authorize(credentials_file: Path, staged_token: Path):
        assert runtime.store.calendar_grant(account.account_id).state == "consent_pending"
        staged_token.write_text("calendar-read-cache", encoding="utf-8")
        return AuthorizedCalendar(), True

    monkeypatch.setattr(
        engine_api.MicrosoftCalendarReadAuthorization,
        "authorize_with_status",
        authorize,
    )
    monkeypatch.setattr(
        engine_api.MicrosoftCalendarReadAuthorization,
        "from_token",
        lambda *args: AuthorizedCalendar(),
    )
    grant_states: list[str] = []
    set_calendar_grant = engine_api.Store.set_calendar_grant

    def record_calendar_grant(
        store: engine_api.Store,
        account_id: str,
        profile: str,
        state: str,
        **identity,
    ):
        if account_id == account.account_id:
            grant_states.append(state)
        return set_calendar_grant(store, account_id, profile, state, **identity)

    monkeypatch.setattr(engine_api.Store, "set_calendar_grant", record_calendar_grant)
    payload = {"provider": account.provider, "account_id": account.account_id}

    response = engine_api._response(request(config_path, "calendar.read.connect", payload))

    assert response["ok"] is True
    assert grant_states == ["consent_pending", "ready"]
    assert response["data"] == {
        "account_id": account.account_id,
        "available": True,
        "entitlement_active": True,
        "profile": "read",
        "scope": "Calendars.Read",
        "state": "ready",
    }
    calendar_token = microsoft_calendar_read_token_file(runtime.config, account)
    assert calendar_token.read_text(encoding="utf-8") == "calendar-read-cache"
    assert calendar_token.stat().st_mode & 0o777 == 0o600
    assert mail_token.read_text(encoding="utf-8") == "mail-read-cache"
    grant = runtime.store.calendar_grant(account.account_id)
    assert grant is not None
    assert grant.principal_key == microsoft_principal().key
    encoded = json.dumps(response)
    assert "calendar-read-cache" not in encoded
    assert "home_account_id" not in encoded
    assert str(tmp_path) not in encoded

    monkeypatch.setattr(
        engine_api.MicrosoftCalendarReadAuthorization,
        "authorize_with_status",
        lambda *args: pytest.fail("Authorization started without a staging directory"),
    )

    def staging_unavailable(*args, **kwargs):
        raise OSError("calendar staging directory is unavailable")

    monkeypatch.setattr(engine_api.tempfile, "TemporaryDirectory", staging_unavailable)
    staging_failure = engine_api._response(request(config_path, "calendar.read.connect", payload))

    assert staging_failure["error"]["code"] == "calendar_error"
    assert runtime.store.calendar_grant(account.account_id) == grant
    assert calendar_token.read_text(encoding="utf-8") == "calendar-read-cache"


@pytest.mark.parametrize(
    ("previous_state", "expected_ok"),
    [("ready", False), ("revoked", True)],
)
def test_calendar_connect_rebinds_revoked_but_not_ready_grant_to_new_principal(
    previous_state: str,
    expected_ok: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    account = runtime.store.register_mail_account(
        "microsoft365",
        f"microsoft365-{'6' * 32}",
        display_name="Microsoft 365",
        address="owner@example.com",
        active=True,
    )
    original = microsoft_principal()
    replacement = microsoft_principal(object_id="replacement-object")
    runtime.store.set_calendar_grant(
        account.account_id,
        "read",
        previous_state,
        principal_key=original.key,
        home_account_id=original.home_account_id,
        tenant_id=original.tenant_id,
        object_id=original.object_id,
        email_address=original.email_address,
    )
    mail_token = mail_account_token_file(runtime.config, account)
    mail_token.parent.mkdir(parents=True)
    mail_token.write_text("replacement-mailbox-cache", encoding="utf-8")
    calendar_token = microsoft_calendar_read_token_file(runtime.config, account)
    calendar_token.write_text("original-calendar-cache", encoding="utf-8")
    monkeypatch.setattr(engine_api, "_calendar_entitlement_active", lambda: True)
    monkeypatch.setattr(
        engine_api,
        "microsoft_mailbox_principal",
        lambda *args: replacement,
    )

    class ReplacementCalendar:
        principal = replacement

    def authorize(credentials_file: Path, staged_token: Path):
        staged_token.write_text("replacement-calendar-cache", encoding="utf-8")
        return ReplacementCalendar(), True

    monkeypatch.setattr(
        engine_api.MicrosoftCalendarReadAuthorization,
        "authorize_with_status",
        authorize,
    )
    response = engine_api._response(
        request(
            config_path,
            "calendar.read.connect",
            {"provider": account.provider, "account_id": account.account_id},
        )
    )

    assert response["ok"] is expected_ok
    grant = runtime.store.calendar_grant(account.account_id)
    assert grant is not None
    if expected_ok:
        assert response["data"]["state"] == "ready"
        assert grant.state == "ready"
        assert grant.principal_key == replacement.key
        assert calendar_token.read_text(encoding="utf-8") == "replacement-calendar-cache"
    else:
        assert response["error"]["code"] == "calendar_principal_mismatch"
        assert grant.state == "ready"
        assert grant.principal_key == original.key
        assert calendar_token.read_text(encoding="utf-8") == "original-calendar-cache"


@pytest.mark.parametrize("legacy_identity_available", [True, False])
def test_calendar_connect_handles_grantless_legacy_automation_run(
    legacy_identity_available: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    account = runtime.store.register_mail_account(
        "microsoft365",
        f"microsoft365-{'7' * 32}",
        display_name="Microsoft 365",
        address="owner@example.com",
        active=True,
    )
    identity = microsoft_principal()
    legacy_key = hashlib.sha256(
        "\0".join((identity.home_account_id, identity.tenant_id, identity.object_id)).encode()
    ).hexdigest()
    selected_principal = MicrosoftPrincipal(
        home_account_id=identity.home_account_id,
        tenant_id=identity.tenant_id,
        object_id=identity.object_id,
        email_address=identity.email_address,
        legacy_principal_key=legacy_key if legacy_identity_available else None,
    )
    runtime.store.set_calendar_grant(
        account.account_id,
        "read",
        "ready",
        principal_key=legacy_key,
        home_account_id=selected_principal.home_account_id,
        tenant_id=selected_principal.tenant_id,
        object_id=selected_principal.object_id,
        email_address=selected_principal.email_address,
    )
    message_id = scoped_message_id("microsoft365", account.account_id, "schedule-upgrade")
    _add_test_message(
        runtime.store,
        message_id=message_id,
        provider="microsoft365",
        account_id=account.account_id,
        provider_message_id="schedule-upgrade",
        thread_id=None,
        sender="sender@example.com",
        sender_name="Sender",
        subject="Can we meet?",
        received_at="2026-09-07T12:00:00+00:00",
    )
    _mark_test_analyzed(
        runtime.store,
        message_id,
        {
            "category": "scheduling",
            "priority": "normal",
            "summary": "A meeting was requested.",
            "action_required": True,
            "suggested_action": "Review the requested meeting.",
            "deadline_text": None,
            "deadline_iso": None,
            "confidence": 0.9,
        },
        scheduling_automation_principal_key=legacy_key,
    )
    run = runtime.store.automation_run_for_message(message_id)
    assert run is not None
    runtime.store.disconnect_calendar_grant(account.account_id, "read")
    disconnected = runtime.store.calendar_grant(account.account_id, "read")
    assert disconnected is not None
    assert disconnected.principal_key is None

    mail_token = mail_account_token_file(runtime.config, account)
    mail_token.parent.mkdir(parents=True)
    mail_token.write_text("mail-read-cache", encoding="utf-8")
    monkeypatch.setattr(engine_api, "_calendar_entitlement_active", lambda: True)
    monkeypatch.setattr(
        engine_api,
        "microsoft_mailbox_principal",
        lambda *args: selected_principal,
    )

    class SelectedCalendar:
        principal = selected_principal

    def authorize(credentials_file: Path, staged_token: Path):
        staged_token.write_text("calendar-read-cache", encoding="utf-8")
        return SelectedCalendar(), True

    monkeypatch.setattr(
        engine_api.MicrosoftCalendarReadAuthorization,
        "authorize_with_status",
        authorize,
    )

    response = engine_api._response(
        request(
            config_path,
            "calendar.read.connect",
            {"provider": account.provider, "account_id": account.account_id},
        )
    )

    migrated = runtime.store.automation_run(run.run_id)
    assert migrated is not None
    event_keys = {
        event.calendar_principal_key for event in runtime.store.automation_events(run.run_id)
    }
    if legacy_identity_available:
        assert response["ok"] is True
        assert migrated.calendar_principal_key == selected_principal.key
        assert event_keys == {selected_principal.key}
    else:
        assert response["error"]["code"] == "calendar_principal_recovery_required"
        assert migrated.calendar_principal_key == legacy_key
        assert event_keys == {legacy_key}
        restored = runtime.store.calendar_grant(account.account_id, "read")
        assert restored is not None
        assert (restored.state, restored.principal_key) == ("not_requested", None)
        assert not microsoft_calendar_read_token_file(runtime.config, account).exists()


@pytest.mark.parametrize("existing_ready", [False, True])
def test_calendar_read_connect_recovers_exit_after_token_install(
    existing_ready: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    account = runtime.store.register_mail_account(
        "microsoft365",
        f"microsoft365-{'d' * 32}",
        display_name="Microsoft 365",
        address="owner@example.com",
        active=True,
    )
    mail_token = mail_account_token_file(runtime.config, account)
    mail_token.parent.mkdir(parents=True)
    mail_token.write_text("mail-read-cache", encoding="utf-8")
    calendar_token = microsoft_calendar_read_token_file(runtime.config, account)
    selected_principal = microsoft_principal()
    if existing_ready:
        runtime.store.set_calendar_grant(
            account.account_id,
            "read",
            "ready",
            principal_key=selected_principal.key,
            home_account_id=selected_principal.home_account_id,
            tenant_id=selected_principal.tenant_id,
            object_id=selected_principal.object_id,
            email_address=selected_principal.email_address,
        )
        calendar_token.write_text("old-calendar-cache", encoding="utf-8")
    monkeypatch.setattr(engine_api, "_calendar_entitlement_active", lambda: True)
    monkeypatch.setattr(engine_api, "microsoft_mailbox_principal", lambda *args: selected_principal)

    class AuthorizedCalendar:
        principal = selected_principal

    def authorize(credentials_file: Path, staged_token: Path):
        staged_token.write_text("installed-calendar-cache", encoding="utf-8")
        return AuthorizedCalendar(), True

    real_install = engine_api._install_private_token

    def install_then_exit(source: Path, destination: Path) -> None:
        real_install(source, destination)
        raise SystemExit

    monkeypatch.setattr(
        engine_api.MicrosoftCalendarReadAuthorization,
        "authorize_with_status",
        authorize,
    )
    monkeypatch.setattr(engine_api, "_install_private_token", install_then_exit)
    payload = {"provider": account.provider, "account_id": account.account_id}

    with pytest.raises(SystemExit):
        engine_api._response(request(config_path, "calendar.read.connect", payload))

    installed_grant = runtime.store.calendar_grant(account.account_id)
    assert installed_grant is not None
    assert installed_grant.state == "ready"
    assert installed_grant.principal_key == selected_principal.key
    assert calendar_token.read_text(encoding="utf-8") == "installed-calendar-cache"

    monkeypatch.setattr(
        engine_api.MicrosoftCalendarReadAuthorization,
        "from_token",
        lambda *args: AuthorizedCalendar(),
    )
    status = engine_api._response(request(config_path, "calendar.read.status", payload))

    assert status["data"]["state"] == "ready"
    assert status["data"]["available"] is True


@pytest.mark.parametrize("existing_ready", [False, True])
def test_calendar_read_connect_restores_grant_when_ready_transition_fails(
    existing_ready: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    account = runtime.store.register_mail_account(
        "microsoft365",
        f"microsoft365-{'e' * 32}",
        display_name="Microsoft 365",
        address="owner@example.com",
        active=True,
    )
    principal = microsoft_principal()
    mail_token = mail_account_token_file(runtime.config, account)
    mail_token.parent.mkdir(parents=True)
    mail_token.write_text("mail-read-cache", encoding="utf-8")
    calendar_token = microsoft_calendar_read_token_file(runtime.config, account)
    if existing_ready:
        runtime.store.set_calendar_grant(
            account.account_id,
            "read",
            "ready",
            principal_key=principal.key,
            home_account_id=principal.home_account_id,
            tenant_id=principal.tenant_id,
            object_id=principal.object_id,
            email_address=principal.email_address,
        )
        calendar_token.write_text("old-calendar-cache", encoding="utf-8")
    monkeypatch.setattr(engine_api, "_calendar_entitlement_active", lambda: True)
    monkeypatch.setattr(engine_api, "microsoft_mailbox_principal", lambda *args: principal)

    class AuthorizedCalendar:
        principal = microsoft_principal()

    staged_paths: list[Path] = []

    def authorize(credentials_file: Path, staged_token: Path):
        staged_paths.append(staged_token)
        staged_token.write_text("new-calendar-cache", encoding="utf-8")
        return AuthorizedCalendar(), True

    monkeypatch.setattr(
        engine_api.MicrosoftCalendarReadAuthorization,
        "authorize_with_status",
        authorize,
    )
    real_set = engine_api.Store.set_calendar_grant
    failed = False

    def fail_first_ready(self, account_id: str, profile: str, state: str, **identity):
        nonlocal failed
        if state == "ready" and not failed:
            failed = True
            raise sqlite3.OperationalError("database is temporarily busy")
        return real_set(self, account_id, profile, state, **identity)

    monkeypatch.setattr(engine_api.Store, "set_calendar_grant", fail_first_ready)
    response = engine_api._response(
        request(
            config_path,
            "calendar.read.connect",
            {"provider": account.provider, "account_id": account.account_id},
        )
    )

    assert response["error"]["code"] == "calendar_error"
    restored = runtime.store.calendar_grant(account.account_id)
    assert restored is not None
    assert restored.state == ("ready" if existing_ready else "not_requested")
    assert restored.principal_key == (principal.key if existing_ready else None)
    assert calendar_token.exists() is existing_ready
    if existing_ready:
        assert calendar_token.read_text(encoding="utf-8") == "old-calendar-cache"
    assert staged_paths and not staged_paths[0].exists()


@pytest.mark.parametrize("existing_ready", [False, True])
def test_calendar_read_connect_restores_grant_when_rejection_transition_fails(
    existing_ready: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    account = runtime.store.register_mail_account(
        "microsoft365",
        f"microsoft365-{'7' * 32}",
        display_name="Microsoft 365",
        address="owner@example.com",
        active=True,
    )
    principal = microsoft_principal()
    mail_token = mail_account_token_file(runtime.config, account)
    mail_token.parent.mkdir(parents=True)
    mail_token.write_text("mail-read-cache", encoding="utf-8")
    calendar_token = microsoft_calendar_read_token_file(runtime.config, account)
    if existing_ready:
        runtime.store.set_calendar_grant(
            account.account_id,
            "read",
            "ready",
            principal_key=principal.key,
            home_account_id=principal.home_account_id,
            tenant_id=principal.tenant_id,
            object_id=principal.object_id,
            email_address=principal.email_address,
        )
        calendar_token.write_text("old-calendar-cache", encoding="utf-8")
    monkeypatch.setattr(engine_api, "_calendar_entitlement_active", lambda: True)
    monkeypatch.setattr(engine_api, "microsoft_mailbox_principal", lambda *args: principal)
    monkeypatch.setattr(
        engine_api.MicrosoftCalendarReadAuthorization,
        "authorize_with_status",
        lambda *args: (_ for _ in ()).throw(MicrosoftAuthorizationRejected("declined")),
    )
    real_set = engine_api.Store.set_calendar_grant
    failed = False

    def fail_rejected(self, account_id: str, profile: str, state: str, **identity):
        nonlocal failed
        if state == "rejected" and not failed:
            failed = True
            raise sqlite3.OperationalError("database is temporarily busy")
        return real_set(self, account_id, profile, state, **identity)

    monkeypatch.setattr(engine_api.Store, "set_calendar_grant", fail_rejected)
    response = engine_api._response(
        request(
            config_path,
            "calendar.read.connect",
            {"provider": account.provider, "account_id": account.account_id},
        )
    )

    assert response["error"]["code"] == "calendar_error"
    restored = runtime.store.calendar_grant(account.account_id)
    assert restored is not None
    assert restored.state == ("ready" if existing_ready else "not_requested")
    assert restored.principal_key == (principal.key if existing_ready else None)
    assert calendar_token.exists() is existing_ready
    if existing_ready:
        assert calendar_token.read_text(encoding="utf-8") == "old-calendar-cache"


def test_calendar_read_status_cannot_race_a_calendar_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    account = runtime.store.register_mail_account(
        "microsoft365",
        f"microsoft365-{'8' * 32}",
        display_name="Microsoft 365",
        address="owner@example.com",
        active=True,
    )
    principal = microsoft_principal()
    runtime.store.set_calendar_grant(
        account.account_id,
        "read",
        "ready",
        principal_key=principal.key,
        home_account_id=principal.home_account_id,
        tenant_id=principal.tenant_id,
        object_id=principal.object_id,
        email_address=principal.email_address,
    )
    token_file = microsoft_calendar_read_token_file(runtime.config, account)
    token_file.parent.mkdir(parents=True)
    token_file.write_text("calendar-read-cache", encoding="utf-8")
    monkeypatch.setattr(engine_api, "_calendar_entitlement_active", lambda: True)
    monkeypatch.setattr(
        engine_api.MicrosoftCalendarReadAuthorization,
        "from_token",
        lambda *args: pytest.fail("Status validation crossed the held operation lock"),
    )
    payload = {"provider": account.provider, "account_id": account.account_id}
    lock_path = engine_api._production_check_lock_path(runtime.config)
    monkeypatch.setattr(engine_api, "operation_lock_supported", lambda path: False)

    with engine_api.operation_lock(lock_path, "test lock"):
        response = engine_api._response(request(config_path, "calendar.read.status", payload))

    assert response["error"]["code"] == "runtime_error"
    assert "Another mailbox operation is already running" in response["error"]["message"]

    monkeypatch.setattr(engine_api, "operation_lock_uses_soft_fallback", lambda path: True)
    monkeypatch.setattr(
        engine_api.MicrosoftCalendarReadAuthorization,
        "from_token",
        lambda *args: SimpleNamespace(principal=principal),
    )
    monkeypatch.setattr(
        engine_api,
        "microsoft_mailbox_principal",
        lambda *args: principal,
    )

    fallback = engine_api._response(request(config_path, "calendar.read.status", payload))

    assert fallback["data"]["state"] == "ready"
    assert fallback["data"]["available"] is True


def test_calendar_read_connect_restores_grant_if_entitlement_expires_during_consent(
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
    principal = microsoft_principal()
    mail_token = mail_account_token_file(runtime.config, account)
    mail_token.parent.mkdir(parents=True)
    mail_token.write_text("mail-read-cache", encoding="utf-8")
    entitlement = iter((True, False))
    monkeypatch.setattr(
        engine_api,
        "_calendar_entitlement_active",
        lambda: next(entitlement),
    )
    monkeypatch.setattr(
        engine_api,
        "microsoft_mailbox_principal",
        lambda *args: principal,
    )

    def authorize(credentials_file: Path, staged_token: Path):
        staged_token.write_text("new-calendar-cache", encoding="utf-8")
        return SimpleNamespace(principal=principal), True

    monkeypatch.setattr(
        engine_api.MicrosoftCalendarReadAuthorization,
        "authorize_with_status",
        authorize,
    )
    monkeypatch.setattr(
        engine_api,
        "_install_private_token",
        lambda *args: pytest.fail("Expired consent installed a calendar token"),
    )

    response = engine_api._response(
        request(
            config_path,
            "calendar.read.connect",
            {"provider": account.provider, "account_id": account.account_id},
        )
    )

    assert response["error"]["code"] == "calendar_entitlement_required"
    assert runtime.store.calendar_grant(account.account_id).state == "not_requested"
    assert not microsoft_calendar_read_token_file(runtime.config, account).exists()


def test_calendar_read_entitlement_and_principal_mismatch_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    account = runtime.store.register_mail_account(
        "microsoft365",
        f"microsoft365-{'b' * 32}",
        display_name="Microsoft 365",
        address="owner@example.com",
        active=True,
    )
    payload = {"provider": account.provider, "account_id": account.account_id}
    monkeypatch.setattr(engine_api, "_calendar_entitlement_active", lambda: False)
    monkeypatch.setattr(
        engine_api.MicrosoftCalendarReadAuthorization,
        "authorize_with_status",
        lambda *args: pytest.fail("Locked calendar setup reached Microsoft authorization"),
    )

    locked = engine_api._response(request(config_path, "calendar.read.status", payload))
    rejected = engine_api._response(request(config_path, "calendar.read.connect", payload))

    assert locked["data"]["state"] == "not_requested"
    assert locked["data"]["available"] is False
    assert rejected["error"]["code"] == "calendar_entitlement_required"

    original = microsoft_principal()
    runtime.store.set_calendar_grant(
        account.account_id,
        "read",
        "ready",
        principal_key=original.key,
        home_account_id=original.home_account_id,
        tenant_id=original.tenant_id,
        object_id=original.object_id,
        email_address=original.email_address,
    )
    calendar_token = microsoft_calendar_read_token_file(runtime.config, account)
    calendar_token.parent.mkdir(parents=True, exist_ok=True)
    calendar_token.write_text("preserved-calendar-cache", encoding="utf-8")
    monkeypatch.setattr(engine_api, "_calendar_entitlement_active", lambda: True)

    class CurrentCalendar:
        principal = original

    def rejected_calendar_cache(*args):
        raise engine_api.MicrosoftAuthorizationRejected("Calendar cache was revoked")

    monkeypatch.setattr(
        engine_api.MicrosoftCalendarReadAuthorization,
        "from_token",
        rejected_calendar_cache,
    )
    revoked_cache = engine_api._response(request(config_path, "calendar.read.status", payload))

    assert revoked_cache["data"]["state"] == "revoked"
    assert revoked_cache["data"]["available"] is False
    revoked_grant = runtime.store.calendar_grant(account.account_id)
    assert revoked_grant.state == "revoked"
    assert revoked_grant.principal_key == original.key

    runtime.store.set_calendar_grant(
        account.account_id,
        "read",
        "ready",
        principal_key=original.key,
        home_account_id=original.home_account_id,
        tenant_id=original.tenant_id,
        object_id=original.object_id,
        email_address=original.email_address,
    )

    monkeypatch.setattr(
        engine_api.MicrosoftCalendarReadAuthorization,
        "from_token",
        lambda *args: CurrentCalendar(),
    )

    def rejected_mailbox_cache(*args):
        raise engine_api.MicrosoftAuthorizationRejected("Mailbox cache was revoked")

    monkeypatch.setattr(engine_api, "microsoft_mailbox_principal", rejected_mailbox_cache)
    unavailable_mailbox = engine_api._response(
        request(config_path, "calendar.read.status", payload)
    )

    assert unavailable_mailbox["data"]["state"] == "ready"
    assert unavailable_mailbox["data"]["available"] is False

    monkeypatch.setattr(
        engine_api,
        "microsoft_mailbox_principal",
        lambda *args: microsoft_principal(object_id="replacement-object"),
    )

    stale_binding = engine_api._response(request(config_path, "calendar.read.status", payload))

    assert stale_binding["data"]["state"] == "revoked"
    assert stale_binding["data"]["available"] is False
    assert runtime.store.calendar_grant(account.account_id).state == "revoked"

    monkeypatch.setattr(engine_api, "microsoft_mailbox_principal", lambda *args: original)

    def consent_pending(*args):
        raise engine_api.MicrosoftCalendarConsentPending("Administrator approval is pending")

    monkeypatch.setattr(
        engine_api.MicrosoftCalendarReadAuthorization,
        "authorize_with_status",
        consent_pending,
    )
    pending = engine_api._response(request(config_path, "calendar.read.connect", payload))

    assert pending["data"]["state"] == "consent_pending"
    assert pending["data"]["available"] is False
    assert runtime.store.calendar_grant(account.account_id).state == "consent_pending"
    assert calendar_token.read_text(encoding="utf-8") == "preserved-calendar-cache"

    runtime.store.set_calendar_grant(
        account.account_id,
        "read",
        "ready",
        principal_key=original.key,
        home_account_id=original.home_account_id,
        tenant_id=original.tenant_id,
        object_id=original.object_id,
        email_address=original.email_address,
    )

    def transient_calendar_error(*args):
        raise Microsoft365Error("Microsoft authorization service is temporarily unavailable")

    monkeypatch.setattr(
        engine_api.MicrosoftCalendarReadAuthorization,
        "authorize_with_status",
        transient_calendar_error,
    )
    transient = engine_api._response(request(config_path, "calendar.read.connect", payload))

    assert transient["error"]["code"] == "calendar_error"
    assert runtime.store.calendar_grant(account.account_id).state == "ready"
    assert runtime.store.calendar_grant(account.account_id).principal_key == original.key
    assert calendar_token.read_text(encoding="utf-8") == "preserved-calendar-cache"

    class AuthorizedCalendar:
        principal = original

    def authorize_current(credentials_file: Path, staged_token: Path):
        staged_token.write_text("replacement-calendar-cache", encoding="utf-8")
        return AuthorizedCalendar(), True

    def fail_token_install(source: Path, destination: Path) -> None:
        raise OSError("calendar cache destination is unavailable")

    monkeypatch.setattr(
        engine_api.MicrosoftCalendarReadAuthorization,
        "authorize_with_status",
        authorize_current,
    )
    monkeypatch.setattr(engine_api, "_install_private_token", fail_token_install)
    install_failure = engine_api._response(request(config_path, "calendar.read.connect", payload))

    assert install_failure["error"]["code"] == "calendar_error"
    assert runtime.store.calendar_grant(account.account_id).state == "ready"
    assert runtime.store.calendar_grant(account.account_id).principal_key == original.key
    assert calendar_token.read_text(encoding="utf-8") == "preserved-calendar-cache"

    runtime.store.disconnect_calendar_read(account.account_id)
    first_attempt_failure = engine_api._response(
        request(config_path, "calendar.read.connect", payload)
    )

    assert first_attempt_failure["error"]["code"] == "calendar_error"
    assert runtime.store.calendar_grant(account.account_id).state == "not_requested"

    runtime.store.set_calendar_grant(
        account.account_id,
        "read",
        "ready",
        principal_key=original.key,
        home_account_id=original.home_account_id,
        tenant_id=original.tenant_id,
        object_id=original.object_id,
        email_address=original.email_address,
    )

    class WrongCalendar:
        principal = microsoft_principal(object_id="object-2")

    def authorize_wrong(credentials_file: Path, staged_token: Path):
        staged_token.write_text("wrong-calendar-cache", encoding="utf-8")
        return WrongCalendar(), True

    monkeypatch.setattr(
        engine_api.MicrosoftCalendarReadAuthorization,
        "authorize_with_status",
        authorize_wrong,
    )

    mismatch = engine_api._response(request(config_path, "calendar.read.connect", payload))

    assert mismatch["error"]["code"] == "account_identity_mismatch"
    assert calendar_token.read_text(encoding="utf-8") == "preserved-calendar-cache"
    assert runtime.store.calendar_grant(account.account_id).principal_key == original.key
    assert runtime.store.calendar_grant(account.account_id).state == "ready"


def test_calendar_read_disconnect_preserves_mailbox_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    account = runtime.store.register_mail_account(
        "microsoft365",
        f"microsoft365-{'c' * 32}",
        display_name="Microsoft 365",
        address="owner@example.com",
        active=True,
    )
    selected_principal = microsoft_principal()
    runtime.store.set_calendar_grant(
        account.account_id,
        "read",
        "ready",
        principal_key=selected_principal.key,
        home_account_id=selected_principal.home_account_id,
        tenant_id=selected_principal.tenant_id,
        object_id=selected_principal.object_id,
        email_address=selected_principal.email_address,
    )
    mail_token = mail_account_token_file(runtime.config, account)
    mail_token.parent.mkdir(parents=True)
    mail_token.write_text("mail-read-cache", encoding="utf-8")
    calendar_token = microsoft_calendar_read_token_file(runtime.config, account)
    calendar_token.write_text("calendar-read-cache", encoding="utf-8")
    monkeypatch.setattr(engine_api, "_calendar_entitlement_active", lambda: False)

    locked_status = engine_api._response(
        request(
            config_path,
            "calendar.read.status",
            {"provider": account.provider, "account_id": account.account_id},
        )
    )
    assert locked_status["data"]["state"] == "ready"
    assert locked_status["data"]["available"] is False
    assert locked_status["data"]["entitlement_active"] is False

    response = engine_api._response(
        request(
            config_path,
            "calendar.read.disconnect",
            {"provider": account.provider, "account_id": account.account_id},
        )
    )

    assert response["data"]["state"] == "not_requested"
    assert not calendar_token.exists()
    assert mail_token.read_text(encoding="utf-8") == "mail-read-cache"
    assert runtime.store.calendar_grant(account.account_id).state == "not_requested"


@pytest.mark.parametrize(
    ("profile", "authorization_name", "scope"),
    [
        ("read", "MicrosoftCalendarReadAuthorization", "Calendars.Read"),
        ("proposal", "MicrosoftCalendarProposalAuthorization", "Calendars.Read.Shared"),
        ("write", "MicrosoftCalendarWriteAuthorization", "Calendars.ReadWrite"),
    ],
)
def test_calendar_profile_lifecycles_use_distinct_private_caches(
    profile: str,
    authorization_name: str,
    scope: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    account = runtime.store.register_mail_account(
        "microsoft365",
        f"microsoft365-{'f' * 32}",
        display_name="Microsoft 365",
        address="owner@example.com",
        active=True,
    )
    selected_principal = microsoft_principal()
    mail_token = mail_account_token_file(runtime.config, account)
    mail_token.parent.mkdir(parents=True)
    mail_token.write_text("mail-read-cache", encoding="utf-8")
    monkeypatch.setattr(engine_api, "_calendar_entitlement_active", lambda: True)
    monkeypatch.setattr(
        engine_api,
        "microsoft_mailbox_principal",
        lambda *args: selected_principal,
    )

    class AuthorizedCalendar:
        principal = selected_principal

    class Authorization:
        @classmethod
        def authorize_with_status(cls, credentials_file: Path, token_file: Path):
            token_file.write_text(f"{profile}-private-cache", encoding="utf-8")
            return AuthorizedCalendar(), True

        @classmethod
        def from_token(cls, credentials_file: Path, token_file: Path):
            assert token_file.read_text(encoding="utf-8") == f"{profile}-private-cache"
            return AuthorizedCalendar()

    monkeypatch.setattr(engine_api, authorization_name, Authorization)
    payload = {"provider": account.provider, "account_id": account.account_id}

    connected = engine_api._response(request(config_path, f"calendar.{profile}.connect", payload))
    status = engine_api._response(request(config_path, f"calendar.{profile}.status", payload))

    assert connected["data"]["state"] == "ready"
    assert status["data"]["available"] is True
    assert status["data"]["scope"] == scope
    profile_token = microsoft_calendar_token_file(runtime.config, account, profile)
    assert profile_token.read_text(encoding="utf-8") == f"{profile}-private-cache"
    for other in {"read", "proposal", "write"} - {profile}:
        assert not microsoft_calendar_token_file(runtime.config, account, other).exists()

    monkeypatch.setattr(
        engine_api,
        "microsoft_mailbox_principal",
        lambda *args: microsoft_principal(object_id="replacement-object"),
    )
    mismatched = engine_api._response(request(config_path, f"calendar.{profile}.status", payload))
    assert mismatched["data"]["state"] == "revoked"
    assert mismatched["data"]["available"] is False

    disconnected = engine_api._response(
        request(config_path, f"calendar.{profile}.disconnect", payload)
    )

    assert disconnected["data"]["state"] == "not_requested"
    assert not profile_token.exists()
    assert mail_token.read_text(encoding="utf-8") == "mail-read-cache"


def engine_calendar_event(event_id: str, subject: str) -> CalendarEvent:
    return CalendarEvent(
        event_id=event_id,
        subject=subject,
        start_date_time="2026-09-07T09:00:00.0000000",
        start_time_zone="UTC",
        end_date_time="2026-09-07T10:00:00.0000000",
        end_time_zone="UTC",
        is_all_day=False,
        location="Office",
    )


def ready_calendar_runtime(
    config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Runtime, object, MicrosoftPrincipal]:
    write_config(config_path)
    runtime = load_runtime(config_path)
    account = runtime.store.register_mail_account(
        "microsoft365",
        f"microsoft365-{'9' * 32}",
        display_name="Microsoft 365",
        address="owner@example.com",
        active=True,
    )
    selected_principal = microsoft_principal()
    identity = {
        "principal_key": selected_principal.key,
        "home_account_id": selected_principal.home_account_id,
        "tenant_id": selected_principal.tenant_id,
        "object_id": selected_principal.object_id,
        "email_address": selected_principal.email_address,
    }
    runtime.store.set_calendar_grant(account.account_id, "read", "ready", **identity)
    mail_token = mail_account_token_file(runtime.config, account)
    mail_token.parent.mkdir(parents=True)
    mail_token.write_text("mail-read-cache", encoding="utf-8")
    microsoft_calendar_read_token_file(runtime.config, account).write_text(
        "calendar-read-cache",
        encoding="utf-8",
    )
    monkeypatch.setattr(engine_api, "_calendar_entitlement_active", lambda: True)
    monkeypatch.setattr(
        engine_api,
        "microsoft_mailbox_principal",
        lambda *args: selected_principal,
    )
    monkeypatch.setattr(
        engine_api,
        "microsoft_cached_mailbox_principal",
        lambda *args: selected_principal,
    )

    class Authorization:
        principal = selected_principal

    monkeypatch.setattr(
        engine_api.MicrosoftCalendarReadAuthorization,
        "from_token",
        lambda *args: Authorization(),
    )
    return runtime, account, selected_principal


def test_calendar_sync_and_offline_read_apply_incremental_tombstones(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    runtime, account, _principal = ready_calendar_runtime(config_path, monkeypatch)
    start = "2026-09-01T00:00:00Z"
    end = "2026-10-01T00:00:00Z"
    cursors: list[str | None] = []

    def delta(authorization, window_start: str, window_end: str, *, cursor=None):
        cursors.append(cursor)
        if cursor is None:
            return CalendarDeltaRound(
                (
                    CalendarDeltaChange(
                        "event-1",
                        engine_calendar_event("event-1", "Planning"),
                    ),
                ),
                "https://graph.microsoft.com/v1.0/me/calendarView/delta?$deltatoken=one",
            )
        return CalendarDeltaRound(
            (
                CalendarDeltaChange("event-1", None),
                CalendarDeltaChange("event-2", engine_calendar_event("event-2", "Review")),
            ),
            "https://graph.microsoft.com/v1.0/me/calendarView/delta?$deltatoken=two",
        )

    monkeypatch.setattr(engine_api, "calendar_delta_round", delta)
    payload = {
        "provider": account.provider,
        "account_id": account.account_id,
        "window_start": start,
        "window_end": end,
    }

    first = engine_api._response(request(config_path, "calendar.read.sync", payload))
    first_read = engine_api._response(request(config_path, "calendar.read.events", payload))
    second = engine_api._response(request(config_path, "calendar.read.sync", payload))
    second_read = engine_api._response(request(config_path, "calendar.read.events", payload))

    assert first["data"]["event_count"] == 1
    assert first_read["data"]["events"][0]["subject"] == "Planning"
    assert second["data"]["event_count"] == 1
    assert second_read["data"]["events"][0]["event_id"] == "event-2"
    assert cursors == [None, cursors[1]]
    assert cursors[1] is not None and cursors[1].endswith("one")
    assert runtime.store.calendar_window(account.account_id).cursor.endswith("two")  # type: ignore[union-attr]


def test_calendar_offline_read_rejects_locally_cached_mailbox_principal_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    runtime, account, _principal = ready_calendar_runtime(config_path, monkeypatch)
    monkeypatch.setattr(
        engine_api,
        "microsoft_cached_mailbox_principal",
        lambda *args: microsoft_principal(object_id="replacement-object"),
    )

    response = engine_api._response(
        request(
            config_path,
            "calendar.read.events",
            {
                "provider": account.provider,
                "account_id": account.account_id,
                "window_start": "2026-09-01T00:00:00Z",
                "window_end": "2026-10-01T00:00:00Z",
            },
        )
    )

    assert response["error"]["code"] == "calendar_principal_mismatch"
    grant = runtime.store.calendar_grant(account.account_id)
    assert grant is not None
    assert grant.state == "ready"


def test_calendar_sync_persists_revoked_grant_after_graph_rejects_read_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    runtime, account, _principal = ready_calendar_runtime(config_path, monkeypatch)
    monkeypatch.setattr(
        engine_api,
        "calendar_delta_round",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            MicrosoftAuthorizationRejected("Microsoft rejected the calendar read authorization")
        ),
    )

    response = engine_api._response(
        request(
            config_path,
            "calendar.read.sync",
            {
                "provider": account.provider,
                "account_id": account.account_id,
                "window_start": "2026-09-01T00:00:00Z",
                "window_end": "2026-10-01T00:00:00Z",
            },
        )
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "calendar_authorization_revoked"
    assert runtime.store.calendar_grant(account.account_id).state == "revoked"


def test_calendar_stale_replacement_failure_preserves_completed_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    runtime, account, selected_principal = ready_calendar_runtime(config_path, monkeypatch)
    start = "2026-09-01T00:00:00.000000Z"
    end = "2026-10-01T00:00:00.000000Z"
    runtime.store.commit_calendar_round(
        account_id=account.account_id,
        principal_key=selected_principal.key,
        window_start=start,
        window_end=end,
        cursor="https://graph.microsoft.com/v1.0/me/calendarView/delta?$deltatoken=old",
        changes=(
            engine_api.CalendarEventMutation(
                "event-1",
                engine_api.CalendarEventProjection(
                    event_id="event-1",
                    subject="Preserved",
                    start_date_time="2026-09-07T09:00:00.0000000",
                    start_time_zone="UTC",
                    end_date_time="2026-09-07T10:00:00.0000000",
                    end_time_zone="UTC",
                    is_all_day=False,
                    location="Office",
                ),
            ),
        ),
        replace=True,
    )
    calls: list[str | None] = []

    def delta(authorization, window_start: str, window_end: str, *, cursor=None):
        calls.append(cursor)
        if cursor is not None:
            raise StaleCalendarCursor("expired")
        raise Microsoft365Error("replacement unavailable")

    monkeypatch.setattr(engine_api, "calendar_delta_round", delta)
    payload = {
        "provider": account.provider,
        "account_id": account.account_id,
        "window_start": start,
        "window_end": end,
    }

    response = engine_api._response(request(config_path, "calendar.read.sync", payload))

    assert response["error"]["code"] == "calendar_error"
    assert len(calls) == 2 and calls[0] is not None and calls[1] is None
    assert runtime.store.calendar_window(account.account_id).cursor.endswith("old")  # type: ignore[union-attr]
    assert runtime.store.calendar_events(account.account_id)[0].subject == "Preserved"


def test_calendar_events_response_limit_accepts_exact_utf8_size_and_rejects_one_less(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    runtime, account, selected_principal = ready_calendar_runtime(config_path, monkeypatch)
    start = "2026-09-01T00:00:00.000000Z"
    end = "2026-10-01T00:00:00.000000Z"
    runtime.store.commit_calendar_round(
        account_id=account.account_id,
        principal_key=selected_principal.key,
        window_start=start,
        window_end=end,
        cursor="https://graph.microsoft.com/v1.0/me/calendarView/delta?$deltatoken=one",
        changes=(
            engine_api.CalendarEventMutation(
                "event-1",
                engine_calendar_event("event-1", "Plan 🗓️"),
            ),
        ),
        replace=True,
    )
    calendar_request = request(
        config_path,
        "calendar.read.events",
        {
            "provider": account.provider,
            "account_id": account.account_id,
            "window_start": start,
            "window_end": end,
        },
    )
    response = engine_api._response(calendar_request)
    encoded_size = len(
        json.dumps(
            response,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )

    monkeypatch.setattr(engine_api, "MAX_CALENDAR_EVENTS_RESPONSE_BYTES", encoded_size)
    assert engine_api._response(calendar_request)["ok"] is True

    monkeypatch.setattr(
        engine_api.MicrosoftCalendarReadAuthorization,
        "from_token",
        lambda *args: pytest.fail("Offline calendar read refreshed its calendar token"),
    )
    monkeypatch.setattr(
        engine_api,
        "microsoft_mailbox_principal",
        lambda *args: pytest.fail("Offline calendar read refreshed its mailbox token"),
    )
    lock_path = engine_api._production_check_lock_path(runtime.config)
    with engine_api.operation_lock(lock_path, "test sync in progress"):
        assert engine_api._response(calendar_request)["ok"] is True

    monkeypatch.setattr(engine_api, "MAX_CALENDAR_EVENTS_RESPONSE_BYTES", encoded_size - 1)
    rejected = engine_api._response(calendar_request)
    assert rejected["ok"] is False
    assert rejected["error"]["code"] == "calendar_result_too_large"

    monkeypatch.setattr(engine_api, "MAX_CALENDAR_EVENTS_RESPONSE_BYTES", encoded_size)
    calendar_token = microsoft_calendar_read_token_file(runtime.config, account)
    calendar_token.unlink()
    missing_calendar_cache = engine_api._response(calendar_request)
    assert missing_calendar_cache["error"]["code"] == "calendar_authorization_required"

    calendar_token.write_text("calendar-read-cache", encoding="utf-8")
    mail_account_token_file(runtime.config, account).unlink()
    missing_mailbox_cache = engine_api._response(calendar_request)
    assert missing_mailbox_cache["error"]["code"] == "mailbox_authorization_required"


def test_mail_account_connect_installs_private_imap_credentials_and_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    opened_credentials: list[Path] = []

    class AuthorizedImap:
        def initial_cursor(self) -> str:
            return IMAP_CURSOR

    def from_credentials_file(path: Path) -> AuthorizedImap:
        opened_credentials.append(path)
        assert "private-password" in path.read_text(encoding="utf-8")
        return AuthorizedImap()

    monkeypatch.setattr(engine_api.ImapGateway, "from_credentials_file", from_credentials_file)

    response = engine_api._response(
        request(
            config_path,
            "mail.accounts.connect",
            {
                "provider": "imap",
                "connection": {
                    "email_address": "OWNER@Example.com",
                    "host": "mail.example.com",
                    "port": 993,
                    "security": "tls",
                    "username": "owner@example.com",
                    "password": "private-password",
                },
            },
        )
    )

    assert response["ok"] is True
    account_response = response["data"]["account"]
    assert account_response["provider"] == "imap"
    assert account_response["address"] == "owner@example.com"
    assert account_response["active"] is True
    runtime = load_runtime(config_path)
    account = runtime.store.active_mail_account()
    assert account is not None
    assert account.account_id.startswith("imap-")
    credentials_file = mail_account_token_file(runtime.config, account)
    assert credentials_file.is_file()
    assert credentials_file.stat().st_mode & 0o777 == 0o600
    assert runtime.store.state(provider="imap", account_id=account.account_id)[0] == (IMAP_CURSOR)
    encoded = json.dumps(response)
    assert "private-password" not in encoded
    assert "mail.example.com" not in encoded
    assert str(tmp_path) not in encoded
    assert len(opened_credentials) == 1


def test_mail_account_reconnect_rejects_a_different_imap_identity_before_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    account = runtime.store.register_mail_account(
        "imap",
        f"imap-{'a' * 32}",
        display_name="Other mail server",
        address="owner@example.com",
        active=True,
    )
    destination = mail_account_token_file(runtime.config, account)
    monkeypatch.setattr(
        engine_api.ImapGateway,
        "from_credentials_file",
        lambda path: pytest.fail("identity mismatch must fail before opening IMAP"),
    )

    response = engine_api._response(
        request(
            config_path,
            "mail.accounts.reconnect",
            {
                "provider": "imap",
                "account_id": account.account_id,
                "connection": {
                    "email_address": "different@example.com",
                    "host": "mail.example.com",
                    "port": 993,
                    "security": "tls",
                    "username": "different@example.com",
                    "password": "private-password",
                },
            },
        )
    )

    assert response["error"]["code"] == "account_identity_mismatch"
    assert not destination.exists()


def test_mail_account_reconnect_verifies_imap_and_preserves_existing_cursor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    account = runtime.store.register_mail_account(
        "imap",
        f"imap-{'b' * 32}",
        display_name="Other mail server",
        address="owner@example.com",
        active=True,
    )
    runtime.store.set_state(
        IMAP_CURSOR,
        provider=account.provider,
        account_id=account.account_id,
    )
    destination = mail_account_token_file(runtime.config, account)
    destination.parent.mkdir(parents=True)
    write_credentials(destination, imap_credentials(password="old-password"))
    probes = 0

    class VerifiedImap:
        def initial_cursor(self) -> str:
            nonlocal probes
            probes += 1
            return REPLACEMENT_IMAP_CURSOR

    monkeypatch.setattr(
        engine_api.ImapGateway,
        "from_credentials_file",
        lambda _path: VerifiedImap(),
    )

    response = engine_api._response(
        request(
            config_path,
            "mail.accounts.reconnect",
            {
                "provider": "imap",
                "account_id": account.account_id,
                "connection": {
                    "email_address": "owner@example.com",
                    "host": "mail.example.com",
                    "port": 993,
                    "security": "tls",
                    "username": "owner@example.com",
                    "password": "new-password",
                },
            },
        )
    )

    assert response["ok"] is True
    assert response["data"]["baseline_initialized"] is False
    assert probes == 1
    assert "new-password" in destination.read_text(encoding="utf-8")
    assert runtime.store.state(provider=account.provider, account_id=account.account_id)[0] == (
        IMAP_CURSOR
    )


def test_mail_account_reconnect_resets_cursor_when_server_mailbox_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    account = runtime.store.register_mail_account(
        "imap",
        f"imap-{'d' * 32}",
        display_name="Other mail server",
        address="owner@example.com",
        active=True,
    )
    runtime.store.set_state(
        IMAP_CURSOR,
        provider=account.provider,
        account_id=account.account_id,
    )
    destination = mail_account_token_file(runtime.config, account)
    destination.parent.mkdir(parents=True)
    write_credentials(destination, imap_credentials(host="old.example.com"))

    class ReplacementImap:
        def initial_cursor(self) -> str:
            return REPLACEMENT_IMAP_CURSOR

    monkeypatch.setattr(
        engine_api.ImapGateway,
        "from_credentials_file",
        lambda _path: ReplacementImap(),
    )

    response = engine_api._response(
        request(
            config_path,
            "mail.accounts.reconnect",
            {
                "provider": "imap",
                "account_id": account.account_id,
                "connection": {
                    "email_address": "owner@example.com",
                    "host": "replacement.example.com",
                    "port": 993,
                    "security": "tls",
                    "username": "owner@example.com",
                    "password": "new-password",
                },
            },
        )
    )

    assert response["ok"] is True
    assert response["data"]["baseline_initialized"] is True
    assert runtime.store.state(provider=account.provider, account_id=account.account_id)[0] == (
        REPLACEMENT_IMAP_CURSOR
    )
    assert "replacement.example.com" in destination.read_text(encoding="utf-8")


def test_mail_account_reconnect_uses_retained_cursor_binding_after_disconnect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    account = runtime.store.register_mail_account(
        "imap",
        f"imap-{'e' * 32}",
        display_name="Other mail server",
        address="owner@example.com",
        active=True,
    )
    retained_cursor = f"eom-imap-v2:{imap_mailbox_identity(imap_credentials())}:44:7"
    runtime.store.set_state(
        retained_cursor,
        provider=account.provider,
        account_id=account.account_id,
    )

    class ReconnectedImap:
        def initial_cursor(self) -> str:
            return REPLACEMENT_IMAP_CURSOR

    monkeypatch.setattr(
        engine_api.ImapGateway,
        "from_credentials_file",
        lambda _path: ReconnectedImap(),
    )

    response = engine_api._response(
        request(
            config_path,
            "mail.accounts.reconnect",
            {
                "provider": "imap",
                "account_id": account.account_id,
                "connection": {
                    "email_address": "owner@example.com",
                    "host": "mail.example.com",
                    "port": 993,
                    "security": "tls",
                    "username": "owner@example.com",
                    "password": "new-password",
                },
            },
        )
    )

    assert response["ok"] is True
    assert response["data"]["baseline_initialized"] is False
    assert runtime.store.state(provider=account.provider, account_id=account.account_id)[0] == (
        retained_cursor
    )


def test_mail_account_reconnect_preserves_imap_credentials_when_probe_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    account = runtime.store.register_mail_account(
        "imap",
        f"imap-{'c' * 32}",
        display_name="Other mail server",
        address="owner@example.com",
        active=True,
    )
    runtime.store.set_state(
        IMAP_CURSOR,
        provider=account.provider,
        account_id=account.account_id,
    )
    destination = mail_account_token_file(runtime.config, account)
    destination.parent.mkdir(parents=True)
    write_credentials(destination, imap_credentials(password="preserved-password"))
    preserved = destination.read_text(encoding="utf-8")

    class RejectedImap:
        def initial_cursor(self) -> str:
            raise ImapError("imap_authentication_failed", "Mail server rejected the credentials")

    monkeypatch.setattr(
        engine_api.ImapGateway,
        "from_credentials_file",
        lambda _path: RejectedImap(),
    )

    response = engine_api._response(
        request(
            config_path,
            "mail.accounts.reconnect",
            {
                "provider": "imap",
                "account_id": account.account_id,
                "connection": {
                    "email_address": "owner@example.com",
                    "host": "mail.example.com",
                    "port": 993,
                    "security": "tls",
                    "username": "owner@example.com",
                    "password": "wrong-password",
                },
            },
        )
    )

    assert response["error"]["code"] == "imap_authentication_failed"
    assert destination.read_text(encoding="utf-8") == preserved
    assert runtime.store.state(provider=account.provider, account_id=account.account_id)[0] == (
        IMAP_CURSOR
    )


def test_mail_account_reconnect_rejects_different_microsoft_identity_before_cache_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    account = runtime.store.register_mail_account(
        "microsoft365",
        f"microsoft365-{'b' * 32}",
        display_name="Microsoft 365",
        address="owner@example.com",
        active=True,
    )
    token_file = mail_account_token_file(runtime.config, account)
    token_file.parent.mkdir(parents=True)
    token_file.write_text("preserved cache", encoding="utf-8")
    runtime.store.set_state(
        "preserved cursor",
        provider=account.provider,
        account_id=account.account_id,
    )

    class WrongMicrosoft:
        def profile(self) -> Microsoft365Profile:
            return Microsoft365Profile("other@example.com")

        def initial_cursor(self) -> str:
            pytest.fail("Identity mismatch must fail before baseline access")

    def authorize_with_status(
        credentials_file: Path,
        staged_token: Path,
        *,
        force_reauthorize: bool,
    ) -> tuple[WrongMicrosoft, bool]:
        staged_token.write_text("wrong cache", encoding="utf-8")
        return WrongMicrosoft(), force_reauthorize

    monkeypatch.setattr(
        engine_api.Microsoft365Gateway,
        "authorize_with_status",
        authorize_with_status,
    )

    response = engine_api._response(
        request(
            config_path,
            "mail.accounts.reconnect",
            {"provider": account.provider, "account_id": account.account_id},
        )
    )

    assert response["error"]["code"] == "account_identity_mismatch"
    assert token_file.read_text(encoding="utf-8") == "preserved cache"
    assert runtime.store.state(provider=account.provider, account_id=account.account_id)[0] == (
        "preserved cursor"
    )
    assert list(tmp_path.glob(".microsoft365-authorization-*")) == []


@pytest.mark.parametrize(
    ("replacement_object_id", "expected_grant_state", "expected_read_error"),
    [
        ("object-1", "ready", "calendar_sync_required"),
        ("replacement-object", "revoked", "calendar_authorization_required"),
    ],
)
def test_mail_account_reconnect_binds_offline_calendar_reads_to_immutable_principal(
    replacement_object_id: str,
    expected_grant_state: str,
    expected_read_error: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    account = runtime.store.register_mail_account(
        "microsoft365",
        f"microsoft365-{'d' * 32}",
        display_name="Microsoft 365",
        address="owner@example.com",
        active=True,
    )
    runtime.store.set_state(
        "preserved cursor",
        provider=account.provider,
        account_id=account.account_id,
    )
    original = microsoft_principal()
    runtime.store.set_calendar_grant(
        account.account_id,
        "read",
        "ready",
        principal_key=original.key,
        home_account_id=original.home_account_id,
        tenant_id=original.tenant_id,
        object_id=original.object_id,
        email_address=original.email_address,
    )
    mail_token = mail_account_token_file(runtime.config, account)
    mail_token.parent.mkdir(parents=True)
    mail_token.write_text("preserved cache", encoding="utf-8")
    calendar_token = microsoft_calendar_read_token_file(runtime.config, account)
    calendar_token.write_text("calendar cache", encoding="utf-8")

    class AuthorizedMicrosoft:
        def profile(self) -> Microsoft365Profile:
            return Microsoft365Profile("owner@example.com")

        def initial_cursor(self) -> str:
            pytest.fail("An existing mailbox must preserve its cursor")

    def authorize_with_status(
        credentials_file: Path,
        staged_token: Path,
        *,
        force_reauthorize: bool,
    ) -> tuple[AuthorizedMicrosoft, bool]:
        staged_token.write_text("replacement cache", encoding="utf-8")
        return AuthorizedMicrosoft(), force_reauthorize

    monkeypatch.setattr(
        engine_api.Microsoft365Gateway,
        "authorize_with_status",
        authorize_with_status,
    )
    monkeypatch.setattr(
        engine_api,
        "microsoft_mailbox_principal",
        lambda *args: microsoft_principal(object_id=replacement_object_id),
    )
    monkeypatch.setattr(
        engine_api,
        "microsoft_cached_mailbox_principal",
        lambda *args: microsoft_principal(object_id=replacement_object_id),
    )
    monkeypatch.setattr(engine_api, "_calendar_entitlement_active", lambda: True)

    response = engine_api._response(
        request(
            config_path,
            "mail.accounts.reconnect",
            {"provider": account.provider, "account_id": account.account_id},
        )
    )

    assert response["ok"] is True
    assert mail_token.read_text(encoding="utf-8") == "replacement cache"
    grant = runtime.store.calendar_grant(account.account_id)
    assert grant is not None
    assert grant.state == expected_grant_state

    read = engine_api._response(
        request(
            config_path,
            "calendar.read.events",
            {
                "provider": account.provider,
                "account_id": account.account_id,
                "window_start": "2026-09-01T00:00:00Z",
                "window_end": "2026-10-01T00:00:00Z",
            },
        )
    )
    assert read["error"]["code"] == expected_read_error


def test_mail_account_reconnect_preserves_calendar_grant_until_replacement_installs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    account = runtime.store.register_mail_account(
        "microsoft365",
        f"microsoft365-{'e' * 32}",
        display_name="Microsoft 365",
        address="owner@example.com",
        active=True,
    )
    original = microsoft_principal()
    runtime.store.set_calendar_grant(
        account.account_id,
        "read",
        "ready",
        principal_key=original.key,
        home_account_id=original.home_account_id,
        tenant_id=original.tenant_id,
        object_id=original.object_id,
        email_address=original.email_address,
    )
    mail_token = mail_account_token_file(runtime.config, account)
    mail_token.parent.mkdir(parents=True)
    mail_token.write_text("preserved cache", encoding="utf-8")

    class AuthorizedMicrosoft:
        def profile(self) -> Microsoft365Profile:
            return Microsoft365Profile("owner@example.com")

        def initial_cursor(self) -> str:
            raise Microsoft365Error("baseline unavailable")

    def authorize_with_status(
        credentials_file: Path,
        staged_token: Path,
        *,
        force_reauthorize: bool,
    ) -> tuple[AuthorizedMicrosoft, bool]:
        staged_token.write_text("replacement cache", encoding="utf-8")
        return AuthorizedMicrosoft(), force_reauthorize

    monkeypatch.setattr(
        engine_api.Microsoft365Gateway,
        "authorize_with_status",
        authorize_with_status,
    )
    monkeypatch.setattr(
        engine_api,
        "microsoft_mailbox_principal",
        lambda *args: microsoft_principal(object_id="replacement-object"),
    )

    response = engine_api._response(
        request(
            config_path,
            "mail.accounts.reconnect",
            {"provider": account.provider, "account_id": account.account_id},
        )
    )

    assert response["error"]["code"] == "mailbox_error"
    assert mail_token.read_text(encoding="utf-8") == "preserved cache"
    grant = runtime.store.calendar_grant(account.account_id)
    assert grant is not None
    assert grant.state == "ready"


def test_mail_account_connect_reuses_and_activates_known_disconnected_microsoft_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    existing = runtime.store.register_mail_account(
        "microsoft365",
        f"microsoft365-{'c' * 32}",
        display_name="Microsoft 365",
        address="owner@example.com",
        active=False,
    )
    runtime.store.set_state(
        "preserved cursor",
        provider=existing.provider,
        account_id=existing.account_id,
    )

    class AuthorizedMicrosoft:
        def profile(self) -> Microsoft365Profile:
            return Microsoft365Profile("owner@example.com")

        def initial_cursor(self) -> str:
            pytest.fail("A reused account must preserve its existing baseline")

    def authorize_with_status(
        credentials_file: Path,
        token_file: Path,
        *,
        force_reauthorize: bool,
    ) -> tuple[AuthorizedMicrosoft, bool]:
        token_file.write_text("replacement cache", encoding="utf-8")
        return AuthorizedMicrosoft(), force_reauthorize

    monkeypatch.setattr(
        engine_api.Microsoft365Gateway,
        "authorize_with_status",
        authorize_with_status,
    )

    response = engine_api._response(
        request(config_path, "mail.accounts.connect", {"provider": "microsoft365"})
    )

    assert response["ok"] is True
    assert response["data"]["account"]["account_id"] == existing.account_id
    assert response["data"]["account"]["active"] is True
    assert response["data"]["baseline_initialized"] is False
    assert len(runtime.store.mail_accounts()) == 2
    assert runtime.store.state(provider=existing.provider, account_id=existing.account_id)[0] == (
        "preserved cursor"
    )


def test_microsoft_provider_error_uses_generic_secret_free_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)

    def reject(*args, **kwargs):
        raise Microsoft365Error("provider detail with private-token-value")

    monkeypatch.setattr(engine_api.Microsoft365Gateway, "authorize_with_status", reject)

    response = engine_api._response(
        request(config_path, "mail.accounts.connect", {"provider": "microsoft365"})
    )

    assert response["error"] == {
        "code": "mailbox_error",
        "message": "Email provider operation failed; see stderr for details",
    }
    assert "private-token-value" not in json.dumps(response)


def test_private_token_install_only_syncs_the_new_writable_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "staged-token.json"
    destination = tmp_path / "installed" / "readonly-token.json"
    source.write_bytes(b'{"token": "private"}')
    original_fsync = engine_api.os.fsync
    fsync_calls = 0

    def tracking_fsync(descriptor: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        original_fsync(descriptor)

    monkeypatch.setattr(engine_api.os, "fsync", tracking_fsync)

    engine_api._install_private_token(source, destination)

    assert fsync_calls == 1
    assert destination.read_bytes() == source.read_bytes()


def test_mail_account_reconnect_rejects_different_identity_before_replacing_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.store.update_mail_account_identity(
        "gmail",
        "gmail-default",
        display_name="Gmail",
        address="owner@example.com",
    )
    runtime.config.gmail_token_file.write_text("preserved token", encoding="utf-8")

    class ExistingGmail:
        def profile(self) -> GmailProfile:
            return GmailProfile("owner@example.com", "current-cursor")

    class WrongGmail:
        def profile(self) -> GmailProfile:
            return GmailProfile("other@example.com", "other-cursor")

    def authorize_with_status(
        credentials_file: Path,
        token_file: Path,
        *,
        force_reauthorize: bool,
    ) -> tuple[WrongGmail, bool]:
        token_file.write_text("wrong account token", encoding="utf-8")
        return WrongGmail(), force_reauthorize

    monkeypatch.setattr(
        engine_api.GmailGateway,
        "from_token",
        lambda credentials_file, token_file: ExistingGmail(),
    )
    monkeypatch.setattr(engine_api.GmailGateway, "authorize_with_status", authorize_with_status)

    response = engine_api._response(
        request(
            config_path,
            "mail.accounts.reconnect",
            {"provider": "gmail", "account_id": "gmail-default"},
        )
    )

    assert response["error"]["code"] == "account_identity_mismatch"
    assert runtime.config.gmail_token_file.read_text(encoding="utf-8") == "preserved token"
    assert runtime.store.active_mail_account().address == "owner@example.com"
    assert list(tmp_path.glob(".gmail-authorization-*")) == []


def test_gmail_reconnect_rebinds_rotated_identity_for_the_next_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.store.update_mail_account_identity(
        DEFAULT_MAIL_PROVIDER,
        DEFAULT_MAIL_ACCOUNT_ID,
        display_name="Gmail",
        address="owner@example.com",
    )
    established_identity = _bind_test_mailbox(
        runtime.store,
        DEFAULT_MAIL_PROVIDER,
        DEFAULT_MAIL_ACCOUNT_ID,
    )
    runtime.store.set_state(
        "preserved-cursor",
        provider=DEFAULT_MAIL_PROVIDER,
        account_id=DEFAULT_MAIL_ACCOUNT_ID,
        mailbox_identity_key=established_identity,
    )
    runtime.config.gmail_token_file.write_text("established token", encoding="utf-8")
    rotated_identity = "b" * 64

    class Gmail:
        def __init__(self, identity_key: str):
            self.identity_key = identity_key

        def profile(self) -> GmailProfile:
            return GmailProfile("owner@example.com", "current-cursor")

        def mailbox_address(self) -> str:
            return self.profile().email_address

        def mailbox_identity_key(self) -> str:
            return self.identity_key

        def changes_since(self, cursor: str) -> MailboxChanges:
            assert cursor == "current-cursor"
            return MailboxChanges((), cursor)

    established_gmail = Gmail(established_identity)
    rotated_gmail = Gmail(rotated_identity)

    def from_token(credentials_file: Path, token_file: Path) -> Gmail:
        assert credentials_file == runtime.config.gmail_credentials_file
        return (
            rotated_gmail
            if token_file.read_text(encoding="utf-8") == "rotated token"
            else established_gmail
        )

    def authorize_with_status(
        credentials_file: Path,
        token_file: Path,
        *,
        force_reauthorize: bool,
    ) -> tuple[Gmail, bool]:
        assert credentials_file == runtime.config.gmail_credentials_file
        assert force_reauthorize is True
        token_file.write_text("rotated token", encoding="utf-8")
        return rotated_gmail, True

    monkeypatch.setattr(engine_api.GmailGateway, "from_token", from_token)
    monkeypatch.setattr(
        engine_api.GmailGateway,
        "authorize_with_status",
        authorize_with_status,
    )

    reconnect = engine_api._response(
        request(
            config_path,
            "mail.accounts.reconnect",
            {"provider": DEFAULT_MAIL_PROVIDER, "account_id": DEFAULT_MAIL_ACCOUNT_ID},
        )
    )
    checked = engine_api._response(request(config_path, "watcher.check", {"dry_run": True}))

    assert reconnect["ok"] is True
    assert reconnect["data"]["baseline_initialized"] is True
    assert checked["ok"] is True
    account = runtime.store.active_mail_account()
    assert account is not None
    assert account.mailbox_identity_key == rotated_identity
    assert runtime.store.state(
        provider=DEFAULT_MAIL_PROVIDER,
        account_id=DEFAULT_MAIL_ACCOUNT_ID,
    )[0] == "current-cursor"


def test_mail_account_disconnect_preserves_history_and_send_authorization(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.config.gmail_token_file.write_text("readonly token", encoding="utf-8")
    runtime.config.gmail_send_token_file.write_text("send token", encoding="utf-8")
    runtime.store.set_state("preserved-cursor", datetime(2026, 9, 1, tzinfo=UTC))
    _add_test_message(
        runtime.store,
        message_id="retained",
        thread_id=None,
        sender="owner@example.com",
        sender_name=None,
        subject="Retained",
        received_at="2026-09-01T12:00:00+00:00",
    )

    response = engine_api._response(
        request(
            config_path,
            "mail.accounts.disconnect",
            {"provider": "gmail", "account_id": "gmail-default"},
        )
    )

    assert response["data"]["account"]["connected"] is False
    assert not runtime.config.gmail_token_file.exists()
    assert runtime.config.gmail_send_token_file.read_text(encoding="utf-8") == "send token"
    assert runtime.store.state()[0] == "preserved-cursor"
    assert runtime.store.has_message("retained")
    checked = engine_api._response(request(config_path, "watcher.check"))
    retained = engine_api._response(request(config_path, "inbox.query", {"limit": 25}))
    assert checked["error"]["code"] == "account_unavailable"
    assert [item["message_id"] for item in retained["data"]["items"]] == ["retained"]


def test_mail_account_activate_rejects_disconnected_account(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)

    response = engine_api._response(
        request(
            config_path,
            "mail.accounts.activate",
            {"provider": "gmail", "account_id": "gmail-default"},
        )
    )

    assert response["error"]["code"] == "account_unavailable"


def test_mail_account_activate_switches_one_connected_account_and_preserves_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.config.gmail_token_file.write_text("first token", encoding="utf-8")
    runtime.store.set_state("first-cursor")
    second = runtime.store.register_mail_account(
        "gmail",
        f"gmail-{'a' * 32}",
        display_name="Gmail",
        address="second@example.com",
    )
    second_token = mail_account_token_file(runtime.config, second)
    second_token.parent.mkdir(parents=True)
    second_token.write_text("second token", encoding="utf-8")
    runtime.store.set_state(
        "second-cursor",
        provider=second.provider,
        account_id=second.account_id,
    )

    response = engine_api._response(
        request(
            config_path,
            "mail.accounts.activate",
            {"provider": second.provider, "account_id": second.account_id},
        )
    )
    monkeypatch.setattr("eom_email_watcher.model.LocalModel.health", lambda self: (True, "ready"))
    health = engine_api._response(request(config_path, "health.get"))

    assert response["data"]["account"]["active"] is True
    assert runtime.store.active_mail_account().account_id == second.account_id
    assert [account.active for account in runtime.store.mail_accounts()] == [True, False]
    assert runtime.store.state()[0] == "first-cursor"
    assert runtime.store.state(provider=second.provider, account_id=second.account_id)[0] == (
        "second-cursor"
    )
    assert health["data"]["gmail"]["connected"] is True
    assert [
        account["account_id"] for account in health["data"]["mail"]["accounts"] if account["active"]
    ] == [second.account_id]


@pytest.mark.parametrize(
    ("operation", "payload", "code"),
    [
        ("mail.accounts.list", {"unexpected": True}, "invalid_request"),
        ("mail.accounts.connect", {"provider": False}, "invalid_request"),
        ("mail.accounts.connect", {"provider": "pop3"}, "unsupported_provider"),
        ("mail.accounts.connect", {"provider": "imap"}, "imap_configuration_error"),
        (
            "mail.accounts.connect",
            {"provider": "gmail", "connection": {}},
            "invalid_request",
        ),
        (
            "mail.accounts.connect",
            {"provider": "gmail", "connection": None},
            "invalid_request",
        ),
        (
            "mail.accounts.reconnect",
            {"provider": "gmail", "account_id": ""},
            "invalid_request",
        ),
        (
            "mail.accounts.disconnect",
            {"provider": "gmail", "account_id": "x" * 129},
            "invalid_request",
        ),
        (
            "mail.accounts.activate",
            {"provider": "gmail", "account_id": "missing"},
            "not_found",
        ),
    ],
)
def test_mail_account_operations_reject_invalid_boundaries(
    tmp_path: Path,
    operation: str,
    payload: dict[str, object],
    code: str,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)

    response = engine_api._response(request(config_path, operation, payload))

    assert response["error"]["code"] == code


def test_mail_account_connect_reuses_matching_account_without_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.store.update_mail_account_identity(
        "gmail",
        "gmail-default",
        display_name="Gmail",
        address="owner@example.com",
    )
    runtime.config.gmail_token_file.write_text("existing token", encoding="utf-8")

    class AuthorizedGmail:
        def mailbox_identity_key(self) -> str:
            return REPLACEMENT_GMAIL_IDENTITY

        def profile(self) -> GmailProfile:
            return GmailProfile("owner@example.com", "new-cursor")

    def authorize_with_status(
        credentials_file: Path,
        token_file: Path,
        *,
        force_reauthorize: bool,
    ) -> tuple[AuthorizedGmail, bool]:
        token_file.write_text("replacement token", encoding="utf-8")
        return AuthorizedGmail(), force_reauthorize

    monkeypatch.setattr(engine_api.GmailGateway, "authorize_with_status", authorize_with_status)

    response = engine_api._response(
        request(config_path, "mail.accounts.connect", {"provider": "gmail"})
    )

    assert response["data"]["account"]["account_id"] == "gmail-default"
    assert runtime.config.gmail_token_file.read_text(encoding="utf-8") == "replacement token"
    assert len(runtime.store.mail_accounts()) == 1


def test_mail_account_connect_replaces_the_legacy_credential_epoch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.store.set_state("legacy-cursor", datetime(2026, 8, 1, tzinfo=UTC))
    runtime.config.gmail_token_file.write_text("existing token", encoding="utf-8")

    class ExistingGmail:
        def mailbox_identity_key(self) -> str:
            return GMAIL_IDENTITY

        def profile(self) -> GmailProfile:
            return GmailProfile("owner@example.com", "current-cursor")

    class AuthorizedGmail:
        def mailbox_identity_key(self) -> str:
            return REPLACEMENT_GMAIL_IDENTITY

        def profile(self) -> GmailProfile:
            return GmailProfile("owner@example.com", "authorized-cursor")

    def authorize_with_status(
        credentials_file: Path,
        token_file: Path,
        *,
        force_reauthorize: bool,
    ) -> tuple[AuthorizedGmail, bool]:
        assert force_reauthorize is True
        token_file.write_text("replacement token", encoding="utf-8")
        return AuthorizedGmail(), True

    monkeypatch.setattr(
        engine_api.GmailGateway,
        "from_token",
        lambda credentials_file, token_file: ExistingGmail(),
    )
    monkeypatch.setattr(
        engine_api.GmailGateway,
        "authorize_with_status",
        authorize_with_status,
    )

    response = engine_api._response(
        request(config_path, "mail.accounts.connect", {"provider": "gmail"})
    )

    assert response["ok"] is True
    assert response["data"]["baseline_initialized"] is True
    assert response["data"]["account"]["account_id"] == "gmail-default"
    account = runtime.store.active_mail_account()
    assert account is not None
    assert account.address == "owner@example.com"
    assert account.mailbox_identity_key == REPLACEMENT_GMAIL_IDENTITY
    assert runtime.store.state()[0] == "authorized-cursor"
    assert runtime.config.gmail_token_file.read_text(encoding="utf-8") == "replacement token"
    assert len(runtime.store.mail_accounts()) == 1


def test_mail_account_connect_keeps_unidentified_legacy_history_separate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.store.set_state("legacy-cursor", datetime(2026, 8, 1, tzinfo=UTC))
    _add_test_message(
        runtime.store,
        message_id="legacy-message",
        thread_id=None,
        sender="legacy@example.com",
        sender_name=None,
        subject="Legacy history",
        received_at="2026-08-01T12:00:00+00:00",
    )

    class AuthorizedGmail:
        def mailbox_identity_key(self) -> str:
            return REPLACEMENT_GMAIL_IDENTITY

        def profile(self) -> GmailProfile:
            return GmailProfile("new-owner@example.com", "new-cursor")

    def authorize_with_status(
        credentials_file: Path,
        token_file: Path,
        *,
        force_reauthorize: bool,
    ) -> tuple[AuthorizedGmail, bool]:
        token_file.write_text("new account token", encoding="utf-8")
        return AuthorizedGmail(), force_reauthorize

    monkeypatch.setattr(engine_api.GmailGateway, "authorize_with_status", authorize_with_status)

    response = engine_api._response(
        request(config_path, "mail.accounts.connect", {"provider": "gmail"})
    )

    assert response["ok"] is True
    new_account = runtime.store.active_mail_account()
    assert new_account is not None
    assert new_account.account_id.startswith("gmail-")
    assert new_account.address == "new-owner@example.com"
    assert (
        mail_account_token_file(runtime.config, new_account).read_text(encoding="utf-8")
        == "new account token"
    )
    legacy = runtime.store.mail_account("gmail", "gmail-default")
    assert legacy is not None
    assert legacy.active is False
    assert legacy.address is None
    assert runtime.store.state(provider="gmail", account_id="gmail-default")[0] == "legacy-cursor"
    assert (
        runtime.store.state(provider="gmail", account_id=new_account.account_id)[0] == "new-cursor"
    )
    assert runtime.store.has_message("legacy-message")


def test_mail_account_connect_activates_replacement_for_rejected_legacy_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.store.set_state("legacy-cursor", datetime(2026, 8, 1, tzinfo=UTC))
    runtime.config.gmail_token_file.write_text("rejected legacy token", encoding="utf-8")

    class RejectedGmail:
        def profile(self) -> GmailProfile:
            raise GmailAuthorizationRejected("rejected")

    class AuthorizedGmail:
        def mailbox_identity_key(self) -> str:
            return REPLACEMENT_GMAIL_IDENTITY

        def profile(self) -> GmailProfile:
            return GmailProfile("new-owner@example.com", "new-cursor")

    def authorize_with_status(
        credentials_file: Path,
        token_file: Path,
        *,
        force_reauthorize: bool,
    ) -> tuple[AuthorizedGmail, bool]:
        token_file.write_text("new account token", encoding="utf-8")
        return AuthorizedGmail(), force_reauthorize

    monkeypatch.setattr(
        engine_api.GmailGateway,
        "from_token",
        lambda credentials_file, token_file: RejectedGmail(),
    )
    monkeypatch.setattr(engine_api.GmailGateway, "authorize_with_status", authorize_with_status)

    response = engine_api._response(
        request(config_path, "mail.accounts.connect", {"provider": "gmail"})
    )

    assert response["ok"] is True
    active = runtime.store.active_mail_account()
    assert active is not None
    assert active.account_id.startswith("gmail-")
    assert active.address == "new-owner@example.com"
    assert response["data"]["account"]["active"] is True
    legacy = runtime.store.mail_account("gmail", "gmail-default")
    assert legacy is not None
    assert legacy.active is False
    assert runtime.config.gmail_token_file.read_text(encoding="utf-8") == ("rejected legacy token")


def test_mail_account_reconnect_refuses_unverifiable_legacy_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.store.set_state("legacy-cursor")
    monkeypatch.setattr(
        engine_api.GmailGateway,
        "authorize_with_status",
        lambda *args, **kwargs: pytest.fail("OAuth must not rebind unidentified history"),
    )

    response = engine_api._response(
        request(
            config_path,
            "mail.accounts.reconnect",
            {"provider": "gmail", "account_id": "gmail-default"},
        )
    )

    assert response["error"]["code"] == "account_identity_unverified"


def test_settings_update_is_allowlisted_atomic_and_secret_free(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path, extra_settings="# preserve this\nextension_key = 7")

    response = engine_api._response(
        request(
            config_path,
            "settings.update",
            {
                "model_base_url": "http://localhost:8080/v1/",
                "model_name": " replacement-model ",
                "poll_interval_minutes": 45,
                "retention_days": 365,
                "notifications_enabled": False,
            },
        )
    )

    assert response["ok"] is True
    assert response["data"]["poll_interval_minutes"] == 45
    assert response["data"]["retention_days"] == 365
    assert response["data"]["notifications_enabled"] is False
    assert response["data"]["local_model"] == {
        "authentication_required": False,
        "editable": True,
        "endpoint": "http://localhost:8080/v1",
        "model": "replacement-model",
        "timeout_seconds": 60.0,
        "token_configured": False,
    }
    assert "# preserve this" in config_path.read_text(encoding="utf-8")
    assert "extension_key = 7" in config_path.read_text(encoding="utf-8")
    encoded = json.dumps(response)
    assert "token.json" not in encoded
    assert "send-token.json" not in encoded


def test_settings_update_applies_shortened_retention_immediately(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path, extra_settings="retention_days = 180")
    runtime = load_runtime(config_path)
    for message_id, received_at in (
        ("expired", datetime.now(UTC).replace(microsecond=0) - timedelta(days=2)),
        ("current", datetime.now(UTC).replace(microsecond=0)),
    ):
        _add_test_message(
            runtime.store,
            message_id=message_id,
            thread_id=None,
            sender="billing@example.com",
            sender_name="Billing",
            subject=message_id,
            received_at=received_at.isoformat(),
        )

    response = engine_api._response(request(config_path, "settings.update", {"retention_days": 1}))

    assert response["ok"] is True
    assert response["data"]["retention_days"] == 1
    assert [row["message_id"] for row in runtime.store.recent(10)] == ["current"]


def test_production_check_reloads_retention_after_acquiring_operation_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path, extra_settings="retention_days = 180")
    stale_runtime = load_runtime(config_path)
    stale_runtime.config.gmail_token_file.write_text("connected token", encoding="utf-8")
    stale_runtime.store.set_state("100", datetime.now(UTC))
    expired_at = (datetime.now(UTC) - timedelta(days=2)).isoformat()

    class ExpiredGmail:
        def mailbox_identity_key(self) -> str:
            return _test_mailbox_identity("gmail", "gmail-default")

        def history_message_ids(self, cursor: str):
            return ["expired"], "200"

        def changes_since(self, cursor: str) -> MailboxChanges:
            message_ids, newest = self.history_message_ids(cursor)
            return MailboxChanges(tuple(message_ids), newest)

        def metadata(self, message_id: str) -> MessageMetadata:
            return MessageMetadata(
                message_id,
                None,
                "a@example.com",
                "Trusted A",
                "Expired message",
                expired_at,
                frozenset({"INBOX"}),
            )

        def full_payload(self, message_id: str):
            raise AssertionError("expired message must not reach full-body retrieval")

    real_load_runtime = engine_api.load_runtime
    load_count = 0
    lock_entered = False

    def load_runtime_after_lock(path: Path) -> Runtime:
        nonlocal load_count
        assert lock_entered is True
        load_count += 1
        return real_load_runtime(path)

    @contextmanager
    def shorten_retention_before_lock_entry(lock_path: Path, busy_message: str):
        nonlocal lock_entered
        engine_api.update_settings(config_path, {"retention_days": 1})
        stale_runtime.store.purge(1)
        lock_entered = True
        yield

    monkeypatch.setattr(engine_api, "load_runtime", load_runtime_after_lock)
    monkeypatch.setattr(engine_api, "operation_lock", shorten_retention_before_lock_entry)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: ExpiredGmail())

    response = engine_api._response(request(config_path, "watcher.check"))

    assert response["ok"] is True
    assert response["data"]["discovered"] == 0
    assert load_count == 1
    assert load_config(config_path).retention_days == 1
    assert stale_runtime.store.recent(10) == []


@pytest.mark.parametrize(
    "operation",
    ["notifications.pending", "notifications.pending_under_host_lock"],
)
def test_notifications_pending_purges_expired_intents_before_host_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path, extra_settings="retention_days = 1")
    runtime = load_runtime(config_path)
    _add_test_message(
        runtime.store,
        message_id="expired",
        thread_id=None,
        sender="a@example.com",
        sender_name="Trusted A",
        subject="Expired message",
        received_at=(datetime.now(UTC) - timedelta(days=2)).isoformat(),
    )
    _mark_test_analyzed(
        runtime.store,
        "expired",
        {
            "category": "customer_request",
            "priority": "high",
            "summary": "Please respond.",
            "action_required": True,
            "suggested_action": "Reply.",
            "deadline_text": None,
            "deadline_iso": None,
            "confidence": 0.9,
        },
    )
    monkeypatch.setattr(engine_api, "load_runtime", lambda _path: runtime)

    count_response = None
    if operation == "notifications.pending_under_host_lock":
        lock_path = engine_api._production_check_lock_path(runtime.config)
        with engine_api.operation_lock(lock_path, "test operation busy"):
            response = engine_api._response(request(config_path, operation))
            count_response = engine_api._response(
                request(config_path, "notifications.count_under_host_lock")
            )
    else:
        response = engine_api._response(request(config_path, operation))

    assert response["ok"] is True
    assert response["data"]["items"] == []
    assert runtime.store.notification_intents() == []
    assert runtime.store.recent(10) == []
    if count_response is not None:
        assert count_response["data"] == {"count": 0}


def test_host_operation_lock_returns_configured_database_lock_path(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    config = load_config(config_path)

    response = engine_api._response(request(config_path, "host.operation_lock"))

    assert response["ok"] is True
    assert response["data"] == {
        "path": str(config.database_file.with_name(f"{config.database_file.name}.check.lock"))
    }


def test_settings_reports_gateway_model_as_read_only_and_rejects_mutation(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(
        config_path,
        extra_settings=(
            'model_backend = "gateway"\n'
            f'model_api_token_file = "{tmp_path / "model-token"}"\n'
            f'model_ca_file = "{tmp_path / "gateway-ca.pem"}"'
        ),
    )
    gateway_config = (
        config_path.read_text(encoding="utf-8")
        .replace(
            'model_base_url = "http://127.0.0.1:1234/v1"',
            'model_base_url = "https://inference.office.internal:8443"',
        )
        .replace("model_require_auth = false", "model_require_auth = true")
    )
    config_path.write_text(gateway_config, encoding="utf-8")
    original = config_path.read_bytes()

    settings = engine_api._response(request(config_path, "settings.get"))
    changed = engine_api._response(
        request(
            config_path,
            "settings.update",
            {"model_name": "operator-selected-model"},
        )
    )

    assert settings["data"]["local_model"]["editable"] is False
    assert settings["data"]["local_model"]["model"] == "Managed by inference gateway"
    assert changed["error"]["code"] == "invalid_request"
    assert "managed" in changed["error"]["message"]
    assert config_path.read_bytes() == original


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"unknown": True},
        {"poll_interval_minutes": False},
        {"retention_days": 3651},
        {"notifications_enabled": "false"},
        {"model_base_url": "https://models.example.com/v1"},
        {"model_name": "   "},
    ],
)
def test_settings_update_rejects_invalid_payload_without_mutation(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    original = config_path.read_bytes()

    response = engine_api._response(request(config_path, "settings.update", payload))

    assert response["error"]["code"] == "invalid_request"
    assert config_path.read_bytes() == original


@pytest.mark.parametrize(
    ("extra_settings", "timezone", "message"),
    [
        (
            'retention_days = "seven"',
            "America/Chicago",
            "retention_days must be an integer",
        ),
        ("", "/tmp/foo", "Unknown timezone: /tmp/foo"),
    ],
)
def test_settings_update_preserves_existing_configuration_errors(
    tmp_path: Path, extra_settings: str, timezone: str, message: str
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path, extra_settings=extra_settings, timezone=timezone)
    original = config_path.read_bytes()

    response = engine_api._response(
        request(
            config_path,
            "settings.update",
            {"poll_interval_minutes": 45},
        )
    )

    assert response["error"] == {
        "code": "configuration_error",
        "message": message,
    }
    assert config_path.read_bytes() == original


@pytest.mark.parametrize("missing_parent", [False, True])
def test_settings_update_preserves_missing_configuration_error(
    tmp_path: Path, missing_parent: bool
) -> None:
    config_path = (
        tmp_path / "absent" / "missing.toml" if missing_parent else tmp_path / "missing.toml"
    )

    response = engine_api._response(
        request(config_path, "settings.update", {"poll_interval_minutes": 45})
    )

    assert response["error"]["code"] == "configuration_error"
    assert "Configuration not found" in response["error"]["message"]
    assert not config_path.exists()


def test_permanent_analysis_failure_is_visible_and_explicitly_requeueable(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    _add_test_message(
        runtime.store,
        message_id="m1",
        thread_id=None,
        sender="a@example.com",
        sender_name=None,
        subject="Subject",
        received_at="2026-07-18T14:00:00+00:00",
    )
    runtime.store.reserve_analysis_request("m1", 20_000)
    runtime.store.record_analysis_failure(
        "m1",
        "Inference gateway error: forbidden",
        0,
        retryable=False,
        error_code="forbidden",
    )

    inbox = engine_api._response(request(config_path, "inbox.recent", {"limit": 1}))
    assert inbox["data"]["items"][0]["analysis_retryable"] is False
    assert inbox["data"]["items"][0]["analysis_error_code"] == "forbidden"

    requeued = engine_api._response(request(config_path, "analysis.requeue", {"message_id": "m1"}))
    assert requeued["data"] == {"status": "requeued"}
    assert runtime.store.pending()[0].analysis_request_id is None
    refreshed = engine_api._response(request(config_path, "inbox.recent", {"limit": 1}))
    assert refreshed["data"]["items"][0]["analysis_retryable"] is None

    duplicate = engine_api._response(request(config_path, "analysis.requeue", {"message_id": "m1"}))
    assert duplicate["error"]["code"] == "conflict"

    missing = engine_api._response(
        request(config_path, "analysis.requeue", {"message_id": "missing"})
    )
    assert missing["error"]["code"] == "not_found"


def test_watchlist_mutations_are_normalized_and_return_explicit_errors(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path, include_senders=False)

    added = engine_api._response(
        request(
            config_path,
            "watchlist.add",
            {"email": "New Person <NEW@Example.com>", "name": "  New Person  "},
        )
    )
    duplicate = engine_api._response(
        request(config_path, "watchlist.add", {"email": "new@example.com"})
    )
    missing = engine_api._response(
        request(config_path, "watchlist.remove", {"email": "missing@example.com"})
    )
    removed = engine_api._response(
        request(config_path, "watchlist.remove", {"email": "NEW@example.com"})
    )
    listed = engine_api._response(request(config_path, "watchlist.list"))

    assert added["data"]["item"] == {
        "email": "new@example.com",
        "name": "New Person",
    }
    assert duplicate["error"]["code"] == "conflict"
    assert missing["error"]["code"] == "not_found"
    assert removed["data"]["item"] == added["data"]["item"]
    assert listed["data"]["items"] == []


def test_watchlist_mutations_hold_production_operation_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path, include_senders=False)
    lock_held = False
    lock_paths: list[Path] = []
    original_add_sender = engine_api.add_sender
    original_remove_sender = engine_api.remove_sender

    @contextmanager
    def mailbox_operation_lock(lock_path: Path, busy_message: str):
        nonlocal lock_held
        assert busy_message == "Another mailbox operation is already running"
        assert lock_held is False
        lock_paths.append(lock_path)
        lock_held = True
        try:
            yield
        finally:
            lock_held = False

    def add_sender_while_locked(path: Path, email: str, name: str | None):
        assert lock_held is True
        return original_add_sender(path, email, name)

    def remove_sender_while_locked(path: Path, email: str):
        assert lock_held is True
        return original_remove_sender(path, email)

    monkeypatch.setattr(engine_api, "operation_lock_supported", lambda _path: True)
    monkeypatch.setattr(engine_api, "operation_lock", mailbox_operation_lock)
    monkeypatch.setattr(engine_api, "add_sender", add_sender_while_locked)
    monkeypatch.setattr(engine_api, "remove_sender", remove_sender_while_locked)

    added = engine_api._response(
        request(config_path, "watchlist.add", {"email": "new@example.com"})
    )
    removed = engine_api._response(
        request(config_path, "watchlist.remove", {"email": "new@example.com"})
    )

    assert added["ok"] is True
    assert removed["ok"] is True
    expected_lock_path = engine_api._production_check_lock_path(load_config(config_path))
    assert lock_paths == [expected_lock_path, expected_lock_path]
    assert lock_held is False


@pytest.mark.parametrize("operation", ["watchlist.add", "watchlist.remove"])
def test_watchlist_mutations_fail_closed_when_production_lock_is_busy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path, include_senders=False)

    @contextmanager
    def busy_operation_lock(_lock_path: Path, _busy_message: str):
        raise engine_api.OperationLockBusy("mailbox operation is busy")
        yield

    monkeypatch.setattr(engine_api, "operation_lock_supported", lambda _path: True)
    monkeypatch.setattr(engine_api, "operation_lock", busy_operation_lock)
    response = engine_api._response(
        request(config_path, operation, {"email": "new@example.com"})
    )

    assert response["error"] == {
        "code": "mailbox_busy",
        "message": "mailbox operation is busy",
        "retryable": True,
    }
    assert load_config(config_path).senders == ()


def test_attachment_export_uses_inactive_source_account_and_safe_private_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.config.gmail_token_file.write_text("source token", encoding="utf-8")
    active_account = runtime.store.register_mail_account(
        "gmail",
        f"gmail-{'a' * 32}",
        display_name="Gmail",
        address="active@example.com",
        active=True,
    )
    active_token = mail_account_token_file(runtime.config, active_account)
    active_token.parent.mkdir(parents=True)
    active_token.write_text("active token", encoding="utf-8")
    local_message_id = scoped_message_id("gmail", "gmail-default", "provider-message")
    _add_test_message(
        runtime.store,
        message_id=local_message_id,
        provider="gmail",
        account_id="gmail-default",
        provider_message_id="provider-message",
        thread_id=None,
        sender="a@example.com",
        sender_name=None,
        subject="Attachment",
        received_at="2026-07-18T14:00:00+00:00",
    )
    runtime.store.replace_attachments(
        local_message_id,
        (
            AttachmentDescriptor(
                "", "gmail-attachment", "../../private.PDF", "application/pdf", 4, 0
            ),
        ),
    )
    destination = tmp_path / "exports"
    destination.mkdir()

    class FakeAttachmentGmail:
        def mailbox_identity_key(self) -> str:
            return _test_mailbox_identity("gmail", "gmail-default")

        def attachment_bytes(
            self, message_id: str, part_id: str, attachment_id: str | None
        ) -> bytes:
            assert (message_id, part_id, attachment_id) == (
                "provider-message",
                "",
                "gmail-attachment",
            )
            return b"%PDF"

    opened_tokens: list[Path] = []

    monkeypatch.setattr(
        engine_api.GmailGateway,
        "from_token",
        lambda _credentials, token: opened_tokens.append(token) or FakeAttachmentGmail(),
    )
    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)

    response = engine_api._response(
        request(
            config_path,
            "attachment.export",
            {
                "message_id": local_message_id,
                "part_id": "",
                "destination_dir": str(destination),
            },
        )
    )

    assert response["ok"] is True
    assert runtime.store.active_mail_account().account_id == active_account.account_id
    assert opened_tokens == [runtime.config.gmail_token_file]
    exported = Path(response["data"]["path"])
    assert exported.parent == destination.resolve()
    assert exported.name.startswith("email-watcher-attachment-")
    assert exported.name != "private.PDF"
    assert exported.suffix == ".pdf"
    assert exported.read_bytes() == b"%PDF"
    assert exported.stat().st_mode & 0o777 == 0o600
    assert response["data"] == {
        "byte_size": 4,
        "filename": "../../private.PDF",
        "media_type": "application/pdf",
        "path": str(exported),
    }


def test_attachment_export_uses_downloaded_size_for_imap_provider_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    account = runtime.store.register_mail_account(
        "imap",
        f"imap-{'a' * 32}",
        display_name="Other mail server",
        address="owner@example.com",
        active=True,
    )
    mailbox_identity_key = "a" * 64
    runtime.store.reconcile_mailbox_identity(
        account.provider,
        account.account_id,
        mailbox_identity_key,
        legacy_status="replacement",
    )
    credentials_file = mail_account_token_file(runtime.config, account)
    credentials_file.parent.mkdir(parents=True)
    credentials_file.write_text("private credentials", encoding="utf-8")
    local_message_id = scoped_message_id("imap", account.account_id, "provider-message")
    runtime.store.add_message(
        message_id=local_message_id,
        provider="imap",
        account_id=account.account_id,
        provider_message_id="provider-message",
        thread_id=None,
        sender="a@example.com",
        sender_name=None,
        subject="Attachment",
        received_at="2026-09-12T14:00:00+00:00",
        mailbox_identity_key=mailbox_identity_key,
        admission=AdmissionProvenance(
            kind="exact_sender",
            selector_id="sender:a@example.com",
            display_name=None,
            mailbox_identity_key=mailbox_identity_key,
            admitted_at="2026-09-12T14:00:00+00:00",
        ),
    )
    runtime.store.replace_attachments(
        local_message_id,
        (AttachmentDescriptor("mime-0", None, "invoice.pdf", "application/pdf", 198, 0),),
    )
    destination = tmp_path / "exports"
    destination.mkdir()

    class FakeAttachmentImap:
        def __init__(self) -> None:
            self.session_active = False

        @contextmanager
        def polling_session(self):
            assert self.session_active is False
            self.session_active = True
            try:
                yield
            finally:
                self.session_active = False

        def mailbox_identity_key(self) -> str:
            assert self.session_active, "identity read outside IMAP session"
            return mailbox_identity_key

        def attachment_bytes(
            self, message_id: str, part_id: str, attachment_id: str | None
        ) -> bytes:
            assert self.session_active, "attachment read outside IMAP session"
            assert (message_id, part_id, attachment_id) == (
                "provider-message",
                "mime-0",
                None,
            )
            return b"%PDF"

    monkeypatch.setattr(
        engine_api.ImapGateway,
        "from_credentials_file",
        lambda path: FakeAttachmentImap() if path == credentials_file else pytest.fail(path),
    )
    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)

    response = engine_api._response(
        request(
            config_path,
            "attachment.export",
            {
                "message_id": local_message_id,
                "part_id": "mime-0",
                "destination_dir": str(destination),
            },
        )
    )

    assert response["ok"] is True
    assert response["data"]["byte_size"] == 4
    assert Path(response["data"]["path"]).read_bytes() == b"%PDF"


def test_attachment_export_rejects_an_unconfigured_account_before_provider_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    local_message_id = scoped_message_id("microsoft365", "account-2", "provider-message")
    _add_test_message(
        runtime.store,
        message_id=local_message_id,
        provider="microsoft365",
        account_id="account-2",
        provider_message_id="provider-message",
        thread_id=None,
        sender="a@example.com",
        sender_name=None,
        subject="Attachment",
        received_at="2026-07-18T14:00:00+00:00",
    )
    runtime.store.replace_attachments(
        local_message_id,
        (AttachmentDescriptor("2", "attachment", "file.pdf", "application/pdf", 4, 0),),
    )
    destination = tmp_path / "exports"
    destination.mkdir()
    monkeypatch.setattr(engine_api, "load_runtime", lambda _path: runtime)
    monkeypatch.setattr(
        engine_api,
        "load_mailbox_account",
        lambda *_args: pytest.fail("An unavailable account must not be opened"),
    )

    response = engine_api._response(
        request(
            config_path,
            "attachment.export",
            {
                "message_id": local_message_id,
                "part_id": "2",
                "destination_dir": str(destination),
            },
        )
    )

    assert response["error"]["code"] == "account_unavailable"
    assert list(destination.iterdir()) == []


@pytest.mark.parametrize(
    ("operation", "operation_payload"),
    [
        (
            "connect.attachment.capabilities",
            {"message_id": "MESSAGE_ID", "part_id": "2"},
        ),
        (
            "connect.attachment.invoke",
            {
                "request_id": "11111111-1111-4111-8111-111111111111",
                "message_id": "MESSAGE_ID",
                "part_id": "2",
                "provider": {
                    "app_id": "provider",
                    "version": "1.0.0",
                    "instance_id": "provider-instance",
                },
                "capability": {"id": "document.summarize", "version": "1.0"},
                "parameters": {},
                "confirmed": False,
            },
        ),
        (
            "connect.attachment.summarize",
            {"message_id": "MESSAGE_ID", "part_id": "2"},
        ),
    ],
)
def test_connect_paths_reject_an_unconfigured_account_before_provider_interaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    operation_payload: dict[str, object],
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    local_message_id = scoped_message_id("microsoft365", "account-2", "provider-message")
    _add_test_message(
        runtime.store,
        message_id=local_message_id,
        provider="microsoft365",
        account_id="account-2",
        provider_message_id="provider-message",
        thread_id=None,
        sender="a@example.com",
        sender_name=None,
        subject="Attachment",
        received_at="2026-07-18T14:00:00+00:00",
    )
    runtime.store.replace_attachments(
        local_message_id,
        (AttachmentDescriptor("2", "attachment", "file.pdf", "application/pdf", 4, 0),),
    )
    monkeypatch.setattr(engine_api, "load_runtime", lambda _path: runtime)

    def reject_provider_interaction(*_args: object, **_kwargs: object) -> None:
        pytest.fail("An unavailable mailbox must be rejected before provider interaction")

    monkeypatch.setattr(engine_api, "load_mailbox_account", reject_provider_interaction)
    monkeypatch.setattr(engine_api.connect, "discover_capabilities", reject_provider_interaction)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_summary_capability",
        reject_provider_interaction,
    )
    monkeypatch.setattr(
        engine_api.connect,
        "require_connect_entitlement",
        reject_provider_interaction,
    )
    payload = {
        key: local_message_id if value == "MESSAGE_ID" else value
        for key, value in operation_payload.items()
    }

    response = engine_api._response(request(config_path, operation, payload))

    assert response["error"]["code"] == "account_unavailable"


def test_attachment_export_fails_closed_before_gmail_or_file_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    destination = tmp_path / "exports"
    destination.mkdir()
    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.GmailGateway,
        "from_token",
        lambda *args: (_ for _ in ()).throw(AssertionError("Gmail must not be called")),
    )

    missing = engine_api._response(
        request(
            config_path,
            "attachment.export",
            {
                "message_id": "missing",
                "part_id": "",
                "destination_dir": str(destination),
            },
        )
    )
    relative = engine_api._response(
        request(
            config_path,
            "attachment.export",
            {"message_id": "missing", "part_id": "", "destination_dir": "relative"},
        )
    )

    assert missing["error"]["code"] == "not_found"
    assert relative["error"]["code"] == "invalid_request"
    assert list(destination.iterdir()) == []


def test_attachment_export_reports_provider_neutral_byte_count_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    runtime.config.gmail_token_file.write_text("connected token", encoding="utf-8")
    _add_test_message(
        runtime.store,
        message_id="m1",
        thread_id=None,
        sender="a@example.com",
        sender_name=None,
        subject="Attachment",
        received_at="2026-07-18T14:00:00+00:00",
    )
    runtime.store.replace_attachments(
        "m1",
        (AttachmentDescriptor("2", "attachment", "file.pdf", "application/pdf", 0, 0),),
    )
    destination = tmp_path / "exports"
    destination.mkdir()

    class TruncatedAttachmentGmail:
        def mailbox_identity_key(self) -> str:
            return _test_mailbox_identity("gmail", "gmail-default")

        def attachment_bytes(self, *args) -> bytes:
            return b"bad"

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.GmailGateway,
        "from_token",
        lambda *args: TruncatedAttachmentGmail(),
    )

    response = engine_api._response(
        request(
            config_path,
            "attachment.export",
            {"message_id": "m1", "part_id": "2", "destination_dir": str(destination)},
        )
    )

    assert response["error"] == {
        "code": "mailbox_error",
        "message": "Email provider operation failed; see stderr for details",
    }
    assert list(destination.iterdir()) == []


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"email": 42},
        {"email": "invalid"},
        {"email": "valid@example.com", "name": 42},
        {"email": "valid@example.com", "name": "Bad\nName"},
        {"email": "valid@example.com", "extra": True},
    ],
)
def test_watchlist_add_rejects_invalid_payloads(tmp_path: Path, payload: dict[str, object]) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path, include_senders=False)

    response = engine_api._response(request(config_path, "watchlist.add", payload))

    assert response["error"]["code"] == "invalid_request"


@pytest.mark.parametrize(
    "sender_config",
    [
        '[[senders]]\nemail = "invalid"\n',
        (
            '[[senders]]\nemail = "duplicate@example.com"\n'
            '[[senders]]\nemail = "DUPLICATE@example.com"\n'
        ),
    ],
)
def test_watchlist_mutation_preserves_existing_configuration_errors(
    tmp_path: Path, sender_config: str
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path, include_senders=False)
    config_path.write_text(
        f"{config_path.read_text(encoding='utf-8')}\n{sender_config}",
        encoding="utf-8",
    )

    added = engine_api._response(
        request(config_path, "watchlist.add", {"email": "valid@example.com"})
    )
    removed = engine_api._response(
        request(config_path, "watchlist.remove", {"email": "valid@example.com"})
    )

    assert added["error"]["code"] == "configuration_error"
    assert removed["error"]["code"] == "configuration_error"


def test_zero_sender_check_is_inactive_without_gmail_and_uses_operation_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path, include_senders=False)
    runtime = load_runtime(config_path)
    _add_test_message(
        runtime.store,
        message_id="queued",
        thread_id=None,
        sender="former@example.com",
        sender_name="Former",
        subject="Queued before removal",
        received_at="2026-07-18T14:00:00+00:00",
    )
    _mark_test_analyzed(runtime.store, "queued", FakeModel().analyze().model_dump())
    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.GmailGateway,
        "from_token",
        lambda *args: (_ for _ in ()).throw(AssertionError("Gmail must not be called")),
    )
    lock_paths: list[Path] = []

    @contextmanager
    def acquired_lock(lock_path: Path, _busy_message: str):
        lock_paths.append(lock_path)
        yield

    monkeypatch.setattr(engine_api, "operation_lock_supported", lambda _path: True)
    monkeypatch.setattr(engine_api, "operation_lock", acquired_lock)

    response = engine_api._response(request(config_path, "watcher.check"))

    assert response["data"] == {
        "active": False,
        "automation_processed": 0,
        "automation_review_required": 0,
        "discovered": 0,
        "fallback_notified": 0,
        "pending_notifications": 1,
        "purged": 0,
        "stale_cursor_recovered": False,
        "summarized": 0,
    }
    assert lock_paths == [engine_api._production_check_lock_path(runtime.config)]


def test_zero_sender_check_still_rejects_incompatible_host_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(
        config_path,
        include_senders=False,
        ntfy_topic="configured-private-topic",
    )
    runtime = load_runtime(config_path)
    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.GmailGateway,
        "from_token",
        lambda *args: (_ for _ in ()).throw(AssertionError("Gmail must not be called")),
    )

    response = engine_api._response(request(config_path, "watcher.check"))

    assert response["ok"] is False
    assert response["error"]["code"] == "unsupported_configuration"


class FakeGmail:
    def set_operation_timeout(self, timeout_seconds: float) -> None:
        pass

    def mailbox_identity_key(self) -> str:
        return _test_mailbox_identity("gmail", "gmail-default")

    def history_message_ids(self, cursor: str):
        return ["m1"], "200"

    def changes_since(self, cursor: str) -> MailboxChanges:
        message_ids, newest = self.history_message_ids(cursor)
        return MailboxChanges(tuple(message_ids), newest)

    def metadata(self, message_id: str) -> MessageMetadata:
        return MessageMetadata(
            message_id,
            None,
            "a@example.com",
            "Untrusted Header Name",
            "Action needed",
            "2026-07-18T14:00:00+00:00",
            frozenset({"INBOX"}),
        )

    def full_payload(self, message_id: str):
        return {"mimeType": "text/plain", "body": {"data": "SGVsbG8="}}

    def content(self, message_id: str, body_char_limit: int) -> MessageContent:
        body, attachment_names, attachments = extract_body(
            self.full_payload(message_id), body_char_limit
        )
        return MessageContent(body, attachment_names, attachments)


class FakeModel:
    def health(self) -> tuple[bool, str]:
        return True, "HTTP 200"

    def analyze(self, **kwargs) -> Analysis:
        return Analysis(
            category="customer_request",
            priority="high",
            summary="Please respond.",
            action_required=True,
            suggested_action="Reply.",
            deadline_text=None,
            deadline_iso=None,
            confidence=0.9,
        )


def test_check_defers_delivery_until_state_checked_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    loaded = load_runtime(config_path)
    loaded.store.set_state("100", datetime(2026, 7, 18, tzinfo=UTC))
    loaded.config.gmail_token_file.write_text("connected token", encoding="utf-8")
    runtime = Runtime(config=loaded.config, store=loaded.store, model=FakeModel())
    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: FakeGmail())

    checked = engine_api._response(request(config_path, "watcher.check"))
    assert checked["ok"] is True
    assert checked["data"]["summarized"] == 1
    assert checked["data"]["pending_notifications"] == 1
    assert loaded.store.recent(1)[0]["status"] == "analyzed"

    pending = engine_api._response(request(config_path, "notifications.pending"))
    intent = pending["data"]["items"][0]
    assert intent["kind"] == "analysis"
    assert intent["body"] == "Please respond.\nNext: Reply."
    assert intent["title"] == "Trusted A: Action needed"

    stale = engine_api._response(
        request(
            config_path,
            "notifications.ack",
            {"message_id": intent["message_id"], "kind": "analysis", "analysis_at": "stale"},
        )
    )
    assert stale["error"]["code"] == "stale_notification"
    assert loaded.store.recent(1)[0]["status"] == "analyzed"

    acknowledged = engine_api._response(
        request(
            config_path,
            "notifications.ack",
            {
                "message_id": intent["message_id"],
                "kind": intent["kind"],
                "analysis_at": intent["analysis_at"],
            },
        )
    )
    assert acknowledged["data"]["status"] == "acknowledged"
    assert loaded.store.recent(1)[0]["status"] == "summarized"

    duplicate = engine_api._response(
        request(
            config_path,
            "notifications.ack",
            {
                "message_id": intent["message_id"],
                "kind": intent["kind"],
                "analysis_at": intent["analysis_at"],
            },
        )
    )
    assert duplicate["data"]["status"] == "already_acknowledged"


def test_check_maps_unresolved_legacy_recovery_to_retryable_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api,
        "run_watcher_check",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            engine_api.LegacyMailboxIdentityUnverified("legacy identity remains unresolved")
        ),
    )

    response = engine_api._response(request(config_path, "watcher.check"))

    assert response["error"] == {
        "code": "legacy_mailbox_identity_unverified",
        "message": "legacy identity remains unresolved",
        "retryable": True,
    }


def test_check_maps_mailbox_identity_change_to_domain_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api,
        "run_watcher_check",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            engine_api.MailboxIdentityChanged("mailbox identity changed")
        ),
    )

    response = engine_api._response(request(config_path, "watcher.check", {"dry_run": True}))

    assert response["error"] == {
        "code": "mailbox_identity_changed",
        "message": "mailbox identity changed",
    }


def test_check_preserves_gmail_recovery_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    monkeypatch.setattr(engine_api, "load_runtime", lambda _path: runtime)
    monkeypatch.setattr(
        engine_api,
        "run_watcher_check",
        lambda *args, **kwargs: {
            "active": True,
            "discovered": 2,
            "summarized": 1,
            "fallback_notified": 0,
            "purged": 0,
            "stale_cursor_recovered": True,
            "recovery_pending": True,
            "recovery_state": "backoff",
            "recovery_failure_code": "gmail_recovery_page_invalid",
            "recovery_next_retry_at": "2026-09-20T03:00:00+00:00",
        },
    )

    response = engine_api._response(request(config_path, "watcher.check"))

    assert response["ok"] is True
    assert response["data"] == {
        "active": True,
        "discovered": 2,
        "summarized": 1,
        "fallback_notified": 0,
        "purged": 0,
        "stale_cursor_recovered": True,
        "recovery_pending": True,
        "recovery_state": "backoff",
        "recovery_failure_code": "gmail_recovery_page_invalid",
        "recovery_next_retry_at": "2026-09-20T03:00:00+00:00",
        "pending_notifications": 0,
    }


@pytest.mark.parametrize(
    ("sender", "expected_messages", "expected_fires"),
    [
        ("a@example.com", 1, 1),
        ("not-allowed@example.com", 0, 0),
    ],
)
def test_rule_put_then_real_watcher_check_dispatches_matching_connect_fire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sender: str,
    expected_messages: int,
    expected_fires: int,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    loaded = load_runtime(config_path)
    loaded.store.set_state("100", datetime(2026, 7, 18, tzinfo=UTC))
    loaded.config.gmail_token_file.write_text("connected token", encoding="utf-8")
    runtime = Runtime(config=loaded.config, store=loaded.store, model=FakeModel())

    class InvoiceGmail(FakeGmail):
        def metadata(self, message_id: str) -> MessageMetadata:
            return MessageMetadata(
                message_id,
                None,
                sender,
                "Invoice Sender",
                "Invoice attached",
                "2026-07-18T14:00:00+00:00",
                frozenset({"INBOX"}),
            )

        def full_payload(self, message_id: str) -> dict[str, object]:
            return {
                "mimeType": "multipart/mixed",
                "parts": [
                    {
                        "mimeType": "application/pdf",
                        "partId": "2",
                        "filename": "invoice.pdf",
                        "body": {"attachmentId": "attachment-2", "size": 42},
                    }
                ],
            }

        def attachment_bytes(self, *args: object) -> bytes:
            return b"x" * 42

    selected = connect.DiscoveredCapability(
        protocol_version=2,
        base_url="http://127.0.0.1:32123/",
        token="A" * 43,
        app_id="document-summarizer",
        app_name="Document Summarizer",
        app_version="0.1.0",
        instance_id="11111111-1111-4111-8111-111111111111",
        capability_id="document.summarize",
        capability_version="1.0",
        action_label="Summarize",
        action_description="Summarize this document locally.",
        accepts=(connect.AcceptedArtifactType("application/pdf", 1024),),
        produces=("application/vnd.local-connect.cited-summary+json",),
        parameters=(
            connect.CapabilityParameter(
                name="mode",
                value_type="string",
                required=False,
                label="Summary mode",
                description="Choose general, story, or contract. Defaults to general.",
            ),
        ),
        external_effects=False,
        confirmation_required=False,
    )
    definition = {
        "name": "Contract watch",
        "scope": {},
        "trigger": {"source_kind": "mail.message"},
        "conditions": [
            {
                "field": "attachment.media_type",
                "op": "equals",
                "value": "application/pdf",
            }
        ],
        "action": {
            "kind": "connect.invoke",
            "capability": {"id": "document.summarize", "version": "1.0"},
            "provider": {
                "app_id": selected.app_id,
                "version": selected.app_version,
                "instance_id": selected.instance_id,
            },
            "parameters": {"mode": "contract"},
        },
        "confirm_each": False,
    }

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: InvoiceGmail())
    monkeypatch.setattr(engine_api, "_automation_entitlement_active", lambda: True)
    monkeypatch.setattr(engine_api.connect, "require_connect_entitlement", lambda: None)
    monkeypatch.setattr(
        engine_api.connect,
        "discover_capabilities",
        lambda **kwargs: connect.CapabilityCatalog((selected,)),
    )
    monkeypatch.setattr(
        engine_api,
        "_pump_generic_connect_lane",
        lambda active_runtime, head: engine_api._connect_queue_item(
            active_runtime, head.job_id, "queued"
        ),
    )

    created = engine_api._response(
        request(
            config_path,
            "automation.rules.put",
            {"definition": definition},
        )
    )
    checked = engine_api._response(request(config_path, "watcher.check"))

    assert created["ok"] is True
    assert checked["ok"] is True
    assert checked["data"]["summarized"] == expected_messages
    with loaded.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == expected_messages
        assert db.execute("SELECT COUNT(*) FROM automation_fires").fetchone()[0] == expected_fires
        assert (
            db.execute("SELECT COUNT(*) FROM automation_fire_attempts").fetchone()[0]
            == expected_fires
        )
        assert (
            db.execute("SELECT COUNT(*) FROM connect_attachment_jobs").fetchone()[0]
            == expected_fires
        )


def test_host_notification_contract_delivers_and_state_checks_automation_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    account_id = f"microsoft365-{'e' * 32}"
    runtime.store.register_mail_account(
        "microsoft365",
        account_id,
        display_name="Microsoft 365",
        address="owner@example.com",
        active=False,
    )
    message_id = scoped_message_id("microsoft365", account_id, "schedule-1")
    _add_test_message(
        runtime.store,
        message_id=message_id,
        provider="microsoft365",
        account_id=account_id,
        provider_message_id="schedule-1",
        thread_id=None,
        sender="a@example.com",
        sender_name="Trusted A",
        subject="Meeting request",
        received_at=datetime.now(UTC).isoformat(),
    )
    _mark_test_analyzed(
        runtime.store,
        message_id,
        {
            "category": "scheduling",
            "priority": "normal",
            "summary": "A meeting was requested.",
            "action_required": True,
            "suggested_action": "Review the request.",
            "deadline_text": None,
            "deadline_iso": None,
            "confidence": 0.9,
        },
        scheduling_automation_principal_key="a" * 64,
    )
    runtime.store.mark_delivery_complete(message_id, notified=True)
    run = runtime.store.automation_run_for_message(message_id)
    assert run is not None
    current = runtime.store.transition_automation_to_review(
        run.run_id,
        run.state_version,
        next_state="manual_review",
        failure_code="source_invalid",
    )
    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)

    pending = engine_api._response(request(config_path, "notifications.pending"))
    intent = pending["data"]["items"][0]

    assert intent == {
        "analysis_at": current.updated_at,
        "body": "The scheduling source could not be processed safely.",
        "kind": "automation_review",
        "message_id": run.run_id,
        "priority": "normal",
        "revision": str(current.state_version),
        "subject_id": run.run_id,
        "subject_type": "automation_run",
        "title": "Trusted A: Meeting request",
    }

    acknowledgement_payload = {
        key: intent[key]
        for key in (
            "analysis_at",
            "kind",
            "message_id",
            "revision",
            "subject_id",
            "subject_type",
        )
    }
    stale = engine_api._response(
        request(
            config_path,
            "notifications.ack",
            {**acknowledgement_payload, "revision": str(current.state_version + 1)},
        )
    )
    assert stale["error"]["code"] == "stale_notification"

    acknowledged = engine_api._response(
        request(config_path, "notifications.ack", acknowledgement_payload)
    )
    duplicate = engine_api._response(
        request(config_path, "notifications.ack", acknowledgement_payload)
    )

    assert acknowledged["data"]["status"] == "acknowledged"
    assert duplicate["data"]["status"] == "already_acknowledged"


def test_check_rejects_ntfy_before_gmail_or_state_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path, ntfy_topic="configured-private-topic")
    loaded = load_runtime(config_path)
    loaded.store.set_state("100", datetime(2026, 7, 18, tzinfo=UTC))
    runtime = Runtime(config=loaded.config, store=loaded.store, model=FakeModel())
    _add_test_message(
        loaded.store,
        message_id="queued",
        thread_id=None,
        sender="a@example.com",
        sender_name=None,
        subject="Queued notification",
        received_at="2026-07-18T14:00:00+00:00",
    )
    loaded.store.record_failure("queued", "local model unavailable", 0)
    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api.GmailGateway,
        "from_token",
        lambda *args: (_ for _ in ()).throw(AssertionError("Gmail must not be called")),
    )

    health = engine_api._response(request(config_path, "health.get"))
    settings = engine_api._response(request(config_path, "settings.get"))
    assert health["data"]["notifications"] == {
        "delivery": "host",
        "enabled": True,
        "host_delivery_ready": False,
        "ntfy_configured": True,
    }
    assert settings["data"]["polling_supported"] is False

    checked = engine_api._response(request(config_path, "watcher.check"))
    pending = engine_api._response(request(config_path, "notifications.pending"))
    acknowledged = engine_api._response(
        request(
            config_path,
            "notifications.ack",
            {"message_id": "queued", "kind": "fallback", "analysis_at": None},
        )
    )

    assert checked["ok"] is False
    assert checked["error"]["code"] == "unsupported_configuration"
    assert pending["error"]["code"] == "unsupported_configuration"
    assert acknowledged["error"]["code"] == "unsupported_configuration"
    assert loaded.store.state()[0] == "100"
    assert loaded.store.notification_intents()[0].message_id == "queued"


def test_unsupported_platform_is_reported_before_production_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    loaded = load_runtime(config_path)
    loaded.store.set_state("100", datetime(2026, 7, 18, tzinfo=UTC))
    runtime = Runtime(config=loaded.config, store=loaded.store, model=FakeModel())
    gmail_calls = 0

    def gmail_from_token(*args):
        nonlocal gmail_calls
        gmail_calls += 1
        return FakeGmail()

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    checked_lock_paths: list[Path] = []

    def operation_lock_unsupported(lock_path: Path) -> bool:
        checked_lock_paths.append(lock_path)
        return False

    monkeypatch.setattr(engine_api, "operation_lock_supported", operation_lock_unsupported)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", gmail_from_token)

    health = engine_api._response(request(config_path, "health.get"))
    settings = engine_api._response(request(config_path, "settings.get"))
    checked = engine_api._response(request(config_path, "watcher.check"))

    assert health["data"]["production_check_supported"] is False
    assert health["data"]["notifications"]["host_delivery_ready"] is False
    assert settings["data"]["polling_supported"] is False
    assert checked["error"]["code"] == "unsupported_platform"
    expected_lock_path = loaded.config.database_file.with_name(
        f"{loaded.config.database_file.name}.check.lock"
    )
    assert checked_lock_paths == [
        expected_lock_path,
        expected_lock_path,
        expected_lock_path,
    ]
    assert gmail_calls == 0
    assert loaded.store.state()[0] == "100"


def test_disabled_notifications_hide_analysis_and_fallback_intents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path, notifications_enabled=False)
    runtime = load_runtime(config_path)
    runtime.config.gmail_token_file.write_text("connected token", encoding="utf-8")
    _add_test_message(
        runtime.store,
        message_id="m1",
        thread_id=None,
        sender="a@example.com",
        sender_name="Untrusted Header Name",
        subject="Action needed",
        received_at="2026-07-18T14:00:00+00:00",
    )
    runtime.store.record_failure("m1", "local model unavailable", 0)
    _add_test_message(
        runtime.store,
        message_id="m2",
        thread_id=None,
        sender="a@example.com",
        sender_name="Untrusted Header Name",
        subject="Analyzed action",
        received_at="2026-07-18T15:00:00+00:00",
    )
    _mark_test_analyzed(
        runtime.store,
        "m2",
        {
            "category": "customer_request",
            "priority": "high",
            "summary": "Please respond.",
            "action_required": True,
            "suggested_action": "Reply.",
            "deadline_text": None,
            "deadline_iso": None,
            "confidence": 0.9,
        },
    )
    runtime.store.set_state("100", datetime(2026, 7, 18, tzinfo=UTC))
    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: FakeGmail())

    pending = engine_api._response(request(config_path, "notifications.pending"))
    checked = engine_api._response(request(config_path, "watcher.check", {"dry_run": True}))

    assert pending["data"]["items"] == []
    assert checked["data"]["pending_notifications"] == 0
    assert {row["status"] for row in runtime.store.recent(10)} == {
        "pending",
        "analyzed",
    }


def test_check_reports_complete_notification_backlog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    loaded = load_runtime(config_path)
    loaded.config.gmail_token_file.write_text("connected token", encoding="utf-8")
    loaded.store.set_state("100", datetime(2026, 7, 18, tzinfo=UTC))
    _bind_test_mailbox(loaded.store, "gmail", "gmail-default")
    runtime = Runtime(config=loaded.config, store=loaded.store, model=FakeModel())
    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", lambda *args: FakeGmail())
    monkeypatch.setattr(runtime.store, "notification_intent_count", lambda: 501)

    checked = engine_api._response(request(config_path, "watcher.check", {"dry_run": True}))

    assert checked["data"]["pending_notifications"] == 501


def test_protocol_rejects_unknown_top_level_fields_before_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    loaded = load_runtime(config_path)
    loaded.store.set_state("100", datetime(2026, 7, 18, tzinfo=UTC))
    runtime = Runtime(config=loaded.config, store=loaded.store, model=FakeModel())
    gmail_calls = 0

    def gmail_from_token(*args):
        nonlocal gmail_calls
        gmail_calls += 1
        return FakeGmail()

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", gmail_from_token)

    for field, value in (("dry_run", True), ("paylod", {"dry_run": True})):
        invalid = request(config_path, "watcher.check")
        invalid[field] = value
        response = engine_api._response(invalid)
        assert response["error"]["code"] == "invalid_request"
        assert field in response["error"]["message"]

    assert gmail_calls == 0
    assert loaded.store.state()[0] == "100"


def test_gmail_error_response_redacts_configured_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    loaded = load_runtime(config_path)
    loaded.config.gmail_token_file.write_text("connected token", encoding="utf-8")
    loaded.store.set_state("100", datetime(2026, 7, 18, tzinfo=UTC))
    runtime = Runtime(config=loaded.config, store=loaded.store, model=FakeModel())
    sensitive_path = tmp_path / "private-token.json"

    def fail_from_token(*args):
        raise GmailError(f"Invalid OAuth token file: {sensitive_path}")

    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(engine_api.GmailGateway, "from_token", fail_from_token)

    with caplog.at_level(logging.WARNING, logger=engine_api.__name__):
        response = engine_api._response(request(config_path, "watcher.check"))

    assert response["error"] == {
        "code": "gmail_error",
        "message": "Gmail operation failed; see stderr for details",
    }
    assert str(sensitive_path) not in json.dumps(response)
    assert str(sensitive_path) in caplog.text


@pytest.mark.parametrize(
    ("error", "code", "retryable"),
    [
        (
            GmailLabelCatalogInvalid("malformed label payload"),
            "gmail_label_catalog_invalid",
            False,
        ),
        (
            GmailLabelCatalogUnavailable("provider offline"),
            "gmail_label_catalog_unavailable",
            True,
        ),
        (
            GmailRecoveryPageInvalid("malformed recovery page"),
            "gmail_recovery_page_invalid",
            True,
        ),
        (
            GmailRecoveryPageTokenInvalid("rejected page token"),
            "gmail_recovery_page_token_invalid",
            True,
        ),
        (
            GmailLabelStoreError("gmail_recovery_snapshot_too_large"),
            "gmail_recovery_snapshot_too_large",
            False,
        ),
        (
            GmailLabelStoreError("gmail_recovery_counter_overflow"),
            "gmail_recovery_counter_overflow",
            False,
        ),
    ],
)
def test_visible_gmail_integrity_errors_preserve_stable_codes(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    code: str,
    retryable: bool,
) -> None:
    def fail_dispatch(_request: object) -> dict[str, object]:
        raise error

    monkeypatch.setattr(engine_api, "dispatch", fail_dispatch)

    response = engine_api._response(
        {"protocol": 1, "operation": "watcher.check", "payload": {}}
    )

    assert response["error"]["code"] == code
    assert response["error"]["retryable"] is retryable
    assert str(error) not in response["error"]["message"]


@pytest.mark.parametrize(
    ("extra_settings", "timezone", "message"),
    [
        (
            'retention_days = "seven"',
            "America/Chicago",
            "retention_days must be an integer",
        ),
        ("", "/tmp/foo", "Unknown timezone: /tmp/foo"),
    ],
)
def test_malformed_setting_returns_configuration_error(
    tmp_path: Path, extra_settings: str, timezone: str, message: str
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(
        config_path,
        extra_settings=extra_settings,
        timezone=timezone,
    )

    response = engine_api._response(request(config_path, "settings.get"))

    assert response["error"] == {
        "code": "configuration_error",
        "message": message,
    }


def test_protocol_rejects_unknown_payload_fields(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    response = engine_api._response(
        request(config_path, "watchlist.list", {"secret": "should-not-be-accepted"})
    )
    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


def test_protocol_rejects_boolean_version(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    invalid = request(config_path, "watchlist.list")
    invalid["protocol"] = True
    response = engine_api._response(invalid)
    assert response["ok"] is False
    assert response["error"]["code"] == "unsupported_protocol"


def test_main_emits_one_json_error_for_invalid_input(monkeypatch, capsys) -> None:
    monkeypatch.setattr(engine_api.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"{")))
    with pytest.raises(SystemExit) as exit_info:
        engine_api.main()
    assert exit_info.value.code == 2
    output = capsys.readouterr()
    assert output.err == ""
    assert json.loads(output.out) == {
        "error": {"code": "invalid_json", "message": "Request must be valid JSON"},
        "ok": False,
        "operation": None,
        "protocol": 1,
    }


def test_main_writes_one_utf8_envelope_without_text_encoding_or_newline_translation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LegacyStdout:
        encoding = "cp1252"
        buffer = io.BytesIO()

        @staticmethod
        def write(value: str) -> int:
            pytest.fail(f"Engine response used text stdout: {value!r}")

    stdout = LegacyStdout()
    response = {
        "data": {"subject": "Planning 🗓️"},
        "ok": True,
        "operation": "calendar.read.events",
        "protocol": 1,
    }
    monkeypatch.setattr(engine_api.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"{}")))
    monkeypatch.setattr(engine_api.sys, "stdout", stdout)
    monkeypatch.setattr(engine_api, "_response", lambda request: response)

    with pytest.raises(SystemExit) as exit_info:
        engine_api.main()

    assert exit_info.value.code == 0
    assert stdout.buffer.getvalue() == (
        json.dumps(response, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
        + b"\n"
    )


@pytest.mark.parametrize("parse_error", [ValueError("integer too long"), RecursionError()])
def test_main_converts_bounded_json_parse_failures_to_one_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    parse_error: Exception,
) -> None:
    decode = json.loads

    def fail_parse(raw: bytes, **kwargs: object):
        raise parse_error

    monkeypatch.setattr(engine_api.json, "loads", fail_parse)
    monkeypatch.setattr(engine_api.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"{}")))

    with pytest.raises(SystemExit) as exit_info:
        engine_api.main()

    assert exit_info.value.code == 2
    output = capsys.readouterr()
    assert output.err == ""
    assert decode(output.out) == {
        "error": {"code": "invalid_json", "message": "Request must be valid JSON"},
        "ok": False,
        "operation": None,
        "protocol": 1,
    }


def _automation_definition() -> dict[str, object]:
    return {
        "name": "Invoice PDFs",
        "scope": {},
        "trigger": {"source_kind": "mail.message"},
        "conditions": [
            {
                "field": "attachment.media_type",
                "op": "equals",
                "value": "application/pdf",
            }
        ],
        "action": {
            "kind": "connect.invoke",
            "capability": {"id": "invoice.extract", "version": "1.0"},
            "provider": {
                "app_id": "invoice-processor",
                "version": "1.0.0",
                "instance_id": "11111111-1111-4111-8111-111111111111",
            },
            "parameters": {},
        },
        "confirm_each": False,
    }


def test_automation_rule_operations_expose_one_cas_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    monkeypatch.setattr(engine_api, "operation_lock_supported", lambda path: True)

    @contextmanager
    def available_lock(path: Path, busy_message: str):
        yield

    monkeypatch.setattr(engine_api, "operation_lock", available_lock)
    created = engine_api._response(
        request(
            config_path,
            "automation.rules.put",
            {"definition": _automation_definition()},
        )
    )
    assert created["ok"] is True
    summary = created["data"]["rule"]["summary"]
    assert summary["version"] == 1
    assert summary["enabled"] is True
    assert summary["system"] is False
    assert summary["valid"] is True

    listed = engine_api._response(request(config_path, "automation.rules.list"))
    assert listed["data"] == {"revision": 1, "rules": [summary]}
    rule_id = summary["rule_id"]
    fetched = engine_api._response(
        request(config_path, "automation.rules.get", {"rule_id": rule_id})
    )
    canonical_definition = _automation_definition()
    canonical_definition["scope"] = {"account_id": None, "provider": None}
    assert fetched["data"]["rule"]["definition"] == canonical_definition

    disabled = engine_api._response(
        request(
            config_path,
            "automation.rules.set_enabled",
            {"rule_id": rule_id, "expected_version": 1, "enabled": False},
        )
    )
    assert disabled["data"]["rule"]["summary"]["version"] == 2
    no_op = engine_api._response(
        request(
            config_path,
            "automation.rules.set_enabled",
            {"rule_id": rule_id, "expected_version": 2, "enabled": False},
        )
    )
    assert no_op["data"] == disabled["data"]
    stale = engine_api._response(
        request(
            config_path,
            "automation.rules.set_enabled",
            {"rule_id": rule_id, "expected_version": 1, "enabled": False},
        )
    )
    assert stale["error"]["code"] == "stale_rule"

    deleted = engine_api._response(
        request(
            config_path,
            "automation.rules.delete",
            {"rule_id": rule_id, "expected_version": 2},
        )
    )
    assert deleted["data"] == {"rule_id": rule_id, "version": 3, "deleted": True}
    missing = engine_api._response(
        request(config_path, "automation.rules.get", {"rule_id": rule_id})
    )
    assert missing["error"]["code"] == "not_found"


@pytest.mark.parametrize("expected_version", [True, 0, -1, 2**63, "1", None])
def test_automation_rule_mutation_rejects_non_strict_versions(
    tmp_path: Path,
    expected_version: object,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    response = engine_api._response(
        request(
            config_path,
            "automation.rules.delete",
            {
                "rule_id": "11111111-1111-4111-8111-111111111111",
                "expected_version": expected_version,
            },
        )
    )
    assert response["error"]["code"] == "invalid_request"


def test_automation_rule_lock_contention_is_retryable_mailbox_busy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    monkeypatch.setattr(engine_api, "operation_lock_supported", lambda path: True)

    @contextmanager
    def busy_lock(path: Path, busy_message: str):
        raise engine_api.OperationLockBusy(busy_message)
        yield

    monkeypatch.setattr(engine_api, "operation_lock", busy_lock)
    response = engine_api._response(
        request(
            config_path,
            "automation.rules.put",
            {"definition": _automation_definition()},
        )
    )
    assert response["error"] == {
        "code": "mailbox_busy",
        "message": "Another mailbox operation is already running",
        "retryable": True,
    }


def test_automation_rule_mutation_rejects_unsupported_lock_before_runtime_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    monkeypatch.setattr(engine_api, "operation_lock_supported", lambda path: False)
    monkeypatch.setattr(
        engine_api,
        "load_runtime",
        lambda path: pytest.fail("unsupported locking reached Store construction"),
    )

    response = engine_api._response(
        request(
            config_path,
            "automation.rules.put",
            {"definition": _automation_definition()},
        )
    )

    assert response["error"]["code"] == "unsupported_platform"


def test_automation_rule_put_rejects_oversized_canonical_bytes_before_runtime_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    definition = _automation_definition()
    definition["scope"] = {"provider": "gmail", "account_id": "gmail-default"}
    definition["action"]["parameters"] = {
        **{f"k{index}": "x" * 1000 for index in range(1, 16)},
        "k0": "x" * 816,
    }
    monkeypatch.setattr(
        engine_api,
        "load_runtime",
        lambda path: pytest.fail("oversized rule reached runtime construction"),
    )

    response = engine_api._response(
        request(config_path, "automation.rules.put", {"definition": definition})
    )

    assert response["error"]["code"] == "invalid_rule"
    assert "16384 bytes" in response["error"]["message"]


def test_account_scoped_rule_create_rejects_limit_before_mailbox_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    for _index in range(MAX_AUTOMATION_RULES):
        runtime.store.put_automation_rule(_automation_definition())
    definition = _automation_definition()
    definition["scope"] = {
        "provider": DEFAULT_MAIL_PROVIDER,
        "account_id": DEFAULT_MAIL_ACCOUNT_ID,
    }

    @contextmanager
    def available_lock(path: Path, busy_message: str):
        yield

    monkeypatch.setattr(engine_api, "operation_lock_supported", lambda path: True)
    monkeypatch.setattr(engine_api, "operation_lock", available_lock)
    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api,
        "load_mailbox_account",
        lambda *args: pytest.fail("rule-limit rejection reached mailbox reconciliation"),
    )

    response = engine_api._response(
        request(config_path, "automation.rules.put", {"definition": definition})
    )

    assert response["error"]["code"] == "rule_limit"


def test_account_scoped_rule_distinguishes_unknown_and_transient_identity_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    definition = _automation_definition()
    definition["scope"] = {"provider": "gmail", "account_id": "missing"}

    unknown = engine_api._response(
        request(config_path, "automation.rules.put", {"definition": definition})
    )
    assert unknown["error"]["code"] == "invalid_rule"

    definition["scope"] = {"provider": "gmail", "account_id": "gmail-default"}
    monkeypatch.setattr(
        engine_api,
        "load_mailbox_account",
        lambda *args: (_ for _ in ()).throw(GmailError("token lock is busy")),
    )
    transient = engine_api._response(
        request(config_path, "automation.rules.put", {"definition": definition})
    )
    assert transient["error"] == {
        "code": "mailbox_identity_unavailable",
        "message": "Mailbox identity could not be verified; retry",
        "retryable": True,
    }


@pytest.mark.parametrize(
    ("edit_state", "expected_code"),
    [
        ("missing", "not_found"),
        ("stale", "stale_rule"),
        ("system", "system_rule_protected"),
    ],
)
def test_account_scoped_rule_edit_rejects_before_mailbox_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    edit_state: str,
    expected_code: str,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    mailbox_identity_key = _bind_test_mailbox(
        runtime.store,
        DEFAULT_MAIL_PROVIDER,
        DEFAULT_MAIL_ACCOUNT_ID,
    )
    definition = _automation_definition()
    definition["scope"] = {
        "provider": DEFAULT_MAIL_PROVIDER,
        "account_id": DEFAULT_MAIL_ACCOUNT_ID,
    }
    expected_version = 1
    if edit_state == "missing":
        rule_id = "33333333-3333-4333-8333-333333333333"
    else:
        created = runtime.store.put_automation_rule(
            definition,
            expected_account_identity=mailbox_identity_key,
        )
        rule_id = created.summary.rule_id
        if edit_state == "stale":
            expected_version = 2
        else:
            with runtime.store.connection() as db:
                db.execute("UPDATE automation_rules SET system = 1 WHERE rule_id = ?", (rule_id,))

    @contextmanager
    def available_lock(path: Path, busy_message: str):
        yield

    monkeypatch.setattr(engine_api, "operation_lock_supported", lambda path: True)
    monkeypatch.setattr(engine_api, "operation_lock", available_lock)
    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api,
        "load_mailbox_account",
        lambda *args: pytest.fail("rejected edit reached mailbox reconciliation"),
    )

    response = engine_api._response(
        request(
            config_path,
            "automation.rules.put",
            {
                "rule_id": rule_id,
                "expected_version": expected_version,
                "definition": definition,
            },
        )
    )

    assert response["error"]["code"] == expected_code


def test_account_scoped_rule_verification_and_commit_share_operation_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    runtime = load_runtime(config_path)
    lock_held = False
    mailbox_identity_key = "a" * 64

    class ScopedGateway:
        @contextmanager
        def polling_session(self):
            assert lock_held
            yield

        def mailbox_identity_key(self) -> str:
            assert lock_held
            return mailbox_identity_key

    @contextmanager
    def operation_lock(path: Path, busy_message: str):
        nonlocal lock_held
        assert not lock_held
        lock_held = True
        try:
            yield
        finally:
            lock_held = False

    original_put = runtime.store.put_automation_rule

    def guarded_put(*args, **kwargs):
        assert lock_held
        return original_put(*args, **kwargs)

    monkeypatch.setattr(engine_api, "operation_lock_supported", lambda path: True)
    monkeypatch.setattr(engine_api, "operation_lock", operation_lock)
    monkeypatch.setattr(engine_api, "load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        engine_api,
        "load_mailbox_account",
        lambda *args: MailboxSession(
            DEFAULT_MAIL_PROVIDER,
            DEFAULT_MAIL_ACCOUNT_ID,
            ScopedGateway(),
        ),
    )
    monkeypatch.setattr(runtime.store, "put_automation_rule", guarded_put)
    definition = _automation_definition()
    definition["scope"] = {
        "provider": DEFAULT_MAIL_PROVIDER,
        "account_id": DEFAULT_MAIL_ACCOUNT_ID,
    }

    response = engine_api._response(
        request(config_path, "automation.rules.put", {"definition": definition})
    )

    assert response["ok"] is True
    assert lock_held is False


@pytest.mark.parametrize(
    "raw",
    [
        b'{"protocol":1,"protocol":1}',
        b'{"protocol":1,"operation":"automation.rules.put","config_path":"x",'
        b'"payload":{"definition":{"name":"one","name":"two"}}}',
        b'{"protocol":1,"operation":"automation.rules.put","config_path":"x",'
        b'"payload":{"definition":{"action":{"kind":"connect.invoke",'
        b'"kind":"connect.invoke"}}}}',
        b'{"protocol":1,"operation":"automation.rules.put","config_path":"x",'
        b'"payload":{"definition":{"conditions":[{"field":"sender",'
        b'"field":"subject"}]}}}',
    ],
)
def test_main_rejects_duplicate_json_members_at_every_rule_depth(
    raw: bytes,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(engine_api.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(raw)))

    with pytest.raises(SystemExit) as exit_info:
        engine_api.main()

    assert exit_info.value.code == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_json"
