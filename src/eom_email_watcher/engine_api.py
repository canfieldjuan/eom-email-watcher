from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path

from .config import (
    ConfigError,
    DuplicateSenderError,
    InvalidSenderError,
    Sender,
    SenderNotFoundError,
    add_sender,
    load_config,
    remove_sender,
)
from .db import NotificationIntent
from .gmail import GmailError, GmailGateway
from .locking import operation_lock, operation_lock_supported
from .runtime import Runtime, load_runtime
from .service import Watcher

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 1_000_000
REQUEST_FIELDS = frozenset({"protocol", "operation", "config_path", "payload"})

logger = logging.getLogger(__name__)


class ApiError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _payload(request: dict[str, object], allowed: set[str] | None = None) -> dict[str, object]:
    value = request.get("payload", {})
    if not isinstance(value, dict):
        raise ApiError("invalid_request", "payload must be an object")
    unknown = set(value) - (allowed or set())
    if unknown:
        fields = ", ".join(sorted(unknown))
        raise ApiError("invalid_request", f"Unsupported payload fields: {fields}")
    return value


def _config_path(request: dict[str, object]) -> Path:
    value = request.get("config_path")
    if not isinstance(value, str) or not value.strip():
        raise ApiError("invalid_request", "config_path must be a non-empty string")
    return Path(value).expanduser()


def _bounded_limit(payload: dict[str, object], *, default: int) -> int:
    value = payload.get("limit", default)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 500:
        raise ApiError("invalid_request", "limit must be an integer between 1 and 500")
    return value


def _runtime(request: dict[str, object]) -> Runtime:
    return load_runtime(_config_path(request))


def _host_notification_intents(runtime: Runtime, limit: int) -> list[NotificationIntent]:
    if not runtime.config.notifications_enabled:
        return []
    return runtime.store.notification_intents(limit)


def _host_notification_intent_count(runtime: Runtime) -> int:
    if not runtime.config.notifications_enabled:
        return 0
    return runtime.store.notification_intent_count()


def _require_host_delivery_compatible(runtime: Runtime) -> None:
    if runtime.config.ntfy_topic:
        raise ApiError(
            "unsupported_configuration",
            "Host delivery operations cannot run while ntfy delivery is configured",
        )


def _health(request: dict[str, object]) -> dict[str, object]:
    _payload(request)
    runtime = _runtime(request)
    config = runtime.config
    production_check_supported = operation_lock_supported()
    state = runtime.store.state()
    model_ok, model_detail = runtime.model.health()
    return {
        "database": {"ok": True, "initialized": state is not None},
        "gmail": {
            "credentials_configured": config.gmail_credentials_file.exists(),
            "connected": config.gmail_token_file.exists(),
        },
        "last_check": state[1] if state else None,
        "local_model": {
            "authentication_required": config.model_require_auth,
            "detail": model_detail,
            "endpoint": config.model_base_url,
            "model": config.model_name,
            "ok": model_ok,
            "token_configured": bool(
                config.model_api_token_file and config.model_api_token_file.exists()
            ),
        },
        "notifications": {
            "delivery": "host",
            "enabled": config.notifications_enabled,
            "host_delivery_ready": (
                config.ntfy_topic is None and production_check_supported
            ),
            "ntfy_configured": config.ntfy_topic is not None,
        },
        "production_check_supported": production_check_supported,
        "watchlist_count": len(config.senders),
    }


def _check(request: dict[str, object]) -> dict[str, object]:
    payload = _payload(request, {"dry_run"})
    dry_run = payload.get("dry_run", False)
    if not isinstance(dry_run, bool):
        raise ApiError("invalid_request", "dry_run must be a boolean")

    runtime = _runtime(request)
    config = runtime.config
    _require_host_delivery_compatible(runtime)
    if not config.senders:
        return {
            **Watcher.inactive_result(config, runtime.store, dry_run=dry_run),
            "pending_notifications": _host_notification_intent_count(runtime),
        }
    if not dry_run and not operation_lock_supported():
        raise ApiError(
            "unsupported_platform",
            "Production watcher checks require POSIX operation locking",
        )

    lock_path = config.database_file.with_name(f"{config.database_file.name}.check.lock")

    def run() -> dict[str, int | bool]:
        gmail = GmailGateway.from_token(config.gmail_credentials_file, config.gmail_token_file)
        return Watcher(config, runtime.store, gmail, runtime.model).check(
            dry_run=dry_run,
            deliver_notifications=False,
        )

    if dry_run:
        result = run()
    else:
        with operation_lock(lock_path, "Another production check is already running"):
            result = run()
    return {
        **result,
        "pending_notifications": _host_notification_intent_count(runtime),
    }


def _recent(request: dict[str, object]) -> dict[str, object]:
    payload = _payload(request, {"limit"})
    limit = _bounded_limit(payload, default=20)
    rows = _runtime(request).store.recent(limit)
    return {"items": rows}


def _watchlist(request: dict[str, object]) -> dict[str, object]:
    _payload(request)
    config = load_config(_config_path(request))
    return {
        "items": [
            {"email": sender.email, "name": sender.name}
            for sender in sorted(config.senders, key=lambda item: item.email)
        ]
    }


def _sender_data(sender: Sender) -> dict[str, str | None]:
    return {"email": sender.email, "name": sender.name}


def _watchlist_add(request: dict[str, object]) -> dict[str, object]:
    payload = _payload(request, {"email", "name"})
    email = payload.get("email")
    name = payload.get("name")
    if not isinstance(email, str) or not email.strip():
        raise ApiError("invalid_request", "email must be a non-empty string")
    if name is not None and not isinstance(name, str):
        raise ApiError("invalid_request", "name must be a string or null")
    try:
        sender = add_sender(_config_path(request), email, name)
    except InvalidSenderError as exc:
        raise ApiError("invalid_request", str(exc)) from exc
    except DuplicateSenderError as exc:
        raise ApiError("conflict", str(exc)) from exc
    return {"item": _sender_data(sender)}


def _watchlist_remove(request: dict[str, object]) -> dict[str, object]:
    payload = _payload(request, {"email"})
    email = payload.get("email")
    if not isinstance(email, str) or not email.strip():
        raise ApiError("invalid_request", "email must be a non-empty string")
    try:
        sender = remove_sender(_config_path(request), email)
    except InvalidSenderError as exc:
        raise ApiError("invalid_request", str(exc)) from exc
    except SenderNotFoundError as exc:
        raise ApiError("not_found", str(exc)) from exc
    return {"item": _sender_data(sender)}


def _settings(request: dict[str, object]) -> dict[str, object]:
    _payload(request)
    config = load_config(_config_path(request))
    return {
        "body_char_limit": config.body_char_limit,
        "local_model": {
            "authentication_required": config.model_require_auth,
            "endpoint": config.model_base_url,
            "model": config.model_name,
            "timeout_seconds": config.model_timeout_seconds,
            "token_configured": bool(
                config.model_api_token_file and config.model_api_token_file.exists()
            ),
        },
        "notifications_enabled": config.notifications_enabled,
        "retention_days": config.retention_days,
        "timezone": config.timezone,
    }


def _notification_payload(
    intent: NotificationIntent, sender_names: dict[str, str | None]
) -> dict[str, object]:
    label = sender_names.get(intent.sender) or intent.sender_name or intent.sender
    title = f"{label}: {intent.subject}"
    if intent.kind == "analysis":
        lines = [intent.summary]
        if intent.suggested_action:
            lines.append(f"Next: {intent.suggested_action}")
        if intent.deadline_iso:
            lines.append(f"Deadline: {intent.deadline_iso}")
        body = "\n".join(line for line in lines if line)
        priority = intent.priority or "normal"
    else:
        body = "A watched email arrived. Local summary unavailable; it will be retried."
        priority = "normal"
    return {
        "analysis_at": intent.analysis_at,
        "body": body,
        "kind": intent.kind,
        "message_id": intent.message_id,
        "priority": priority,
        "title": title,
    }


def _notifications_pending(request: dict[str, object]) -> dict[str, object]:
    payload = _payload(request, {"limit"})
    limit = _bounded_limit(payload, default=25)
    runtime = _runtime(request)
    config = runtime.config
    _require_host_delivery_compatible(runtime)
    intents = _host_notification_intents(runtime, limit)
    sender_names = {sender.email: sender.name for sender in config.senders}
    return {
        "items": [_notification_payload(intent, sender_names) for intent in intents]
    }


def _notifications_ack(request: dict[str, object]) -> dict[str, object]:
    payload = _payload(request, {"message_id", "kind", "analysis_at"})
    message_id = payload.get("message_id")
    kind = payload.get("kind")
    analysis_at = payload.get("analysis_at")
    if not isinstance(message_id, str) or not message_id.strip():
        raise ApiError("invalid_request", "message_id must be a non-empty string")
    if kind not in {"analysis", "fallback"}:
        raise ApiError("invalid_request", "kind must be analysis or fallback")
    if analysis_at is not None and not isinstance(analysis_at, str):
        raise ApiError("invalid_request", "analysis_at must be a string or null")
    runtime = _runtime(request)
    _require_host_delivery_compatible(runtime)
    try:
        status = runtime.store.acknowledge_notification(
            message_id=message_id,
            kind=kind,
            analysis_at=analysis_at,
        )
    except KeyError as exc:
        raise ApiError("not_found", "Notification message was not found") from exc
    except ValueError as exc:
        raise ApiError("invalid_request", str(exc)) from exc
    except RuntimeError as exc:
        raise ApiError("stale_notification", str(exc)) from exc
    return {"status": status}


OPERATIONS: dict[str, Callable[[dict[str, object]], dict[str, object]]] = {
    "health.get": _health,
    "inbox.recent": _recent,
    "notifications.ack": _notifications_ack,
    "notifications.pending": _notifications_pending,
    "settings.get": _settings,
    "watcher.check": _check,
    "watchlist.add": _watchlist_add,
    "watchlist.list": _watchlist,
    "watchlist.remove": _watchlist_remove,
}


def dispatch(request: object) -> dict[str, object]:
    if not isinstance(request, dict):
        raise ApiError("invalid_request", "request must be an object")
    unknown = set(request) - REQUEST_FIELDS
    if unknown:
        fields = ", ".join(sorted(str(field) for field in unknown))
        raise ApiError("invalid_request", f"Unsupported request fields: {fields}")
    protocol = request.get("protocol")
    if isinstance(protocol, bool) or protocol != PROTOCOL_VERSION:
        raise ApiError("unsupported_protocol", f"protocol must be {PROTOCOL_VERSION}")
    operation = request.get("operation")
    if not isinstance(operation, str) or operation not in OPERATIONS:
        raise ApiError("unsupported_operation", "operation is not supported")
    return OPERATIONS[operation](request)


def _response(request: object) -> dict[str, object]:
    requested_operation = request.get("operation") if isinstance(request, dict) else None
    operation = requested_operation if isinstance(requested_operation, str) else None
    try:
        data = dispatch(request)
        return {
            "data": data,
            "ok": True,
            "operation": operation,
            "protocol": PROTOCOL_VERSION,
        }
    except ApiError as exc:
        return {
            "error": {"code": exc.code, "message": str(exc)},
            "ok": False,
            "operation": operation,
            "protocol": PROTOCOL_VERSION,
        }
    except ConfigError as exc:
        return {
            "error": {"code": "configuration_error", "message": str(exc)},
            "ok": False,
            "operation": operation,
            "protocol": PROTOCOL_VERSION,
        }
    except GmailError as exc:
        logger.warning("Gmail operation failed: %s", exc)
        return {
            "error": {
                "code": "gmail_error",
                "message": "Gmail operation failed; see stderr for details",
            },
            "ok": False,
            "operation": operation,
            "protocol": PROTOCOL_VERSION,
        }
    except RuntimeError as exc:
        return {
            "error": {"code": "runtime_error", "message": str(exc)},
            "ok": False,
            "operation": operation,
            "protocol": PROTOCOL_VERSION,
        }
    except Exception:
        logger.exception("Unhandled engine API error")
        return {
            "error": {"code": "internal_error", "message": "Internal engine error"},
            "ok": False,
            "operation": operation,
            "protocol": PROTOCOL_VERSION,
        }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    os.umask(0o077)
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        request: object = None
        response = {
            "error": {"code": "request_too_large", "message": "Request is too large"},
            "ok": False,
            "operation": None,
            "protocol": PROTOCOL_VERSION,
        }
    else:
        try:
            request = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, RecursionError):
            request = None
            response = {
                "error": {"code": "invalid_json", "message": "Request must be valid JSON"},
                "ok": False,
                "operation": None,
                "protocol": PROTOCOL_VERSION,
            }
        else:
            response = _response(request)
    print(json.dumps(response, separators=(",", ":"), sort_keys=True))
    raise SystemExit(0 if response["ok"] else 2)


if __name__ == "__main__":
    main()
