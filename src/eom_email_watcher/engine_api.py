from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import sqlite3
import sys
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path

from filelock import FileLock
from filelock import Timeout as FileLockTimeout

from . import connect, entitlement
from .config import (
    MUTABLE_DESKTOP_SETTINGS,
    Config,
    ConfigAlreadyExistsError,
    ConfigError,
    DuplicateSenderError,
    InvalidConfigInitializationError,
    InvalidSenderError,
    InvalidSettingsUpdateError,
    Sender,
    SenderNotFoundError,
    add_sender,
    initialize_config,
    load_config,
    remove_sender,
    update_settings,
)
from .db import (
    CalendarEventMutation,
    CalendarEventProjection,
    CalendarGrant,
    ConnectJob,
    ConnectOutput,
    MailAccount,
    MessageSource,
    NotificationIntent,
    Store,
)
from .gmail import (
    TOKEN_LOCK_TIMEOUT_SECONDS,
    GmailAuthorizationRejected,
    GmailError,
    GmailGateway,
    gmail_credentials_configured,
)
from .imap import (
    IMAP_CONNECTION_METHOD,
    IMAP_PROVIDER,
    ImapError,
    ImapGateway,
    credentials_from_connection,
    imap_cursor_mailbox_identity,
    imap_mailbox_identity,
    load_credentials,
    write_credentials,
)
from .locking import (
    operation_lock,
    operation_lock_supported,
    operation_lock_uses_soft_fallback,
)
from .mailbox import (
    DEFAULT_MAIL_ACCOUNT_ID,
    DEFAULT_MAIL_PROVIDER,
    MailboxAccountUnavailable,
    MailboxError,
    MailboxGateway,
)
from .microsoft365 import (
    MICROSOFT365_PROVIDER,
    Microsoft365Error,
    Microsoft365Gateway,
    MicrosoftAuthorizationRejected,
)
from .microsoft_calendar import (
    CALENDAR_AUTHORIZATION_PROFILES,
    CALENDAR_PROPOSAL_PROFILE,
    CALENDAR_READ_PROFILE,
    CALENDAR_WRITE_PROFILE,
    CalendarDeltaRound,
    MicrosoftCalendarAuthorization,
    MicrosoftCalendarConsentPending,
    MicrosoftCalendarProposalAuthorization,
    MicrosoftCalendarReadAuthorization,
    MicrosoftCalendarWriteAuthorization,
    StaleCalendarCursor,
    calendar_delta_round,
    canonical_calendar_window,
    microsoft_cached_mailbox_principal,
    microsoft_mailbox_principal,
)
from .runtime import (
    MAIL_PROVIDER_NAMES,
    Runtime,
    load_mailbox_account,
    load_runtime,
    mail_account_connected,
    mail_account_token_file,
    mail_provider_connection_available,
    microsoft_calendar_read_token_file,
    microsoft_calendar_token_file,
)
from .service import run_watcher_check

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 1_000_000
MAX_NATIVE_TEXT_OUTPUT_BYTES = 256 * 1024
MAX_INBOX_CURSOR_BYTES = 1024
MAX_CALENDAR_EVENTS_RESPONSE_BYTES = 16 * 1024 * 1024
INBOX_PRIORITIES = frozenset({"urgent", "high", "normal", "low", "untriaged"})
INBOX_CATEGORIES = frozenset(
    {
        "invoice",
        "scheduling",
        "customer_request",
        "automated_notice",
        "informational",
        "other",
        "unclassified",
    }
)
INBOX_STATUSES = frozenset({"pending", "analyzed", "summarized", "skipped"})
REQUEST_FIELDS = frozenset({"protocol", "operation", "config_path", "payload"})

logger = logging.getLogger(__name__)
SAFE_ATTACHMENT_SUFFIX = re.compile(r"\.[A-Za-z0-9]{1,12}\Z")


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


def _bounded_limit(payload: dict[str, object], *, default: int, maximum: int = 500) -> int:
    value = payload.get("limit", default)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ApiError("invalid_request", f"limit must be an integer between 1 and {maximum}")
    return value


def _optional_inbox_text(payload: dict[str, object], name: str, *, maximum: int) -> str | None:
    value = payload.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ApiError(
            "invalid_request",
            f"{name} must be a non-empty string of at most {maximum} characters",
        )
    return value.strip()


def _optional_inbox_choice(
    payload: dict[str, object], name: str, choices: frozenset[str]
) -> str | None:
    value = payload.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or value not in choices:
        raise ApiError("invalid_request", f"{name} must be one of: {', '.join(sorted(choices))}")
    return value


def _encode_inbox_cursor(cursor: tuple[str, str] | None) -> str | None:
    if cursor is None:
        return None
    encoded = json.dumps(
        {"message_id": cursor[1], "received_at": cursor[0], "v": 1},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return base64.urlsafe_b64encode(encoded).rstrip(b"=").decode("ascii")


def _decode_inbox_cursor(value: object) -> tuple[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > MAX_INBOX_CURSOR_BYTES * 2:
        raise ApiError("invalid_request", "cursor is invalid")
    try:
        padded = value + "=" * (-len(value) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=True)
        if len(raw) > MAX_INBOX_CURSOR_BYTES:
            raise ValueError
        decoded = json.loads(raw)
    except (
        UnicodeDecodeError,
        UnicodeEncodeError,
        ValueError,
        binascii.Error,
        json.JSONDecodeError,
    ) as exc:
        raise ApiError("invalid_request", "cursor is invalid") from exc
    if not isinstance(decoded, dict) or set(decoded) != {
        "message_id",
        "received_at",
        "v",
    }:
        raise ApiError("invalid_request", "cursor is invalid")
    message_id = decoded["message_id"]
    received_at = decoded["received_at"]
    if (
        type(decoded["v"]) is not int
        or decoded["v"] != 1
        or not isinstance(message_id, str)
        or not 0 < len(message_id) <= 512
        or not isinstance(received_at, str)
        or not 0 < len(received_at) <= 64
    ):
        raise ApiError("invalid_request", "cursor is invalid")
    return received_at, message_id


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


def _production_check_lock_path(config: Config) -> Path:
    return config.database_file.with_name(f"{config.database_file.name}.check.lock")


def _host_operation_lock(request: dict[str, object]) -> dict[str, object]:
    _payload(request)
    runtime = _runtime(request)
    lock_path = _production_check_lock_path(runtime.config)
    if not operation_lock_supported(lock_path):
        raise ApiError(
            "unsupported_platform",
            "Host operations require native operation locking",
        )
    return {"path": str(lock_path)}


def _require_host_delivery_compatible(runtime: Runtime) -> None:
    if runtime.config.ntfy_topic:
        raise ApiError(
            "unsupported_configuration",
            "Host delivery operations cannot run while ntfy delivery is configured",
        )


def _mail_provider(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        raise ApiError("invalid_request", "provider must be a non-empty string")
    provider = value.strip().casefold()
    if provider not in MAIL_PROVIDER_NAMES:
        raise ApiError("unsupported_provider", "That email provider is not available")
    return provider


def _mail_account_key(payload: dict[str, object]) -> tuple[str, str]:
    provider = _mail_provider(payload.get("provider"))
    account_id = payload.get("account_id")
    if not isinstance(account_id, str) or not account_id.strip() or len(account_id) > 128:
        raise ApiError("invalid_request", "account_id must be a non-empty string")
    return provider, account_id.strip()


def _mail_account_public(runtime: Runtime, account: MailAccount) -> dict[str, object]:
    state = runtime.store.state(provider=account.provider, account_id=account.account_id)
    return {
        "account_id": account.account_id,
        "active": account.active,
        "address": account.address,
        "connected": mail_account_connected(runtime.config, account),
        "display_name": account.display_name,
        "last_check": state[1] if state else None,
        "provider": account.provider,
    }


def _mail_accounts_public(runtime: Runtime) -> dict[str, object]:
    accounts = runtime.store.mail_accounts()
    return {
        "accounts": [_mail_account_public(runtime, account) for account in accounts],
        "providers": [
            {
                "connection_available": mail_provider_connection_available(
                    runtime.config, provider
                ),
                "connection_method": (
                    IMAP_CONNECTION_METHOD if provider == IMAP_PROVIDER else "browser_oauth"
                ),
                "display_name": display_name,
                "multiple_accounts": True,
                "provider": provider,
            }
            for provider, display_name in sorted(MAIL_PROVIDER_NAMES.items())
        ],
    }


def _install_private_token(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    local_copy: Path | None = None
    try:
        with FileLock(f"{destination}.lock", timeout=TOKEN_LOCK_TIMEOUT_SECONDS):
            # Windows rejects fsync on this read-only source descriptor. Durability is
            # established on the new writable copy before its atomic replacement below.
            content = source.read_bytes()
            local_copy = _write_private_file(
                destination.parent,
                ".readonly-token-",
                ".json",
                content,
            )
            os.replace(local_copy, destination)
            local_copy = None
    except FileLockTimeout as exc:
        raise MailboxAccountUnavailable("The email account token is busy; retry") from exc
    finally:
        if local_copy is not None:
            local_copy.unlink(missing_ok=True)


def _disconnect_mail_account_token(config: Config, account: MailAccount) -> None:
    token_file = mail_account_token_file(config, account)
    if not token_file.is_file():
        return
    try:
        with FileLock(f"{token_file}.lock", timeout=TOKEN_LOCK_TIMEOUT_SECONDS):
            token_file.unlink(missing_ok=True)
    except FileLockTimeout as exc:
        raise MailboxAccountUnavailable("The email account token is busy; retry") from exc


def _authorize_gmail_account(
    runtime: Runtime,
    account: MailAccount | None,
    *,
    reuse_valid_token: bool,
) -> dict[str, object]:
    rejected_accounts: set[tuple[str, str]] = set()
    if account is None:
        legacy = runtime.store.mail_account(DEFAULT_MAIL_PROVIDER, DEFAULT_MAIL_ACCOUNT_ID)
        if legacy is not None and legacy.address is None:
            legacy_token = mail_account_token_file(runtime.config, legacy)
            if legacy_token.is_file():
                try:
                    legacy_profile = GmailGateway.from_token(
                        runtime.config.gmail_credentials_file, legacy_token
                    ).profile()
                except GmailAuthorizationRejected:
                    rejected_accounts.add((legacy.provider, legacy.account_id))
                else:
                    runtime.store.update_mail_account_identity(
                        legacy.provider,
                        legacy.account_id,
                        display_name=MAIL_PROVIDER_NAMES[legacy.provider],
                        address=legacy_profile.email_address,
                    )
    token_file = mail_account_token_file(runtime.config, account) if account else None
    if (
        account is not None
        and account.address is None
        and (token_file is None or not token_file.is_file())
        and runtime.store.mail_account_has_history(account.provider, account.account_id)
    ):
        raise ApiError(
            "account_identity_unverified",
            "The existing mailbox identity cannot be verified; connect it as a new account",
        )
    if account is not None and token_file is not None and token_file.is_file():
        try:
            current_gmail = GmailGateway.from_token(
                runtime.config.gmail_credentials_file, token_file
            )
            current_profile = current_gmail.profile()
        except GmailAuthorizationRejected as exc:
            rejected_accounts.add((account.provider, account.account_id))
            if account.address is None and runtime.store.mail_account_has_history(
                account.provider, account.account_id
            ):
                raise ApiError(
                    "account_identity_unverified",
                    "The existing mailbox identity cannot be verified; connect it as a new account",
                ) from exc
        else:
            if account.address is None:
                account = runtime.store.update_mail_account_identity(
                    account.provider,
                    account.account_id,
                    display_name=MAIL_PROVIDER_NAMES[account.provider],
                    address=current_profile.email_address,
                )
            if reuse_valid_token and current_profile.email_address == account.address:
                return _finish_mail_authorization(runtime, account, current_profile.history_id)

    authorization_parent = (
        token_file.parent if token_file is not None else runtime.config.database_file.parent
    )
    authorization_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(
        prefix=".gmail-authorization-",
        dir=authorization_parent,
    ) as directory:
        staged_token = Path(directory) / "readonly-token.json"
        gmail, _changed = GmailGateway.authorize_with_status(
            runtime.config.gmail_credentials_file,
            staged_token,
            force_reauthorize=True,
        )
        profile = gmail.profile()

        activate_after_connect = False
        if account is None:
            account = runtime.store.mail_account_by_address(
                DEFAULT_MAIL_PROVIDER, profile.email_address
            )
            if account is None:
                legacy = runtime.store.mail_account(DEFAULT_MAIL_PROVIDER, DEFAULT_MAIL_ACCOUNT_ID)
                if (
                    legacy is not None
                    and legacy.address is None
                    and not runtime.store.mail_account_has_history(
                        legacy.provider, legacy.account_id
                    )
                ):
                    account = legacy
                else:
                    account = runtime.store.register_mail_account(
                        DEFAULT_MAIL_PROVIDER,
                        f"gmail-{uuid.uuid4().hex}",
                        display_name=MAIL_PROVIDER_NAMES[DEFAULT_MAIL_PROVIDER],
                        address=profile.email_address,
                        active=False,
                    )
            active = runtime.store.active_mail_account()
            activate_after_connect = (
                active is None
                or (active.provider, active.account_id) in rejected_accounts
                or not mail_account_connected(runtime.config, active)
            )
        elif account.address is not None and profile.email_address != account.address:
            raise ApiError(
                "account_identity_mismatch",
                "The authorized mailbox does not match the selected email account",
            )

        token_file = mail_account_token_file(runtime.config, account)
        _install_private_token(staged_token, token_file)

    account = runtime.store.update_mail_account_identity(
        account.provider,
        account.account_id,
        display_name=MAIL_PROVIDER_NAMES[account.provider],
        address=profile.email_address,
    )
    if activate_after_connect and not account.active:
        account = runtime.store.activate_mail_account(account.provider, account.account_id)
    return _finish_mail_authorization(runtime, account, profile.history_id)


def _finish_mail_authorization(
    runtime: Runtime,
    account: MailAccount,
    cursor: str,
) -> dict[str, object]:
    initialize_baseline = (
        runtime.store.state(provider=account.provider, account_id=account.account_id) is None
    )
    if initialize_baseline:
        runtime.store.set_state(
            cursor,
            provider=account.provider,
            account_id=account.account_id,
        )
    return {
        "account": _mail_account_public(runtime, account),
        "baseline_initialized": initialize_baseline,
    }


def _mismatched_calendar_grants_for_replacement_principal(
    runtime: Runtime,
    account: MailAccount,
    staged_mail_token: Path,
) -> tuple[CalendarGrant, ...]:
    ready_grants = tuple(
        grant
        for profile in CALENDAR_AUTHORIZATION_PROFILES
        if (grant := runtime.store.calendar_grant(account.account_id, profile)) is not None
        and grant.state == "ready"
    )
    if not ready_grants:
        return ()
    replacement = microsoft_mailbox_principal(
        runtime.config.microsoft_credentials_file,
        staged_mail_token,
    )
    return tuple(grant for grant in ready_grants if grant.principal_key != replacement.key)


def _authorize_microsoft_account(
    runtime: Runtime,
    account: MailAccount | None,
) -> dict[str, object]:
    connect_request = account is None
    if (
        account is not None
        and account.address is None
        and runtime.store.mail_account_has_history(account.provider, account.account_id)
    ):
        raise ApiError(
            "account_identity_unverified",
            "The existing mailbox identity cannot be verified; connect it as a new account",
        )

    authorization_parent = (
        mail_account_token_file(runtime.config, account).parent
        if account is not None
        else runtime.config.database_file.parent
    )
    authorization_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(
        prefix=".microsoft365-authorization-",
        dir=authorization_parent,
    ) as directory:
        staged_token = Path(directory) / "msal-cache.json"
        microsoft, _changed = Microsoft365Gateway.authorize_with_status(
            runtime.config.microsoft_credentials_file,
            staged_token,
            force_reauthorize=True,
        )
        profile = microsoft.profile()

        if account is None:
            account = runtime.store.mail_account_by_address(
                MICROSOFT365_PROVIDER,
                profile.email_address,
            )
        elif account.address is not None and profile.email_address != account.address:
            raise ApiError(
                "account_identity_mismatch",
                "The authorized mailbox does not match the selected email account",
            )

        mismatched_calendar_grants = (
            _mismatched_calendar_grants_for_replacement_principal(
                runtime,
                account,
                staged_token,
            )
            if account is not None
            else ()
        )

        initialize_baseline = (
            account is None
            or runtime.store.state(
                provider=account.provider,
                account_id=account.account_id,
            )
            is None
        )
        baseline = microsoft.initial_cursor() if initialize_baseline else None

        activate_after_connect = False
        if connect_request:
            active = runtime.store.active_mail_account()
            activate_after_connect = active is None or not mail_account_connected(
                runtime.config, active
            )
        if account is None:
            account = runtime.store.register_mail_account(
                MICROSOFT365_PROVIDER,
                f"microsoft365-{uuid.uuid4().hex}",
                display_name=MAIL_PROVIDER_NAMES[MICROSOFT365_PROVIDER],
                address=profile.email_address,
                active=False,
            )
        token_file = mail_account_token_file(runtime.config, account)
        _install_private_token(staged_token, token_file)
        for grant in mismatched_calendar_grants:
            if not runtime.store.revoke_calendar_grant_if_current(grant):
                raise ApiError(
                    "calendar_state_changed",
                    "The calendar authorization changed during mailbox reconnection; retry",
                )

    account = runtime.store.update_mail_account_identity(
        account.provider,
        account.account_id,
        display_name=MAIL_PROVIDER_NAMES[account.provider],
        address=profile.email_address,
    )
    if activate_after_connect and not account.active:
        account = runtime.store.activate_mail_account(account.provider, account.account_id)
    if initialize_baseline:
        assert baseline is not None
        return _finish_mail_authorization(runtime, account, baseline)
    return {
        "account": _mail_account_public(runtime, account),
        "baseline_initialized": False,
    }


def _connect_imap_account(
    runtime: Runtime,
    account: MailAccount | None,
    connection: object,
) -> dict[str, object]:
    credentials = credentials_from_connection(connection)
    if account is not None and account.address != credentials.email_address:
        raise ApiError(
            "account_identity_mismatch",
            "The mailbox address does not match the selected email account",
        )

    previous_mailbox_identity: str | None = None
    if account is not None:
        current_credentials = mail_account_token_file(runtime.config, account)
        if current_credentials.is_file():
            try:
                previous_mailbox_identity = imap_mailbox_identity(
                    load_credentials(current_credentials)
                )
            except ImapError:
                previous_mailbox_identity = None
        if previous_mailbox_identity is None:
            state = runtime.store.state(provider=account.provider, account_id=account.account_id)
            if state is not None:
                try:
                    previous_mailbox_identity = imap_cursor_mailbox_identity(state[0])
                except ImapError:
                    previous_mailbox_identity = None

    authorization_parent = (
        mail_account_token_file(runtime.config, account).parent
        if account is not None
        else runtime.config.database_file.parent
    )
    authorization_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(
        prefix=".imap-authorization-",
        dir=authorization_parent,
    ) as directory:
        staged_credentials = Path(directory) / "credentials.json"
        write_credentials(staged_credentials, credentials)
        gateway = ImapGateway.from_credentials_file(staged_credentials)

        activate_after_connect = False
        if account is None:
            account = runtime.store.mail_account_by_address(
                IMAP_PROVIDER,
                credentials.email_address,
            )
            active = runtime.store.active_mail_account()
            activate_after_connect = active is None or not mail_account_connected(
                runtime.config, active
            )
            if account is not None:
                current_credentials = mail_account_token_file(runtime.config, account)
                if current_credentials.is_file():
                    try:
                        previous_mailbox_identity = imap_mailbox_identity(
                            load_credentials(current_credentials)
                        )
                    except ImapError:
                        previous_mailbox_identity = None
                if previous_mailbox_identity is None:
                    state = runtime.store.state(
                        provider=account.provider,
                        account_id=account.account_id,
                    )
                    if state is not None:
                        try:
                            previous_mailbox_identity = imap_cursor_mailbox_identity(state[0])
                        except ImapError:
                            previous_mailbox_identity = None
        mailbox_changed = (
            account is not None and previous_mailbox_identity != imap_mailbox_identity(credentials)
        )
        initialize_baseline = (
            account is None
            or mailbox_changed
            or runtime.store.state(
                provider=account.provider,
                account_id=account.account_id,
            )
            is None
        )
        verified_cursor = gateway.initial_cursor()
        baseline = verified_cursor if initialize_baseline else None

        if account is None:
            account = runtime.store.register_mail_account(
                IMAP_PROVIDER,
                f"imap-{uuid.uuid4().hex}",
                display_name=MAIL_PROVIDER_NAMES[IMAP_PROVIDER],
                address=credentials.email_address,
                active=False,
            )
        destination = mail_account_token_file(runtime.config, account)
        _install_private_token(staged_credentials, destination)

    account = runtime.store.update_mail_account_identity(
        account.provider,
        account.account_id,
        display_name=MAIL_PROVIDER_NAMES[account.provider],
        address=credentials.email_address,
    )
    if activate_after_connect and not account.active:
        account = runtime.store.activate_mail_account(account.provider, account.account_id)
    if mailbox_changed:
        assert baseline is not None
        runtime.store.set_state(
            baseline,
            provider=account.provider,
            account_id=account.account_id,
        )
        return {
            "account": _mail_account_public(runtime, account),
            "baseline_initialized": True,
        }
    if initialize_baseline:
        assert baseline is not None
        return _finish_mail_authorization(runtime, account, baseline)
    return {
        "account": _mail_account_public(runtime, account),
        "baseline_initialized": False,
    }


def _with_mail_account_mutation(
    request: dict[str, object],
    operation: Callable[[Runtime], dict[str, object]],
) -> dict[str, object]:
    runtime = _runtime(request)
    lock_path = _production_check_lock_path(runtime.config)
    if not operation_lock_supported(lock_path):
        raise ApiError(
            "unsupported_platform",
            "Email account changes require native operation locking",
        )
    with operation_lock(lock_path, "Another mailbox operation is already running"):
        return operation(_runtime(request))


def _health(request: dict[str, object]) -> dict[str, object]:
    _payload(request)
    runtime = _runtime(request)
    config = runtime.config
    production_check_supported = operation_lock_supported(_production_check_lock_path(config))
    active_account = runtime.store.active_mail_account()
    state = (
        runtime.store.state(
            provider=active_account.provider,
            account_id=active_account.account_id,
        )
        if active_account is not None
        else None
    )
    model_ok, model_detail = runtime.model.health()
    mail = _mail_accounts_public(runtime)
    gmail_connected = bool(
        active_account is not None
        and active_account.provider == DEFAULT_MAIL_PROVIDER
        and mail_account_connected(config, active_account)
    )
    return {
        "database": {"ok": True, "initialized": state is not None},
        "gmail": {
            "credentials_configured": gmail_credentials_configured(config.gmail_credentials_file),
            "connected": gmail_connected,
        },
        "last_check": state[1] if state else None,
        "mail": mail,
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
            "host_delivery_ready": (config.ntfy_topic is None and production_check_supported),
            "ntfy_configured": config.ntfy_topic is not None,
        },
        "production_check_supported": production_check_supported,
        "watchlist_count": len(config.senders),
    }


def _connect_entitlement_status(request: dict[str, object]) -> dict[str, object]:
    _payload(request)
    return entitlement.connect_entitlement_status().public_dict()


def _connect_entitlement_install(request: dict[str, object]) -> dict[str, object]:
    payload = _payload(request, {"source_path"})
    value = payload.get("source_path")
    if not isinstance(value, str) or not value.strip() or not Path(value).is_absolute():
        raise ApiError(
            entitlement.SOURCE_INVALID,
            "the selected Connect entitlement is not a safe, valid license file",
        )
    try:
        status = entitlement.install_connect_entitlement(Path(value))
    except entitlement.EntitlementInstallError as exc:
        raise ApiError(exc.code, str(exc)) from exc
    return status.public_dict()


def _calendar_account(
    runtime: Runtime,
    payload: dict[str, object],
    *,
    require_address: bool = True,
) -> MailAccount:
    provider, account_id = _mail_account_key(payload)
    if provider != MICROSOFT365_PROVIDER:
        raise ApiError("unsupported_provider", "Calendar read requires a Microsoft 365 account")
    account = runtime.store.mail_account(provider, account_id)
    if account is None:
        raise ApiError("not_found", "The Microsoft 365 account was not found")
    if require_address and account.address is None:
        raise ApiError(
            "account_identity_unverified",
            "Reconnect the Microsoft 365 mailbox before authorizing its calendar",
        )
    return account


def _calendar_entitlement_active() -> bool:
    return entitlement.feature_entitlement_decision(entitlement.CONNECT_FEATURE_ID).is_active


def _automation_entitlement_active() -> bool:
    return entitlement.feature_entitlements_active(
        entitlement.CONNECT_FEATURE_ID,
        entitlement.AUTOMATIONS_FEATURE_ID,
    )


def _require_calendar_entitlement() -> None:
    if not _calendar_entitlement_active():
        raise ApiError(
            "calendar_entitlement_required",
            "Calendar setup requires an active capability-exchange entitlement",
        )


def _calendar_grant_identity(grant: CalendarGrant | None) -> dict[str, str | None]:
    return {
        "principal_key": getattr(grant, "principal_key", None),
        "home_account_id": getattr(grant, "home_account_id", None),
        "tenant_id": getattr(grant, "tenant_id", None),
        "object_id": getattr(grant, "object_id", None),
        "email_address": getattr(grant, "email_address", None),
    }


def _restore_calendar_grant(
    runtime: Runtime,
    account_id: str,
    profile: str,
    previous: CalendarGrant | None,
) -> None:
    runtime.store.set_calendar_grant(
        account_id,
        profile,
        previous.state if previous is not None else "not_requested",
        **_calendar_grant_identity(previous),
    )


def _calendar_authorization_type(
    profile: str,
) -> type[MicrosoftCalendarAuthorization]:
    if profile == CALENDAR_READ_PROFILE:
        return MicrosoftCalendarReadAuthorization
    if profile == CALENDAR_PROPOSAL_PROFILE:
        return MicrosoftCalendarProposalAuthorization
    if profile == CALENDAR_WRITE_PROFILE:
        return MicrosoftCalendarWriteAuthorization
    raise ValueError("calendar authorization profile is invalid")


def _calendar_status_data(
    runtime: Runtime,
    account: MailAccount,
    profile: str,
) -> dict[str, object]:
    entitlement_active = _calendar_entitlement_active()
    grant = runtime.store.calendar_grant(account.account_id, profile)
    token_file = microsoft_calendar_token_file(runtime.config, account, profile)
    token_configured = token_file.is_file()
    state = grant.state if grant is not None else "not_requested"
    principal_matches = False
    if state == "ready" and not token_configured:
        state = "revoked"
    elif entitlement_active and state == "ready" and grant is not None:
        try:
            calendar_authorization = _calendar_authorization_type(profile).from_token(
                runtime.config.microsoft_credentials_file,
                token_file,
            )
        except MicrosoftAuthorizationRejected:
            state = "revoked"
        except Microsoft365Error:
            pass
        else:
            if calendar_authorization.principal.key != grant.principal_key:
                state = "revoked"
            else:
                try:
                    mailbox_principal = microsoft_mailbox_principal(
                        runtime.config.microsoft_credentials_file,
                        mail_account_token_file(runtime.config, account),
                    )
                except Microsoft365Error:
                    pass
                else:
                    principal_matches = mailbox_principal.key == grant.principal_key
                    if not principal_matches:
                        state = "revoked"
    if state == "revoked" and grant is not None:
        runtime.store.revoke_calendar_grant_if_current(grant)
    return {
        "account_id": account.account_id,
        "available": (
            entitlement_active and state == "ready" and token_configured and principal_matches
        ),
        "entitlement_active": entitlement_active,
        "profile": profile,
        "scope": CALENDAR_AUTHORIZATION_PROFILES[profile].scopes[0],
        # Revoking an existing grant must remain possible after entitlement loss.
        # Keep the non-secret consent state visible while `available` stays false.
        "state": state,
    }


def _with_calendar_observation(
    request: dict[str, object],
    operation: Callable[[Runtime], dict[str, object]],
) -> dict[str, object]:
    runtime = _runtime(request)
    lock_path = _production_check_lock_path(runtime.config)
    if operation_lock_uses_soft_fallback(lock_path):
        # Calendar mutations fail closed on this platform, so this read cannot race one.
        return operation(runtime)
    with operation_lock(lock_path, "Another mailbox operation is already running"):
        return operation(_runtime(request))


def _calendar_status(request: dict[str, object], profile: str) -> dict[str, object]:
    payload = _payload(request, {"provider", "account_id"})

    def status(runtime: Runtime) -> dict[str, object]:
        account = _calendar_account(runtime, payload, require_address=False)
        return _calendar_status_data(runtime, account, profile)

    return _with_calendar_observation(request, status)


def _calendar_connect(request: dict[str, object], profile: str) -> dict[str, object]:
    payload = _payload(request, {"provider", "account_id"})

    def connect(runtime: Runtime) -> dict[str, object]:
        _require_calendar_entitlement()
        account = _calendar_account(runtime, payload)
        previous = runtime.store.calendar_grant(account.account_id, profile)
        try:
            mailbox_principal = microsoft_mailbox_principal(
                runtime.config.microsoft_credentials_file,
                mail_account_token_file(runtime.config, account),
            )
        except MicrosoftAuthorizationRejected as exc:
            raise ApiError("mailbox_authorization_required", str(exc)) from exc
        except Microsoft365Error as exc:
            raise ApiError("calendar_error", str(exc)) from exc
        token_file = microsoft_calendar_token_file(runtime.config, account, profile)
        token_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            authorization_directory = tempfile.TemporaryDirectory(
                prefix=f".calendar-{profile}-authorization-",
                dir=token_file.parent,
            )
        except OSError as exc:
            raise ApiError(
                "calendar_error",
                "Microsoft calendar authorization could not be staged; retry",
            ) from exc
        with authorization_directory as directory:
            staged_token = Path(directory) / f"calendar-{profile}.msal-cache.json"
            runtime.store.set_calendar_grant(
                account.account_id,
                profile,
                "consent_pending",
                **_calendar_grant_identity(previous),
            )
            try:
                calendar, _changed = _calendar_authorization_type(profile).authorize_with_status(
                    runtime.config.microsoft_credentials_file,
                    staged_token,
                )
            except MicrosoftCalendarConsentPending:
                return _calendar_status_data(runtime, account, profile)
            except MicrosoftAuthorizationRejected as exc:
                try:
                    runtime.store.set_calendar_grant(
                        account.account_id,
                        profile,
                        "rejected",
                        **_calendar_grant_identity(previous),
                    )
                except sqlite3.Error as state_exc:
                    try:
                        _restore_calendar_grant(
                            runtime,
                            account.account_id,
                            profile,
                            previous,
                        )
                    except sqlite3.Error as restore_exc:
                        raise ApiError(
                            "calendar_state_error",
                            "Microsoft calendar consent state could not be restored; "
                            "disconnect and retry",
                        ) from restore_exc
                    raise ApiError(
                        "calendar_error",
                        "Microsoft calendar rejection could not be recorded; retry",
                    ) from state_exc
                raise ApiError("calendar_authorization_rejected", str(exc)) from exc
            except Microsoft365Error as exc:
                _restore_calendar_grant(runtime, account.account_id, profile, previous)
                raise ApiError("calendar_error", str(exc)) from exc

            principal = calendar.principal
            if principal.key != mailbox_principal.key:
                _restore_calendar_grant(runtime, account.account_id, profile, previous)
                raise ApiError(
                    "account_identity_mismatch",
                    "The authorized calendar does not match the selected Microsoft principal",
                )
            if (
                previous is not None
                and previous.state == "ready"
                and previous.principal_key not in {None, principal.key}
            ):
                _restore_calendar_grant(runtime, account.account_id, profile, previous)
                raise ApiError(
                    "calendar_principal_mismatch",
                    "The authorized calendar does not match the existing calendar principal",
                )
            if not _calendar_entitlement_active():
                _restore_calendar_grant(runtime, account.account_id, profile, previous)
                raise ApiError(
                    "calendar_entitlement_required",
                    "Calendar setup requires an active capability-exchange entitlement",
                )
            ready_identity = {
                "principal_key": principal.key,
                "home_account_id": principal.home_account_id,
                "tenant_id": principal.tenant_id,
                "object_id": principal.object_id,
                "email_address": principal.email_address,
            }
            try:
                runtime.store.set_calendar_grant(
                    account.account_id,
                    profile,
                    "ready",
                    **ready_identity,
                )
            except sqlite3.Error as exc:
                try:
                    _restore_calendar_grant(runtime, account.account_id, profile, previous)
                except sqlite3.Error as restore_exc:
                    raise ApiError(
                        "calendar_state_error",
                        "Microsoft calendar consent state could not be restored; "
                        "disconnect and retry",
                    ) from restore_exc
                raise ApiError(
                    "calendar_error",
                    "Microsoft calendar authorization could not be recorded; retry",
                ) from exc
            try:
                _install_private_token(staged_token, token_file)
            except (MailboxAccountUnavailable, OSError) as exc:
                _restore_calendar_grant(runtime, account.account_id, profile, previous)
                raise ApiError(
                    "calendar_error",
                    "Microsoft calendar authorization could not be saved; retry",
                ) from exc
        return _calendar_status_data(runtime, account, profile)

    return _with_mail_account_mutation(request, connect)


def _calendar_disconnect(request: dict[str, object], profile: str) -> dict[str, object]:
    payload = _payload(request, {"provider", "account_id"})

    def disconnect(runtime: Runtime) -> dict[str, object]:
        account = _calendar_account(runtime, payload, require_address=False)
        token_file = microsoft_calendar_token_file(runtime.config, account, profile)
        try:
            with FileLock(f"{token_file}.lock", timeout=TOKEN_LOCK_TIMEOUT_SECONDS):
                token_file.unlink(missing_ok=True)
        except FileLockTimeout as exc:
            raise ApiError("calendar_busy", "The calendar authorization is busy; retry") from exc
        runtime.store.disconnect_calendar_grant(account.account_id, profile)
        return {
            "account_id": account.account_id,
            "available": False,
            "entitlement_active": _calendar_entitlement_active(),
            "profile": profile,
            "scope": CALENDAR_AUTHORIZATION_PROFILES[profile].scopes[0],
            "state": "not_requested",
        }

    return _with_mail_account_mutation(request, disconnect)


def _calendar_read_status(request: dict[str, object]) -> dict[str, object]:
    return _calendar_status(request, CALENDAR_READ_PROFILE)


def _calendar_read_connect(request: dict[str, object]) -> dict[str, object]:
    return _calendar_connect(request, CALENDAR_READ_PROFILE)


def _calendar_read_disconnect(request: dict[str, object]) -> dict[str, object]:
    return _calendar_disconnect(request, CALENDAR_READ_PROFILE)


def _calendar_proposal_status(request: dict[str, object]) -> dict[str, object]:
    return _calendar_status(request, CALENDAR_PROPOSAL_PROFILE)


def _calendar_proposal_connect(request: dict[str, object]) -> dict[str, object]:
    return _calendar_connect(request, CALENDAR_PROPOSAL_PROFILE)


def _calendar_proposal_disconnect(request: dict[str, object]) -> dict[str, object]:
    return _calendar_disconnect(request, CALENDAR_PROPOSAL_PROFILE)


def _calendar_write_status(request: dict[str, object]) -> dict[str, object]:
    return _calendar_status(request, CALENDAR_WRITE_PROFILE)


def _calendar_write_connect(request: dict[str, object]) -> dict[str, object]:
    return _calendar_connect(request, CALENDAR_WRITE_PROFILE)


def _calendar_write_disconnect(request: dict[str, object]) -> dict[str, object]:
    return _calendar_disconnect(request, CALENDAR_WRITE_PROFILE)


def _calendar_window_payload(payload: dict[str, object]) -> tuple[str, str]:
    try:
        return canonical_calendar_window(
            payload.get("window_start"),
            payload.get("window_end"),
        )
    except ValueError as exc:
        raise ApiError("invalid_request", str(exc)) from exc


def _ready_calendar_read(
    runtime: Runtime,
    account: MailAccount,
) -> tuple[MicrosoftCalendarReadAuthorization, CalendarGrant]:
    _require_calendar_entitlement()
    grant = runtime.store.calendar_grant(account.account_id, CALENDAR_READ_PROFILE)
    if grant is None or grant.state != "ready":
        raise ApiError(
            "calendar_authorization_required",
            "Authorize calendar read access before using the calendar",
        )
    try:
        authorization = MicrosoftCalendarReadAuthorization.from_token(
            runtime.config.microsoft_credentials_file,
            microsoft_calendar_read_token_file(runtime.config, account),
        )
    except MicrosoftAuthorizationRejected as exc:
        runtime.store.revoke_calendar_grant_if_current(grant)
        raise ApiError("calendar_authorization_revoked", str(exc)) from exc
    except Microsoft365Error as exc:
        raise ApiError("calendar_error", str(exc)) from exc
    try:
        mailbox_principal = microsoft_mailbox_principal(
            runtime.config.microsoft_credentials_file,
            mail_account_token_file(runtime.config, account),
        )
    except MicrosoftAuthorizationRejected as exc:
        raise ApiError("mailbox_authorization_required", str(exc)) from exc
    except Microsoft365Error as exc:
        raise ApiError("calendar_error", str(exc)) from exc
    if (
        authorization.principal.key != grant.principal_key
        or mailbox_principal.key != grant.principal_key
    ):
        runtime.store.revoke_calendar_grant_if_current(grant)
        raise ApiError(
            "calendar_principal_mismatch",
            "The calendar grant does not match the selected Microsoft principal",
        )
    return authorization, grant


def _ready_local_calendar_read(runtime: Runtime, account: MailAccount) -> CalendarGrant:
    """Validate an offline projection read without refreshing either token cache."""
    _require_calendar_entitlement()
    grant = runtime.store.calendar_grant(account.account_id, CALENDAR_READ_PROFILE)
    if grant is None or grant.state != "ready":
        raise ApiError(
            "calendar_authorization_required",
            "Authorize calendar read access before using the calendar",
        )
    if not microsoft_calendar_read_token_file(runtime.config, account).is_file():
        raise ApiError(
            "calendar_authorization_required",
            "Authorize calendar read access before using the calendar",
        )
    if not mail_account_token_file(runtime.config, account).is_file():
        raise ApiError(
            "mailbox_authorization_required",
            "Reconnect the Microsoft 365 mailbox before reading its calendar",
        )
    try:
        mailbox_principal = microsoft_cached_mailbox_principal(
            mail_account_token_file(runtime.config, account)
        )
    except MicrosoftAuthorizationRejected as exc:
        raise ApiError("mailbox_authorization_required", str(exc)) from exc
    except Microsoft365Error as exc:
        raise ApiError("calendar_error", str(exc)) from exc
    if mailbox_principal.key != grant.principal_key:
        raise ApiError(
            "calendar_principal_mismatch",
            "The calendar grant does not match the selected Microsoft principal",
        )
    return grant


def _calendar_mutations(delta: CalendarDeltaRound) -> tuple[CalendarEventMutation, ...]:
    return tuple(
        CalendarEventMutation(
            event_id=change.event_id,
            event=(
                None
                if change.event is None
                else CalendarEventProjection(
                    event_id=change.event.event_id,
                    subject=change.event.subject,
                    start_date_time=change.event.start_date_time,
                    start_time_zone=change.event.start_time_zone,
                    end_date_time=change.event.end_date_time,
                    end_time_zone=change.event.end_time_zone,
                    is_all_day=change.event.is_all_day,
                    location=change.event.location,
                )
            ),
        )
        for change in delta.changes
    )


def _calendar_read_sync(request: dict[str, object]) -> dict[str, object]:
    payload = _payload(request, {"provider", "account_id", "window_start", "window_end"})
    window_start, window_end = _calendar_window_payload(payload)

    def sync(runtime: Runtime) -> dict[str, object]:
        account = _calendar_account(runtime, payload)
        authorization, grant = _ready_calendar_read(runtime, account)
        completed = runtime.store.calendar_window(account.account_id)
        same_projection = (
            completed is not None
            and completed.principal_key == grant.principal_key
            and completed.window_start == window_start
            and completed.window_end == window_end
        )
        cursor = completed.cursor if same_projection else None
        recovered = False
        try:
            delta = calendar_delta_round(
                authorization,
                window_start,
                window_end,
                cursor=cursor,
            )
        except StaleCalendarCursor:
            recovered = True
            try:
                delta = calendar_delta_round(
                    authorization,
                    window_start,
                    window_end,
                )
            except MicrosoftAuthorizationRejected as exc:
                runtime.store.revoke_calendar_grant_if_current(grant)
                raise ApiError("calendar_authorization_revoked", str(exc)) from exc
            except Microsoft365Error as exc:
                raise ApiError("calendar_error", str(exc)) from exc
        except MicrosoftAuthorizationRejected as exc:
            runtime.store.revoke_calendar_grant_if_current(grant)
            raise ApiError("calendar_authorization_revoked", str(exc)) from exc
        except Microsoft365Error as exc:
            raise ApiError("calendar_error", str(exc)) from exc
        try:
            runtime.store.commit_calendar_round(
                account_id=account.account_id,
                principal_key=grant.principal_key or "",
                window_start=window_start,
                window_end=window_end,
                cursor=delta.cursor,
                changes=_calendar_mutations(delta),
                replace=not same_projection or recovered,
            )
        except (sqlite3.Error, ValueError) as exc:
            raise ApiError(
                "calendar_state_error",
                "The completed calendar round could not be saved; retry",
            ) from exc
        return {
            "account_id": account.account_id,
            "change_count": len(delta.changes),
            "event_count": len(runtime.store.calendar_events(account.account_id)),
            "recovered_stale_cursor": recovered,
            "window_end": window_end,
            "window_start": window_start,
        }

    return _with_mail_account_mutation(request, sync)


def _calendar_event_data(event: CalendarEventProjection) -> dict[str, object]:
    return {
        "end": {"date_time": event.end_date_time, "time_zone": event.end_time_zone},
        "event_id": event.event_id,
        "is_all_day": event.is_all_day,
        "location": event.location,
        "start": {"date_time": event.start_date_time, "time_zone": event.start_time_zone},
        "subject": event.subject,
    }


def _calendar_read_events(request: dict[str, object]) -> dict[str, object]:
    payload = _payload(request, {"provider", "account_id", "window_start", "window_end"})
    window_start, window_end = _calendar_window_payload(payload)
    runtime = _runtime(request)
    account = _calendar_account(runtime, payload)
    grant = _ready_local_calendar_read(runtime, account)
    completed, events = runtime.store.calendar_projection(account.account_id)
    if (
        completed is None
        or completed.principal_key != grant.principal_key
        or completed.window_start != window_start
        or completed.window_end != window_end
    ):
        raise ApiError(
            "calendar_sync_required",
            "Synchronize this calendar window before reading its events",
        )
    data: dict[str, object] = {
        "account_id": account.account_id,
        "events": [_calendar_event_data(event) for event in events],
        "window_end": window_end,
        "window_start": window_start,
    }
    encoded = json.dumps(
        {
            "data": data,
            "ok": True,
            "operation": "calendar.read.events",
            "protocol": PROTOCOL_VERSION,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) + 1 > MAX_CALENDAR_EVENTS_RESPONSE_BYTES:
        raise ApiError(
            "calendar_result_too_large",
            "The calendar projection exceeds the engine response limit",
        )
    return data


def _mail_accounts(request: dict[str, object]) -> dict[str, object]:
    _payload(request)
    return _mail_accounts_public(_runtime(request))


def _mail_account_connect(request: dict[str, object]) -> dict[str, object]:
    payload = _payload(request, {"provider", "connection"})
    provider = _mail_provider(payload.get("provider"))
    connection = payload.get("connection")
    if provider != IMAP_PROVIDER and "connection" in payload:
        raise ApiError("invalid_request", "Browser authorization does not accept connection data")

    def connect(runtime: Runtime) -> dict[str, object]:
        if provider == DEFAULT_MAIL_PROVIDER:
            return _authorize_gmail_account(runtime, None, reuse_valid_token=False)
        if provider == MICROSOFT365_PROVIDER:
            return _authorize_microsoft_account(runtime, None)
        if provider == IMAP_PROVIDER:
            return _connect_imap_account(runtime, None, connection)
        raise ApiError("unsupported_provider", "That email provider is not available")

    return _with_mail_account_mutation(request, connect)


def _mail_account_reconnect(request: dict[str, object]) -> dict[str, object]:
    payload = _payload(request, {"provider", "account_id", "connection"})
    provider, account_id = _mail_account_key(payload)
    connection = payload.get("connection")
    if provider != IMAP_PROVIDER and "connection" in payload:
        raise ApiError("invalid_request", "Browser authorization does not accept connection data")

    def reconnect(runtime: Runtime) -> dict[str, object]:
        account = runtime.store.mail_account(provider, account_id)
        if account is None:
            raise ApiError("not_found", "The email account was not found")
        if provider == DEFAULT_MAIL_PROVIDER:
            return _authorize_gmail_account(runtime, account, reuse_valid_token=False)
        if provider == MICROSOFT365_PROVIDER:
            return _authorize_microsoft_account(runtime, account)
        if provider == IMAP_PROVIDER:
            return _connect_imap_account(runtime, account, connection)
        raise ApiError("unsupported_provider", "That email provider is not available")

    return _with_mail_account_mutation(request, reconnect)


def _mail_account_disconnect(request: dict[str, object]) -> dict[str, object]:
    payload = _payload(request, {"provider", "account_id"})
    provider, account_id = _mail_account_key(payload)

    def disconnect(runtime: Runtime) -> dict[str, object]:
        account = runtime.store.mail_account(provider, account_id)
        if account is None:
            raise ApiError("not_found", "The email account was not found")
        _disconnect_mail_account_token(runtime.config, account)
        return {"account": _mail_account_public(runtime, account)}

    return _with_mail_account_mutation(request, disconnect)


def _mail_account_activate(request: dict[str, object]) -> dict[str, object]:
    payload = _payload(request, {"provider", "account_id"})
    provider, account_id = _mail_account_key(payload)

    def activate(runtime: Runtime) -> dict[str, object]:
        account = runtime.store.mail_account(provider, account_id)
        if account is None:
            raise ApiError("not_found", "The email account was not found")
        if not mail_account_connected(runtime.config, account):
            raise ApiError("account_unavailable", "Connect the email account before using it")
        return {
            "account": _mail_account_public(
                runtime, runtime.store.activate_mail_account(provider, account_id)
            )
        }

    return _with_mail_account_mutation(request, activate)


def _gmail_authorize(request: dict[str, object]) -> dict[str, object]:
    """Compatibility operation for existing CLI/desktop protocol clients."""
    _payload(request)

    def authorize(runtime: Runtime) -> dict[str, object]:
        account = runtime.store.active_mail_account()
        if account is None:
            raise ApiError("not_found", "The Gmail account was not found")
        if account.provider != DEFAULT_MAIL_PROVIDER:
            raise ApiError("unsupported_provider", "The active email account is not Gmail")
        result = _authorize_gmail_account(runtime, account, reuse_valid_token=True)
        return {
            "baseline_initialized": result["baseline_initialized"],
            "connected": True,
        }

    return _with_mail_account_mutation(request, authorize)


def _check(request: dict[str, object]) -> dict[str, object]:
    payload = _payload(request, {"dry_run"})
    dry_run = payload.get("dry_run", False)
    if not isinstance(dry_run, bool):
        raise ApiError("invalid_request", "dry_run must be a boolean")

    runtime = _runtime(request)
    _require_host_delivery_compatible(runtime)

    def run(active_runtime: Runtime) -> dict[str, object]:
        _require_host_delivery_compatible(active_runtime)
        result = run_watcher_check(
            active_runtime.config,
            active_runtime.store,
            active_runtime.model,
            dry_run=dry_run,
            deliver_notifications=False,
        )
        return {
            **result,
            "pending_notifications": _host_notification_intent_count(active_runtime),
        }

    if dry_run:
        return run(runtime)

    lock_path = _production_check_lock_path(runtime.config)
    if not operation_lock_supported(lock_path):
        raise ApiError(
            "unsupported_platform",
            "Production watcher checks require native operation locking",
        )
    with operation_lock(lock_path, "Another production check is already running"):
        return run(_runtime(request))


def _hide_locked_automation_previews(rows: list[dict[str, object]]) -> None:
    if _automation_entitlement_active():
        return
    for row in rows:
        row["calendar_proposal"] = None


def _recent(request: dict[str, object]) -> dict[str, object]:
    payload = _payload(request, {"limit"})
    limit = _bounded_limit(payload, default=20)
    rows = _runtime(request).store.recent(limit)
    _hide_locked_automation_previews(rows)
    return {"items": rows}


def _query_inbox(request: dict[str, object]) -> dict[str, object]:
    payload = _payload(
        request,
        {
            "category",
            "account_id",
            "cursor",
            "keyword",
            "limit",
            "priority",
            "provider",
            "sender_query",
            "status",
        },
    )
    limit = _bounded_limit(payload, default=25, maximum=100)
    cursor = _decode_inbox_cursor(payload.get("cursor"))
    sender_query = _optional_inbox_text(payload, "sender_query", maximum=320)
    keyword = _optional_inbox_text(payload, "keyword", maximum=200)
    provider = _optional_inbox_text(payload, "provider", maximum=64)
    account_id = _optional_inbox_text(payload, "account_id", maximum=128)
    priority = _optional_inbox_choice(payload, "priority", INBOX_PRIORITIES)
    category = _optional_inbox_choice(payload, "category", INBOX_CATEGORIES)
    status = _optional_inbox_choice(payload, "status", INBOX_STATUSES)
    rows, next_cursor = _runtime(request).store.query_inbox(
        limit=limit,
        cursor=cursor,
        sender_query=sender_query,
        priority=priority,
        category=category,
        status=status,
        keyword=keyword,
        provider=provider,
        account_id=account_id,
    )
    _hide_locked_automation_previews(rows)
    return {"items": rows, "next_cursor": _encode_inbox_cursor(next_cursor)}


def _inbox_delete(request: dict[str, object]) -> dict[str, object]:
    payload = _payload(request, {"message_id"})
    message_id = payload.get("message_id")
    if not isinstance(message_id, str) or not message_id.strip() or len(message_id) > 512:
        raise ApiError(
            "invalid_request",
            "message_id must be a non-empty string of at most 512 characters",
        )
    runtime = _runtime(request)
    lock_path = _production_check_lock_path(runtime.config)
    if not operation_lock_supported(lock_path):
        raise ApiError(
            "unsupported_platform",
            "Local inbox changes require native operation locking",
        )
    with operation_lock(lock_path, "Another watcher operation is already running"):
        deleted = runtime.store.delete_message(message_id)
    if not deleted:
        raise ApiError("not_found", "Message was not found")
    return {"deleted": True, "message_id": message_id}


def _inbox_clear(request: dict[str, object]) -> dict[str, object]:
    _payload(request)
    runtime = _runtime(request)
    lock_path = _production_check_lock_path(runtime.config)
    if not operation_lock_supported(lock_path):
        raise ApiError(
            "unsupported_platform",
            "Local inbox changes require native operation locking",
        )
    with operation_lock(lock_path, "Another watcher operation is already running"):
        deleted = runtime.store.clear_messages()
    return {"deleted": deleted}


def _analysis_requeue(request: dict[str, object]) -> dict[str, object]:
    payload = _payload(request, {"message_id"})
    message_id = payload.get("message_id")
    if not isinstance(message_id, str) or not message_id.strip():
        raise ApiError("invalid_request", "message_id must be a non-empty string")
    try:
        status = _runtime(request).store.requeue_analysis(message_id)
    except KeyError as exc:
        raise ApiError("not_found", "Message was not found") from exc
    except RuntimeError as exc:
        raise ApiError("conflict", str(exc)) from exc
    return {"status": status}


def _attachment_destination(value: object) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ApiError("invalid_request", "destination_dir must be a non-empty string")
    destination = Path(value)
    if not destination.is_absolute():
        raise ApiError("invalid_request", "destination_dir must be an absolute path")
    try:
        resolved = destination.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ApiError("invalid_request", "destination_dir must be an existing directory") from exc
    if not resolved.is_dir():
        raise ApiError("invalid_request", "destination_dir must be an existing directory")
    return resolved


def _write_private_file(destination: Path, prefix: str, suffix: str, content: bytes) -> Path:
    descriptor, raw_path = tempfile.mkstemp(
        prefix=prefix,
        suffix=suffix,
        dir=destination,
    )
    path = Path(raw_path)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        path.chmod(0o600)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def _write_attachment(destination: Path, filename: str, content: bytes) -> Path:
    suffix = Path(filename).suffix
    if SAFE_ATTACHMENT_SUFFIX.fullmatch(suffix) is None:
        suffix = ""
    return _write_private_file(
        destination,
        "email-watcher-attachment-",
        suffix.casefold(),
        content,
    )


def _write_capability_output(destination: Path, content: bytes) -> Path:
    return _write_private_file(destination, "email-watcher-output-", ".bin", content)


def _configured_message_source(runtime: Runtime, message_id: str) -> MessageSource:
    try:
        source = runtime.store.message_source(message_id)
    except KeyError as exc:
        raise ApiError("not_found", "Message was not found") from exc
    account = runtime.store.mail_account(source.provider, source.account_id)
    if account is None or not mail_account_connected(runtime.config, account):
        raise ApiError(
            "account_unavailable",
            "The message's mailbox account is not available in this application version.",
        )
    return source


def _configured_mailbox_gateway(runtime: Runtime, source: MessageSource) -> MailboxGateway:
    mailbox = load_mailbox_account(
        runtime.config,
        runtime.store,
        source.provider,
        source.account_id,
    )
    if (mailbox.provider, mailbox.account_id) != (source.provider, source.account_id):
        raise RuntimeError("Mailbox identity changed while opening the provider")
    return mailbox.gateway


def _attachment_export(request: dict[str, object]) -> dict[str, object]:
    payload = _payload(request, {"message_id", "part_id", "destination_dir"})
    message_id = payload.get("message_id")
    part_id = payload.get("part_id")
    if not isinstance(message_id, str) or not message_id.strip():
        raise ApiError("invalid_request", "message_id must be a non-empty string")
    if not isinstance(part_id, str):
        raise ApiError("invalid_request", "part_id must be a string")
    destination = _attachment_destination(payload.get("destination_dir"))
    runtime = _runtime(request)
    try:
        attachment = runtime.store.attachment(message_id, part_id)
    except KeyError as exc:
        raise ApiError("not_found", "Attachment was not found") from exc
    source = _configured_message_source(runtime, message_id)
    gateway = _configured_mailbox_gateway(runtime, source)
    content = gateway.attachment_bytes(
        source.provider_message_id,
        part_id,
        attachment.attachment_id,
    )
    if len(content) != attachment.byte_size:
        raise MailboxError("Mailbox attachment size did not match stored metadata")
    try:
        path = _write_attachment(destination, attachment.filename, content)
    except OSError as exc:
        logger.warning("Attachment export failed: %s", exc)
        raise ApiError("export_failed", "Attachment could not be prepared") from exc
    return {
        "byte_size": len(content),
        "filename": attachment.filename,
        "media_type": attachment.media_type,
        "path": str(path),
    }


def _connect_capabilities(request: dict[str, object]) -> dict[str, object]:
    _payload(request)
    return connect.discover_summary_capability().public_result()


def _connect_attachment_ids(payload: dict[str, object]) -> tuple[str, str]:
    message_id = payload.get("message_id")
    part_id = payload.get("part_id")
    if not isinstance(message_id, str) or not message_id.strip():
        raise ApiError("invalid_request", "message_id must be a non-empty string")
    if not isinstance(part_id, str):
        raise ApiError("invalid_request", "part_id must be a string")
    return message_id, part_id


def _connect_attachment_capabilities(request: dict[str, object]) -> dict[str, object]:
    payload = _payload(request, {"message_id", "part_id"})
    message_id, part_id = _connect_attachment_ids(payload)
    runtime = _runtime(request)
    try:
        attachment = runtime.store.attachment(message_id, part_id)
    except KeyError as exc:
        raise ApiError("not_found", "Attachment was not found") from exc
    _configured_message_source(runtime, message_id)
    catalog = connect.discover_capabilities()
    items = catalog.compatible(attachment.media_type, attachment.byte_size)
    return {
        "items": [capability.public_dict() for capability in items],
        "diagnostic": (
            {"code": catalog.diagnostic_code} if catalog.diagnostic_code is not None else None
        ),
    }


def _selection_object(
    payload: dict[str, object], field: str, required_fields: set[str]
) -> dict[str, object]:
    value = payload.get(field)
    if not isinstance(value, dict) or set(value) != required_fields:
        names = ", ".join(sorted(required_fields))
        raise ApiError(
            "invalid_request",
            f"{field} must be an object containing exactly: {names}",
        )
    if any(not isinstance(value[name], str) or not value[name].strip() for name in required_fields):
        raise ApiError("invalid_request", f"{field} identity fields must be non-empty strings")
    return value


def _generic_invocation_selection(
    payload: dict[str, object],
) -> tuple[
    dict[str, str],
    dict[str, str],
    dict[str, object],
    bool,
]:
    provider = _selection_object(
        payload,
        "provider",
        {"app_id", "version", "instance_id"},
    )
    capability_ref = _selection_object(payload, "capability", {"id", "version"})
    parameters = payload.get("parameters", {})
    if not isinstance(parameters, dict) or any(not isinstance(name, str) for name in parameters):
        raise ApiError("invalid_request", "parameters must be an object with string keys")
    confirmed = payload.get("confirmed", False)
    if not isinstance(confirmed, bool):
        raise ApiError("invalid_request", "confirmed must be a boolean")

    return (
        {name: str(provider[name]) for name in ("app_id", "version", "instance_id")},
        {name: str(capability_ref[name]) for name in ("id", "version")},
        parameters,
        confirmed,
    )


def _discover_selected_generic_capability(
    provider: dict[str, str],
    capability_ref: dict[str, str],
    parameters: dict[str, object],
) -> tuple[connect.DiscoveredCapability, dict[str, str | int | bool]]:

    instance_id = str(provider["instance_id"])
    catalog = connect.discover_capabilities(provider_instance_id=instance_id)
    provider_items = tuple(
        item
        for item in catalog.items
        if item.app_id == provider["app_id"]
        and item.app_version == provider["version"]
        and item.instance_id == instance_id
    )
    if not provider_items:
        raise ApiError(
            "provider_unavailable",
            "The selected local capability provider is unavailable.",
        )
    matches = tuple(
        item
        for item in provider_items
        if item.capability_id == capability_ref["id"]
        and item.capability_version == capability_ref["version"]
    )
    if len(matches) != 1:
        raise ApiError(
            "capability_unavailable",
            "The selected local capability is unavailable.",
        )
    selected = matches[0]
    return selected, connect.validate_capability_parameters(selected, parameters)


def _connect_result(job: ConnectJob) -> dict[str, object]:
    if (
        job.status != "completed"
        or job.summary_version is None
        or job.summary_text is None
        or job.warnings_json is None
    ):
        raise RuntimeError("Completed Connect job is missing its durable result")
    warnings = Store.completed_connect_warnings(job)
    return {
        "job_id": job.job_id,
        "capability_id": job.capability_id,
        "capability_version": job.capability_version,
        "status": job.status,
        "summary": {
            "summary_version": job.summary_version,
            "text": job.summary_text,
            "warnings": warnings,
        },
    }


def _tracked_job(job: ConnectJob, filename: str) -> connect.PreparedSummaryJob:
    return connect.restore_summary_job(
        job_id=job.job_id,
        artifact_id=job.input_artifact_id,
        media_type=job.input_media_type,
        byte_size=job.input_byte_size,
        sha256=job.input_sha256,
        filename=filename,
    )


def _generic_connect_result(job: ConnectJob) -> dict[str, object]:
    if (
        job.protocol_version != connect.GENERIC_PROTOCOL_VERSION
        or job.status != "completed"
        or job.provider_app_id is None
        or job.provider_app_version is None
        or job.provider_instance_id is None
        or job.input_display_name is None
        or job.source_app_id is None
    ):
        raise RuntimeError("Completed Connect v2 job is missing its durable provenance")
    outputs = Store.completed_connect_outputs(job)
    return {
        "protocol_version": job.protocol_version,
        "job_id": job.job_id,
        "status": job.status,
        "provider": {
            "app_id": job.provider_app_id,
            "version": job.provider_app_version,
            "instance_id": job.provider_instance_id,
        },
        "capability": {
            "id": job.capability_id,
            "version": job.capability_version,
        },
        "input": {
            "artifact_id": job.input_artifact_id,
            "media_type": job.input_media_type,
            "display_name": job.input_display_name,
            "byte_size": job.input_byte_size,
            "sha256": job.input_sha256,
            "source_app_id": job.source_app_id,
        },
        "outputs": [output.metadata() for output in outputs],
    }


def _selected_connect_output(
    payload: dict[str, object], runtime: Runtime
) -> tuple[ConnectJob, ConnectOutput]:
    message_id, part_id = _connect_attachment_ids(payload)
    job_id = payload.get("job_id")
    artifact_id = payload.get("artifact_id")
    if not isinstance(job_id, str) or not job_id.strip():
        raise ApiError("invalid_request", "job_id must be a non-empty string")
    if not isinstance(artifact_id, str) or not artifact_id.strip():
        raise ApiError("invalid_request", "artifact_id must be a non-empty string")
    job = runtime.store.connect_job(job_id)
    if (
        job is None
        or job.protocol_version != connect.GENERIC_PROTOCOL_VERSION
        or job.message_id != message_id
        or job.part_id != part_id
    ):
        raise ApiError("not_found", "Capability output was not found")
    if job.status != "completed":
        raise ApiError("conflict", "Capability output is not complete")
    try:
        outputs = runtime.store.completed_connect_outputs(job)
    except RuntimeError as exc:
        logger.warning("Stored capability output failed validation: %s", exc)
        raise ApiError("output_invalid", "Stored capability output is invalid") from exc
    output = next((item for item in outputs if item.artifact_id == artifact_id), None)
    if output is None:
        raise ApiError("not_found", "Capability output was not found")
    return job, output


def _connect_output_present(request: dict[str, object]) -> dict[str, object]:
    payload = _payload(request, {"message_id", "part_id", "job_id", "artifact_id"})
    job, output = _selected_connect_output(payload, _runtime(request))
    if output.media_type == connect.OUTPUT_MEDIA_TYPE:
        summary = connect.decode_document_summary_output(
            connect.CapabilityOutput(
                artifact_id=output.artifact_id,
                media_type=output.media_type,
                display_name=output.display_name,
                byte_size=output.byte_size,
                sha256=output.sha256,
                payload=output.payload,
            ),
            connect.ArtifactIdentity(
                artifact_id=job.input_artifact_id,
                media_type=job.input_media_type,
                byte_size=job.input_byte_size,
                sha256=job.input_sha256,
            ),
        )
        presentation: dict[str, object] = {
            "kind": "document_summary",
            "summary": {
                "summary_version": summary.summary_version,
                "text": summary.text,
                "warnings": [dict(warning) for warning in summary.warnings],
            },
        }
    elif output.media_type == "text/plain" and output.byte_size <= MAX_NATIVE_TEXT_OUTPUT_BYTES:
        try:
            text = output.payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ApiError("output_invalid", "Text capability output is not valid UTF-8") from exc
        presentation = {"kind": "text", "text": text}
    else:
        presentation = {"kind": "opaque"}
    return {
        "job_id": job.job_id,
        "output": output.metadata(),
        "presentation": presentation,
    }


def _connect_output_export(request: dict[str, object]) -> dict[str, object]:
    payload = _payload(
        request,
        {"message_id", "part_id", "job_id", "artifact_id", "destination_dir"},
    )
    job, output = _selected_connect_output(payload, _runtime(request))
    destination = _attachment_destination(payload.get("destination_dir"))
    try:
        path = _write_capability_output(destination, output.payload)
    except OSError as exc:
        logger.warning("Capability output export failed: %s", exc)
        raise ApiError("export_failed", "Capability output could not be prepared") from exc
    return {
        "job_id": job.job_id,
        "output": output.metadata(),
        "path": str(path),
    }


def _tracked_generic_job(
    job: ConnectJob, capability: connect.DiscoveredCapability
) -> connect.PreparedCapabilityJob:
    if (
        job.protocol_version != connect.GENERIC_PROTOCOL_VERSION
        or job.capability_id != capability.capability_id
        or job.capability_version != capability.capability_version
        or job.provider_app_id != capability.app_id
        or job.provider_app_version != capability.app_version
        or job.provider_instance_id != capability.instance_id
        or job.request_json is None
    ):
        raise RuntimeError("Connect v2 job is missing its durable provider request")
    return connect.restore_persisted_capability_job(capability, job.request_json)


def _apply_connect_update(
    store: Store, update: connect.JobUpdate | connect.CapabilityJobUpdate
) -> ConnectJob:
    allowed = {
        "requested": {"accepted", "processing", "completed", "failed"},
        "accepted": {"processing", "completed", "failed"},
        "processing": {"completed", "failed"},
    }
    active_rank = {"requested": 0, "accepted": 1, "processing": 2}
    for _attempt in range(4):
        current = store.connect_job(update.job_id)
        if current is None:
            raise RuntimeError("Connect job disappeared before its status could persist")
        if current.status == update.status:
            return current
        if update.status in active_rank and (
            current.status in {"completed", "failed"}
            or active_rank.get(current.status, -1) > active_rank[update.status]
        ):
            return current
        if update.status not in allowed.get(current.status, set()):
            raise connect.ConnectError(
                "JOB_STATE_INVALID",
                "The local capability provider returned an invalid job transition.",
            )
        try:
            return store.transition_connect_job(
                job_id=update.job_id,
                expected_state=current.status,
                next_state=update.status,
                provider_app_id=update.provider_app_id,
                provider_instance_id=update.provider_instance_id,
                result=update.result.store_dict() if update.result else None,
                error=(
                    {
                        "code": update.error.code,
                        "message": str(update.error),
                        "retryable": update.error.retryable,
                    }
                    if update.error
                    else None
                ),
            )
        except RuntimeError:
            continue
    raise RuntimeError("Connect job status could not be persisted after concurrent updates")


def _mark_connect_failed(
    store: Store,
    job_id: str,
    provider: connect.ProviderCapability | connect.DiscoveredCapability,
    error: connect.ConnectError,
) -> None:
    for _attempt in range(4):
        current = store.connect_job(job_id)
        if current is None or current.status not in {
            "requested",
            "accepted",
            "processing",
        }:
            return
        try:
            store.transition_connect_job(
                job_id=job_id,
                expected_state=current.status,
                next_state="failed",
                provider_app_id=provider.app_id,
                provider_instance_id=provider.instance_id,
                error={
                    "code": error.code,
                    "message": str(error),
                    "retryable": error.retryable,
                },
            )
            return
        except RuntimeError:
            continue
    raise RuntimeError("Connect failure could not be persisted after concurrent updates")


def _run_connect_job(
    runtime: Runtime,
    provider: connect.ProviderCapability,
    job: connect.PreparedSummaryJob,
    content: bytes | None,
) -> dict[str, object]:
    client = connect.ConnectClient(provider)
    try:
        initial = client.submit(job, content) if content is not None else client.get(job)
        persisted = _apply_connect_update(runtime.store, initial)
        if persisted.status == "completed":
            return _connect_result(persisted)
        if persisted.status == "failed":
            raise _stored_connect_failure(persisted)
        final = client.wait_for_terminal(
            job,
            initial,
            lambda update: _apply_connect_update(runtime.store, update),
        )
        if final.status == "failed":
            if final.error is None:
                raise RuntimeError("Failed Connect job omitted its error")
            raise final.error
        if final.status != "completed" or final.result is None:
            raise RuntimeError("Connect job completed without a summary")
        if persisted.status != "completed":
            persisted = runtime.store.connect_job(job.job_id) or persisted
        return _connect_result(persisted)
    except connect.ConnectError as exc:
        if not exc.retryable and exc.code != "JOB_NOT_FOUND":
            try:
                _mark_connect_failed(runtime.store, job.job_id, provider, exc)
            except Exception:
                logger.exception("Connect failure could not be persisted")
                raise RuntimeError("Connect failure could not be persisted safely") from exc
        raise


def _query_connect_job(
    runtime: Runtime,
    provider: connect.ProviderCapability,
    job: connect.PreparedSummaryJob,
) -> dict[str, object] | None:
    try:
        return _run_connect_job(runtime, provider, job, None)
    except connect.ConnectError as exc:
        current = runtime.store.connect_job(job.job_id)
        if (
            exc.code == "JOB_NOT_FOUND"
            and current is not None
            and current.status in {"requested", "accepted", "processing"}
        ):
            return None
        raise


def _run_generic_connect_job(
    runtime: Runtime,
    capability: connect.DiscoveredCapability,
    job: connect.PreparedCapabilityJob,
    content: bytes | None,
) -> dict[str, object]:
    client = connect.ConnectV2Client(capability)
    try:
        initial = client.submit(job, content) if content is not None else client.get(job)
        persisted = _apply_connect_update(runtime.store, initial)
        if persisted.status == "completed":
            return _generic_connect_result(persisted)
        if persisted.status == "failed":
            raise _stored_connect_failure(persisted)
        final = client.wait_for_terminal(
            job,
            initial,
            lambda update: _apply_connect_update(runtime.store, update),
        )
        if final.status == "failed":
            if final.error is None:
                raise RuntimeError("Failed Connect v2 job omitted its error")
            raise final.error
        if final.status != "completed" or final.result is None:
            raise RuntimeError("Connect v2 job completed without a result")
        if persisted.status != "completed":
            persisted = runtime.store.connect_job(job.job_id) or persisted
        return _generic_connect_result(persisted)
    except connect.ConnectError as exc:
        if not exc.retryable and exc.code != "JOB_NOT_FOUND":
            try:
                _mark_connect_failed(runtime.store, job.job_id, capability, exc)
            except Exception:
                logger.exception("Connect v2 failure could not be persisted")
                raise RuntimeError("Connect failure could not be persisted safely") from exc
        raise


def _query_generic_connect_job(
    runtime: Runtime,
    capability: connect.DiscoveredCapability,
    job: connect.PreparedCapabilityJob,
) -> dict[str, object] | None:
    try:
        return _run_generic_connect_job(runtime, capability, job, None)
    except connect.ConnectError as exc:
        current = runtime.store.connect_job(job.job_id)
        if (
            exc.code == "JOB_NOT_FOUND"
            and current is not None
            and current.status in {"requested", "accepted", "processing"}
        ):
            return None
        raise


def _stored_connect_failure(job: ConnectJob) -> ApiError:
    return ApiError(
        (job.error_code or "connect_job_failed").casefold(),
        job.error_message or "The local capability job failed.",
    )


def _resume_generic_connect_job(
    runtime: Runtime,
    capability: connect.DiscoveredCapability,
    active: ConnectJob,
    content: Callable[[], bytes],
) -> dict[str, object]:
    tracked = _tracked_generic_job(active, capability)
    reconciled = _query_generic_connect_job(runtime, capability, tracked)
    if reconciled is not None:
        return reconciled
    refreshed = runtime.store.connect_job(active.job_id)
    if refreshed is None:
        raise RuntimeError("Connect v2 job disappeared during reconciliation")
    if refreshed.status == "completed":
        return _generic_connect_result(refreshed)
    if refreshed.status == "failed":
        raise _stored_connect_failure(refreshed)
    tracked = _tracked_generic_job(refreshed, capability)
    if not capability.accepts_artifact(tracked.artifact.media_type, tracked.artifact.byte_size):
        raise ApiError(
            "unsupported_attachment",
            "The attachment is no longer accepted by the selected capability.",
        )
    try:
        runtime.store.reset_connect_job_for_resubmission(
            job_id=tracked.job_id,
            expected_state=refreshed.status,
            provider_app_id=capability.app_id,
            provider_instance_id=capability.instance_id,
        )
    except RuntimeError as exc:
        concurrent = runtime.store.connect_job(tracked.job_id)
        if concurrent is not None and concurrent.status == "completed":
            return _generic_connect_result(concurrent)
        if concurrent is not None and concurrent.status == "failed":
            raise _stored_connect_failure(concurrent) from exc
        raise ApiError(
            "connect_job_in_progress",
            "The local capability job changed while it was being reconciled.",
        ) from exc
    return _run_generic_connect_job(runtime, capability, tracked, content())


def _tracked_invocation_job(
    job: ConnectJob,
    capability: connect.DiscoveredCapability,
    *,
    message_id: str,
    part_id: str,
    parameters: dict[str, str | int | bool],
) -> connect.PreparedCapabilityJob:
    if job.message_id != message_id or job.part_id != part_id:
        raise ApiError(
            "request_id_conflict",
            "The capability request identity belongs to another attachment.",
        )
    try:
        tracked = _tracked_generic_job(job, capability)
    except (RuntimeError, connect.ConnectError) as exc:
        raise ApiError(
            "request_id_conflict",
            "The capability request identity belongs to another invocation.",
        ) from exc
    if dict(tracked.parameters) != parameters:
        raise ApiError(
            "request_id_conflict",
            "The capability request identity belongs to different parameters.",
        )
    return tracked


def _validate_existing_invocation_identity(
    job: ConnectJob,
    *,
    message_id: str,
    part_id: str,
    provider: dict[str, str],
    capability: dict[str, str],
    parameters: dict[str, object],
) -> None:
    if (
        job.protocol_version != connect.GENERIC_PROTOCOL_VERSION
        or job.message_id != message_id
        or job.part_id != part_id
        or job.provider_app_id != provider["app_id"]
        or job.provider_app_version != provider["version"]
        or job.provider_instance_id != provider["instance_id"]
        or job.capability_id != capability["id"]
        or job.capability_version != capability["version"]
    ):
        raise ApiError(
            "request_id_conflict",
            "The capability request identity belongs to another invocation.",
        )
    try:
        requested_parameters = Store.canonical_connect_parameters(parameters)
    except ValueError as exc:
        raise ApiError("invalid_request", "parameters contain unsupported values") from exc
    if Store.connect_job_parameters(job) != requested_parameters:
        raise ApiError(
            "request_id_conflict",
            "The capability request identity belongs to different parameters.",
        )


def _connect_attachment_invoke(request: dict[str, object]) -> dict[str, object]:
    payload = _payload(
        request,
        {
            "request_id",
            "message_id",
            "part_id",
            "provider",
            "capability",
            "parameters",
            "confirmed",
        },
    )
    request_id = connect.validate_job_id(payload.get("request_id"))
    message_id, part_id = _connect_attachment_ids(payload)
    runtime = _runtime(request)
    try:
        attachment = runtime.store.attachment(message_id, part_id)
    except KeyError as exc:
        raise ApiError("not_found", "Attachment was not found") from exc
    source = _configured_message_source(runtime, message_id)
    provider_ref, capability_ref, requested_parameters, confirmed = _generic_invocation_selection(
        payload
    )
    existing = runtime.store.connect_job(request_id)
    if existing is not None:
        _validate_existing_invocation_identity(
            existing,
            message_id=message_id,
            part_id=part_id,
            provider=provider_ref,
            capability=capability_ref,
            parameters=requested_parameters,
        )
        if existing.status == "completed":
            return _generic_connect_result(existing)
        if existing.status == "failed":
            raise _stored_connect_failure(existing)

    connect.require_connect_entitlement()

    capability, parameters = _discover_selected_generic_capability(
        provider_ref,
        capability_ref,
        requested_parameters,
    )
    cached_content: bytes | None = None

    def attachment_content() -> bytes:
        nonlocal cached_content
        if cached_content is None:
            gateway = _configured_mailbox_gateway(runtime, source)
            cached_content = gateway.attachment_bytes(
                source.provider_message_id,
                part_id,
                attachment.attachment_id,
            )
            if len(cached_content) != attachment.byte_size:
                raise MailboxError("Mailbox attachment size did not match stored metadata")
        return cached_content

    if existing is not None:
        _tracked_invocation_job(
            existing,
            capability,
            message_id=message_id,
            part_id=part_id,
            parameters=parameters,
        )
        return _resume_generic_connect_job(
            runtime,
            capability,
            existing,
            attachment_content,
        )

    if not capability.accepts_artifact(attachment.media_type, attachment.byte_size):
        raise ApiError(
            "unsupported_attachment",
            "The attachment is not accepted by the selected capability.",
        )
    if (capability.external_effects or capability.confirmation_required) and not confirmed:
        raise ApiError(
            "confirmation_required",
            "The selected capability requires explicit confirmation.",
        )
    content = attachment_content()
    candidate = connect.prepare_capability_job(
        capability,
        content,
        attachment.media_type,
        attachment.filename,
        parameters=parameters,
        confirmed=confirmed,
        job_id=request_id,
    )
    try:
        runtime.store.create_connect_job(
            job_id=candidate.job_id,
            message_id=message_id,
            part_id=part_id,
            protocol_version=connect.GENERIC_PROTOCOL_VERSION,
            capability_id=capability.capability_id,
            capability_version=capability.capability_version,
            provider_app_id=capability.app_id,
            provider_app_version=capability.app_version,
            provider_instance_id=capability.instance_id,
            input_artifact_id=candidate.artifact.artifact_id,
            input_media_type=candidate.artifact.media_type,
            input_byte_size=candidate.artifact.byte_size,
            input_sha256=candidate.artifact.sha256,
            input_display_name=candidate.display_name,
            source_app_id=connect.SOURCE_APP_ID,
            request_json=candidate.request_json,
        )
    except sqlite3.IntegrityError as exc:
        exact = runtime.store.connect_job(request_id)
        if exact is not None:
            _tracked_invocation_job(
                exact,
                capability,
                message_id=message_id,
                part_id=part_id,
                parameters=parameters,
            )
            if exact.status == "completed":
                return _generic_connect_result(exact)
            if exact.status == "failed":
                raise _stored_connect_failure(exact) from exc
            return _resume_generic_connect_job(
                runtime,
                capability,
                exact,
                attachment_content,
            )
        raise
    return _run_generic_connect_job(runtime, capability, candidate, content)


def _connect_attachment_summarize(request: dict[str, object]) -> dict[str, object]:
    payload = _payload(request, {"message_id", "part_id"})
    message_id = payload.get("message_id")
    part_id = payload.get("part_id")
    if not isinstance(message_id, str) or not message_id.strip():
        raise ApiError("invalid_request", "message_id must be a non-empty string")
    if not isinstance(part_id, str):
        raise ApiError("invalid_request", "part_id must be a string")

    runtime = _runtime(request)
    try:
        attachment = runtime.store.attachment(message_id, part_id)
    except KeyError as exc:
        raise ApiError("not_found", "Attachment was not found") from exc
    source = _configured_message_source(runtime, message_id)
    if not connect.capability_matches_attachment(attachment.media_type, attachment.byte_size):
        raise ApiError("unsupported_attachment", "This attachment is not a supported PDF")

    completed = runtime.store.completed_connect_job(
        message_id=message_id,
        part_id=part_id,
        capability_id=connect.CAPABILITY_ID,
        capability_version=connect.CAPABILITY_VERSION,
    )
    if completed is not None:
        return _connect_result(completed)

    connect.require_connect_entitlement()

    active = runtime.store.active_connect_job(
        message_id=message_id,
        part_id=part_id,
        capability_id=connect.CAPABILITY_ID,
        capability_version=connect.CAPABILITY_VERSION,
    )
    discovery = (
        connect.discover_summary_capability(provider_instance_id=active.provider_instance_id)
        if active is not None
        else connect.discover_summary_capability()
    )
    provider = discovery.provider
    if provider is None:
        if active is not None:
            raise ApiError(
                "provider_unavailable",
                "The provider for the active local capability job is unavailable.",
            )
        code = discovery.diagnostic_code or "provider_unavailable"
        message = (
            "More than one compatible local capability provider is available."
            if code == "ambiguous_provider"
            else "No compatible local document summary capability is available."
        )
        raise ApiError(code, message)
    resubmit_expected_state: str | None = None
    if active is not None:
        if (
            active.provider_app_id != provider.app_id
            or active.provider_instance_id != provider.instance_id
        ):
            raise ApiError(
                "provider_unavailable",
                "The provider for the active local capability job is unavailable.",
            )
        tracked = _tracked_job(active, attachment.filename)
        reconciled = _query_connect_job(runtime, provider, tracked)
        if reconciled is not None:
            return reconciled
        refreshed = runtime.store.connect_job(active.job_id)
        if refreshed is None:
            raise RuntimeError("Connect job disappeared during reconciliation")
        if refreshed.status == "completed":
            return _connect_result(refreshed)
        if refreshed.status == "failed":
            raise ApiError(
                (refreshed.error_code or "connect_job_failed").lower(),
                refreshed.error_message or "The local capability job failed.",
            )
        if (
            refreshed.provider_app_id != provider.app_id
            or refreshed.provider_instance_id != provider.instance_id
        ):
            raise ApiError(
                "provider_unavailable",
                "The provider for the active local capability job is unavailable.",
            )
        resubmit_expected_state = refreshed.status
        job = _tracked_job(refreshed, attachment.filename)
    else:
        job = None

    if attachment.byte_size > provider.max_input_bytes:
        raise ApiError("input_too_large", "The PDF exceeds the provider's input limit")
    gateway = _configured_mailbox_gateway(runtime, source)
    content = gateway.attachment_bytes(
        source.provider_message_id,
        part_id,
        attachment.attachment_id,
    )
    if len(content) != attachment.byte_size:
        raise MailboxError("Mailbox attachment size did not match stored metadata")
    if job is None:
        job = connect.prepare_summary_job(content, attachment.filename)
        try:
            runtime.store.create_connect_job(
                job_id=job.job_id,
                message_id=message_id,
                part_id=part_id,
                capability_id=connect.CAPABILITY_ID,
                capability_version=connect.CAPABILITY_VERSION,
                provider_app_id=provider.app_id,
                provider_instance_id=provider.instance_id,
                input_artifact_id=job.artifact.artifact_id,
                input_media_type=job.artifact.media_type,
                input_byte_size=job.artifact.byte_size,
                input_sha256=job.artifact.sha256,
            )
        except sqlite3.IntegrityError as exc:
            concurrent = runtime.store.active_connect_job(
                message_id=message_id,
                part_id=part_id,
                capability_id=connect.CAPABILITY_ID,
                capability_version=connect.CAPABILITY_VERSION,
            )
            if concurrent is not None:
                raise ApiError(
                    "connect_job_in_progress",
                    "A summary job is already in progress for this attachment.",
                ) from exc
            raise
    elif resubmit_expected_state is not None:
        try:
            runtime.store.reset_connect_job_for_resubmission(
                job_id=job.job_id,
                expected_state=resubmit_expected_state,
                provider_app_id=provider.app_id,
                provider_instance_id=provider.instance_id,
            )
        except RuntimeError as exc:
            concurrent = runtime.store.connect_job(job.job_id)
            if concurrent is not None and concurrent.status == "completed":
                return _connect_result(concurrent)
            raise ApiError(
                "connect_job_in_progress",
                "The local capability job changed while it was being reconciled.",
            ) from exc
    return _run_connect_job(runtime, provider, job, content)


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


def _settings_data(config: Config) -> dict[str, object]:
    return {
        "body_char_limit": config.body_char_limit,
        "local_model": {
            "authentication_required": config.model_require_auth,
            "editable": config.model_backend == "loopback",
            "endpoint": config.model_base_url,
            "model": config.model_name,
            "timeout_seconds": config.model_timeout_seconds,
            "token_configured": bool(
                config.model_api_token_file and config.model_api_token_file.exists()
            ),
        },
        "notifications_enabled": config.notifications_enabled,
        "poll_interval_minutes": config.poll_interval_minutes,
        "polling_supported": (
            config.ntfy_topic is None
            and operation_lock_supported(_production_check_lock_path(config))
        ),
        "retention_days": config.retention_days,
        "timezone": config.timezone,
    }


def _config_initialize(request: dict[str, object]) -> dict[str, object]:
    payload = _payload(request, {"model_base_url", "model_name", "timezone"})
    values: dict[str, str] = {}
    for field in ("model_base_url", "model_name", "timezone"):
        value = payload.get(field)
        if not isinstance(value, str):
            raise ApiError("invalid_request", f"{field} must be a string")
        values[field] = value
    try:
        config = initialize_config(_config_path(request), **values)
    except InvalidConfigInitializationError as exc:
        raise ApiError("invalid_request", str(exc)) from exc
    except ConfigAlreadyExistsError as exc:
        raise ApiError("conflict", str(exc)) from exc
    return {"created": True, "settings": _settings_data(config)}


def _settings(request: dict[str, object]) -> dict[str, object]:
    _payload(request)
    return _settings_data(load_config(_config_path(request)))


def _settings_update(request: dict[str, object]) -> dict[str, object]:
    payload = _payload(
        request,
        set(MUTABLE_DESKTOP_SETTINGS),
    )
    try:
        if "retention_days" not in payload:
            config = update_settings(_config_path(request), payload)
        else:
            runtime = _runtime(request)
            lock_path = _production_check_lock_path(runtime.config)
            if not operation_lock_supported(lock_path):
                raise ApiError(
                    "unsupported_platform",
                    "Retention changes require native operation locking",
                )
            with operation_lock(lock_path, "Another watcher operation is already running"):
                config = update_settings(_config_path(request), payload)
                runtime.store.purge(config.retention_days)
    except InvalidSettingsUpdateError as exc:
        raise ApiError("invalid_request", str(exc)) from exc
    return _settings_data(config)


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
    elif intent.kind == "automation_review":
        body = intent.summary or "A scheduling mention needs manual review."
        priority = "normal"
    else:
        body = "A watched email arrived. Local summary unavailable; it will be retried."
        priority = "normal"
    return {
        "analysis_at": intent.analysis_at,
        "body": body,
        "kind": intent.kind,
        "message_id": intent.message_id,
        "priority": priority,
        "revision": intent.revision,
        "subject_id": intent.subject_id,
        "subject_type": intent.subject_type,
        "title": title,
    }


def _notifications_pending(request: dict[str, object]) -> dict[str, object]:
    payload = _payload(request, {"limit"})
    limit = _bounded_limit(payload, default=25)
    runtime = _runtime(request)
    _require_host_delivery_compatible(runtime)
    lock_path = _production_check_lock_path(runtime.config)
    if not operation_lock_supported(lock_path):
        raise ApiError(
            "unsupported_platform",
            "Host notification delivery requires native operation locking",
        )
    with operation_lock(lock_path, "Another watcher operation is already running"):
        runtime = _runtime(request)
        items = _pending_notification_payloads(runtime, limit)
    return {"items": items}


def _pending_notification_payloads(runtime: Runtime, limit: int) -> list[dict[str, object]]:
    config = runtime.config
    _require_host_delivery_compatible(runtime)
    runtime.store.purge(config.retention_days)
    intents = _host_notification_intents(runtime, limit)
    sender_names = {sender.email: sender.name for sender in config.senders}
    return [_notification_payload(intent, sender_names) for intent in intents]


def _notifications_pending_under_host_lock(
    request: dict[str, object],
) -> dict[str, object]:
    payload = _payload(request, {"limit"})
    limit = _bounded_limit(payload, default=25)
    return {"items": _pending_notification_payloads(_runtime(request), limit)}


def _notifications_count_under_host_lock(
    request: dict[str, object],
) -> dict[str, object]:
    _payload(request)
    runtime = _runtime(request)
    _require_host_delivery_compatible(runtime)
    runtime.store.purge(runtime.config.retention_days)
    return {"count": _host_notification_intent_count(runtime)}


def _notifications_ack(request: dict[str, object]) -> dict[str, object]:
    payload = _payload(
        request,
        {
            "message_id",
            "kind",
            "analysis_at",
            "subject_type",
            "subject_id",
            "revision",
        },
    )
    message_id = payload.get("message_id")
    kind = payload.get("kind")
    analysis_at = payload.get("analysis_at")
    subject_type = payload.get("subject_type")
    subject_id = payload.get("subject_id")
    revision = payload.get("revision")
    if not isinstance(message_id, str) or not message_id.strip():
        raise ApiError("invalid_request", "message_id must be a non-empty string")
    if kind not in {"analysis", "fallback", "automation_review"}:
        raise ApiError(
            "invalid_request",
            "kind must be analysis, fallback, or automation_review",
        )
    if analysis_at is not None and not isinstance(analysis_at, str):
        raise ApiError("invalid_request", "analysis_at must be a string or null")
    for field_name, value in (
        ("subject_type", subject_type),
        ("subject_id", subject_id),
        ("revision", revision),
    ):
        if value is not None and (not isinstance(value, str) or not value):
            raise ApiError("invalid_request", f"{field_name} must be a non-empty string or null")
    runtime = _runtime(request)
    _require_host_delivery_compatible(runtime)
    try:
        status = runtime.store.acknowledge_notification(
            message_id=message_id,
            kind=kind,
            analysis_at=analysis_at,
            subject_type=subject_type,
            subject_id=subject_id,
            revision=revision,
        )
    except KeyError as exc:
        raise ApiError("not_found", "Notification message was not found") from exc
    except ValueError as exc:
        raise ApiError("invalid_request", str(exc)) from exc
    except RuntimeError as exc:
        raise ApiError("stale_notification", str(exc)) from exc
    return {"status": status}


OPERATIONS: dict[str, Callable[[dict[str, object]], dict[str, object]]] = {
    "analysis.requeue": _analysis_requeue,
    "attachment.export": _attachment_export,
    "calendar.read.connect": _calendar_read_connect,
    "calendar.read.disconnect": _calendar_read_disconnect,
    "calendar.read.events": _calendar_read_events,
    "calendar.read.status": _calendar_read_status,
    "calendar.read.sync": _calendar_read_sync,
    "calendar.proposal.connect": _calendar_proposal_connect,
    "calendar.proposal.disconnect": _calendar_proposal_disconnect,
    "calendar.proposal.status": _calendar_proposal_status,
    "calendar.write.connect": _calendar_write_connect,
    "calendar.write.disconnect": _calendar_write_disconnect,
    "calendar.write.status": _calendar_write_status,
    "config.initialize": _config_initialize,
    "connect.attachment.capabilities": _connect_attachment_capabilities,
    "connect.attachment.invoke": _connect_attachment_invoke,
    "connect.attachment.summarize": _connect_attachment_summarize,
    "connect.capabilities": _connect_capabilities,
    "connect.entitlement.install": _connect_entitlement_install,
    "connect.entitlement.status": _connect_entitlement_status,
    "connect.output.export": _connect_output_export,
    "connect.output.present": _connect_output_present,
    "gmail.authorize": _gmail_authorize,
    "health.get": _health,
    "host.operation_lock": _host_operation_lock,
    "inbox.clear": _inbox_clear,
    "inbox.delete": _inbox_delete,
    "inbox.query": _query_inbox,
    "inbox.recent": _recent,
    "mail.accounts.activate": _mail_account_activate,
    "mail.accounts.connect": _mail_account_connect,
    "mail.accounts.disconnect": _mail_account_disconnect,
    "mail.accounts.list": _mail_accounts,
    "mail.accounts.reconnect": _mail_account_reconnect,
    "notifications.ack": _notifications_ack,
    "notifications.count_under_host_lock": _notifications_count_under_host_lock,
    "notifications.pending": _notifications_pending,
    "notifications.pending_under_host_lock": _notifications_pending_under_host_lock,
    "settings.get": _settings,
    "settings.update": _settings_update,
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
    except MailboxAccountUnavailable as exc:
        return {
            "error": {"code": "account_unavailable", "message": str(exc)},
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
    except ImapError as exc:
        logger.warning("IMAP operation failed (%s): %s", exc.code, exc)
        return {
            "error": {"code": exc.code, "message": str(exc)},
            "ok": False,
            "operation": operation,
            "protocol": PROTOCOL_VERSION,
        }
    except MailboxError as exc:
        logger.warning("Mailbox operation failed: %s", exc)
        return {
            "error": {
                "code": "mailbox_error",
                "message": "Email provider operation failed; see stderr for details",
            },
            "ok": False,
            "operation": operation,
            "protocol": PROTOCOL_VERSION,
        }
    except connect.ConnectError as exc:
        logger.warning("Connect operation failed (%s): %s", exc.code, exc)
        return {
            "error": {"code": exc.code.casefold(), "message": str(exc)},
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
                "error": {
                    "code": "invalid_json",
                    "message": "Request must be valid JSON",
                },
                "ok": False,
                "operation": None,
                "protocol": PROTOCOL_VERSION,
            }
        else:
            response = _response(request)
    encoded = json.dumps(
        response,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    sys.stdout.buffer.write(encoded + b"\n")
    sys.stdout.buffer.flush()
    raise SystemExit(0 if response["ok"] else 2)


if __name__ == "__main__":
    main()
