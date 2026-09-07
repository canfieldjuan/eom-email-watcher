from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .config import Config, normalize_validated_address
from .db import (
    AnalyzedMessage,
    AutomationExtractionPayload,
    AutomationSourceChanged,
    AutomationWork,
    NotificationIntent,
    PendingMessage,
    Store,
)
from .entitlement import (
    AUTOMATIONS_FEATURE_ID,
    CONNECT_FEATURE_ID,
    feature_entitlements_active,
)
from .mailbox import (
    MailboxAccountUnavailable,
    MailboxError,
    MailboxGateway,
    MailboxMessageInvalid,
    MailboxMessageUnavailable,
    MailboxSession,
    StaleMailboxCursor,
    default_mailbox_session,
    mailbox_polling_session,
    scoped_message_id,
)
from .microsoft365 import (
    MICROSOFT365_PROVIDER,
    Microsoft365Error,
    MicrosoftAuthorizationRejected,
)
from .microsoft_calendar import MicrosoftCalendarProposalAuthorization
from .model import (
    MAX_GATEWAY_BODY_CHARS,
    MAX_GATEWAY_SENDER_CHARS,
    MAX_GATEWAY_SUBJECT_CHARS,
    Analysis,
    GatewayModelError,
    ModelError,
    ModelRuntime,
    bounded_gateway_attachment_names,
    bounded_gateway_text,
)
from .notifications import NotificationError, send_analysis, send_fallback, send_review
from .runtime import (
    load_configured_mailbox,
    load_mailbox_account,
    mail_account_token_file,
    microsoft_calendar_token_file,
)
from .scheduling import SchedulingSource, SchedulingViolation, scheduling_source_sha256

logger = logging.getLogger(__name__)


def _scheduling_authorization_principal(
    config: Config,
    store: Store,
    *,
    provider: str,
    account_id: str,
    expected_principal_key: str | None = None,
) -> str | None:
    if provider != MICROSOFT365_PROVIDER:
        return None
    if not feature_entitlements_active(CONNECT_FEATURE_ID, AUTOMATIONS_FEATURE_ID):
        return None
    account = store.mail_account(provider, account_id)
    if account is None or account.address is None:
        return None
    proposal_grant = store.calendar_grant(account_id, "proposal")
    if (
        proposal_grant is None
        or proposal_grant.state != "ready"
        or proposal_grant.principal_key is None
        or (
            expected_principal_key is not None
            and proposal_grant.principal_key != expected_principal_key
        )
    ):
        return None
    try:
        authorization = MicrosoftCalendarProposalAuthorization.from_matching_tokens(
            config.microsoft_credentials_file,
            microsoft_calendar_token_file(config, account, "proposal"),
            mail_account_token_file(config, account),
            proposal_grant.principal_key,
        )
    except MicrosoftAuthorizationRejected:
        store.revoke_calendar_grant_if_current(proposal_grant)
        return None
    except (MailboxAccountUnavailable, Microsoft365Error):
        return None
    if not feature_entitlements_active(CONNECT_FEATURE_ID, AUTOMATIONS_FEATURE_ID):
        return None
    principal_key = authorization.principal.key
    if expected_principal_key is not None and principal_key != expected_principal_key:
        return None
    return principal_key


def _scheduling_automation_principal(
    config: Config,
    store: Store,
    message: PendingMessage,
) -> str | None:
    return _scheduling_authorization_principal(
        config,
        store,
        provider=message.provider,
        account_id=message.account_id,
    )


@dataclass(frozen=True)
class AutomationProcessing:
    processed: int
    review_required: int
    attempted_run_ids: frozenset[str]
    purged: int


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _retry_feedback(payloads: list[AutomationExtractionPayload]) -> tuple[SchedulingViolation, ...]:
    rejected = next(
        (payload for payload in reversed(payloads) if payload.status == "rejected"),
        None,
    )
    if rejected is None or rejected.violations_json is None:
        return ()
    try:
        values = json.loads(rejected.violations_json)
    except (UnicodeDecodeError, ValueError):
        logger.error("Stored scheduling validation feedback is unreadable")
        return ()
    if not isinstance(values, list):
        return ()
    return tuple(
        SchedulingViolation(str(value.get("code", "")), str(value.get("path", "")))
        for value in values
        if isinstance(value, dict)
    )


def _accepted_extraction_outcome(intent: str) -> tuple[str, str | None]:
    if intent == "new_meeting":
        return "proposing", None
    if intent == "reschedule":
        return "manual_review", "reschedule_not_supported"
    if intent == "cancellation":
        return "manual_review", "cancellation_not_supported"
    return "ambiguous", "ambiguous_extraction"


def _transition_source_problem(
    store: Store,
    work: AutomationWork,
    *,
    next_state: str,
    failure_code: str,
) -> None:
    current = store.automation_run(work.run.run_id)
    if current is None or current.state not in {"detected", "extracting"}:
        return
    store.transition_automation_to_review(
        current.run_id,
        current.state_version,
        next_state=next_state,
        failure_code=failure_code,
    )


def process_scheduling_automations(
    config: Config,
    store: Store,
    model: ModelRuntime,
    *,
    exclude_run_ids: frozenset[str] = frozenset(),
    limit: int = 25,
    now: datetime | None = None,
) -> AutomationProcessing:
    observed_at = (now or _utc_now()).astimezone(UTC)
    purge_outcome = store.purge_with_outcome(config.retention_days, now=observed_at)
    processed = purge_outcome.automation_review_required
    review_required = purge_outcome.automation_review_required
    attempted: set[str] = set()
    capacity_used = 0
    cursor: tuple[str, str] | None = None
    while capacity_used < limit:
        page = store.recoverable_automation_runs(limit, after=cursor, now=observed_at)
        if not page:
            break
        for work in page:
            run = work.run
            cursor = (run.created_at, run.run_id)
            if run.run_id in exclude_run_ids:
                continue
            if capacity_used >= limit:
                break
            if (
                _scheduling_authorization_principal(
                    config,
                    store,
                    provider=run.provider,
                    account_id=run.account_id,
                    expected_principal_key=run.calendar_principal_key,
                )
                is None
            ):
                continue
            attempted.add(run.run_id)
            try:
                mailbox = load_mailbox_account(config, store, run.provider, run.account_id)
            except MailboxAccountUnavailable as exc:
                logger.info("Scheduling run %s mailbox unavailable: %s", run.run_id, exc)
                continue
            except MailboxError as exc:
                logger.warning(
                    "Scheduling run %s mailbox temporarily unavailable: %s",
                    run.run_id,
                    exc,
                )
                continue

            body_char_limit = run.extraction_body_char_limit or config.body_char_limit
            timezone = run.extraction_timezone or config.timezone
            try:
                if run.extraction_context_at is not None:
                    context_at = datetime.fromisoformat(run.extraction_context_at)
                else:
                    received_at = datetime.fromisoformat(work.received_at)
                    if received_at.tzinfo is None:
                        raise ValueError("Scheduling source time must be timezone-aware")
                    context_at = received_at.astimezone(config.zone)
                if context_at.tzinfo is None:
                    raise ValueError("Scheduling context must be timezone-aware")
                if run.state == "extracting" and work.extraction_organizer_address is None:
                    raise ValueError("Reserved extraction organizer is unavailable")
                organizer_address = normalize_validated_address(
                    work.extraction_organizer_address or work.organizer_address
                )
            except (OverflowError, ValueError):
                capacity_used += 1
                _transition_source_problem(
                    store,
                    work,
                    next_state="manual_review",
                    failure_code="extraction_context_invalid",
                )
                processed += 1
                review_required += 1
                continue
            try:
                with mailbox_polling_session(mailbox.gateway):
                    content = mailbox.gateway.content(work.provider_message_id, body_char_limit)
            except MailboxMessageUnavailable as exc:
                capacity_used += 1
                logger.info("Scheduling run %s source unavailable: %s", run.run_id, exc)
                _transition_source_problem(
                    store,
                    work,
                    next_state="source_unavailable",
                    failure_code="source_unavailable",
                )
                processed += 1
                review_required += 1
                continue
            except MailboxMessageInvalid as exc:
                capacity_used += 1
                logger.warning("Scheduling run %s source invalid: %s", run.run_id, exc)
                _transition_source_problem(
                    store,
                    work,
                    next_state="manual_review",
                    failure_code="source_invalid",
                )
                processed += 1
                review_required += 1
                continue
            except MailboxError as exc:
                logger.warning(
                    "Scheduling run %s source temporarily unavailable: %s",
                    run.run_id,
                    exc,
                )
                continue

            capacity_used += 1
            source = SchedulingSource(
                sender=bounded_gateway_text(work.sender, MAX_GATEWAY_SENDER_CHARS),
                subject=bounded_gateway_text(work.subject, MAX_GATEWAY_SUBJECT_CHARS),
                received_at=work.received_at,
                body=bounded_gateway_text(content.body, MAX_GATEWAY_BODY_CHARS),
                attachment_names=bounded_gateway_attachment_names(content.attachment_names),
                organizer_address=organizer_address,
                configured_timezone=timezone,
                context_at=context_at,
            )
            source_sha256 = scheduling_source_sha256(source)
            current = run
            while current.state in {"detected", "extracting"}:
                try:
                    reservation = store.reserve_automation_extraction(
                        current.run_id,
                        current.state_version,
                        source_content_sha256=source_sha256,
                        context_at=context_at.isoformat(),
                        timezone=timezone,
                        body_char_limit=body_char_limit,
                        organizer_address=organizer_address,
                    )
                except AutomationSourceChanged:
                    _transition_source_problem(
                        store,
                        work,
                        next_state="manual_review",
                        failure_code="source_changed",
                    )
                    processed += 1
                    review_required += 1
                    break
                reserved_run = store.automation_run(current.run_id)
                if (
                    reserved_run is None
                    or reserved_run.current_payload_id != reservation.payload_id
                ):
                    raise RuntimeError("Scheduling extraction reservation was not current")
                feedback = _retry_feedback(store.automation_extraction_payloads(current.run_id))
                try:
                    result = model.extract_scheduling(
                        source=source,
                        feedback=feedback,
                        request_id=reservation.request_id,
                    )
                except GatewayModelError as exc:
                    logger.warning("Scheduling run %s extraction unavailable: %s", run.run_id, exc)
                    failed = store.record_automation_extraction_failure(
                        current.run_id,
                        reserved_run.state_version,
                        payload_id=reservation.payload_id,
                        error_code=exc.code,
                        retryable=exc.retryable,
                        retry_after_seconds=exc.retry_after_seconds,
                        now=_utc_now(),
                    )
                    if failed.state == "manual_review":
                        processed += 1
                        review_required += 1
                    break
                except ModelError as exc:
                    logger.warning("Scheduling run %s extraction unavailable: %s", run.run_id, exc)
                    store.record_automation_extraction_failure(
                        current.run_id,
                        reserved_run.state_version,
                        payload_id=reservation.payload_id,
                        error_code="model_unavailable",
                        retryable=True,
                        now=_utc_now(),
                    )
                    break
                accepted_state: str | None = None
                accepted_code: str | None = None
                if result.accepted:
                    assert result.extraction is not None
                    accepted_state, accepted_code = _accepted_extraction_outcome(
                        result.extraction.intent
                    )
                current = store.record_automation_extraction(
                    current.run_id,
                    reserved_run.state_version,
                    payload_id=reservation.payload_id,
                    result_sha256=result.result_sha256,
                    result_json=result.result_json,
                    violations=[
                        {"code": violation.code, "path": violation.path}
                        for violation in result.violations
                    ],
                    accepted_state=accepted_state,
                    accepted_code=accepted_code,
                )
                if current.state == "extracting":
                    continue
                processed += 1
                if current.state in {"ambiguous", "manual_review", "source_unavailable"}:
                    review_required += 1
                break
    return AutomationProcessing(
        processed,
        review_required,
        frozenset(attempted),
        purge_outcome.messages,
    )


def _received_at_or_none(value: str, *, observed_at: datetime) -> datetime | None:
    try:
        received = datetime.fromisoformat(value)
        if received.tzinfo is None:
            return None
        return min(received.astimezone(UTC), observed_at)
    except (OverflowError, ValueError):
        return None


def _deliver_automation_review_intent(
    config: Config,
    store: Store,
    intent: NotificationIntent,
    sender_names: dict[str, str | None],
    *,
    dry_run: bool,
) -> bool:
    if not config.notifications_enabled:
        return False
    label = sender_names.get(intent.sender) or intent.sender_name or intent.sender
    try:
        send_review(
            label,
            intent.subject,
            intent.summary or "A scheduling mention needs manual review.",
            ntfy_topic=config.ntfy_topic,
            ntfy_url=config.ntfy_url,
            dry_run=dry_run,
        )
        if not dry_run:
            store.acknowledge_notification(
                message_id=intent.message_id,
                kind=intent.kind,
                analysis_at=intent.analysis_at,
                subject_type=intent.subject_type,
                subject_id=intent.subject_id,
                revision=intent.revision,
            )
        return True
    except NotificationError as exc:
        logger.warning("Automation review notification unavailable: %s", exc)
        return False


class Watcher:
    def __init__(
        self,
        config: Config,
        store: Store,
        mailbox: MailboxGateway | MailboxSession,
        model: ModelRuntime,
    ):
        self.config = config
        self.store = store
        self.mailbox = (
            mailbox if isinstance(mailbox, MailboxSession) else default_mailbox_session(mailbox)
        )
        self.gateway = self.mailbox.gateway
        self.model = model
        self.sender_names = {sender.email: sender.name for sender in config.senders}

    def bootstrap(self) -> str:
        cursor = self.gateway.initial_cursor()
        self.store.set_state(
            cursor,
            provider=self.mailbox.provider,
            account_id=self.mailbox.account_id,
        )
        return cursor

    @staticmethod
    def inactive_result(config: Config, store: Store, *, dry_run: bool) -> dict[str, int | bool]:
        purged = 0 if dry_run else store.purge(config.retention_days)
        return {
            "active": False,
            "discovered": 0,
            "summarized": 0,
            "fallback_notified": 0,
            "purged": purged,
            "stale_cursor_recovered": False,
        }

    def check(
        self, *, dry_run: bool = False, deliver_notifications: bool = True
    ) -> dict[str, int | bool]:
        if not self.config.senders:
            return self.inactive_result(self.config, self.store, dry_run=dry_run)
        with mailbox_polling_session(self.gateway):
            return self._check_active(
                dry_run=dry_run,
                deliver_notifications=deliver_notifications,
            )

    def _check_active(self, *, dry_run: bool, deliver_notifications: bool) -> dict[str, int | bool]:
        checked_at = datetime.now(UTC)
        retention_cutoff = checked_at - timedelta(days=self.config.retention_days)
        purged = 0 if dry_run else self.store.purge(self.config.retention_days, now=checked_at)
        state = self.store.state(
            provider=self.mailbox.provider,
            account_id=self.mailbox.account_id,
        )
        if not state:
            raise RuntimeError("Watcher is not initialized. Run: eom-mail-watch setup")
        cursor, last_success = state
        recovered = False
        try:
            changes = self.gateway.changes_since(cursor)
        except StaleMailboxCursor:
            recovered = True
            since = datetime.fromisoformat(last_success).astimezone(UTC) - timedelta(minutes=5)
            since = max(since, retention_cutoff)
            changes = self.gateway.recover_since(self.config.allowlist, since)

        added = 0
        dry_run_messages: list[PendingMessage] = []
        for provider_message_id in changes.message_ids:
            if self.store.has_seen_message(
                provider_message_id,
                provider=self.mailbox.provider,
                account_id=self.mailbox.account_id,
            ):
                continue
            try:
                metadata = self.gateway.metadata(provider_message_id)
            except MailboxMessageUnavailable as exc:
                logger.info(
                    "Skipping message %s (gone before fetch): %s",
                    provider_message_id,
                    exc,
                )
                continue
            except MailboxMessageInvalid as exc:
                logger.warning(
                    "Skipping message %s with unsafe metadata: %s",
                    provider_message_id,
                    exc,
                )
                continue
            if metadata.message_id != provider_message_id:
                raise RuntimeError("Mailbox metadata identity did not match the change record")
            if "INBOX" not in metadata.labels or metadata.sender not in self.config.allowlist:
                continue
            received_at = _received_at_or_none(metadata.received_at, observed_at=checked_at)
            if received_at is None or received_at < retention_cutoff:
                logger.info(
                    "Skipping message %s outside the configured retention window",
                    provider_message_id,
                )
                continue
            values = {
                "message_id": scoped_message_id(
                    self.mailbox.provider,
                    self.mailbox.account_id,
                    metadata.message_id,
                ),
                "provider": self.mailbox.provider,
                "account_id": self.mailbox.account_id,
                "provider_message_id": metadata.message_id,
                "thread_id": metadata.thread_id,
                "sender": metadata.sender,
                "sender_name": metadata.sender_name or self.sender_names.get(metadata.sender),
                "subject": metadata.subject,
                "received_at": received_at.isoformat(),
            }
            if dry_run:
                dry_run_messages.append(
                    PendingMessage(
                        **values,
                        attempts=0,
                        fallback_notified_at=None,
                        analysis_request_id=None,
                        analysis_context_at=None,
                        analysis_body_char_limit=None,
                    )
                )
                added += 1
            elif self.store.add_message(**values):
                added += 1

        if not dry_run:
            self.store.set_state(
                changes.cursor,
                provider=self.mailbox.provider,
                account_id=self.mailbox.account_id,
            )
        summarized, fallback = self._process_pending(
            dry_run=dry_run,
            deliver_notifications=deliver_notifications,
            extra=dry_run_messages,
            retention_cutoff=retention_cutoff,
            retention_observed_at=checked_at,
        )
        if not dry_run:
            purged += self.store.purge(self.config.retention_days, now=checked_at)
        return {
            "active": True,
            "discovered": added,
            "summarized": summarized,
            "fallback_notified": fallback,
            "purged": purged,
            "stale_cursor_recovered": recovered,
        }

    def _label(self, message: PendingMessage | AnalyzedMessage | NotificationIntent) -> str:
        return self.sender_names.get(message.sender) or message.sender_name or message.sender

    @staticmethod
    def _stored_analysis(message: AnalyzedMessage) -> Analysis:
        return Analysis(
            category=message.category,
            priority=message.priority,
            summary=message.summary,
            action_required=bool(message.action_required),
            suggested_action=message.suggested_action,
            deadline_text=message.deadline_text,
            deadline_iso=message.deadline_iso,
            confidence=message.confidence,
        )

    def _send_fallback(
        self, message: PendingMessage | AnalyzedMessage | NotificationIntent, dry_run: bool
    ) -> int:
        if not self.config.notifications_enabled or getattr(message, "fallback_notified_at", None):
            return 0
        try:
            send_fallback(
                self._label(message),
                message.subject,
                ntfy_topic=self.config.ntfy_topic,
                ntfy_url=self.config.ntfy_url,
                dry_run=dry_run,
            )
            if not dry_run:
                self.store.mark_fallback_notified(message.message_id)
            return 1
        except NotificationError as exc:
            logger.warning("Fallback notification unavailable: %s", exc)
            return 0

    def _deliver_automation_review(self, intent: NotificationIntent, dry_run: bool) -> int:
        return int(
            _deliver_automation_review_intent(
                self.config,
                self.store,
                intent,
                self.sender_names,
                dry_run=dry_run,
            )
        )

    def _deliver_analysis(
        self,
        message: PendingMessage | AnalyzedMessage,
        analysis: Analysis,
        dry_run: bool,
        attempts: int | None = None,
    ) -> int:
        if not self.config.notifications_enabled:
            if not dry_run:
                self.store.mark_delivery_complete(message.message_id, notified=False)
            return 0
        try:
            send_analysis(
                self._label(message),
                message.subject,
                analysis,
                ntfy_topic=self.config.ntfy_topic,
                ntfy_url=self.config.ntfy_url,
                dry_run=dry_run,
            )
        except NotificationError as exc:
            logger.warning("Message %s notification unavailable: %s", message.message_id, exc)
            fallback = self._send_fallback(message, dry_run)
            if not dry_run:
                self.store.record_failure(
                    message.message_id,
                    str(exc),
                    message.attempts if attempts is None else attempts,
                )
            return fallback
        if not dry_run:
            self.store.mark_delivery_complete(message.message_id, notified=True)
        return 0

    def _process_pending(
        self,
        *,
        dry_run: bool,
        deliver_notifications: bool,
        extra: list[PendingMessage] | None = None,
        retention_cutoff: datetime,
        retention_observed_at: datetime,
    ) -> tuple[int, int]:
        summarized = 0
        fallback = 0
        if deliver_notifications:
            for intent in self.store.notification_intents(kind="fallback"):
                fallback += self._send_fallback(intent, dry_run)
            for intent in self.store.notification_intents(kind="automation_review"):
                self._deliver_automation_review(intent, dry_run)
        for message in self.store.pending_delivery():
            received_at = _received_at_or_none(
                message.received_at, observed_at=retention_observed_at
            )
            if received_at is None or received_at < retention_cutoff:
                continue
            if deliver_notifications:
                fallback += self._deliver_analysis(message, self._stored_analysis(message), dry_run)
            elif not self.config.notifications_enabled and not dry_run:
                self.store.mark_delivery_complete(message.message_id, notified=False)
        for message in [
            *self.store.pending(
                provider=self.mailbox.provider,
                account_id=self.mailbox.account_id,
            ),
            *(extra or []),
        ]:
            received_at = _received_at_or_none(
                message.received_at, observed_at=retention_observed_at
            )
            if received_at is None or received_at < retention_cutoff:
                continue
            try:
                if dry_run:
                    request_id = None
                    body_char_limit = self.config.body_char_limit
                    current_local_time = datetime.now(self.config.zone)
                else:
                    request = self.store.reserve_analysis_request(
                        message.message_id,
                        self.config.body_char_limit,
                    )
                    request_id = request.request_id
                    body_char_limit = request.body_char_limit
                    current_local_time = datetime.fromisoformat(request.context_at)
                content = self.gateway.content(message.provider_message_id, body_char_limit)
                if not dry_run:
                    self.store.replace_attachments(message.message_id, content.attachments)
                analysis = self.model.analyze(
                    sender=message.sender,
                    subject=message.subject,
                    received_at=message.received_at,
                    body=content.body,
                    attachment_names=content.attachment_names,
                    current_local_time=current_local_time,
                    request_id=request_id,
                )
                if not dry_run:
                    scheduling_principal_key = (
                        _scheduling_automation_principal(self.config, self.store, message)
                        if analysis.category == "scheduling"
                        else None
                    )
                    self.store.mark_analyzed(
                        message.message_id,
                        analysis.model_dump(),
                        scheduling_automation_principal_key=scheduling_principal_key,
                    )
                summarized += 1
                if deliver_notifications:
                    fallback += self._deliver_analysis(message, analysis, dry_run, attempts=0)
                elif not self.config.notifications_enabled and not dry_run:
                    self.store.mark_delivery_complete(message.message_id, notified=False)
            except MailboxMessageUnavailable as exc:
                logger.info(
                    "Skipping pending message %s (gone before fetch): %s",
                    message.message_id,
                    exc,
                )
                if not dry_run:
                    self.store.mark_skipped(message.message_id)
                continue
            except MailboxMessageInvalid as exc:
                logger.warning("Message %s cannot be processed: %s", message.message_id, exc)
                if deliver_notifications:
                    fallback += self._send_fallback(message, dry_run)
                if not dry_run:
                    self.store.record_analysis_failure(
                        message.message_id,
                        str(exc),
                        message.attempts,
                        retryable=False,
                        error_code=exc.code,
                    )
            except GatewayModelError as exc:
                logger.warning("Message %s summary unavailable: %s", message.message_id, exc)
                if deliver_notifications:
                    fallback += self._send_fallback(message, dry_run)
                if not dry_run:
                    self.store.record_analysis_failure(
                        message.message_id,
                        str(exc),
                        message.attempts,
                        retryable=exc.retryable,
                        error_code=exc.code,
                        retry_after_seconds=exc.retry_after_seconds,
                    )
            except ModelError as exc:
                logger.warning("Message %s summary unavailable: %s", message.message_id, exc)
                if deliver_notifications:
                    fallback += self._send_fallback(message, dry_run)
                if not dry_run:
                    self.store.record_analysis_failure(
                        message.message_id,
                        str(exc),
                        message.attempts,
                        retryable=True,
                    )
        return summarized, fallback


def run_watcher_check(
    config: Config,
    store: Store,
    model: ModelRuntime,
    *,
    dry_run: bool = False,
    deliver_notifications: bool = True,
) -> dict[str, int | bool]:
    if dry_run:
        if not config.senders:
            result = Watcher.inactive_result(config, store, dry_run=True)
        else:
            mailbox = load_configured_mailbox(config, store)
            result = Watcher(config, store, mailbox, model).check(
                dry_run=True,
                deliver_notifications=deliver_notifications,
            )
        return {
            **result,
            "automation_processed": 0,
            "automation_review_required": 0,
        }

    before = process_scheduling_automations(config, store, model)
    if config.senders:
        mailbox = load_configured_mailbox(config, store)
        result = Watcher(config, store, mailbox, model).check(
            dry_run=False,
            deliver_notifications=deliver_notifications,
        )
    else:
        result = Watcher.inactive_result(config, store, dry_run=False)
    after = process_scheduling_automations(
        config,
        store,
        model,
        exclude_run_ids=before.attempted_run_ids,
    )
    if deliver_notifications and config.notifications_enabled:
        sender_names = {sender.email: sender.name for sender in config.senders}
        for intent in store.notification_intents(kind="automation_review"):
            _deliver_automation_review_intent(
                config,
                store,
                intent,
                sender_names,
                dry_run=False,
            )
    return {
        **result,
        "purged": int(result["purged"]) + before.purged + after.purged,
        "automation_processed": before.processed + after.processed,
        "automation_review_required": before.review_required + after.review_required,
    }
