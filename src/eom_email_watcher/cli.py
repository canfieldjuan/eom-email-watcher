from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import stat
import sys
from pathlib import Path

from . import __version__
from .config import DEFAULT_CONFIG, ConfigError, load_config, secure_runtime_paths
from .db import Store
from .gmail import GmailError, GmailGateway
from .model import LocalModel
from .notifications import NotificationError, send_fallback
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
    check = commands.add_parser("check", help="Poll Gmail once")
    check.add_argument("--dry-run", action="store_true", help="Analyze without changing state")
    commands.add_parser("doctor", help="Check local configuration and dependencies")
    recent = commands.add_parser("recent", help="Show recent watched-message results")
    recent.add_argument("--limit", type=int, default=20)
    return parser


def _runtime(config_path: Path) -> tuple[object, Store, LocalModel]:
    config = load_config(config_path)
    secure_runtime_paths(config)
    store = Store(config.database_file)
    store.initialize()
    model = LocalModel(
        config.model_base_url,
        config.model_name,
        config.model_timeout_seconds,
        config.model_api_token_file,
        config.model_require_auth,
    )
    return config, store, model


def _doctor(config_path: Path) -> int:
    checks: dict[str, dict[str, object]] = {}
    try:
        config, store, model = _runtime(config_path)
        checks["config"] = {"ok": True, "path": str(config.path), "senders": len(config.senders)}
        checks["state_directory"] = {
            "ok": stat.S_IMODE(config.database_file.parent.stat().st_mode) == 0o700,
            "mode": oct(stat.S_IMODE(config.database_file.parent.stat().st_mode)),
        }
        checks["database"] = {"ok": True, "initialized": store.state() is not None}
        checks["oauth_credentials"] = {"ok": config.gmail_credentials_file.exists()}
        checks["oauth_token"] = {"ok": config.gmail_token_file.exists()}
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
    config, store, model = _runtime(config_path)
    if config.gmail_token_file.exists():
        gmail = GmailGateway.from_token(config.gmail_credentials_file, config.gmail_token_file)
    else:
        gmail = GmailGateway.authorize(config.gmail_credentials_file, config.gmail_token_file)
    watcher = Watcher(config, store, gmail, model)
    history_id = watcher.bootstrap()
    if config.notifications_enabled:
        try:
            send_fallback("EOM Email Watcher", "Setup complete", dry_run=False)
        except NotificationError as exc:
            print(f"Warning: notification test failed: {exc}", file=sys.stderr)
    print(f"Gmail authorized. Baseline history cursor saved ({history_id}); no old mail imported.")
    return 0


def _check(config_path: Path, dry_run: bool) -> int:
    config, store, model = _runtime(config_path)
    gmail = GmailGateway.from_token(config.gmail_credentials_file, config.gmail_token_file)
    result = Watcher(config, store, gmail, model).check(dry_run=dry_run)
    print(json.dumps(result, indent=2))
    return 0


def _recent(config_path: Path, limit: int) -> int:
    if not 1 <= limit <= 500:
        raise ConfigError("--limit must be between 1 and 500")
    _config, store, _model = _runtime(config_path)
    print(json.dumps(store.recent(limit), indent=2))
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
        elif args.command == "check":
            code = _check(args.config, args.dry_run)
        else:
            code = _recent(args.config, args.limit)
    except (ConfigError, GmailError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        code = 2
    raise SystemExit(code)


if __name__ == "__main__":
    main()
