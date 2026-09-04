from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import stat
import sys
from contextlib import contextmanager, nullcontext
from datetime import datetime
from pathlib import Path

from . import __version__
from .config import DEFAULT_CONFIG, ConfigError
from .db import Store
from .locking import operation_lock
from .mailbox import MailboxError
from .model import ModelRuntime
from .notifications import NotificationError, send_fallback
from .outbound import GmailSender, SendError, previous_month_email
from .runtime import (
    configured_mailbox_identity,
    load_configured_mailbox,
    load_runtime,
    mail_account_connected,
)
from .service import Watcher


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eom-mail-watch", description="Watch trusted Gmail senders using a local model"
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("-v", "--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("setup", help="Authorize Gmail and start from the current inbox state")
    commands.add_parser("setup-send", help="Authorize the separate Gmail send-only token")
    check = commands.add_parser("check", help="Poll Gmail once")
    check.add_argument("--dry-run", action="store_true", help="Analyze without changing state")
    commands.add_parser("doctor", help="Check local configuration and dependencies")
    recent = commands.add_parser("recent", help="Show recent watched-message results")
    recent.add_argument("--limit", type=int, default=20)
    requeue_analysis = commands.add_parser(
        "requeue-analysis", help="Retry one permanently paused message analysis"
    )
    requeue_analysis.add_argument("message_id")
    send_hours = commands.add_parser("send-hours", help="Send the monthly Firefly hours request")
    send_hours.add_argument("--test-to", help="Send a marked test without consuming monthly dedupe")
    send_hours.add_argument("--dry-run", action="store_true")
    outbound_status = commands.add_parser(
        "outbound-status", help="Inspect one outbound dedupe record without contacting Gmail"
    )
    outbound_status.add_argument("dedupe_key")
    outbound_resolve = commands.add_parser(
        "outbound-resolve", help="Resolve an outbound reservation after external verification"
    )
    outbound_resolve.add_argument("dedupe_key")
    resolution = outbound_resolve.add_mutually_exclusive_group(required=True)
    resolution.add_argument("--confirm-sent", metavar="GMAIL_MESSAGE_ID")
    resolution.add_argument("--confirm-unsent", action="store_true")
    return parser


def _runtime(config_path: Path) -> tuple[object, Store, ModelRuntime]:
    runtime = load_runtime(config_path)
    return runtime.config, runtime.store, runtime.model


@contextmanager
def _production_check_lock(database_file: Path):
    lock_path = database_file.with_name(f"{database_file.name}.check.lock")
    with operation_lock(lock_path, "Another production check is already running"):
        yield


@contextmanager
def _outbound_operation_lock(database_file: Path):
    lock_path = database_file.with_name(f"{database_file.name}.outbound.lock")
    with operation_lock(lock_path, "Another outbound operation is already running"):
        yield


def _doctor(config_path: Path) -> int:
    checks: dict[str, dict[str, object]] = {}
    try:
        config, store, model = _runtime(config_path)
        checks["config"] = {"ok": True, "path": str(config.path), "senders": len(config.senders)}
        checks["state_directory"] = {
            "ok": stat.S_IMODE(config.database_file.parent.stat().st_mode) == 0o700,
            "mode": oct(stat.S_IMODE(config.database_file.parent.stat().st_mode)),
        }
        provider, account_id = configured_mailbox_identity(store)
        active_account = store.mail_account(provider, account_id)
        assert active_account is not None
        checks["database"] = {
            "ok": True,
            "initialized": store.state(
                provider=provider,
                account_id=account_id,
            )
            is not None,
        }
        checks["oauth_credentials"] = {"ok": config.gmail_credentials_file.exists()}
        checks["oauth_token"] = {"ok": mail_account_connected(config, active_account)}
        checks["send_oauth_token"] = {
            "ok": config.gmail_send_token_file.exists() if config.monthly_hours_recipient else True
        }
        checks["model_api_token"] = {
            "ok": bool(config.model_api_token_file and config.model_api_token_file.exists())
            if config.model_require_auth
            else True
        }
        model_ok, detail = model.health()
        checks["local_model_server"] = {
            "ok": model_ok,
            "detail": detail,
            "url": config.model_base_url,
        }
        checks["notify_send"] = {"ok": shutil.which("notify-send") is not None}
    except ConfigError as exc:
        checks["config"] = {"ok": False, "error": str(exc)}
    print(json.dumps(checks, indent=2))
    return 0 if checks and all(bool(item.get("ok")) for item in checks.values()) else 1


def _setup(config_path: Path) -> int:
    from .engine_api import ApiError, dispatch

    config, _store, _model = _runtime(config_path)
    request = {
        "protocol": 1,
        "operation": "gmail.authorize",
        "config_path": str(config_path),
        "payload": {},
    }
    try:
        authorization = dispatch(request)
    except ApiError as exc:
        if exc.code != "account_identity_unverified":
            raise
        authorization = dispatch(
            {
                **request,
                "operation": "mail.accounts.connect",
                "payload": {"provider": "gmail"},
            }
        )
    if config.notifications_enabled:
        try:
            delivery = send_fallback(
                "EOM Email Watcher",
                "Setup complete",
                ntfy_topic=config.ntfy_topic,
                ntfy_url=config.ntfy_url,
                dry_run=False,
            )
            if delivery.failures:
                print(
                    f"Warning: notification test partially failed: {'; '.join(delivery.failures)}",
                    file=sys.stderr,
                )
        except NotificationError as exc:
            print(f"Warning: notification test failed: {exc}", file=sys.stderr)
    if authorization["baseline_initialized"]:
        print("Gmail authorized. Baseline initialized; no old mail imported.")
    else:
        print("Gmail authorization verified. Existing mailbox position preserved.")
    return 0


def _check(config_path: Path, dry_run: bool) -> int:
    config, store, model = _runtime(config_path)

    def run(active_config, active_store, active_model):
        if not active_config.senders:
            return Watcher.inactive_result(active_config, active_store, dry_run=dry_run)
        mailbox = load_configured_mailbox(active_config, active_store)
        return Watcher(active_config, active_store, mailbox, active_model).check(dry_run=dry_run)

    if dry_run:
        result = run(config, store, model)
    else:
        with _production_check_lock(config.database_file):
            result = run(*_runtime(config_path))
    print(json.dumps(result, indent=2))
    return 0


def _setup_send(config_path: Path) -> int:
    config, _store, _model = _runtime(config_path)
    if config.gmail_send_token_file.exists():
        GmailSender.from_token(config.gmail_send_token_file)
    else:
        GmailSender.authorize(config.gmail_credentials_file, config.gmail_send_token_file)
    print("Gmail send-only authorization is ready.")
    return 0


def _send_hours(config_path: Path, *, test_to: str | None, dry_run: bool) -> int:
    config, store, _model = _runtime(config_path)
    recipient = test_to or config.monthly_hours_recipient
    if not recipient or "@" not in recipient:
        raise ConfigError("monthly_hours_recipient must be configured")
    content = previous_month_email(datetime.now(config.zone).date())
    dedupe_key = f"monthly-hours:{content.period_key}"
    subject = f"[TEST] {content.subject}" if test_to else content.subject
    if dry_run:
        print(json.dumps({"to": recipient, "subject": subject, "body": content.body}, indent=2))
        return 0
    lock = nullcontext() if test_to else _outbound_operation_lock(store.path)
    with lock:
        if not test_to:
            status = store.outbound_status(dedupe_key)
            if status == "sent":
                print(json.dumps({"status": "already_sent", "period": content.period_key}))
                return 0
            if status:
                raise SendError(
                    f"Outbound send {dedupe_key} is {status} and requires manual reconciliation"
                )
        sender = GmailSender.from_token(config.gmail_send_token_file)
        if not test_to and not store.reserve_outbound(
            dedupe_key=dedupe_key, recipient=recipient, subject=subject
        ):
            status = store.outbound_status(dedupe_key)
            if status == "sent":
                print(json.dumps({"status": "already_sent", "period": content.period_key}))
                return 0
            raise SendError(
                f"Outbound send {dedupe_key} is {status or 'blocked'} and requires "
                "manual reconciliation"
            )
        try:
            message_id = sender.send(recipient, subject, content.body)
            if not test_to:
                store.record_outbound(
                    dedupe_key=dedupe_key,
                    recipient=recipient,
                    subject=subject,
                    gmail_message_id=message_id,
                )
        except Exception as exc:
            if not test_to:
                store.mark_outbound_ambiguous(dedupe_key, type(exc).__name__)
            raise
    print(json.dumps({"status": "sent", "to": recipient, "subject": subject}))
    return 0


def _outbound_status(config_path: Path, dedupe_key: str) -> int:
    _config, store, _model = _runtime(config_path)
    details = store.outbound_details(dedupe_key)
    print(json.dumps(details or {"dedupe_key": dedupe_key, "status": "not_found"}, indent=2))
    return 0


def _outbound_resolve(
    config_path: Path,
    dedupe_key: str,
    *,
    confirm_sent: str | None,
    confirm_unsent: bool,
) -> int:
    if (confirm_sent is not None) == confirm_unsent:
        raise RuntimeError("Exactly one outbound resolution confirmation is required")
    _config, store, _model = _runtime(config_path)
    with _outbound_operation_lock(store.path):
        if confirm_sent is not None:
            gmail_message_id = confirm_sent.strip()
            store.reconcile_outbound_sent(dedupe_key, gmail_message_id)
            result = {
                "action": "confirmed_sent",
                "dedupe_key": dedupe_key,
                "gmail_message_id": gmail_message_id,
                "status": "sent",
            }
        else:
            store.release_outbound(dedupe_key)
            result = {
                "action": "confirmed_unsent",
                "dedupe_key": dedupe_key,
                "status": "released_for_future_retry",
            }
    print(json.dumps(result, indent=2))
    return 0


def _recent(config_path: Path, limit: int) -> int:
    if not 1 <= limit <= 500:
        raise ConfigError("--limit must be between 1 and 500")
    _config, store, _model = _runtime(config_path)
    print(json.dumps(store.recent(limit), indent=2))
    return 0


def _requeue_analysis(config_path: Path, message_id: str) -> int:
    _config, store, _model = _runtime(config_path)
    try:
        status = store.requeue_analysis(message_id)
    except KeyError as exc:
        raise RuntimeError("Message was not found") from exc
    print(json.dumps({"message_id": message_id, "status": status}, indent=2))
    return 0


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    os.umask(0o077)
    try:
        if args.command == "doctor":
            code = _doctor(args.config)
        elif args.command == "setup":
            code = _setup(args.config)
        elif args.command == "setup-send":
            code = _setup_send(args.config)
        elif args.command == "check":
            code = _check(args.config, args.dry_run)
        elif args.command == "send-hours":
            code = _send_hours(args.config, test_to=args.test_to, dry_run=args.dry_run)
        elif args.command == "outbound-status":
            code = _outbound_status(args.config, args.dedupe_key)
        elif args.command == "outbound-resolve":
            code = _outbound_resolve(
                args.config,
                args.dedupe_key,
                confirm_sent=args.confirm_sent,
                confirm_unsent=args.confirm_unsent,
            )
        elif args.command == "requeue-analysis":
            code = _requeue_analysis(args.config, args.message_id)
        else:
            code = _recent(args.config, args.limit)
    except (ConfigError, MailboxError, SendError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        code = 2
    raise SystemExit(code)


if __name__ == "__main__":
    main()
