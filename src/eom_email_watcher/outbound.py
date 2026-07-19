from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import date
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SEND_SCOPES = ("https://www.googleapis.com/auth/gmail.send",)


class SendError(RuntimeError):
    """Gmail send authorization or delivery failed."""


@dataclass(frozen=True)
class MonthlyEmail:
    period_key: str
    subject: str
    body: str


def previous_month_email(today: date) -> MonthlyEmail:
    previous_year = today.year if today.month > 1 else today.year - 1
    previous_month = today.month - 1 if today.month > 1 else 12
    period = date(previous_year, previous_month, 1)
    period_label = period.strftime("%B %Y")
    return MonthlyEmail(
        period_key=period.strftime("%Y-%m"),
        subject=f"Firefly Hours for {period_label}",
        body=(
            "Hi Maria,\n\n"
            "I hope you're doing well. When you have a chance, could you please send me "
            f"the Firefly hours for {period_label}? Once I receive them, I'll prepare "
            "your invoice.\n\n"
            "Thank you,\n"
            "Juan Canfield\n"
            "Effingham Office Maids\n"
        ),
    )


class GmailSender:
    def __init__(self, service: Any):
        self.service = service

    @classmethod
    def authorize(cls, credentials_file: Path, token_file: Path) -> GmailSender:
        if not credentials_file.exists():
            raise SendError(f"OAuth desktop credentials not found: {credentials_file}")
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), SEND_SCOPES)
        credentials = flow.run_local_server(
            host="127.0.0.1", port=0, open_browser=True, prompt="consent"
        )
        token_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        token_file.write_text(credentials.to_json(), encoding="utf-8")
        token_file.chmod(0o600)
        return cls(build("gmail", "v1", credentials=credentials, cache_discovery=False))

    @classmethod
    def from_token(cls, token_file: Path) -> GmailSender:
        if not token_file.exists():
            raise SendError("Gmail sending is not authorized. Run: eom-mail-watch setup-send")
        credentials = Credentials.from_authorized_user_file(str(token_file), SEND_SCOPES)
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            token_file.write_text(credentials.to_json(), encoding="utf-8")
            token_file.chmod(0o600)
        if not credentials.valid:
            raise SendError("Gmail send token is invalid. Run: eom-mail-watch setup-send")
        return cls(build("gmail", "v1", credentials=credentials, cache_discovery=False))

    def send(self, recipient: str, subject: str, body: str) -> str:
        message = EmailMessage()
        message["To"] = recipient
        message["Subject"] = subject
        message.set_content(body)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        try:
            result = self.service.users().messages().send(userId="me", body={"raw": raw}).execute()
        except HttpError as exc:
            raise SendError(f"Gmail send failed (HTTP {exc.resp.status})") from exc
        return str(result["id"])
