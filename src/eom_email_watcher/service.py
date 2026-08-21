from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from .config import Config
from .db import PendingMessage, Store
from .gmail import GmailGateway, MessageUnavailable, StaleHistoryCursor
from .mime import extract_body
from .model import LocalModel, ModelError
from .notifications import NotificationError, send_analysis, send_fallback

logger = logging.getLogger(__name__)


class Watcher:
    def __init__(self, config: Config, store: Store, gmail: GmailGateway, model: LocalModel):
        self.config = config
        self.store = store
        self.gmail = gmail
        self.model = model
        self.sender_names = {sender.email: sender.name for sender in config.senders}

    def bootstrap(self) -> str:
        history_id = self.gmail.profile_history_id()
        self.store.set_state(history_id)
        return history_id

    def check(self, *, dry_run: bool = False) -> dict[str, int | bool]:
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
                    PendingMessage(**values, attempts=0, fallback_notified_at=None)
                )
                added += 1
            elif self.store.add_message(**values):
                added += 1

        if not dry_run:
            self.store.set_state(newest_cursor)
        summarized, fallback = self._process_pending(dry_run=dry_run, extra=dry_run_messages)
        purged = 0 if dry_run else self.store.purge(self.config.retention_days)
        return {
            "discovered": added,
            "summarized": summarized,
            "fallback_notified": fallback,
            "purged": purged,
            "stale_cursor_recovered": recovered,
        }

    def _label(self, message: PendingMessage) -> str:
        return self.sender_names.get(message.sender) or message.sender_name or message.sender

    def _process_pending(
        self, *, dry_run: bool, extra: list[PendingMessage] | None = None
    ) -> tuple[int, int]:
        summarized = 0
        fallback = 0
        for message in [*self.store.pending(), *(extra or [])]:
            try:
                payload = self.gmail.full_payload(message.message_id)
                body, attachments = extract_body(payload, self.config.body_char_limit)
                analysis = self.model.analyze(
                    sender=message.sender,
                    subject=message.subject,
                    received_at=message.received_at,
                    body=body,
                    attachment_names=attachments,
                    current_local_time=datetime.now(self.config.zone),
                )
                should_notify = (
                    self.config.notifications_enabled and not message.fallback_notified_at
                )
                if should_notify:
                    send_analysis(
                        self._label(message),
                        message.subject,
                        analysis,
                        ntfy_topic=self.config.ntfy_topic,
                        ntfy_url=self.config.ntfy_url,
                        dry_run=dry_run,
                    )
                if not dry_run:
                    self.store.mark_summarized(
                        message.message_id, analysis.model_dump(), notified=should_notify
                    )
                summarized += 1
            except MessageUnavailable as exc:
                logger.info(
                    "Skipping pending message %s (gone before fetch): %s",
                    message.message_id,
                    exc,
                )
                if not dry_run:
                    self.store.mark_skipped(message.message_id)
                continue
            except (ModelError, NotificationError) as exc:
                logger.warning("Message %s summary unavailable: %s", message.message_id, exc)
                if self.config.notifications_enabled and not message.fallback_notified_at:
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
                        fallback += 1
                    except NotificationError as notify_exc:
                        logger.warning("Fallback notification unavailable: %s", notify_exc)
                if not dry_run:
                    self.store.record_failure(message.message_id, str(exc), message.attempts)
        return summarized, fallback
