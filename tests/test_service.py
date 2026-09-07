import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eom_email_watcher import service as service_module
from eom_email_watcher.config import Config, Sender
from eom_email_watcher.db import Store
from eom_email_watcher.gmail import (
    MessageMetadata,
    MessageUnavailable,
    StaleHistoryCursor,
)
from eom_email_watcher.mailbox import (
    MailboxChanges,
    MailboxMessageInvalid,
    MailboxSession,
    MessageContent,
    scoped_message_id,
)
from eom_email_watcher.microsoft365 import (
    MICROSOFT365_PROVIDER,
    Microsoft365Error,
    MicrosoftAuthorizationRejected,
)
from eom_email_watcher.mime import extract_body
from eom_email_watcher.model import (
    MAX_GATEWAY_ATTACHMENT_COUNT,
    MAX_GATEWAY_ATTACHMENT_NAME_CHARS,
    Analysis,
    GatewayModelError,
    ModelError,
)
from eom_email_watcher.notifications import NotificationError
from eom_email_watcher.scheduling import (
    SchedulingSource,
    scheduling_source_sha256,
    validate_scheduling_output,
)
from eom_email_watcher.service import Watcher, process_scheduling_automations, run_watcher_check


class FakeGmail:
    def __init__(self, stale: bool = False):
        self.stale = stale
        self.full_payload_calls = 0
        self.search_since_value: datetime | None = None

    def profile_history_id(self) -> str:
        return "200"

    def initial_cursor(self) -> str:
        return self.profile_history_id()

    def history_message_ids(self, cursor: str):
        if self.stale:
            raise StaleHistoryCursor()
        return ["allowed", "blocked"], "200"

    def changes_since(self, cursor: str) -> MailboxChanges:
        message_ids, newest = self.history_message_ids(cursor)
        return MailboxChanges(tuple(message_ids), newest)

    def search_since(self, addresses, since):
        self.search_since_value = since
        return ["allowed"]

    def recover_since(self, addresses, since) -> MailboxChanges:
        return MailboxChanges(tuple(self.search_since(addresses, since)), self.initial_cursor())

    def metadata(self, message_id: str) -> MessageMetadata:
        sender = "trusted@example.com" if message_id == "allowed" else "stranger@example.com"
        return MessageMetadata(
            message_id,
            None,
            sender,
            None,
            "Subject",
            "2026-07-18T12:00:00+00:00",
            frozenset({"INBOX"}),
        )

    def full_payload(self, message_id: str):
        self.full_payload_calls += 1
        return {"mimeType": "text/plain", "body": {"data": "SGVsbG8="}}

    def content(self, message_id: str, body_char_limit: int) -> MessageContent:
        body, attachment_names, attachments = extract_body(
            self.full_payload(message_id), body_char_limit
        )
        return MessageContent(body, attachment_names, attachments)


class FreshGmail(FakeGmail):
    def metadata(self, message_id: str) -> MessageMetadata:
        return replace(super().metadata(message_id), received_at=datetime.now(UTC).isoformat())


class FutureDatedGmail(FakeGmail):
    def metadata(self, message_id: str) -> MessageMetadata:
        future = datetime.now(UTC) + service_module.timedelta(days=3650)
        return replace(super().metadata(message_id), received_at=future.isoformat())


class InvalidDatedGmail(FakeGmail):
    def metadata(self, message_id: str) -> MessageMetadata:
        return replace(super().metadata(message_id), received_at="")


class FakeModel:
    def __init__(self):
        self.calls = 0

    def analyze(self, **kwargs) -> Analysis:
        self.calls += 1
        return Analysis(
            category="informational",
            priority="normal",
            summary="A short update.",
            action_required=False,
            suggested_action=None,
            deadline_text=None,
            deadline_iso=None,
            confidence=0.9,
        )


class SchedulingModel(FakeModel):
    def analyze(self, **kwargs) -> Analysis:
        self.calls += 1
        return Analysis(
            category="scheduling",
            priority="normal",
            summary="The sender requested a meeting.",
            action_required=True,
            suggested_action="Review the meeting request.",
            deadline_text=None,
            deadline_iso=None,
            confidence=0.9,
        )


class AutomationGateway:
    body = "Meet jane@example.com on September 8, 2026 from 10:00 to 10:30 AM."

    def content(self, message_id: str, body_char_limit: int) -> MessageContent:
        assert message_id == "schedule-1"
        return MessageContent(self.body[:body_char_limit], (), ())


class AnyAutomationGateway(AutomationGateway):
    def content(self, message_id: str, body_char_limit: int) -> MessageContent:
        return MessageContent(self.body[:body_char_limit], (), ())


class MissingAutomationSource(AutomationGateway):
    def content(self, message_id: str, body_char_limit: int) -> MessageContent:
        raise MessageUnavailable("deleted")


class FailingAutomationProvider(AutomationGateway):
    def content(self, message_id: str, body_char_limit: int) -> MessageContent:
        raise Microsoft365Error("temporarily unavailable")


class FailingOlderAutomationProvider(AutomationGateway):
    def content(self, message_id: str, body_char_limit: int) -> MessageContent:
        if message_id == "older-failure":
            raise Microsoft365Error("temporarily unavailable")
        return MessageContent(self.body[:body_char_limit], (), ())


class MetadataHeavyAutomationProvider(AutomationGateway):
    def content(self, message_id: str, body_char_limit: int) -> MessageContent:
        names = tuple(
            f"attachment-{index}-{'x' * MAX_GATEWAY_ATTACHMENT_NAME_CHARS}.pdf"
            for index in range(MAX_GATEWAY_ATTACHMENT_COUNT + 1)
        )
        return MessageContent(self.body[:body_char_limit], names, ())


class MalformedUnicodeAutomationProvider(AutomationGateway):
    def content(self, message_id: str, body_char_limit: int) -> MessageContent:
        return MessageContent(f"{self.body}\ud800"[:body_char_limit], (), ())


class ExtractionModel(FakeModel):
    def __init__(self, outputs: list[str | Exception]):
        super().__init__()
        self.outputs = outputs
        self.extraction_calls: list[tuple[str, tuple[str, ...]]] = []
        self.extraction_sources: list[SchedulingSource] = []

    def extract_scheduling(
        self,
        *,
        source: SchedulingSource,
        feedback,
        request_id: str,
    ):
        self.extraction_sources.append(source)
        self.extraction_calls.append((request_id, tuple(violation.code for violation in feedback)))
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return validate_scheduling_output(output, source)


class AttachmentGmail(FakeGmail):
    def full_payload(self, message_id: str):
        self.full_payload_calls += 1
        return {
            "mimeType": "multipart/mixed",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": "SGVsbG8="}},
                {
                    "mimeType": "application/pdf",
                    "partId": "2",
                    "filename": "invoice.pdf",
                    "body": {"attachmentId": "gmail-attachment", "size": 1234},
                },
            ],
        }


class FailingCaptureModel(FakeModel):
    def __init__(self):
        super().__init__()
        self.attachment_names: tuple[str, ...] | None = None

    def analyze(self, **kwargs) -> Analysis:
        self.calls += 1
        self.attachment_names = kwargs["attachment_names"]
        raise ModelError("local model unavailable")


class InvalidContentGmail(FakeGmail):
    def content(self, message_id: str, body_char_limit: int) -> MessageContent:
        raise MailboxMessageInvalid("message_too_large", "Message exceeds the safe size limit")


class PollScopedGmail(FreshGmail):
    def __init__(self) -> None:
        super().__init__()
        self.in_polling_session = False
        self.polling_sessions = 0

    @contextmanager
    def polling_session(self):
        self.polling_sessions += 1
        self.in_polling_session = True
        try:
            yield
        finally:
            self.in_polling_session = False

    def changes_since(self, cursor: str) -> MailboxChanges:
        assert self.in_polling_session is True
        return super().changes_since(cursor)

    def metadata(self, message_id: str) -> MessageMetadata:
        assert self.in_polling_session is True
        return super().metadata(message_id)

    def content(self, message_id: str, body_char_limit: int) -> MessageContent:
        assert self.in_polling_session is True
        return super().content(message_id, body_char_limit)


class InvalidMetadataGmail(FreshGmail):
    def metadata(self, message_id: str) -> MessageMetadata:
        raise MailboxMessageInvalid("headers_too_large", "Message headers are unsafe")


def config(tmp_path: Path) -> Config:
    return Config(
        path=tmp_path / "config.toml",
        timezone="America/Chicago",
        body_char_limit=20_000,
        retention_days=180,
        poll_interval_minutes=120,
        gmail_credentials_file=tmp_path / "credentials.json",
        microsoft_credentials_file=tmp_path / "microsoft-oauth-client.json",
        gmail_token_file=tmp_path / "token.json",
        gmail_send_token_file=tmp_path / "send-token.json",
        monthly_hours_recipient="maria@example.com",
        database_file=tmp_path / "db.sqlite3",
        model_backend="loopback",
        model_base_url="http://127.0.0.1:1234/v1",
        model_name="model",
        model_api_token_file=tmp_path / "lm-token",
        model_ca_file=None,
        model_require_auth=True,
        model_timeout_seconds=60,
        notifications_enabled=False,
        ntfy_topic=None,
        ntfy_url="https://ntfy.sh",
        senders=(Sender("trusted@example.com", "Trusted"),),
    )


def valid_scheduling_output(intent: str = "new_meeting") -> str:
    return json.dumps(
        {
            "intent": intent,
            "intent_evidence": {
                "source": "body",
                "quote": "Meet jane@example.com",
            },
            "proposed_times": [
                {
                    "start": "2026-09-08T10:00:00-05:00",
                    "end": "2026-09-08T10:30:00-05:00",
                    "timezone": "America/Chicago",
                    "evidence": [
                        {
                            "source": "body",
                            "quote": "September 8, 2026 from 10:00 to 10:30 AM",
                        }
                    ],
                }
            ],
            "attendees": [
                {
                    "email": "jane@example.com",
                    "evidence": {"source": "body", "quote": "jane@example.com"},
                }
            ],
            "referenced_event": None,
            "confidence": 0.95,
            "ambiguity_reasons": [] if intent != "unclear" else ["No safe intent"],
        }
    )


def admit_scheduling_run(
    store: Store,
    *,
    active: bool = False,
    provider_message_id: str = "schedule-1",
    received_at: str | None = None,
    principal_key: str = "a" * 64,
):
    account_id = f"microsoft365-{'d' * 32}"
    if store.mail_account(MICROSOFT365_PROVIDER, account_id) is None:
        store.register_mail_account(
            MICROSOFT365_PROVIDER,
            account_id,
            display_name="Microsoft 365",
            address="owner@example.com",
            active=active,
        )
    message_id = scoped_message_id(
        MICROSOFT365_PROVIDER,
        account_id,
        provider_message_id,
    )
    store.add_message(
        message_id=message_id,
        provider=MICROSOFT365_PROVIDER,
        account_id=account_id,
        provider_message_id=provider_message_id,
        thread_id=None,
        sender="trusted@example.com",
        sender_name="Trusted",
        subject="Meeting request",
        received_at=received_at or datetime.now(UTC).isoformat(),
    )
    store.mark_analyzed(
        message_id,
        SchedulingModel().analyze().model_dump(),
        scheduling_automation_principal_key=principal_key,
    )
    run = store.automation_run_for_message(message_id)
    assert run is not None
    return account_id, run


def allow_automation_processing(
    monkeypatch: pytest.MonkeyPatch,
    gateway: AutomationGateway,
) -> None:
    monkeypatch.setattr(
        service_module,
        "_scheduling_authorization_principal",
        lambda *args, **kwargs: "a" * 64,
    )
    monkeypatch.setattr(
        service_module,
        "load_mailbox_account",
        lambda config, store, provider, account_id: MailboxSession(
            provider,
            account_id,
            gateway,
        ),
    )


def test_exact_allowlist_and_dedup(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    store.set_state("100", datetime(2026, 7, 18, tzinfo=UTC))
    watcher = Watcher(cfg, store, FakeGmail(), FakeModel())
    result = watcher.check()
    assert result["active"] is True
    assert result["discovered"] == 1
    assert result["summarized"] == 1
    assert len(store.recent(10)) == 1
    assert watcher.check()["discovered"] == 0


@pytest.mark.parametrize(
    ("provider", "features_active", "grant_state", "authorization", "expected"),
    [
        (MICROSOFT365_PROVIDER, False, "ready", "valid", False),
        (MICROSOFT365_PROVIDER, True, None, "valid", False),
        (MICROSOFT365_PROVIDER, True, "consent_pending", "valid", False),
        (MICROSOFT365_PROVIDER, True, "ready", "valid", True),
        (MICROSOFT365_PROVIDER, True, "ready", "rejected", False),
        (MICROSOFT365_PROVIDER, True, "ready", "transient", False),
        ("gmail", True, "ready", "valid", False),
    ],
)
def test_watcher_requires_full_scheduling_automation_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    features_active: bool,
    grant_state: str | None,
    authorization: str,
    expected: bool,
) -> None:
    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    account_id = (
        f"microsoft365-{'a' * 32}" if provider == MICROSOFT365_PROVIDER else "gmail-default"
    )
    if provider == MICROSOFT365_PROVIDER:
        store.register_mail_account(
            provider,
            account_id,
            display_name="Microsoft 365",
            address="user@example.com",
            active=True,
        )
    store.set_state("100", provider=provider, account_id=account_id)
    if grant_state is not None:
        identity = {
            "principal_key": "a" * 64,
            "home_account_id": "home-account",
            "tenant_id": "tenant",
            "object_id": "object",
            "email_address": "user@example.com",
        }
        store.set_calendar_grant(account_id, "proposal", grant_state, **identity)
    entitlement_checks: list[tuple[str, ...]] = []

    def check_features(*feature_ids: str) -> bool:
        entitlement_checks.append(feature_ids)
        return features_active

    monkeypatch.setattr(
        service_module,
        "feature_entitlements_active",
        check_features,
    )
    authorization_checks: list[tuple[Path, Path, Path, str]] = []

    class AuthorizedCalendar:
        class Principal:
            key = "a" * 64

        principal = Principal()

    def validate_authorization(
        credentials_file: Path,
        token_file: Path,
        mailbox_token_file: Path,
        expected_principal_key: str,
    ) -> AuthorizedCalendar:
        authorization_checks.append(
            (credentials_file, token_file, mailbox_token_file, expected_principal_key)
        )
        if authorization == "rejected":
            raise MicrosoftAuthorizationRejected("revoked")
        if authorization == "transient":
            raise Microsoft365Error("temporarily unavailable")
        return AuthorizedCalendar()

    monkeypatch.setattr(
        service_module.MicrosoftCalendarProposalAuthorization,
        "from_matching_tokens",
        validate_authorization,
    )

    session = MailboxSession(provider, account_id, FreshGmail())
    result = Watcher(cfg, store, session, SchedulingModel()).check()

    assert result["summarized"] == 1
    message_id = scoped_message_id(provider, account_id, "allowed")
    run = store.automation_run_for_message(message_id)
    assert (run is not None) is expected
    if run is not None:
        assert (run.state, run.state_version) == ("detected", 1)
        assert run.calendar_principal_key == "a" * 64
        assert [event.transition_kind for event in store.automation_events(run.run_id)] == [
            "detected"
        ]
    should_validate = (
        provider == MICROSOFT365_PROVIDER and features_active and grant_state == "ready"
    )
    assert bool(authorization_checks) is should_validate
    entitlement_check_count = int(provider == MICROSOFT365_PROVIDER)
    if should_validate and authorization == "valid":
        entitlement_check_count += 1
    assert (
        entitlement_checks
        == [(service_module.CONNECT_FEATURE_ID, service_module.AUTOMATIONS_FEATURE_ID)]
        * entitlement_check_count
    )
    grant = store.calendar_grant(account_id, "proposal")
    if authorization == "rejected" and should_validate:
        assert grant is not None
        assert grant.state == "revoked"
    elif authorization == "transient" and should_validate:
        assert grant is not None
        assert grant.state == "ready"


def test_watcher_skips_calendar_authorization_for_non_scheduling_analysis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    account_id = f"microsoft365-{'b' * 32}"
    store.register_mail_account(
        MICROSOFT365_PROVIDER,
        account_id,
        display_name="Microsoft 365",
        address="user@example.com",
        active=True,
    )
    store.set_state("100", provider=MICROSOFT365_PROVIDER, account_id=account_id)
    monkeypatch.setattr(
        service_module,
        "feature_entitlements_active",
        lambda *args: pytest.fail("Informational analysis reached automation authorization"),
    )

    result = Watcher(
        cfg,
        store,
        MailboxSession(MICROSOFT365_PROVIDER, account_id, FreshGmail()),
        FakeModel(),
    ).check()

    assert result["summarized"] == 1
    message_id = scoped_message_id(MICROSOFT365_PROVIDER, account_id, "allowed")
    assert store.automation_run_for_message(message_id) is None


def test_watcher_rechecks_entitlements_after_live_calendar_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    account_id = f"microsoft365-{'c' * 32}"
    store.register_mail_account(
        MICROSOFT365_PROVIDER,
        account_id,
        display_name="Microsoft 365",
        address="user@example.com",
        active=True,
    )
    store.set_state("100", provider=MICROSOFT365_PROVIDER, account_id=account_id)
    identity = {
        "principal_key": "a" * 64,
        "home_account_id": "home-account",
        "tenant_id": "tenant",
        "object_id": "object",
        "email_address": "user@example.com",
    }
    store.set_calendar_grant(account_id, "proposal", "ready", **identity)
    entitlement_decisions = iter((True, False))
    monkeypatch.setattr(
        service_module,
        "feature_entitlements_active",
        lambda *args: next(entitlement_decisions),
    )

    class AuthorizedCalendar:
        class Principal:
            key = "a" * 64

        principal = Principal()

    monkeypatch.setattr(
        service_module.MicrosoftCalendarProposalAuthorization,
        "from_matching_tokens",
        lambda *args: AuthorizedCalendar(),
    )

    result = Watcher(
        cfg,
        store,
        MailboxSession(MICROSOFT365_PROVIDER, account_id, FreshGmail()),
        SchedulingModel(),
    ).check()

    assert result["summarized"] == 1
    message_id = scoped_message_id(MICROSOFT365_PROVIDER, account_id, "allowed")
    assert store.automation_run_for_message(message_id) is None


def test_scheduling_extraction_retries_once_with_feedback_then_proposes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    _account_id, admitted = admit_scheduling_run(store)
    allow_automation_processing(monkeypatch, AutomationGateway())
    model = ExtractionModel(["{}", valid_scheduling_output()])

    result = process_scheduling_automations(cfg, store, model)

    current = store.automation_run(admitted.run_id)
    assert current is not None
    assert current.state == "proposing"
    assert result.processed == 1
    assert result.review_required == 0
    assert len(model.extraction_calls) == 2
    assert model.extraction_calls[0][1] == ()
    assert "schema_missing_field" in model.extraction_calls[1][1]
    assert len(store.automation_extraction_payloads(admitted.run_id)) == 2


def test_scheduling_transport_retry_reuses_reserved_request_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    received_at = "2026-09-07T12:00:00+00:00"
    _account_id, admitted = admit_scheduling_run(store, received_at=received_at)
    allow_automation_processing(monkeypatch, AutomationGateway())
    model = ExtractionModel(
        [
            GatewayModelError(
                "worker_unavailable",
                retryable=True,
                retry_after_seconds=30,
            ),
            valid_scheduling_output(),
        ]
    )
    first_at = datetime(2026, 9, 7, 13, tzinfo=UTC)
    monkeypatch.setattr(service_module, "_utc_now", lambda: first_at)

    first = process_scheduling_automations(cfg, store, model, now=first_at)
    after_failure = store.automation_run(admitted.run_id)
    store = Store(store.path)
    store.initialize()
    before_due = process_scheduling_automations(
        cfg,
        store,
        model,
        now=first_at + timedelta(seconds=29),
    )
    second = process_scheduling_automations(
        cfg,
        store,
        model,
        now=first_at + timedelta(seconds=30),
    )

    assert after_failure is not None
    assert after_failure.state == "extracting"
    assert first.processed == 0
    assert before_due.processed == 0
    assert second.processed == 1
    assert model.extraction_calls[0][0] == model.extraction_calls[1][0]
    payloads = store.automation_extraction_payloads(admitted.run_id)
    assert len(payloads) == 1
    assert payloads[0].status == "accepted"


def test_scheduling_transport_retry_delay_starts_when_request_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    _account_id, admitted = admit_scheduling_run(store)
    allow_automation_processing(monkeypatch, AutomationGateway())
    model = ExtractionModel(
        [GatewayModelError("worker_unavailable", retryable=True, retry_after_seconds=30)]
    )
    started_at = datetime.now(UTC)
    failed_at = started_at + timedelta(minutes=2)
    monkeypatch.setattr(service_module, "_utc_now", lambda: failed_at)

    process_scheduling_automations(cfg, store, model, now=started_at)

    current = store.automation_run(admitted.run_id)
    payload = store.automation_extraction_payloads(admitted.run_id)[0]
    assert current is not None
    assert current.updated_at == failed_at.isoformat()
    assert payload.next_retry_at == (failed_at + timedelta(seconds=30)).isoformat()


def test_permanent_gateway_extraction_failure_becomes_reviewable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    _account_id, admitted = admit_scheduling_run(
        store,
        received_at="2026-09-07T12:00:00+00:00",
    )
    allow_automation_processing(monkeypatch, AutomationGateway())
    model = ExtractionModel([GatewayModelError("forbidden", retryable=False)])

    result = process_scheduling_automations(
        cfg,
        store,
        model,
        now=datetime(2026, 9, 7, 13, tzinfo=UTC),
    )
    repeated = process_scheduling_automations(
        cfg,
        store,
        model,
        now=datetime(2026, 9, 7, 14, tzinfo=UTC),
    )

    current = store.automation_run(admitted.run_id)
    assert current is not None
    assert (current.state, current.failure_code) == ("manual_review", "model_forbidden")
    assert result.review_required == 1
    assert repeated.processed == 0
    assert len(model.extraction_calls) == 1


def test_first_extraction_context_is_anchored_to_message_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    _account_id, admitted = admit_scheduling_run(
        store,
        received_at="2026-09-07T12:00:00+00:00",
    )
    allow_automation_processing(monkeypatch, AutomationGateway())
    model = ExtractionModel([valid_scheduling_output()])

    result = process_scheduling_automations(
        cfg,
        store,
        model,
        now=datetime(2026, 9, 10, 13, tzinfo=UTC),
    )

    current = store.automation_run(admitted.run_id)
    assert current is not None
    assert current.state == "proposing"
    assert result.processed == 1
    assert model.extraction_sources[0].context_at.isoformat() == "2026-09-07T07:00:00-05:00"


def test_retry_uses_the_organizer_pinned_before_account_identity_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    account_id, _admitted = admit_scheduling_run(
        store,
        received_at="2026-09-07T12:00:00+00:00",
    )
    allow_automation_processing(monkeypatch, AutomationGateway())
    model = ExtractionModel([ModelError("offline"), valid_scheduling_output()])
    first_at = datetime(2026, 9, 7, 13, tzinfo=UTC)
    monkeypatch.setattr(service_module, "_utc_now", lambda: first_at)

    process_scheduling_automations(cfg, store, model, now=first_at)
    store.update_mail_account_identity(
        MICROSOFT365_PROVIDER,
        account_id,
        display_name="Microsoft 365",
        address="changed@example.com",
    )
    process_scheduling_automations(
        cfg,
        store,
        model,
        now=first_at + timedelta(seconds=60),
    )

    assert [item.organizer_address for item in model.extraction_sources] == [
        "owner@example.com",
        "owner@example.com",
    ]


def test_second_invalid_extraction_stops_in_review_without_third_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    _account_id, admitted = admit_scheduling_run(store)
    allow_automation_processing(monkeypatch, AutomationGateway())
    model = ExtractionModel(["{}", "{}"])

    result = process_scheduling_automations(cfg, store, model)
    repeated = process_scheduling_automations(cfg, store, model)

    current = store.automation_run(admitted.run_id)
    assert current is not None
    assert (current.state, current.failure_code) == (
        "manual_review",
        "validation_rejected",
    )
    assert result.review_required == 1
    assert repeated.processed == 0
    assert len(model.extraction_calls) == 2
    intent = next(
        intent for intent in store.notification_intents() if intent.kind == "automation_review"
    )
    assert (intent.kind, intent.subject_id, intent.revision) == (
        "automation_review",
        admitted.run_id,
        str(current.state_version),
    )


@pytest.mark.parametrize(
    ("intent", "state", "failure_code"),
    [
        ("reschedule", "manual_review", "reschedule_not_supported"),
        ("cancellation", "manual_review", "cancellation_not_supported"),
        ("unclear", "ambiguous", "ambiguous_extraction"),
    ],
)
def test_non_creation_intents_are_durable_non_writing_review_outcomes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    intent: str,
    state: str,
    failure_code: str,
) -> None:
    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    _account_id, admitted = admit_scheduling_run(store)
    allow_automation_processing(monkeypatch, AutomationGateway())
    model = ExtractionModel([valid_scheduling_output(intent)])

    process_scheduling_automations(cfg, store, model)

    current = store.automation_run(admitted.run_id)
    assert current is not None
    assert (current.state, current.failure_code) == (state, failure_code)
    assert any(intent.kind == "automation_review" for intent in store.notification_intents())


def test_missing_scheduling_source_becomes_reviewable_terminal_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    _account_id, admitted = admit_scheduling_run(store)
    allow_automation_processing(monkeypatch, MissingAutomationSource())
    model = ExtractionModel([])

    result = process_scheduling_automations(cfg, store, model)

    current = store.automation_run(admitted.run_id)
    assert current is not None
    assert (current.state, current.failure_code) == (
        "source_unavailable",
        "source_unavailable",
    )
    assert result.review_required == 1
    assert model.extraction_calls == []
    assert any(intent.kind == "automation_review" for intent in store.notification_intents())


def test_canonical_check_resumes_nonactive_account_with_empty_watchlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = replace(config(tmp_path), senders=())
    store = Store(cfg.database_file)
    store.initialize()
    _account_id, admitted = admit_scheduling_run(store, active=False)
    allow_automation_processing(monkeypatch, AutomationGateway())
    model = ExtractionModel([valid_scheduling_output()])
    monkeypatch.setattr(
        service_module,
        "load_configured_mailbox",
        lambda *args: pytest.fail("Inactive check must not load the active mailbox"),
    )

    result = run_watcher_check(cfg, store, model, deliver_notifications=False)

    current = store.automation_run(admitted.run_id)
    assert current is not None
    assert current.state == "proposing"
    assert result["active"] is False
    assert result["automation_processed"] == 1
    assert result["automation_review_required"] == 0


def test_canonical_check_delivers_automation_review_beyond_mixed_intent_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = replace(config(tmp_path), senders=(), notifications_enabled=True)
    store = Store(cfg.database_file)
    store.initialize()
    _account_id, admitted = admit_scheduling_run(store)
    reviewed = store.transition_automation_to_review(
        admitted.run_id,
        admitted.state_version,
        next_state="manual_review",
        failure_code="source_invalid",
    )
    older = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    with store.connection() as db:
        db.executemany(
            """INSERT INTO messages (
                    message_id, provider, account_id, provider_message_id,
                    sender, subject, received_at, discovered_at, status, last_error
                ) VALUES (
                    ?1, 'gmail', 'gmail-default', ?1,
                    'a@b.com', 'Update', ?2, ?2, 'pending', 'model unavailable'
                )""",
            [(f"backlog-{index}", older) for index in range(25)],
        )
    delivered: list[str] = []
    monkeypatch.setattr(
        service_module,
        "send_review",
        lambda *args, **kwargs: delivered.append(args[1]),
    )

    run_watcher_check(cfg, store, ExtractionModel([]), deliver_notifications=True)

    current = store.automation_run(admitted.run_id)
    assert delivered == ["Meeting request"]
    assert current is not None
    assert current.review_notified_at is not None
    assert current.state_version == reviewed.state_version


def test_recovery_rechecks_authorization_before_fetch_or_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    _account_id, admitted = admit_scheduling_run(store)
    monkeypatch.setattr(
        service_module,
        "_scheduling_authorization_principal",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        service_module,
        "load_mailbox_account",
        lambda *args: pytest.fail("Unauthorized recovery fetched mailbox content"),
    )
    model = ExtractionModel([])

    result = process_scheduling_automations(cfg, store, model)

    current = store.automation_run(admitted.run_id)
    assert current is not None
    assert current.state == "detected"
    assert result.processed == 0
    assert model.extraction_calls == []


def test_recovery_provider_failure_does_not_abort_active_mailbox_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    store.set_state("100", provider="gmail", account_id="gmail-default")
    _account_id, admitted = admit_scheduling_run(store)
    allow_automation_processing(monkeypatch, FailingAutomationProvider())
    healthy_mailbox = FreshGmail()
    monkeypatch.setattr(
        service_module,
        "load_configured_mailbox",
        lambda *args: MailboxSession("gmail", "gmail-default", healthy_mailbox),
    )

    result = run_watcher_check(cfg, store, ExtractionModel([]), deliver_notifications=False)

    current = store.automation_run(admitted.run_id)
    assert result["active"] is True
    assert current is not None
    assert current.state == "detected"
    assert healthy_mailbox.full_payload_calls == 1


def test_recovery_session_failure_does_not_abort_active_mailbox_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    store.set_state("100", provider="gmail", account_id="gmail-default")
    _account_id, admitted = admit_scheduling_run(store)
    monkeypatch.setattr(
        service_module,
        "_scheduling_authorization_principal",
        lambda *args, **kwargs: "a" * 64,
    )
    monkeypatch.setattr(
        service_module,
        "load_mailbox_account",
        lambda *args: (_ for _ in ()).throw(Microsoft365Error("temporarily unavailable")),
    )
    healthy_mailbox = FreshGmail()
    monkeypatch.setattr(
        service_module,
        "load_configured_mailbox",
        lambda *args: MailboxSession("gmail", "gmail-default", healthy_mailbox),
    )

    result = run_watcher_check(cfg, store, ExtractionModel([]), deliver_notifications=False)

    current = store.automation_run(admitted.run_id)
    assert result["active"] is True
    assert current is not None
    assert current.state == "detected"
    assert healthy_mailbox.full_payload_calls == 1


def test_transient_source_failure_does_not_starve_newer_runnable_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    _account_id, failed = admit_scheduling_run(
        store,
        provider_message_id="older-failure",
    )
    _account_id, runnable = admit_scheduling_run(
        store,
        provider_message_id="runnable",
    )
    allow_automation_processing(monkeypatch, FailingOlderAutomationProvider())
    model = ExtractionModel([valid_scheduling_output()])

    result = process_scheduling_automations(cfg, store, model, limit=1)

    failed_current = store.automation_run(failed.run_id)
    runnable_current = store.automation_run(runnable.run_id)
    assert failed_current is not None
    assert failed_current.state == "detected"
    assert runnable_current is not None
    assert runnable_current.state == "proposing"
    assert result.processed == 1
    assert result.attempted_run_ids == frozenset({failed.run_id, runnable.run_id})


def test_scheduling_source_bounds_attachment_metadata_before_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    _account_id, admitted = admit_scheduling_run(store)
    allow_automation_processing(monkeypatch, MetadataHeavyAutomationProvider())
    model = ExtractionModel([valid_scheduling_output()])

    result = process_scheduling_automations(cfg, store, model)

    current = store.automation_run(admitted.run_id)
    bounded_source = model.extraction_sources[0]
    assert result.processed == 1
    assert len(bounded_source.attachment_names) == MAX_GATEWAY_ATTACHMENT_COUNT
    assert all(
        len(name) <= MAX_GATEWAY_ATTACHMENT_NAME_CHARS
        for name in bounded_source.attachment_names
    )
    assert current is not None
    assert current.source_content_sha256 == scheduling_source_sha256(bounded_source)


def test_scheduling_source_sanitizes_unicode_before_hash_and_inference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    _account_id, admitted = admit_scheduling_run(store)
    allow_automation_processing(monkeypatch, MalformedUnicodeAutomationProvider())
    model = ExtractionModel([valid_scheduling_output()])

    result = process_scheduling_automations(cfg, store, model)

    current = store.automation_run(admitted.run_id)
    bounded_source = model.extraction_sources[0]
    assert result.processed == 1
    assert "\ud800" not in bounded_source.body
    assert bounded_source.body.encode("utf-8").endswith(b"?")
    assert current is not None
    assert current.source_content_sha256 == scheduling_source_sha256(bounded_source)


def test_recovery_purges_expired_source_before_fetch_or_inference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    _account_id, admitted = admit_scheduling_run(
        store,
        received_at="2026-01-01T12:00:00+00:00",
    )
    monkeypatch.setattr(
        service_module,
        "_scheduling_authorization_principal",
        lambda *args, **kwargs: "a" * 64,
    )
    monkeypatch.setattr(
        service_module,
        "load_mailbox_account",
        lambda *args: pytest.fail("Expired recovery fetched mailbox content"),
    )
    model = ExtractionModel([])

    result = process_scheduling_automations(
        cfg,
        store,
        model,
        now=datetime(2026, 9, 7, 13, tzinfo=UTC),
    )

    current = store.automation_run(admitted.run_id)
    assert current is not None
    assert (current.state, current.failure_code) == (
        "source_unavailable",
        "source_unavailable",
    )
    assert result.processed == 0
    assert model.extraction_calls == []


def test_unauthorized_oldest_page_does_not_starve_newer_runnable_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    for index in range(25):
        admit_scheduling_run(
            store,
            provider_message_id=f"unauthorized-{index:02d}",
            received_at="2026-09-07T12:00:00+00:00",
            principal_key="b" * 64,
        )
    _account_id, runnable = admit_scheduling_run(
        store,
        provider_message_id="runnable",
        received_at="2026-09-07T12:00:00+00:00",
    )
    monkeypatch.setattr(
        service_module,
        "_scheduling_authorization_principal",
        lambda *args, **kwargs: "a" * 64 if kwargs["expected_principal_key"] == "a" * 64 else None,
    )
    monkeypatch.setattr(
        service_module,
        "load_mailbox_account",
        lambda config, store, provider, account_id: MailboxSession(
            provider,
            account_id,
            AnyAutomationGateway(),
        ),
    )
    model = ExtractionModel([valid_scheduling_output()])

    result = process_scheduling_automations(
        cfg,
        store,
        model,
        now=datetime(2026, 9, 7, 13, tzinfo=UTC),
    )

    current = store.automation_run(runnable.run_id)
    assert current is not None
    assert current.state == "proposing"
    assert result.processed == 1
    assert result.attempted_run_ids == frozenset({runnable.run_id})


def test_watcher_uses_provider_polling_session_for_the_complete_check(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    store.set_state("100")
    gateway = PollScopedGmail()

    result = Watcher(cfg, store, gateway, FakeModel()).check()

    assert result["discovered"] == 1
    assert result["summarized"] == 1
    assert gateway.polling_sessions == 1
    assert gateway.in_polling_session is False


def test_invalid_metadata_is_skipped_without_blocking_the_cursor(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    store.set_state("100")

    result = Watcher(cfg, store, InvalidMetadataGmail(), FakeModel()).check()

    assert result["discovered"] == 0
    assert result["summarized"] == 0
    assert store.state()[0] == "200"


def test_watcher_scopes_sync_and_source_fetch_to_mailbox_session(
    tmp_path: Path,
) -> None:
    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    store.set_state("100", provider="microsoft365", account_id="account-2")
    gateway = FakeGmail()
    session = MailboxSession("microsoft365", "account-2", gateway)

    result = Watcher(cfg, store, session, FakeModel()).check()

    local_id = scoped_message_id("microsoft365", "account-2", "allowed")
    item = store.recent(1)[0]
    assert result["discovered"] == 1
    assert item["message_id"] == local_id
    assert item["provider"] == "microsoft365"
    assert item["account_id"] == "account-2"
    assert gateway.full_payload_calls == 1
    assert store.message_source(local_id).provider_message_id == "allowed"
    assert store.state(provider="microsoft365", account_id="account-2")[0] == "200"
    assert store.state() is None


def test_watcher_does_not_fetch_pending_content_from_another_account(
    tmp_path: Path,
) -> None:
    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    store.set_state("100")
    other_local_id = scoped_message_id("microsoft365", "account-2", "other-message")
    store.add_message(
        message_id=other_local_id,
        provider="microsoft365",
        account_id="account-2",
        provider_message_id="other-message",
        thread_id=None,
        sender="trusted@example.com",
        sender_name="Trusted",
        subject="Other account",
        received_at=datetime.now(UTC).isoformat(),
    )
    gateway = FakeGmail()
    gateway.history_message_ids = lambda cursor: ([], "200")

    result = Watcher(cfg, store, gateway, FakeModel()).check()

    assert result["summarized"] == 0
    assert gateway.full_payload_calls == 0
    assert [item.message_id for item in store.pending()] == [other_local_id]


def test_attachment_inventory_is_durable_before_model_failure(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    store.set_state("100", datetime(2026, 7, 18, tzinfo=UTC))
    model = FailingCaptureModel()

    result = Watcher(cfg, store, AttachmentGmail(), model).check()

    assert result["summarized"] == 0
    assert model.attachment_names == ("invoice.pdf",)
    assert store.recent(1)[0]["attachments"] == [
        {
            "part_id": "2",
            "attachment_id": "gmail-attachment",
            "filename": "invoice.pdf",
            "media_type": "application/pdf",
            "byte_size": 1234,
        }
    ]


def test_permanently_invalid_mailbox_content_does_not_retry_forever(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    store.set_state("100", datetime(2026, 7, 18, tzinfo=UTC))
    model = FakeModel()
    watcher = Watcher(cfg, store, InvalidContentGmail(), model)

    assert watcher.check()["summarized"] == 0
    failed = store.recent(1)[0]
    assert failed["analysis_retryable"] is False
    assert failed["analysis_error_code"] == "message_too_large"
    assert watcher.check()["summarized"] == 0
    assert model.calls == 0


def test_failed_fallback_for_permanent_content_error_retries_without_analysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = replace(config(tmp_path), notifications_enabled=True)
    store = Store(cfg.database_file)
    store.initialize()
    store.set_state("100", datetime(2026, 7, 18, tzinfo=UTC))
    model = FakeModel()
    attempts = 0

    def flaky_fallback(*_args, **_kwargs) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise NotificationError("all channels unavailable")

    monkeypatch.setattr(service_module, "send_fallback", flaky_fallback)
    watcher = Watcher(cfg, store, InvalidContentGmail(), model)

    assert watcher.check()["fallback_notified"] == 0
    assert store.notification_intents()[0].kind == "fallback"
    assert watcher.check()["fallback_notified"] == 1
    assert store.notification_intents() == []
    assert attempts == 2
    assert model.calls == 0


def test_zero_sender_watchlist_is_inactive_without_gmail_or_state(
    tmp_path: Path,
) -> None:
    cfg = replace(config(tmp_path), senders=(), notifications_enabled=True)
    store = Store(cfg.database_file)
    store.initialize()
    for message_id in ("expired", "queued"):
        store.add_message(
            message_id=message_id,
            thread_id=None,
            sender="former@example.com",
            sender_name="Former",
            subject=message_id,
            received_at="2020-01-01T00:00:00+00:00",
        )
    store.mark_skipped("expired")
    store.mark_analyzed("queued", FakeModel().analyze().model_dump())
    with store.connection() as db:
        db.execute(
            "UPDATE messages SET discovered_at = ?",
            ("2020-01-01T00:00:00+00:00",),
        )

    class UnexpectedGmail:
        def __getattr__(self, name: str):
            raise AssertionError(f"Gmail must not be called: {name}")

    result = Watcher(cfg, store, UnexpectedGmail(), FakeModel()).check()

    assert result == {
        "active": False,
        "discovered": 0,
        "fallback_notified": 0,
        "purged": 2,
        "stale_cursor_recovered": False,
        "summarized": 0,
    }
    assert store.recent(10) == []
    assert store.notification_intents() == []


def test_stale_cursor_recovers_with_search(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    store.set_state("old", datetime(2026, 7, 18, tzinfo=UTC))
    result = Watcher(cfg, store, FakeGmail(stale=True), FakeModel()).check()
    assert result["stale_cursor_recovered"] is True
    assert store.state()[0] == "200"


def test_stale_cursor_recovery_is_bounded_by_retention_before_content_fetch(
    tmp_path: Path,
) -> None:
    cfg = replace(config(tmp_path), retention_days=1)
    store = Store(cfg.database_file)
    store.initialize()
    store.set_state("old", datetime.now(UTC) - service_module.timedelta(days=30))
    gmail = FakeGmail(stale=True)
    model = FakeModel()
    before_cutoff = datetime.now(UTC) - service_module.timedelta(days=1, seconds=1)

    result = Watcher(cfg, store, gmail, model).check()

    after_cutoff = datetime.now(UTC) - service_module.timedelta(days=1)
    assert result["stale_cursor_recovered"] is True
    assert result["discovered"] == 0
    assert gmail.full_payload_calls == 0
    assert model.calls == 0
    assert gmail.search_since_value is not None
    assert before_cutoff <= gmail.search_since_value <= after_cutoff


def test_future_received_time_is_clamped_before_persistence(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    store.set_state("100", datetime.now(UTC))
    gmail = FutureDatedGmail()
    before = datetime.now(UTC)

    result = Watcher(cfg, store, gmail, FakeModel()).check()

    after = datetime.now(UTC)
    stored = datetime.fromisoformat(store.recent(1)[0]["received_at"])
    assert result["discovered"] == 1
    assert before <= stored <= after


def test_invalid_source_time_is_rejected_before_content_fetch(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    store.set_state("100", datetime.now(UTC))
    gmail = InvalidDatedGmail()
    model = FakeModel()

    result = Watcher(cfg, store, gmail, model).check()

    assert result["discovered"] == 0
    assert gmail.full_payload_calls == 0
    assert model.calls == 0
    assert store.recent(10) == []


def test_check_reuses_one_retention_snapshot_for_both_purges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    store.set_state("100", datetime.now(UTC))
    gmail = FreshGmail()
    purge_times: list[datetime | None] = []
    real_purge = store.purge

    def record_purge(retention_days: int, *, now: datetime | None = None) -> int:
        purge_times.append(now)
        return real_purge(retention_days, now=now)

    monkeypatch.setattr(store, "purge", record_purge)

    Watcher(cfg, store, gmail, FakeModel()).check(deliver_notifications=False)

    assert len(purge_times) == 2
    assert purge_times[0] is not None
    assert purge_times[0] == purge_times[1]


@pytest.mark.parametrize(("dry_run", "expected_purged"), [(False, 1), (True, 0)])
def test_expired_pending_message_is_excluded_before_content_fetch(
    tmp_path: Path, dry_run: bool, expected_purged: int
) -> None:
    cfg = replace(config(tmp_path), retention_days=1)
    store = Store(cfg.database_file)
    store.initialize()
    store.set_state("100", datetime.now(UTC))
    store.add_message(
        message_id="expired-pending",
        thread_id=None,
        sender="trusted@example.com",
        sender_name="Trusted",
        subject="Expired",
        received_at=(datetime.now(UTC) - service_module.timedelta(days=2)).isoformat(),
    )
    gmail = FakeGmail()
    gmail.history_message_ids = lambda cursor: ([], "200")
    model = FakeModel()

    result = Watcher(cfg, store, gmail, model).check(dry_run=dry_run)

    assert result["purged"] == expected_purged
    assert gmail.full_payload_calls == 0
    assert model.calls == 0
    assert (store.recent(10) == []) is (not dry_run)


def test_dry_run_ignores_pending_source_time_that_overflows_utc(
    tmp_path: Path,
) -> None:
    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    store.set_state("100", datetime.now(UTC))
    store.add_message(
        message_id="overflowing-source-time",
        thread_id=None,
        sender="trusted@example.com",
        sender_name="Trusted",
        subject="Malformed",
        received_at="0001-01-01T00:00:00+23:59",
    )
    gmail = FakeGmail()
    gmail.history_message_ids = lambda cursor: ([], "200")
    model = FakeModel()

    result = Watcher(cfg, store, gmail, model).check(dry_run=True)

    assert result["purged"] == 0
    assert gmail.full_payload_calls == 0
    assert model.calls == 0
    assert [item["message_id"] for item in store.recent(10)] == ["overflowing-source-time"]


def test_dry_run_does_not_advance_cursor_or_store_messages(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    store.set_state("100", datetime(2026, 7, 18, tzinfo=UTC))
    result = Watcher(cfg, store, FakeGmail(), FakeModel()).check(dry_run=True)
    assert result["discovered"] == 1
    assert store.state()[0] == "100"
    assert store.recent(10) == []


class VanishingMetadataGmail(FakeGmail):
    """A message the history feed reported, but that 404s on metadata fetch
    because it was deleted/expunged in the meantime."""

    def history_message_ids(self, cursor: str):
        return ["allowed", "vanished"], "200"

    def metadata(self, message_id: str) -> MessageMetadata:
        if message_id == "vanished":
            raise MessageUnavailable(f"Gmail message {message_id} unavailable (HTTP 404)")
        return super().metadata(message_id)


class VanishingBodyGmail(FakeGmail):
    """Metadata succeeds, but the body 404s (message deleted between the two calls)."""

    def full_payload(self, message_id: str):
        raise MessageUnavailable(f"Gmail message {message_id} unavailable (HTTP 404)")


def test_metadata_404_skips_message_and_still_advances_cursor(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    store.set_state("100", datetime(2026, 7, 18, tzinfo=UTC))
    # Must NOT raise, even though one message 404s on metadata fetch.
    result = Watcher(cfg, store, VanishingMetadataGmail(), FakeModel()).check()
    assert result["discovered"] == 1  # only the surviving message
    assert result["summarized"] == 1
    assert store.state()[0] == "200"  # cursor advances despite the vanished message


def test_body_404_skips_pending_without_crashing(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    store.set_state("100", datetime(2026, 7, 18, tzinfo=UTC))
    result = Watcher(cfg, store, VanishingBodyGmail(), FakeModel()).check()
    assert result["discovered"] == 1  # discovered + stored
    assert result["summarized"] == 0  # body gone -> skipped, not summarized, no crash
    # Dropped from the pending queue (status 'skipped') so a re-run is a no-op.
    assert store.recent(10)[0]["status"] == "skipped"
    assert Watcher(cfg, store, VanishingBodyGmail(), FakeModel()).check()["summarized"] == 0


def _make_retries_due(store: Store) -> None:
    with store.connection() as db:
        db.execute("UPDATE messages SET next_retry_at = NULL")


def test_notification_retry_uses_persisted_analysis(tmp_path: Path, monkeypatch) -> None:
    cfg = replace(config(tmp_path), notifications_enabled=True)
    store = Store(cfg.database_file)
    store.initialize()
    store.set_state("100", datetime(2026, 7, 18, tzinfo=UTC))
    gmail = FakeGmail()
    model = FakeModel()
    analysis_attempts = 0

    def flaky_analysis_notification(*args, **kwargs) -> None:
        nonlocal analysis_attempts
        analysis_attempts += 1
        if analysis_attempts == 1:
            raise NotificationError("all channels unavailable")

    def unavailable_fallback(*args, **kwargs) -> None:
        raise NotificationError("all channels unavailable")

    monkeypatch.setattr(service_module, "send_analysis", flaky_analysis_notification)
    monkeypatch.setattr(service_module, "send_fallback", unavailable_fallback)
    watcher = Watcher(cfg, store, gmail, model)

    first = watcher.check()
    assert first["summarized"] == 1
    assert store.recent(1)[0]["status"] == "analyzed"
    assert gmail.full_payload_calls == 1
    assert model.calls == 1

    _make_retries_due(store)
    second = watcher.check()
    assert second["summarized"] == 0
    assert store.recent(1)[0]["status"] == "summarized"
    assert analysis_attempts == 2
    assert gmail.full_payload_calls == 1
    assert model.calls == 1


class FailOnceModel(FakeModel):
    def analyze(self, **kwargs) -> Analysis:
        self.calls += 1
        if self.calls == 1:
            raise ModelError("local model unavailable")
        return Analysis(
            category="informational",
            priority="normal",
            summary="A short update.",
            action_required=False,
            suggested_action=None,
            deadline_text=None,
            deadline_iso=None,
            confidence=0.9,
        )


class GatewayRetryModel(FakeModel):
    def __init__(self, *, retryable: bool):
        super().__init__()
        self.retryable = retryable
        self.requests: list[tuple[str | None, datetime]] = []

    def analyze(self, **kwargs) -> Analysis:
        self.calls += 1
        self.requests.append((kwargs["request_id"], kwargs["current_local_time"]))
        if self.calls == 1 or not self.retryable:
            raise GatewayModelError(
                "worker_unavailable" if self.retryable else "forbidden",
                retryable=self.retryable,
                retry_after_seconds=30 if self.retryable else None,
            )
        return Analysis(
            category="informational",
            priority="normal",
            summary="A short update.",
            action_required=False,
            suggested_action=None,
            deadline_text=None,
            deadline_iso=None,
            confidence=0.9,
        )


def test_gateway_retry_reuses_durable_request_identity(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    store.set_state("100", datetime(2026, 7, 18, tzinfo=UTC))
    model = GatewayRetryModel(retryable=True)
    watcher = Watcher(cfg, store, FakeGmail(), model)

    assert watcher.check()["summarized"] == 0
    first = store.recent(1)[0]
    assert first["analysis_retryable"] == 1
    assert first["analysis_retry_after_seconds"] == 30

    _make_retries_due(store)
    retry_watcher = Watcher(replace(cfg, timezone="America/New_York"), store, FakeGmail(), model)
    assert retry_watcher.check()["summarized"] == 1
    assert model.requests[0] == model.requests[1]
    assert store.recent(1)[0]["status"] == "summarized"


def test_gateway_permanent_failure_waits_for_explicit_requeue(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    store.set_state("100", datetime(2026, 7, 18, tzinfo=UTC))
    model = GatewayRetryModel(retryable=False)
    watcher = Watcher(cfg, store, FakeGmail(), model)

    assert watcher.check()["summarized"] == 0
    first_request_id = model.requests[0][0]
    assert store.recent(1)[0]["analysis_retryable"] == 0
    assert watcher.check()["summarized"] == 0
    assert model.calls == 1

    store.requeue_analysis("allowed")
    assert watcher.check()["summarized"] == 0
    assert model.calls == 2
    assert model.requests[1][0] != first_request_id


def test_fallback_does_not_suppress_recovered_analysis(tmp_path: Path, monkeypatch) -> None:
    cfg = replace(config(tmp_path), notifications_enabled=True)
    store = Store(cfg.database_file)
    store.initialize()
    store.set_state("100", datetime(2026, 7, 18, tzinfo=UTC))
    model = FailOnceModel()
    delivered: list[str] = []

    monkeypatch.setattr(
        service_module,
        "send_fallback",
        lambda *args, **kwargs: delivered.append("fallback"),
    )
    monkeypatch.setattr(
        service_module,
        "send_analysis",
        lambda *args, **kwargs: delivered.append("analysis"),
    )
    watcher = Watcher(cfg, store, FakeGmail(), model)

    first = watcher.check()
    assert first["fallback_notified"] == 1
    assert delivered == ["fallback"]

    _make_retries_due(store)
    second = watcher.check()
    assert second["summarized"] == 1
    assert delivered == ["fallback", "analysis"]
    assert store.recent(1)[0]["status"] == "summarized"

    watcher.check()
    assert delivered == ["fallback", "analysis"]
    assert model.calls == 2


def test_deferred_delivery_retention_expires_queued_analysis(tmp_path: Path, monkeypatch) -> None:
    cfg = replace(config(tmp_path), notifications_enabled=True, retention_days=1)
    store = Store(cfg.database_file)
    store.initialize()
    store.set_state("100", datetime.now(UTC))
    monkeypatch.setattr(
        service_module,
        "send_analysis",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("host-deferred check must not send a Linux notification")
        ),
    )

    gmail = FreshGmail()
    result = Watcher(cfg, store, gmail, FakeModel()).check(deliver_notifications=False)

    assert result["summarized"] == 1
    assert store.recent(1)[0]["status"] == "analyzed"
    assert store.notification_intents()[0].kind == "analysis"

    old = (datetime.now(UTC) - service_module.timedelta(days=2)).isoformat()
    with store.connection() as db:
        db.execute("UPDATE messages SET received_at = ?", (old,))
    gmail.history_message_ids = lambda cursor: ([], "200")

    watcher = Watcher(cfg, store, gmail, FakeModel())
    watcher.check(deliver_notifications=False)

    assert store.notification_intents() == []
    assert store.recent(1) == []


def test_expired_notification_intent_is_removed_before_cli_delivery(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = replace(config(tmp_path), notifications_enabled=True, retention_days=1)
    store = Store(cfg.database_file)
    store.initialize()
    store.set_state("100", datetime(2026, 7, 18, tzinfo=UTC))
    store.add_message(
        message_id="old-analysis",
        thread_id=None,
        sender="trusted@example.com",
        sender_name="Trusted",
        subject="Action needed",
        received_at=(datetime.now(UTC) - service_module.timedelta(days=2)).isoformat(),
    )
    store.mark_analyzed(
        "old-analysis",
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
    gmail = FakeGmail()
    gmail.history_message_ids = lambda cursor: ([], "200")
    monkeypatch.setattr(
        service_module,
        "send_analysis",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            NotificationError("all channels unavailable")
        ),
    )
    monkeypatch.setattr(
        service_module,
        "send_fallback",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            NotificationError("all channels unavailable")
        ),
    )

    Watcher(cfg, store, gmail, FakeModel()).check(deliver_notifications=True)

    assert store.notification_intents() == []
    assert store.recent(1) == []


def test_deferred_fallback_ack_preserves_eventual_analysis_intent(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = replace(config(tmp_path), notifications_enabled=True)
    store = Store(cfg.database_file)
    store.initialize()
    store.set_state("100", datetime(2026, 7, 18, tzinfo=UTC))
    model = FailOnceModel()
    monkeypatch.setattr(
        service_module,
        "send_fallback",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("host-deferred check must not send a Linux fallback")
        ),
    )
    watcher = Watcher(cfg, store, FakeGmail(), model)

    first = watcher.check(deliver_notifications=False)
    assert first["fallback_notified"] == 0
    assert store.notification_intents()[0].kind == "fallback"
    assert store.acknowledge_notification(message_id="allowed", kind="fallback") == "acknowledged"

    _make_retries_due(store)
    second = watcher.check(deliver_notifications=False)
    assert second["summarized"] == 1
    assert store.notification_intents()[0].kind == "analysis"


def test_deferred_delivery_completes_without_intent_when_notifications_disabled(
    tmp_path: Path,
) -> None:
    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    store.set_state("100", datetime(2026, 7, 18, tzinfo=UTC))

    Watcher(cfg, store, FakeGmail(), FakeModel()).check(deliver_notifications=False)

    assert store.recent(1)[0]["status"] == "summarized"
    assert store.notification_intents() == []
