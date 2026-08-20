from datetime import UTC, datetime
from pathlib import Path

from eom_email_watcher.config import Config, Sender
from eom_email_watcher.db import Store
from eom_email_watcher.gmail import MessageMetadata, MessageUnavailable, StaleHistoryCursor
from eom_email_watcher.model import Analysis
from eom_email_watcher.service import Watcher


class FakeGmail:
    def __init__(self, stale: bool = False):
        self.stale = stale

    def profile_history_id(self) -> str:
        return "200"

    def history_message_ids(self, cursor: str):
        if self.stale:
            raise StaleHistoryCursor()
        return ["allowed", "blocked"], "200"

    def search_since(self, addresses, since):
        return ["allowed"]

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
        return {"mimeType": "text/plain", "body": {"data": "SGVsbG8="}}


class FakeModel:
    def analyze(self, **kwargs) -> Analysis:
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


def config(tmp_path: Path) -> Config:
    return Config(
        path=tmp_path / "config.toml",
        timezone="America/Chicago",
        body_char_limit=20_000,
        retention_days=180,
        gmail_credentials_file=tmp_path / "credentials.json",
        gmail_token_file=tmp_path / "token.json",
        gmail_send_token_file=tmp_path / "send-token.json",
        monthly_hours_recipient="maria@example.com",
        database_file=tmp_path / "db.sqlite3",
        model_base_url="http://127.0.0.1:1234/v1",
        model_name="model",
        model_api_token_file=tmp_path / "lm-token",
        model_require_auth=True,
        model_timeout_seconds=60,
        notifications_enabled=False,
        senders=(Sender("trusted@example.com", "Trusted"),),
    )


def test_exact_allowlist_and_dedup(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    store.set_state("100", datetime(2026, 7, 18, tzinfo=UTC))
    watcher = Watcher(cfg, store, FakeGmail(), FakeModel())
    result = watcher.check()
    assert result["discovered"] == 1
    assert result["summarized"] == 1
    assert len(store.recent(10)) == 1
    assert watcher.check()["discovered"] == 0


def test_stale_cursor_recovers_with_search(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    store = Store(cfg.database_file)
    store.initialize()
    store.set_state("old", datetime(2026, 7, 18, tzinfo=UTC))
    result = Watcher(cfg, store, FakeGmail(stale=True), FakeModel()).check()
    assert result["stale_cursor_recovered"] is True
    assert store.state()[0] == "200"


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
