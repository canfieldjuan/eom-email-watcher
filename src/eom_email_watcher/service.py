from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from .config import Config
from .db import AnalyzedMessage, PendingMessage, Store
from .gmail import GmailGateway, MessageUnavailable, StaleHistoryCursor
from .mime import extract_body
from .model import Analysis, GatewayModelError, ModelError, ModelRuntime
from .notifications import NotificationError, send_analysis, send_fallback

logger = logging.getLogger(__name__)


class Watcher:
    def __init__(self, config: Config, store: Store, gmail: GmailGateway, model: ModelRuntime):
        self.config = config
        self.store = store
        self.gmail = gmail
        self.model = model
        self.sender_names = {sender.email: sender.name for sender in config.senders}

    def bootstrap(self) -> str:
        history_id = self.gmail.profile_history_id()
        self.store.set_state(history_id)
        return history_id

    @staticmethod
    def inactive_result(
        config: Config, store: Store, *, dry_run: bool
    ) -> dict[str, int | bool]:
        purged = (
            0
            if dry_run
            else store.purge(
                config.retention_days,
                preserve_notification_intents=config.notifications_enabled,
            )
        )
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
        state = self.store.state()
        if not state:
            raise RuntimeError("Watcher is not initialized. Run: eom-mail-watch setup")
        cursor, last_success = state
        recovered = False
        try:
            message_ids, newest_cursor = self.gmail.history_message_ids(cursor)
        except StaleHistoryCursor:
            recovered = True
            since = datetime.fromisoformat(last_success).astimezone(UTC) - timedelta(minutes=5)
            message_ids = self.gmail.search_since(self.config.allowlist, since)
            newest_cursor = self.gmail.profile_history_id()

        added = 0
        dry_run_messages: list[PendingMessage] = []
        for message_id in message_ids:
            if self.store.has_message(message_id):
                continue
            try:
                metadata = self.gmail.metadata(message_id)
            except MessageUnavailable as exc:
                logger.info("Skipping message %s (gone before fetch): %s", message_id, exc)
                continue
            if "INBOX" not in metadata.labels or metadata.sender not in self.config.allowlist:
                continue
            values = {
                "message_id": metadata.message_id,
                "thread_id": metadata.thread_id,
                "sender": metadata.sender,
                "sender_name": metadata.sender_name or self.sender_names.get(metadata.sender),
                "subject": metadata.subject,
                "received_at": metadata.received_at,
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
            self.store.set_state(newest_cursor)
        summarized, fallback = self._process_pending(
            dry_run=dry_run,
            deliver_notifications=deliver_notifications,
            extra=dry_run_messages,
        )
        preserve_notification_intents = self.config.notifications_enabled
        purged = (
            0
            if dry_run
            else self.store.purge(
                self.config.retention_days,
                preserve_notification_intents=preserve_notification_intents,
            )
        )
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
    ) -> tuple[int, int]:
        summarized = 0
        fallback = 0
        for message in self.store.pending_delivery():
            if deliver_notifications:
                fallback += self._deliver_analysis(
                    message, self._stored_analysis(message), dry_run
                )
            elif not self.config.notifications_enabled and not dry_run:
                self.store.mark_delivery_complete(message.message_id, notified=False)
        for message in [*self.store.pending(), *(extra or [])]:
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
                    current_local_time = datetime.fromisoformat(
                        request.context_at
                    ).astimezone(self.config.zone)
                payload = self.gmail.full_payload(message.message_id)
                body, attachment_names, attachments = extract_body(
                    payload, body_char_limit
                )
                if not dry_run:
                    self.store.replace_attachments(message.message_id, attachments)
                analysis = self.model.analyze(
                    sender=message.sender,
                    subject=message.subject,
                    received_at=message.received_at,
                    body=body,
                    attachment_names=attachment_names,
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
            except MessageUnavailable as exc:
                logger.info(
                    "Skipping pending message %s (gone before fetch): %s",
                    message.message_id,
                    exc,
                )
                if not dry_run:
                    self.store.mark_skipped(message.message_id)
                continue
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
