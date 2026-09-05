from __future__ import annotations

import imaplib
import json
import os
import smtplib
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from pathlib import Path

import pytest
from tomlkit import dumps, parse

from eom_email_watcher import engine_api
from eom_email_watcher.config import initialize_config
from eom_email_watcher.mailbox import mailbox_polling_session
from eom_email_watcher.runtime import (
    load_configured_mailbox,
    load_runtime,
    mail_account_token_file,
)

MAILBOX_ADDRESS = "owner@example.test"
MAILBOX_PASSWORD = "fixture-password"
WATCHED_SENDER = "watched@example.test"
PDF_BYTES = b"%PDF-1.4\n% local integration fixture\n"


@dataclass(frozen=True)
class GreenMailEnvironment:
    host: str
    imaps_port: int
    smtp_port: int
    ca_file: Path


def _greenmail_environment() -> GreenMailEnvironment:
    names = {
        "host": "EOM_GREENMAIL_HOST",
        "imaps_port": "EOM_GREENMAIL_IMAPS_PORT",
        "smtp_port": "EOM_GREENMAIL_SMTP_PORT",
        "ca_file": "EOM_GREENMAIL_CA_FILE",
    }
    values = {name: os.environ.get(variable) for name, variable in names.items()}
    missing = [names[name] for name, value in values.items() if not value]
    if missing:
        pytest.skip(f"GreenMail integration environment is unavailable: {', '.join(missing)}")
    try:
        imaps_port = int(values["imaps_port"] or "")
        smtp_port = int(values["smtp_port"] or "")
    except ValueError as exc:
        pytest.fail(f"GreenMail integration ports must be integers: {exc}")
    if not 1 <= imaps_port <= 65_535 or not 1 <= smtp_port <= 65_535:
        pytest.fail("GreenMail integration ports must be between 1 and 65535")
    ca_file = Path(values["ca_file"] or "")
    if not ca_file.is_file():
        pytest.fail("GreenMail integration CA file is unavailable")
    return GreenMailEnvironment(
        host=values["host"] or "",
        imaps_port=imaps_port,
        smtp_port=smtp_port,
        ca_file=ca_file,
    )


def _request(config_path: Path, operation: str, payload: dict[str, object]) -> dict[str, object]:
    return {
        "protocol": 1,
        "operation": operation,
        "config_path": str(config_path),
        "payload": payload,
    }


def _initialize_isolated_config(config_path: Path, state_dir: Path) -> None:
    initialize_config(
        config_path,
        timezone="UTC",
        model_base_url="http://127.0.0.1:11434/v1",
        model_name="local-integration-model",
    )
    document = parse(config_path.read_text(encoding="utf-8"))
    document["gmail_credentials_file"] = str(state_dir / "google-oauth-client.json")
    document["microsoft_credentials_file"] = str(state_dir / "microsoft-oauth-client.json")
    document["gmail_token_file"] = str(state_dir / "gmail-token.json")
    document["gmail_send_token_file"] = str(state_dir / "gmail-send-token.json")
    document["database_file"] = str(state_dir / "watcher.sqlite3")
    config_path.write_text(dumps(document), encoding="utf-8")
    config_path.chmod(0o600)


def _connection(environment: GreenMailEnvironment, *, password: str) -> dict[str, object]:
    return {
        "email_address": MAILBOX_ADDRESS,
        "host": environment.host,
        "port": environment.imaps_port,
        "security": "tls",
        "username": MAILBOX_ADDRESS,
        "password": password,
        "ca_file": str(environment.ca_file),
    }


def _send_message(
    environment: GreenMailEnvironment,
    *,
    subject: str,
    body: str,
    attach_pdf: bool = False,
) -> None:
    message = EmailMessage()
    message["From"] = WATCHED_SENDER
    message["To"] = MAILBOX_ADDRESS
    message["Subject"] = subject
    message["Date"] = format_datetime(datetime.now(UTC))
    message["Message-ID"] = make_msgid(domain="example.test")
    message.set_content(body)
    if attach_pdf:
        message.add_attachment(
            PDF_BYTES,
            maintype="application",
            subtype="pdf",
            filename="invoice.pdf",
        )
    with smtplib.SMTP("127.0.0.1", environment.smtp_port, timeout=5) as client:
        client.send_message(message)


def _assert_source_mail_is_unmodified(environment: GreenMailEnvironment) -> None:
    context = ssl.create_default_context(cafile=environment.ca_file)
    with imaplib.IMAP4_SSL(
        environment.host,
        environment.imaps_port,
        ssl_context=context,
        timeout=5,
    ) as client:
        client.login(MAILBOX_ADDRESS, MAILBOX_PASSWORD)
        status, _response = client.select("INBOX", readonly=True)
        assert status == "OK"
        status, response = client.uid("SEARCH", None, "ALL")
        assert status == "OK"
        uids = response[0].split()
        assert len(uids) == 2
        for uid in uids:
            status, flags = client.uid("FETCH", uid, "(FLAGS)")
            assert status == "OK"
            metadata = b" ".join(part for part in flags if isinstance(part, bytes))
            assert b"\\Seen" not in metadata


@pytest.mark.integration
def test_engine_connects_and_polls_real_imap_without_mutating_source(tmp_path: Path) -> None:
    environment = _greenmail_environment()
    config_path = tmp_path / "config.toml"
    _initialize_isolated_config(config_path, tmp_path / "state")
    _send_message(environment, subject="Existing mail", body="Do not backfill this message.")

    rejected = engine_api._response(
        _request(
            config_path,
            "mail.accounts.connect",
            {"provider": "imap", "connection": _connection(environment, password="wrong")},
        )
    )
    assert rejected["ok"] is False
    assert rejected["error"]["code"] == "imap_authentication_failed"
    rejected_runtime = load_runtime(config_path)
    assert all(account.provider != "imap" for account in rejected_runtime.store.mail_accounts())
    assert not (tmp_path / "state" / "mail-accounts").exists()

    connected = engine_api._response(
        _request(
            config_path,
            "mail.accounts.connect",
            {
                "provider": "imap",
                "connection": _connection(environment, password=MAILBOX_PASSWORD),
            },
        )
    )
    assert connected["ok"] is True
    assert connected["data"]["baseline_initialized"] is True
    assert connected["data"]["account"]["provider"] == "imap"
    encoded_response = json.dumps(connected)
    assert MAILBOX_PASSWORD not in encoded_response
    assert str(environment.ca_file) not in encoded_response

    runtime = load_runtime(config_path)
    account = runtime.store.active_mail_account()
    assert account is not None
    assert account.provider == "imap"
    credentials_file = mail_account_token_file(runtime.config, account)
    assert credentials_file.stat().st_mode & 0o777 == 0o600
    state = runtime.store.state(
        provider=account.provider,
        account_id=account.account_id,
    )
    assert state is not None
    cursor, _last_check = state
    assert cursor is not None

    _send_message(
        environment,
        subject="Invoice ready",
        body="Please review the attached invoice.",
        attach_pdf=True,
    )
    session = load_configured_mailbox(runtime.config, runtime.store)
    with mailbox_polling_session(session.gateway):
        changes = session.gateway.changes_since(cursor)
        assert len(changes.message_ids) == 1
        message_id = changes.message_ids[0]
        metadata = session.gateway.metadata(message_id)
        content = session.gateway.content(message_id, 20_000)
        assert metadata.sender == WATCHED_SENDER
        assert metadata.subject == "Invoice ready"
        assert "Please review the attached invoice." in content.body
        assert content.attachment_names == ("invoice.pdf",)
        assert len(content.attachments) == 1
        attachment = content.attachments[0]
        assert attachment.media_type == "application/pdf"
        assert session.gateway.attachment_bytes(
            message_id,
            attachment.part_id,
            attachment.attachment_id,
        ) == PDF_BYTES

    _assert_source_mail_is_unmodified(environment)
