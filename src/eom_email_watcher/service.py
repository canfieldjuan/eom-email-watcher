from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from .config import Config
from .db import AnalyzedMessage, PendingMessage, Store
from .mailbox import (
    MailboxGateway,
    MailboxMessageInvalid,
    MailboxMessageUnavailable,
    MailboxSession,
    StaleMailboxCursor,
    default_mailbox_session,
    scoped_message_id,
)
from .model import Analysis, GatewayModelError, ModelError, ModelRuntime
from .notifications import NotificationError, send_analysis, send_fallback

logger = logging.getLogger(__name__)


def _received_at_or_none(value: str, *, observed_at: datetime) -> datetime | None:
    try:
        received = datetime.fromisoformat(value)
        if received.tzinfo is None:
            return None
        return min(received.astimezone(UTC), observed_at)
    except (OverflowError, ValueError):
        return None


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

    def _label(self, message: PendingMessage | AnalyzedMessage) -> str:
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

    def _send_fallback(self, message: PendingMessage | AnalyzedMessage, dry_run: bool) -> int:
        if not self.config.notifications_enabled or message.fallback_notified_at:
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
                    self.store.mark_analyzed(message.message_id, analysis.model_dump())
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
