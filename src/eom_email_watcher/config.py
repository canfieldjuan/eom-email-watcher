from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import logging
import os
import re
import secrets
import stat
import sys
import tempfile
import tomllib
from collections.abc import Callable, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from email.utils import parseaddr
from ipaddress import ip_address
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from filelock import FileLock
from tomlkit import aot, document, dumps, inline_table, parse, table
from tomlkit.items import AoT, Array

DEFAULT_CONFIG = Path("~/.config/eom-email-watcher/config.toml").expanduser()
DEFAULT_POLL_INTERVAL_MINUTES = 120
DEFAULT_RETENTION_DAYS = 180
MIN_RETENTION_DAYS = 1
MAX_RETENTION_DAYS = 3650
MAX_SENDER_NAME_BYTES = 1024
MAX_ADMISSION_SELECTOR_BYTES = 512
EXACT_SENDER_SELECTOR_PREFIX = "sender:"
NTFY_TOPIC_RE = re.compile(r"^[-_A-Za-z0-9]{20,64}$")
INITIALIZATION_RECEIPT_RE = re.compile(r"^[0-9a-f]{32}$")
DOMAIN_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
GATEWAY_MODEL_LABEL = "Managed by inference gateway"
MUTABLE_DESKTOP_SETTINGS = frozenset(
    {
        "model_base_url",
        "model_name",
        "notifications_enabled",
        "poll_interval_minutes",
        "retention_days",
    }
)

logger = logging.getLogger(__name__)


class ConfigError(ValueError):
    """Configuration is missing or unsafe."""


class InvalidSenderError(ConfigError):
    """A proposed watchlist sender is not valid."""


class DuplicateSenderError(ConfigError):
    """A proposed watchlist sender already exists."""


class SenderNotFoundError(ConfigError):
    """A requested watchlist sender does not exist."""


class InvalidSettingsUpdateError(ConfigError):
    """A proposed desktop settings update is not valid."""


class InvalidConfigInitializationError(ConfigError):
    """A proposed first-run desktop configuration is not valid."""


class ConfigAlreadyExistsError(ConfigError):
    """First-run initialization cannot replace an existing configuration."""


class NtfyDisclosureConflictError(ConfigError):
    """The disclosure acknowledgement no longer applies to the current file."""


class NtfyDisclosureOutcomeUnknownError(ConfigError):
    """The replacement may have committed but final durability is unknown."""


class NtfyDisclosureWriteError(ConfigError):
    """The disclosure acknowledgement failed before replacement."""


class ConfigAdmissionUnavailableError(ConfigError):
    """A safe atomic configuration snapshot could not be produced."""


class ConfigAdmissionStaleError(ConfigError):
    """An admission token no longer describes the current configuration."""


class _MissingConfigPath(Exception):
    """The configured path does not exist."""


class _UnsafeConfigPath(Exception):
    """The configured path cannot be accessed under the migration policy."""


class _PostReplaceDurabilityError(OSError):
    """Atomic replacement returned, but a later durability step failed."""


class _RenameExchangeUnsupported(OSError):
    """The target platform or filesystem cannot perform rename exchange."""


@dataclass(frozen=True)
class Sender:
    email: str
    name: str | None = None


@dataclass(frozen=True)
class Config:
    path: Path
    timezone: str
    body_char_limit: int
    retention_days: int
    poll_interval_minutes: int
    gmail_credentials_file: Path
    microsoft_credentials_file: Path
    gmail_token_file: Path
    gmail_send_token_file: Path
    monthly_hours_recipient: str | None
    database_file: Path
    model_backend: Literal["loopback", "gateway"]
    model_base_url: str
    model_name: str
    model_api_token_file: Path | None
    model_ca_file: Path | None
    model_require_auth: bool
    model_timeout_seconds: float
    notifications_enabled: bool
    ntfy_topic: str | None
    ntfy_url: str
    ntfy_content_disclosure_acknowledged: bool
    senders: tuple[Sender, ...]
    desktop_initialization_receipt: str | None = None

    @property
    def allowlist(self) -> frozenset[str]:
        return frozenset(
            sender.email
            for sender in self.senders
            if _exact_sender_selector_id_or_none(sender.email) is not None
        )

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass(frozen=True)
class NtfyDisclosureStatus:
    state: Literal[
        "missing",
        "normal_admission",
        "acknowledgement_required",
        "manual_repair_required",
    ]
    expected_revision: str | None = None


@dataclass(frozen=True)
class ConfigAdmissionSnapshot:
    config: Config
    token: dict[str, object]


@dataclass
class _SafeConfigHandle:
    path: Path
    parent_fd: int
    name: str
    file_fd: int
    identity: os.stat_result
    content: bytes

    def close(self) -> None:
        os.close(self.file_fd)
        os.close(self.parent_fd)


@dataclass
class _ConfigMutationSource:
    path: Path
    identity: os.stat_result
    content: bytes
    parent_fd: int | None = None
    name: str | None = None
    parent: _HeldParent | None = None


@dataclass(frozen=True)
class _HeldParent:
    path: Path
    identity: os.stat_result


def _path(value: object, key: str) -> Path:
    if isinstance(value, Path):
        return value.expanduser()
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ConfigError(f"{key} must be a non-empty path")
    return Path(os.path.expandvars(value)).expanduser()


def _runtime_state_root() -> Path:
    if "STATE_DIRECTORY" in os.environ:
        configured = os.environ["STATE_DIRECTORY"]
        if not configured or os.pathsep in configured or not Path(configured).is_absolute():
            raise _UnsafeConfigPath
        return Path(configured)
    configured_state_home = os.environ.get("XDG_STATE_HOME")
    if configured_state_home:
        state_home = Path(configured_state_home).expanduser()
        if not state_home.is_absolute():
            raise _UnsafeConfigPath
    else:
        state_home = Path.home() / ".local" / "state"
    return state_home / "eom-email-watcher"


def normalize_address(value: str) -> str:
    _name, address = parseaddr(value)
    return address.strip().casefold()


def normalize_validated_address(value: str) -> str:
    email = normalize_address(value)
    local, separator, domain = email.rpartition("@")
    if (
        separator != "@"
        or not local
        or not domain
        or "@" in local
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
        or not _valid_domain(domain)
        or any(character.isspace() or not character.isprintable() for character in email)
    ):
        raise ValueError("email address is invalid")
    return email


def exact_sender_selector_id(value: str) -> str:
    selector_id = f"{EXACT_SENDER_SELECTOR_PREFIX}{normalize_validated_address(value)}"
    if len(selector_id.encode("utf-8")) > MAX_ADMISSION_SELECTOR_BYTES:
        raise ValueError(
            "sender email creates an admission selector over 512 UTF-8 bytes"
        )
    return selector_id


def _exact_sender_selector_id_or_none(value: str) -> str | None:
    try:
        return exact_sender_selector_id(value)
    except ValueError:
        return None


def _valid_domain(domain: str) -> bool:
    try:
        ascii_domain = domain.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    return len(ascii_domain) <= 253 and all(
        DOMAIN_LABEL_RE.fullmatch(label) for label in ascii_domain.split(".")
    )


def _valid_network_host(host: str) -> bool:
    try:
        ip_address(host)
    except ValueError:
        return _valid_domain(host)
    return True


def admission_sender_display_name(value: str | None) -> str | None:
    if value is None:
        return None
    encoded = value.encode("utf-8")
    if len(encoded) <= MAX_SENDER_NAME_BYTES:
        return value
    return encoded[:MAX_SENDER_NAME_BYTES].decode("utf-8", errors="ignore")


def _sender(
    email_value: str,
    name_value: str | None,
    *,
    invalid_message: str,
    enforce_name_limit: bool = True,
    enforce_selector_limit: bool = True,
) -> Sender:
    try:
        email = normalize_validated_address(email_value)
    except ValueError as exc:
        raise InvalidSenderError(invalid_message) from exc
    if enforce_selector_limit:
        try:
            exact_sender_selector_id(email)
        except ValueError as exc:
            raise InvalidSenderError(str(exc)) from exc
    if name_value is not None and any(
        character in "\r\n" or not character.isprintable() for character in name_value
    ):
        raise InvalidSenderError("sender name must not contain control characters")
    name = name_value.strip() if name_value and name_value.strip() else None
    if (
        enforce_name_limit
        and name is not None
        and len(name.encode("utf-8")) > MAX_SENDER_NAME_BYTES
    ):
        raise InvalidSenderError(
            f"sender name must be at most {MAX_SENDER_NAME_BYTES} UTF-8 bytes"
        )
    return Sender(email=email, name=name)


def validate_model_base_url(value: object) -> str:
    base_url = str(value).rstrip("/")
    if any(character.isspace() or not character.isprintable() for character in base_url):
        raise ConfigError("model_base_url must not contain whitespace or control characters")
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError as exc:
        raise ConfigError(
            "model_base_url must use http with an explicit port on localhost or "
            "127.0.0.1; email bodies may not leave this machine"
        ) from exc

    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.username is not None
        or parsed.password is not None
        or port is None
        or not 1 <= port <= 65_535
    ):
        raise ConfigError(
            "model_base_url must use http with an explicit port on localhost or "
            "127.0.0.1; email bodies may not leave this machine"
        )
    return base_url


def validate_gateway_base_url(value: object) -> str:
    base_url = str(value).rstrip("/")
    if any(character.isspace() or not character.isprintable() for character in base_url):
        raise ConfigError("model_base_url must not contain whitespace or control characters")
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
        httpx.URL(base_url)
    except (ValueError, httpx.InvalidURL) as exc:
        raise ConfigError("gateway model_base_url must be an HTTPS origin") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or not _valid_network_host(parsed.hostname)
        or parsed.username is not None
        or parsed.password is not None
        or "?" in base_url
        or "#" in base_url
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or port is not None
        and not 1 <= port <= 65_535
    ):
        raise ConfigError("gateway model_base_url must be an HTTPS origin")
    return base_url


def _integer_setting(data: dict[str, object], key: str, default: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} must be an integer")
    return value


def _float_setting(data: dict[str, object], key: str, default: float) -> float:
    try:
        return float(data.get(key, default))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ConfigError(f"{key} must be numeric") from exc


def _load_config_bytes(content: bytes, config_path: Path) -> Config:
    try:
        state_root = _runtime_state_root()
    except _UnsafeConfigPath as exc:
        raise ConfigError("Configuration is unavailable or requires manual repair") from exc
    try:
        text = content.decode("utf-8")
        data = tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Invalid TOML in {config_path}: {exc}") from exc

    timezone = data.get("timezone", "America/Chicago")
    if not isinstance(timezone, str):
        raise ConfigError("timezone must be a string")
    try:
        ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ConfigError(f"Unknown timezone: {timezone}") from exc

    raw_senders = data.get("senders", [])
    if not isinstance(raw_senders, list):
        raise ConfigError("senders must be a list of tables")
    senders: list[Sender] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_senders, start=1):
        if not isinstance(raw, dict):
            raise ConfigError(f"senders entry {index} must be a table")
        raw_email = raw.get("email", "")
        if not isinstance(raw_email, str):
            raise ConfigError(f"senders entry {index} has an invalid email")
        name = raw.get("name")
        if name is not None and not isinstance(name, str):
            raise ConfigError(f"senders entry {index} name must be a string")
        try:
            sender = _sender(
                raw_email,
                name,
                invalid_message=f"senders entry {index} has an invalid email",
                enforce_name_limit=False,
                enforce_selector_limit=False,
            )
        except InvalidSenderError as exc:
            raise ConfigError(str(exc)) from exc
        if sender.email in seen:
            raise ConfigError(f"Duplicate sender: {sender.email}")
        seen.add(sender.email)
        senders.append(sender)

    body_limit = _integer_setting(data, "body_char_limit", 20_000)
    retention = _integer_setting(data, "retention_days", DEFAULT_RETENTION_DAYS)
    poll_interval = _integer_setting(data, "poll_interval_minutes", DEFAULT_POLL_INTERVAL_MINUTES)
    timeout = _float_setting(data, "model_timeout_seconds", 60)
    if not 1_000 <= body_limit <= 100_000:
        raise ConfigError("body_char_limit must be between 1000 and 100000")
    if not MIN_RETENTION_DAYS <= retention <= MAX_RETENTION_DAYS:
        raise ConfigError(
            f"retention_days must be between {MIN_RETENTION_DAYS} and {MAX_RETENTION_DAYS}"
        )
    if not 1 <= poll_interval <= 1440:
        raise ConfigError("poll_interval_minutes must be between 1 and 1440")
    if not 1 <= timeout <= 300:
        raise ConfigError("model_timeout_seconds must be between 1 and 300")

    backend = data.get("model_backend", "loopback")
    if not isinstance(backend, str) or backend not in {"loopback", "gateway"}:
        raise ConfigError("model_backend must be loopback or gateway")
    raw_base_url = data.get("model_base_url", "http://127.0.0.1:1234/v1")
    base_url = (
        validate_gateway_base_url(raw_base_url)
        if backend == "gateway"
        else validate_model_base_url(raw_base_url)
    )
    model_name = data.get("model_name")
    if backend == "loopback":
        if not isinstance(model_name, str) or not model_name.strip():
            raise ConfigError("model_name must be set")
        normalized_model_name = model_name.strip()
    else:
        normalized_model_name = GATEWAY_MODEL_LABEL
    raw_require_auth = data.get("model_require_auth", True)
    require_auth = bool(raw_require_auth)
    if backend == "gateway" and raw_require_auth is not True:
        raise ConfigError("gateway model_require_auth must be true")
    raw_token_file = data.get("model_api_token_file")
    if raw_token_file is None:
        token_file = state_root / "lmstudio-api-token" if require_auth else None
    else:
        token_file = _path(raw_token_file, "model_api_token_file") if raw_token_file else None
    if require_auth and token_file is None:
        raise ConfigError("model_api_token_file is required when model_require_auth is true")
    raw_ca_file = data.get("model_ca_file")
    ca_file = _path(raw_ca_file, "model_ca_file") if raw_ca_file else None
    if backend == "gateway" and ca_file is None:
        raise ConfigError("model_ca_file is required when model_backend is gateway")

    raw_ntfy_topic = data.get("ntfy_topic")
    ntfy_topic = str(raw_ntfy_topic).strip() if raw_ntfy_topic else None
    if ntfy_topic and not NTFY_TOPIC_RE.match(ntfy_topic):
        raise ConfigError(
            "ntfy_topic must be 20-64 characters of letters, digits, - or _ "
            "(the topic is the sole secret protecting this channel -- keep it high-entropy)"
        )
    ntfy_url = str(data.get("ntfy_url", "https://ntfy.sh")).rstrip("/")
    if not ntfy_url.startswith("https://"):
        raise ConfigError("ntfy_url must use https://")
    raw_ntfy_disclosure = data.get("ntfy_content_disclosure_acknowledged", False)
    if not isinstance(raw_ntfy_disclosure, bool):
        raise ConfigError("ntfy_content_disclosure_acknowledged must be true or false")
    if ntfy_topic and raw_ntfy_disclosure is not True:
        raise ConfigError(
            "ntfy_topic requires ntfy_content_disclosure_acknowledged = true "
            "because ntfy receives email-derived content"
        )
    raw_initialization_receipt = data.get("desktop_initialization_receipt")
    if raw_initialization_receipt is not None and (
        not isinstance(raw_initialization_receipt, str)
        or INITIALIZATION_RECEIPT_RE.fullmatch(raw_initialization_receipt) is None
    ):
        raise ConfigError("desktop_initialization_receipt is invalid")

    return Config(
        path=config_path,
        timezone=timezone,
        body_char_limit=body_limit,
        retention_days=retention,
        poll_interval_minutes=poll_interval,
        gmail_credentials_file=_path(
            data.get("gmail_credentials_file", state_root / "credentials.json"),
            "gmail_credentials_file",
        ),
        microsoft_credentials_file=_path(
            data.get(
                "microsoft_credentials_file",
                state_root / "microsoft-oauth-client.json",
            ),
            "microsoft_credentials_file",
        ),
        gmail_token_file=_path(
            data.get("gmail_token_file", state_root / "token.json"),
            "gmail_token_file",
        ),
        gmail_send_token_file=_path(
            data.get("gmail_send_token_file", state_root / "send-token.json"),
            "gmail_send_token_file",
        ),
        monthly_hours_recipient=(
            normalize_address(str(data["monthly_hours_recipient"]))
            if data.get("monthly_hours_recipient")
            else None
        ),
        database_file=_path(
            data.get("database_file", state_root / "watcher.sqlite3"),
            "database_file",
        ),
        model_backend=backend,
        model_base_url=base_url,
        model_name=normalized_model_name,
        model_api_token_file=token_file,
        model_ca_file=ca_file,
        model_require_auth=require_auth,
        model_timeout_seconds=timeout,
        notifications_enabled=bool(data.get("notifications_enabled", True)),
        ntfy_topic=ntfy_topic,
        ntfy_url=ntfy_url,
        ntfy_content_disclosure_acknowledged=raw_ntfy_disclosure,
        senders=tuple(senders),
        desktop_initialization_receipt=raw_initialization_receipt,
    )


def load_config(path: Path | None = None) -> Config:
    return _load_runtime_config(path or DEFAULT_CONFIG)


def _absolute_lexical_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _file_open_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def _open_directory_nofollow(path: Path) -> int:
    absolute = _absolute_lexical_path(path)
    if not absolute.is_absolute():
        raise _UnsafeConfigPath
    current_fd = os.open("/", _directory_open_flags())
    try:
        for component in absolute.parts[1:]:
            try:
                next_fd = os.open(component, _directory_open_flags(), dir_fd=current_fd)
            except FileNotFoundError as exc:
                raise _MissingConfigPath from exc
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EACCES, errno.EPERM}:
                    raise _UnsafeConfigPath from exc
                raise
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


def _open_or_create_directory_nofollow(path: Path) -> int:
    absolute = _absolute_lexical_path(path)
    if not absolute.is_absolute():
        raise _UnsafeConfigPath
    current_fd = os.open("/", _directory_open_flags())
    try:
        for component in absolute.parts[1:]:
            try:
                next_fd = os.open(component, _directory_open_flags(), dir_fd=current_fd)
            except FileNotFoundError:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise _UnsafeConfigPath from exc
                try:
                    next_fd = os.open(component, _directory_open_flags(), dir_fd=current_fd)
                except OSError as exc:
                    raise _UnsafeConfigPath from exc
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EACCES, errno.EPERM}:
                    raise _UnsafeConfigPath from exc
                raise
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


def _open_safe_parent(path: Path) -> tuple[Path, int, str]:
    if os.name != "posix" or not hasattr(os, "getuid"):
        raise _UnsafeConfigPath
    absolute = _absolute_lexical_path(path)
    if not absolute.name:
        raise _UnsafeConfigPath
    current_fd = _open_directory_nofollow(absolute.parent)
    try:
        parent_stat = os.fstat(current_fd)
        if (
            not stat.S_ISDIR(parent_stat.st_mode)
            or parent_stat.st_uid != os.geteuid()
            or stat.S_IMODE(parent_stat.st_mode) != 0o700
        ):
            raise _UnsafeConfigPath
        return absolute, current_fd, absolute.name
    except Exception:
        os.close(current_fd)
        raise


def _open_or_create_safe_parent(path: Path) -> tuple[Path, int, str]:
    if os.name != "posix" or not hasattr(os, "getuid"):
        raise _UnsafeConfigPath
    absolute = _absolute_lexical_path(path)
    if not absolute.name:
        raise _UnsafeConfigPath
    current_fd = _open_or_create_directory_nofollow(absolute.parent)
    try:
        parent_stat = os.fstat(current_fd)
        if (
            not stat.S_ISDIR(parent_stat.st_mode)
            or parent_stat.st_uid != os.geteuid()
            or stat.S_IMODE(parent_stat.st_mode) != 0o700
        ):
            raise _UnsafeConfigPath
        return absolute, current_fd, absolute.name
    except Exception:
        os.close(current_fd)
        raise


def _config_serialization_lock_path() -> Path:
    return _runtime_state_root() / "config-serialization.lock"


def _safe_lock_file_stat(file_stat: os.stat_result) -> bool:
    mode = stat.S_IMODE(file_stat.st_mode)
    return (
        stat.S_ISREG(file_stat.st_mode)
        and file_stat.st_nlink == 1
        and file_stat.st_uid == os.geteuid()
        and mode & ~0o600 == 0
    )


def _config_serialization_lock_probe(_stage: str) -> None:
    """Test seam for state-lock path and inode replacement boundaries."""


@contextmanager
def _config_serialization_lock():
    """Serialize every config snapshot and mutation outside the read-only config tree."""

    lock_path = _config_serialization_lock_path()
    if os.name != "posix" or not hasattr(os, "geteuid"):
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock = FileLock(str(lock_path))
            lock.acquire()
        except OSError as exc:
            raise ConfigError("Configuration is unavailable or requires manual repair") from exc
        body_failed = False
        try:
            yield
        except BaseException:
            body_failed = True
            raise
        finally:
            try:
                lock.release()
            except Exception as exc:
                if body_failed:
                    logger.warning("Configuration lock release failed after operation failure")
                else:
                    raise ConfigError(
                        "Configuration is unavailable or requires manual repair"
                    ) from exc
        return

    parent_fd: int | None = None
    lock_fd: int | None = None
    locked = False
    try:
        absolute, parent_fd, name = _open_or_create_safe_parent(lock_path)
        held_parent = _validate_held_parent(
            parent_fd,
            parent_path=absolute.parent,
        )
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            lock_fd = os.open(name, flags | os.O_EXCL, 0o600, dir_fd=parent_fd)
        except FileExistsError:
            lock_fd = os.open(name, flags, dir_fd=parent_fd)

        def validate_lock() -> None:
            opened = os.fstat(lock_fd)
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                not _safe_lock_file_stat(opened)
                or not _safe_lock_file_stat(current)
                or _safe_file_identity(opened) != _safe_file_identity(current)
            ):
                raise _UnsafeConfigPath

        validate_lock()
        _validate_held_parent(parent_fd, held_parent)
        import fcntl

        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        locked = True
        _config_serialization_lock_probe("after_acquire")
        validate_lock()
        _validate_held_parent(parent_fd, held_parent)
    except (OSError, _MissingConfigPath, _UnsafeConfigPath) as exc:
        if locked and lock_fd is not None:
            with suppress(OSError):
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        if lock_fd is not None:
            os.close(lock_fd)
        if parent_fd is not None:
            os.close(parent_fd)
        raise ConfigError("Configuration is unavailable or requires manual repair") from exc

    body_failed = False
    try:
        yield
    except BaseException:
        body_failed = True
        raise
    finally:
        validation_error: Exception | None = None
        if not body_failed:
            try:
                _config_serialization_lock_probe("before_release")
                validate_lock()
                _validate_held_parent(parent_fd, held_parent)
            except (OSError, _MissingConfigPath, _UnsafeConfigPath) as exc:
                validation_error = exc
        if locked and lock_fd is not None:
            with suppress(OSError):
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        if lock_fd is not None:
            os.close(lock_fd)
        if parent_fd is not None:
            os.close(parent_fd)
        if validation_error is not None:
            raise ConfigError(
                "Configuration is unavailable or requires manual repair"
            ) from validation_error


def _read_fd_bytes(file_fd: int) -> bytes:
    os.lseek(file_fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while chunk := os.read(file_fd, 64 * 1024):
        chunks.append(chunk)
    return b"".join(chunks)


def _safe_file_identity(file_stat: os.stat_result) -> tuple[int, int]:
    return file_stat.st_dev, file_stat.st_ino


def _safe_file_version(
    file_stat: os.stat_result,
) -> tuple[int, int, int, int, int, int, int, int, int]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_mode,
        file_stat.st_nlink,
        file_stat.st_uid,
        file_stat.st_gid,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )


def _is_safe_file_stat(file_stat: os.stat_result) -> bool:
    return (
        stat.S_ISREG(file_stat.st_mode)
        and file_stat.st_nlink == 1
        and file_stat.st_uid == os.geteuid()
        and stat.S_IMODE(file_stat.st_mode) == 0o600
    )


def _read_safe_file_at(parent_fd: int, name: str) -> tuple[int, os.stat_result, bytes]:
    try:
        inspected = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise _MissingConfigPath from exc
    except OSError as exc:
        raise _UnsafeConfigPath from exc
    if not _is_safe_file_stat(inspected):
        raise _UnsafeConfigPath
    try:
        file_fd = os.open(name, _file_open_flags(), dir_fd=parent_fd)
    except OSError as exc:
        raise _UnsafeConfigPath from exc
    try:
        opened = os.fstat(file_fd)
        if not _is_safe_file_stat(opened) or _safe_file_version(opened) != _safe_file_version(
            inspected
        ):
            raise _UnsafeConfigPath
        content = _read_fd_bytes(file_fd)
        completed = os.fstat(file_fd)
        if not _is_safe_file_stat(completed) or _safe_file_version(completed) != _safe_file_version(
            opened
        ):
            raise _UnsafeConfigPath
        return file_fd, completed, content
    except Exception:
        os.close(file_fd)
        raise


def _config_mutation_probe(_stage: str, _path: Path) -> None:
    """Test seam for replacement immediately before config publication."""


@contextmanager
def _config_mutation_source(path: Path):
    """Hold one safely read config source while its mutation is prepared."""

    absolute = _absolute_lexical_path(path)
    if os.name != "posix":
        try:
            read_path, identity, content = _read_admission_config_under_lock(absolute)
        except _MissingConfigPath as exc:
            raise ConfigError(
                f"Configuration not found: {path}. Copy config.example.toml and edit it."
            ) from exc
        except (_UnsafeConfigPath, OSError) as exc:
            raise ConfigError("Configuration is unavailable or requires manual repair") from exc
        yield _ConfigMutationSource(read_path, identity, content)
        return

    parent_fd: int | None = None
    file_fd: int | None = None
    try:
        absolute, parent_fd, name = _open_safe_parent(absolute)
        recovery = _recover_ntfy_transaction_at(
            parent_fd,
            name,
            parent_path=absolute.parent,
        )
        if recovery in {"unsupported", "manual_artifact"}:
            raise _UnsafeConfigPath
        parent = _validate_held_parent(parent_fd, parent_path=absolute.parent)
        file_fd, identity, content = _read_safe_file_at(parent_fd, name)
        _validate_held_parent(parent_fd, parent)
        source = _ConfigMutationSource(
            absolute,
            identity,
            content,
            parent_fd=parent_fd,
            name=name,
            parent=parent,
        )
    except _MissingConfigPath as exc:
        if file_fd is not None:
            os.close(file_fd)
        if parent_fd is not None:
            os.close(parent_fd)
        raise ConfigError(
            f"Configuration not found: {path}. Copy config.example.toml and edit it."
        ) from exc
    except (_UnsafeConfigPath, OSError) as exc:
        if file_fd is not None:
            os.close(file_fd)
        if parent_fd is not None:
            os.close(parent_fd)
        raise ConfigError("Configuration is unavailable or requires manual repair") from exc
    try:
        yield source
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def _verify_config_mutation_source(source: _ConfigMutationSource) -> None:
    _config_mutation_probe("before_replace", source.path)
    try:
        if os.name == "posix":
            assert source.parent_fd is not None
            assert source.name is not None
            assert source.parent is not None
            _validate_held_parent(source.parent_fd, source.parent)
            current_fd, current_identity, current_content = _read_safe_file_at(
                source.parent_fd,
                source.name,
            )
            try:
                _validate_held_parent(source.parent_fd, source.parent)
            finally:
                os.close(current_fd)
        else:
            current_identity, current_content = _read_safe_windows_file(source.path)
    except (_MissingConfigPath, _UnsafeConfigPath, OSError) as exc:
        raise ConfigAdmissionStaleError("Configuration admission snapshot changed") from exc
    if _safe_file_version(current_identity) != _safe_file_version(source.identity) or _revision(
        current_content
    ) != _revision(source.content):
        raise ConfigAdmissionStaleError("Configuration admission snapshot changed")


def _open_safe_config(path: Path) -> _SafeConfigHandle:
    absolute, parent_fd, name = _open_safe_parent(path)
    try:
        file_fd, identity, content = _read_safe_file_at(parent_fd, name)
    except Exception:
        os.close(parent_fd)
        raise
    return _SafeConfigHandle(
        path=absolute,
        parent_fd=parent_fd,
        name=name,
        file_fd=file_fd,
        identity=identity,
        content=content,
    )


def _non_posix_admission_file_is_safe(file_stat: os.stat_result) -> bool:
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    file_attributes = getattr(file_stat, "st_file_attributes", 0)
    return (
        stat.S_ISREG(file_stat.st_mode)
        and file_stat.st_nlink == 1
        and not file_attributes & reparse_attribute
    )


def _read_recovered_posix_config_at(
    absolute: Path,
    parent_fd: int,
    name: str,
) -> tuple[Path, os.stat_result, bytes]:
    file_fd: int | None = None
    try:
        recovery = _recover_ntfy_transaction_at(
            parent_fd,
            name,
            parent_path=absolute.parent,
        )
        if recovery in {"unsupported", "manual_artifact"}:
            raise _UnsafeConfigPath
        parent = _validate_held_parent(
            parent_fd,
            parent_path=absolute.parent,
        )
        file_fd, identity, content = _read_safe_file_at(parent_fd, name)
        _validate_held_parent(parent_fd, parent)
        current = os.stat(
            name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if _safe_file_version(current) != _safe_file_version(identity):
            raise _UnsafeConfigPath
        return absolute, identity, content
    finally:
        if file_fd is not None:
            os.close(file_fd)


def _read_admission_config(
    path: Path,
) -> tuple[Path, os.stat_result, bytes]:
    absolute = _absolute_lexical_path(path)
    if os.name == "posix":
        absolute, parent_fd, name = _open_safe_parent(absolute)
        try:
            with _config_serialization_lock():
                return _read_recovered_posix_config_at(
                    absolute,
                    parent_fd,
                    name,
                )
        finally:
            os.close(parent_fd)

    try:
        inspected = os.stat(absolute, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise _MissingConfigPath from exc
    except OSError as exc:
        raise _UnsafeConfigPath from exc
    if not _non_posix_admission_file_is_safe(inspected):
        raise _UnsafeConfigPath
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOINHERIT", 0)
    )
    try:
        file_fd = os.open(absolute, flags)
    except OSError as exc:
        raise _UnsafeConfigPath from exc
    try:
        opened = os.fstat(file_fd)
        if (
            not _non_posix_admission_file_is_safe(opened)
            or _safe_file_version(opened) != _safe_file_version(inspected)
        ):
            raise _UnsafeConfigPath
        content = _read_fd_bytes(file_fd)
        completed = os.fstat(file_fd)
        if (
            not _non_posix_admission_file_is_safe(completed)
            or _safe_file_version(completed) != _safe_file_version(opened)
        ):
            raise _UnsafeConfigPath
        current = os.stat(absolute, follow_symlinks=False)
        if _safe_file_version(current) != _safe_file_version(completed):
            raise _UnsafeConfigPath
        return absolute, completed, content
    except OSError as exc:
        raise _UnsafeConfigPath from exc
    finally:
        os.close(file_fd)


def _read_admission_config_under_lock(
    path: Path,
) -> tuple[Path, os.stat_result, bytes]:
    if os.name != "posix":
        recovery = _recover_windows_publication(path)
        if recovery == "manual_target":
            raise _UnsafeConfigPath
        return _read_admission_config(path)
    absolute, parent_fd, name = _open_safe_parent(path)
    try:
        return _read_recovered_posix_config_at(
            absolute,
            parent_fd,
            name,
        )
    finally:
        os.close(parent_fd)


def _load_runtime_config_from_reader(
    config_path: Path,
    reader: Callable[[], tuple[Path, os.stat_result, bytes]],
) -> Config:
    try:
        absolute, _file_stat, content = reader()
    except _MissingConfigPath as exc:
        raise ConfigError(
            f"Configuration not found: {config_path}. Copy config.example.toml and edit it."
        ) from exc
    except (OSError, _UnsafeConfigPath) as exc:
        raise ConfigError(
            "Configuration is unavailable or requires manual repair"
        ) from exc
    return _load_config_bytes(content, absolute)


def _load_runtime_config(path: Path, *, lock_held: bool = False) -> Config:
    config_path = path.expanduser()
    absolute = _absolute_lexical_path(config_path)
    if lock_held:
        return _load_runtime_config_from_reader(
            config_path,
            lambda: _read_admission_config_under_lock(absolute),
        )
    if os.name == "posix":
        try:
            absolute, parent_fd, name = _open_safe_parent(absolute)
        except _MissingConfigPath as exc:
            raise ConfigError(
                f"Configuration not found: {config_path}. Copy config.example.toml and edit it."
            ) from exc
        except (OSError, _UnsafeConfigPath) as exc:
            raise ConfigError(
                "Configuration is unavailable or requires manual repair"
            ) from exc
        try:
            with _config_serialization_lock():
                return _load_runtime_config_from_reader(
                    config_path,
                    lambda: _read_recovered_posix_config_at(
                        absolute,
                        parent_fd,
                        name,
                    ),
                )
        except OSError as exc:
            raise ConfigError(
                "Configuration is unavailable or requires manual repair"
            ) from exc
        finally:
            os.close(parent_fd)
    try:
        with _config_serialization_lock():
            return _load_runtime_config_from_reader(
                config_path,
                lambda: _read_admission_config_under_lock(absolute),
            )
    except OSError as exc:
        raise ConfigError(
            "Configuration is unavailable or requires manual repair"
        ) from exc


def _config_admission_token(
    file_stat: os.stat_result, content: bytes
) -> dict[str, object]:
    revision = _revision(content)
    identity_payload = json.dumps(
        {
            "ctime_ns": file_stat.st_ctime_ns,
            "device": file_stat.st_dev,
            "gid": file_stat.st_gid,
            "inode": file_stat.st_ino,
            "links": file_stat.st_nlink,
            "mode": file_stat.st_mode,
            "mtime_ns": file_stat.st_mtime_ns,
            "revision": revision,
            "size": file_stat.st_size,
            "uid": file_stat.st_uid,
            "version": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return {
        "version": 1,
        "revision": revision,
        "identity": _revision(identity_payload),
    }


def config_admission_snapshot(path: Path) -> ConfigAdmissionSnapshot:
    try:
        absolute, file_stat, content = _read_admission_config(path)
        config = _load_config_bytes(content, absolute)
    except (ConfigError, OSError, _MissingConfigPath, _UnsafeConfigPath) as exc:
        raise ConfigAdmissionUnavailableError(
            "Configuration admission snapshot is unavailable"
        ) from exc
    return ConfigAdmissionSnapshot(
        config=config,
        token=_config_admission_token(file_stat, content),
    )


def admitted_config(path: Path, expected_token: Mapping[str, object]) -> Config:
    snapshot = config_admission_snapshot(path)
    if dict(expected_token) != snapshot.token:
        raise ConfigAdmissionStaleError("Configuration admission snapshot changed")
    return snapshot.config


def _revision(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _statement_assignment_offset(statement: bytes) -> int | None:
    quote: int | None = None
    escaped = False
    for index, value in enumerate(statement):
        if quote is not None:
            if quote == ord('"') and escaped:
                escaped = False
            elif quote == ord('"') and value == ord("\\"):
                escaped = True
            elif value == quote:
                quote = None
            continue
        if value in {ord('"'), ord("'")}:
            quote = value
        elif value == ord("="):
            return index
        elif value == ord("#"):
            return None
    return None


def _simple_key_is(statement_key: bytes, expected: str) -> bool:
    try:
        parsed = tomllib.loads(statement_key.decode("utf-8") + " = false")
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return False
    return parsed == {expected: False}


def _root_boolean_token_spans(content: bytes, key: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    statement_start = 0
    index = 0
    quote: int | None = None
    multiline = False
    escaped = False
    comment = False
    nesting = 0
    root = True
    while index < len(content):
        value = content[index]
        if comment:
            if value == ord("\n"):
                comment = False
            else:
                index += 1
                continue
        elif quote is not None:
            marker = bytes([quote]) * 3
            delimiter_is_escaped = quote == ord('"') and escaped
            if (
                multiline
                and not delimiter_is_escaped
                and content[index : index + 3] == marker
            ):
                quote = None
                multiline = False
                index += 3
                continue
            if not multiline and value == quote and not escaped:
                quote = None
            if quote == ord('"'):
                if escaped:
                    escaped = False
                elif value == ord("\\"):
                    escaped = True
            index += 1
            continue
        elif value == ord("#"):
            comment = True
        elif value in {ord('"'), ord("'")}:
            if content[index : index + 3] == bytes([value]) * 3:
                quote = value
                multiline = True
                index += 3
                continue
            quote = value
        elif value in {ord("["), ord("{")}:
            nesting += 1
        elif value in {ord("]"), ord("}")} and nesting:
            nesting -= 1

        if value == ord("\n") and quote is None and nesting == 0:
            statement = content[statement_start : index + 1]
            stripped = statement.lstrip(b" \t\r\n")
            if stripped.startswith(b"["):
                root = False
            elif root:
                assignment = _statement_assignment_offset(statement)
                if assignment is not None and _simple_key_is(statement[:assignment], key):
                    value_start = assignment + 1
                    while value_start < len(statement) and statement[value_start] in b" \t":
                        value_start += 1
                    if statement[value_start : value_start + 5] == b"false":
                        spans.append(
                            (statement_start + value_start, statement_start + value_start + 5)
                        )
            statement_start = index + 1
        index += 1

    if statement_start < len(content):
        statement = content[statement_start:]
        stripped = statement.lstrip(b" \t\r\n")
        if not stripped.startswith(b"[") and root:
            assignment = _statement_assignment_offset(statement)
            if assignment is not None and _simple_key_is(statement[:assignment], key):
                value_start = assignment + 1
                while value_start < len(statement) and statement[value_start] in b" \t":
                    value_start += 1
                if statement[value_start : value_start + 5] == b"false":
                    spans.append((statement_start + value_start, statement_start + value_start + 5))
    return spans


def _acknowledged_candidate(content: bytes, data: dict[str, object]) -> bytes | None:
    key = "ntfy_content_disclosure_acknowledged"
    if key not in data:
        first_newline = content.find(b"\n")
        newline = (
            b"\r\n"
            if first_newline > 0 and content[first_newline - 1 : first_newline] == b"\r"
            else b"\n"
        )
        return key.encode() + b" = true" + newline + content
    if data[key] is not False:
        return None
    spans = _root_boolean_token_spans(content, key)
    if len(spans) != 1:
        return None
    start, end = spans[0]
    candidate = content[:start] + b"true" + content[end:]
    return candidate


def _classify_ntfy_disclosure(
    content: bytes, config_path: Path
) -> tuple[NtfyDisclosureStatus, bytes | None]:
    try:
        data = tomllib.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return NtfyDisclosureStatus("manual_repair_required"), None

    topic_present = "ntfy_topic" in data
    raw_topic = data.get("ntfy_topic")
    acknowledgement = data.get("ntfy_content_disclosure_acknowledged", False)
    if topic_present and (
        not isinstance(raw_topic, str) or NTFY_TOPIC_RE.fullmatch(raw_topic.strip()) is None
    ):
        return NtfyDisclosureStatus("manual_repair_required"), None
    if not isinstance(acknowledgement, bool):
        return NtfyDisclosureStatus("manual_repair_required"), None

    if topic_present and acknowledgement is False:
        candidate = _acknowledged_candidate(content, data)
        if candidate is None:
            return NtfyDisclosureStatus("manual_repair_required"), None
        try:
            validated = _load_config_bytes(candidate, config_path)
        except ConfigError:
            return NtfyDisclosureStatus("manual_repair_required"), None
        if validated.ntfy_content_disclosure_acknowledged is not True:
            return NtfyDisclosureStatus("manual_repair_required"), None
        return NtfyDisclosureStatus("acknowledgement_required", _revision(content)), candidate

    try:
        _load_config_bytes(content, config_path)
    except ConfigError:
        return NtfyDisclosureStatus("manual_repair_required"), None
    return NtfyDisclosureStatus("normal_admission"), None


def ntfy_disclosure_status(path: Path) -> NtfyDisclosureStatus:
    if os.name != "posix":
        config_path = path.expanduser()
        try:
            content = config_path.read_bytes()
        except FileNotFoundError:
            return NtfyDisclosureStatus("missing")
        except OSError:
            return NtfyDisclosureStatus("manual_repair_required")
        status, _candidate = _classify_ntfy_disclosure(
            content, config_path
        )
        if status.state == "acknowledgement_required":
            return NtfyDisclosureStatus("manual_repair_required")
        return status

    try:
        absolute, parent_fd, name = _open_safe_parent(path)
    except _MissingConfigPath:
        return NtfyDisclosureStatus("missing")
    except (OSError, _UnsafeConfigPath):
        return NtfyDisclosureStatus("manual_repair_required")

    file_fd: int | None = None
    try:
        with _config_serialization_lock():
            recovery = _recover_ntfy_transaction_at(
                parent_fd,
                name,
                parent_path=absolute.parent,
            )
            if recovery in {"unsupported", "manual_artifact"}:
                return NtfyDisclosureStatus("manual_repair_required")
            try:
                file_fd, _identity, content = _read_safe_file_at(
                    parent_fd, name
                )
            except _MissingConfigPath:
                return NtfyDisclosureStatus("missing")
            except (OSError, _UnsafeConfigPath):
                return NtfyDisclosureStatus("manual_repair_required")
            status, _candidate = _classify_ntfy_disclosure(
                content, absolute
            )
            if (
                status.state == "acknowledgement_required"
                and (
                    not _rename_exchange_available()
                    or not _unnamed_candidate_available()
                )
            ):
                return NtfyDisclosureStatus("manual_repair_required")
            return status
    except Exception:
        return NtfyDisclosureStatus("manual_repair_required")
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(parent_fd)


def _rename_exchange_function():
    if not sys.platform.startswith("linux"):
        return None
    try:
        library = ctypes.CDLL(None, use_errno=True)
    except OSError:
        return None
    return getattr(library, "renameat2", None)


def _rename_exchange_available() -> bool:
    return _rename_exchange_function() is not None


def _linkat_function():
    if not sys.platform.startswith("linux"):
        return None
    try:
        library = ctypes.CDLL(None, use_errno=True)
    except OSError:
        return None
    return getattr(library, "linkat", None)


def _unnamed_candidate_available() -> bool:
    return (
        sys.platform.startswith("linux")
        and hasattr(os, "O_TMPFILE")
        and _linkat_function() is not None
    )


def _open_unnamed_candidate_at(parent_fd: int) -> int:
    return os.open(
        ".",
        os.O_RDWR
        | os.O_TMPFILE
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=parent_fd,
    )


def _link_unnamed_candidate_at(
    candidate_fd: int, parent_fd: int, candidate_name: str
) -> None:
    linkat = _linkat_function()
    if linkat is None:
        raise OSError(errno.ENOSYS, "linkat with AT_EMPTY_PATH is unavailable")
    linkat.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
    ]
    linkat.restype = ctypes.c_int
    result = linkat(
        candidate_fd,
        b"",
        parent_fd,
        os.fsencode(candidate_name),
        0x1000,  # AT_EMPTY_PATH
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _rename_exchange_at(parent_fd: int, first: str, second: str) -> None:
    renameat2 = _rename_exchange_function()
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "atomic rename exchange is unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        parent_fd,
        os.fsencode(first),
        parent_fd,
        os.fsencode(second),
        2,  # RENAME_EXCHANGE
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _transaction_probe(_stage: str) -> None:
    """Test seam for process-death and filesystem-boundary probes."""


def _validate_held_parent(
    parent_fd: int,
    expected: _HeldParent | None = None,
    *,
    parent_path: Path | None = None,
    require_private: bool = True,
    require_current_path: bool = True,
) -> _HeldParent:
    current = os.fstat(parent_fd)
    if expected is None:
        if parent_path is None:
            raise _UnsafeConfigPath
        expected = _HeldParent(_absolute_lexical_path(parent_path), current)
    if (
        not stat.S_ISDIR(current.st_mode)
        or current.st_uid != os.geteuid()
        or _safe_file_identity(current) != _safe_file_identity(expected.identity)
        or current.st_uid != expected.identity.st_uid
        or require_private
        and stat.S_IMODE(current.st_mode) != 0o700
    ):
        raise _UnsafeConfigPath
    if require_current_path:
        try:
            reopened_fd = _open_directory_nofollow(expected.path)
        except (OSError, _MissingConfigPath, _UnsafeConfigPath) as exc:
            raise _UnsafeConfigPath from exc
        try:
            reopened = os.fstat(reopened_fd)
        finally:
            os.close(reopened_fd)
        if (
            not stat.S_ISDIR(reopened.st_mode)
            or reopened.st_uid != os.geteuid()
            or _safe_file_identity(reopened) != _safe_file_identity(expected.identity)
            or require_private
            and stat.S_IMODE(reopened.st_mode) != 0o700
        ):
            raise _UnsafeConfigPath
    return expected


def _transaction_boundary(
    stage: str,
    parent_fd: int,
    expected_parent: _HeldParent,
    *,
    require_private: bool = True,
    require_current_path: bool = True,
) -> None:
    _transaction_probe(stage)
    _validate_held_parent(
        parent_fd,
        expected_parent,
        require_private=require_private,
        require_current_path=require_current_path,
    )


_NTFY_TRANSACTION_VERSION = 3
_NTFY_DISPOSITION_VERSION = 1
_NTFY_UNSUPPORTED_VERSION = 2
_NTFY_TRANSACTION_SUFFIX = ".ntfy-disclosure-transaction"
_NTFY_CANDIDATE_SUFFIX = ".ntfy-disclosure-candidate"
_NTFY_DISPOSITION_SUFFIX = ".ntfy-disclosure-disposition"
_NTFY_UNSUPPORTED_SUFFIX = ".ntfy-disclosure-unsupported"
_FINGERPRINT_KEYS = frozenset(
    {
        "device",
        "inode",
        "mode",
        "links",
        "uid",
        "gid",
        "size",
        "mtime_ns",
        "revision",
    }
)
_CANDIDATE_DESCRIPTOR_KEYS = frozenset(
    {
        "device",
        "inode",
        "mode",
        "uid",
        "gid",
        "size",
        "mtime_ns",
        "revision",
    }
)


def _transaction_names(name: str) -> tuple[str, str, str]:
    return (
        f".{name}{_NTFY_TRANSACTION_SUFFIX}",
        f".{name}{_NTFY_CANDIDATE_SUFFIX}",
        f".{name}{_NTFY_UNSUPPORTED_SUFFIX}",
    )


def _transaction_disposition_name(name: str) -> str:
    return f".{name}{_NTFY_DISPOSITION_SUFFIX}"


def _file_fingerprint(file_stat: os.stat_result, content: bytes) -> dict[str, object]:
    return {
        "device": file_stat.st_dev,
        "inode": file_stat.st_ino,
        "mode": file_stat.st_mode,
        "links": file_stat.st_nlink,
        "uid": file_stat.st_uid,
        "gid": file_stat.st_gid,
        "size": file_stat.st_size,
        "mtime_ns": file_stat.st_mtime_ns,
        "revision": _revision(content),
    }


def _candidate_descriptor(
    file_stat: os.stat_result, content: bytes
) -> dict[str, object]:
    return {
        "device": file_stat.st_dev,
        "inode": file_stat.st_ino,
        "mode": file_stat.st_mode,
        "uid": file_stat.st_uid,
        "gid": file_stat.st_gid,
        "size": file_stat.st_size,
        "mtime_ns": file_stat.st_mtime_ns,
        "revision": _revision(content),
    }


def _valid_fingerprint(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != _FINGERPRINT_KEYS:
        return False
    integer_keys = _FINGERPRINT_KEYS - {"revision"}
    if any(type(value[key]) is not int or value[key] < 0 for key in integer_keys):
        return False
    revision = value["revision"]
    return (
        isinstance(revision, str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", revision) is not None
    )


def _valid_candidate_descriptor(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != _CANDIDATE_DESCRIPTOR_KEYS:
        return False
    integer_keys = _CANDIDATE_DESCRIPTOR_KEYS - {"revision"}
    if any(type(value[key]) is not int or value[key] < 0 for key in integer_keys):
        return False
    revision = value["revision"]
    return (
        stat.S_ISREG(value["mode"])
        and stat.S_IMODE(value["mode"]) == 0o600
        and value["uid"] == os.geteuid()
        and isinstance(revision, str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", revision) is not None
    )


def _fingerprint_matches(
    file_stat: os.stat_result, content: bytes, expected: object
) -> bool:
    return (
        _valid_fingerprint(expected)
        and _file_fingerprint(file_stat, content) == expected
    )


def _candidate_descriptor_matches(
    file_stat: os.stat_result, content: bytes, expected: object
) -> bool:
    if not _valid_candidate_descriptor(expected):
        return False
    actual = {
        "device": file_stat.st_dev,
        "inode": file_stat.st_ino,
        "mode": file_stat.st_mode,
        "uid": file_stat.st_uid,
        "gid": file_stat.st_gid,
        "size": file_stat.st_size,
        "mtime_ns": file_stat.st_mtime_ns,
        "revision": _revision(content),
    }
    return _is_safe_file_stat(file_stat) and actual == expected


def _relaxed_identity_matches_at(
    parent_fd: int, name: str, expected: object
) -> bool:
    if not _valid_fingerprint(expected):
        return False
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return False
    return (
        stat.S_ISREG(current.st_mode)
        and current.st_nlink == 1
        and current.st_uid == os.geteuid()
        and current.st_dev == expected["device"]
        and current.st_ino == expected["inode"]
    )


def _read_optional_safe_file_at(
    parent_fd: int, name: str
) -> tuple[os.stat_result, bytes] | None:
    try:
        file_fd, file_stat, content = _read_safe_file_at(parent_fd, name)
    except _MissingConfigPath:
        return None
    try:
        return file_stat, content
    finally:
        os.close(file_fd)


def _create_private_file_at(
    parent_fd: int,
    name: str,
    content: bytes,
    *,
    expected_parent: _HeldParent | None = None,
    stage: str | None = None,
) -> os.stat_result:
    create_stage = stage or "before_private_file_create"

    def mutation_stage(action: str) -> str:
        if create_stage.endswith("_create"):
            return f"{create_stage[:-7]}_{action}"
        return f"{create_stage}_{action}"

    if expected_parent is not None:
        _transaction_boundary(
            create_stage,
            parent_fd,
            expected_parent,
        )
    file_fd: int | None = None
    created_identity: tuple[int, int] | None = None
    try:
        file_fd = os.open(
            name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_fd,
        )
        created_identity = _safe_file_identity(os.fstat(file_fd))
        if expected_parent is not None:
            _transaction_boundary(
                mutation_stage("fchmod"),
                parent_fd,
                expected_parent,
            )
        os.fchmod(file_fd, 0o600)
        remaining = memoryview(content)
        while remaining:
            if expected_parent is not None:
                _transaction_boundary(
                    mutation_stage("write"),
                    parent_fd,
                    expected_parent,
                )
            written = os.write(file_fd, remaining)
            if written <= 0:
                raise OSError("private file write did not make progress")
            remaining = remaining[written:]
        if expected_parent is not None:
            _transaction_boundary(
                mutation_stage("fsync"),
                parent_fd,
                expected_parent,
            )
        os.fsync(file_fd)
        file_stat = os.fstat(file_fd)
        if not _is_safe_file_stat(file_stat):
            raise _UnsafeConfigPath
        return file_stat
    except Exception:
        if created_identity is not None:
            with suppress(OSError):
                if expected_parent is not None:
                    _transaction_boundary(
                        "before_failed_private_file_cleanup",
                        parent_fd,
                        expected_parent,
                        require_private=False,
                        require_current_path=False,
                    )
                current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if _safe_file_identity(current) == created_identity:
                    if expected_parent is not None:
                        _transaction_boundary(
                            "before_failed_private_file_cleanup_commit",
                            parent_fd,
                            expected_parent,
                            require_private=False,
                            require_current_path=False,
                        )
                    os.unlink(name, dir_fd=parent_fd)
        raise
    finally:
        if file_fd is not None:
            os.close(file_fd)


def _is_safe_unnamed_file_stat(file_stat: os.stat_result) -> bool:
    return (
        stat.S_ISREG(file_stat.st_mode)
        and file_stat.st_nlink == 0
        and file_stat.st_uid == os.geteuid()
        and stat.S_IMODE(file_stat.st_mode) == 0o600
    )


def _prepare_unnamed_candidate_at(
    parent_fd: int,
    expected_parent: _HeldParent,
    content: bytes,
) -> tuple[int, os.stat_result]:
    _transaction_boundary(
        "before_unnamed_candidate_create",
        parent_fd,
        expected_parent,
    )
    candidate_fd = _open_unnamed_candidate_at(parent_fd)
    try:
        _transaction_boundary(
            "before_unnamed_candidate_fchmod",
            parent_fd,
            expected_parent,
        )
        os.fchmod(candidate_fd, 0o600)
        remaining = memoryview(content)
        while remaining:
            _transaction_boundary(
                "before_unnamed_candidate_write",
                parent_fd,
                expected_parent,
            )
            written = os.write(candidate_fd, remaining)
            if written <= 0:
                raise OSError("unnamed candidate write did not make progress")
            remaining = remaining[written:]
            _transaction_probe("after_unnamed_candidate_write")
        _transaction_boundary(
            "before_unnamed_candidate_fsync",
            parent_fd,
            expected_parent,
        )
        os.fsync(candidate_fd)
        candidate_stat = os.fstat(candidate_fd)
        if (
            not _is_safe_unnamed_file_stat(candidate_stat)
            or candidate_stat.st_size != len(content)
        ):
            raise _UnsafeConfigPath
        return candidate_fd, candidate_stat
    except Exception:
        os.close(candidate_fd)
        raise


def _fsync_transaction_parent_at(
    parent_fd: int,
    expected_parent: _HeldParent,
    stage: str,
    *,
    require_private: bool = True,
    require_current_path: bool = True,
) -> None:
    _transaction_boundary(
        stage,
        parent_fd,
        expected_parent,
        require_private=require_private,
        require_current_path=require_current_path,
    )
    os.fsync(parent_fd)


def _unlink_verified_file_at(
    parent_fd: int,
    name: str,
    expected: object,
    *,
    expected_parent: _HeldParent | None = None,
    stage: str = "before_transaction_unlink",
    require_private_parent: bool = True,
    require_current_path: bool = True,
) -> None:
    snapshot = _read_optional_safe_file_at(parent_fd, name)
    if snapshot is None:
        raise _UnsafeConfigPath
    file_stat, content = snapshot
    if not _fingerprint_matches(file_stat, content, expected):
        raise _UnsafeConfigPath
    if expected_parent is not None:
        _transaction_boundary(
            stage,
            parent_fd,
            expected_parent,
            require_private=require_private_parent,
            require_current_path=require_current_path,
        )
    inspected = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if _safe_file_version(inspected) != _safe_file_version(file_stat):
        raise _UnsafeConfigPath
    if expected_parent is not None:
        _transaction_boundary(
            f"{stage}_commit",
            parent_fd,
            expected_parent,
            require_private=require_private_parent,
            require_current_path=require_current_path,
        )
    os.unlink(name, dir_fd=parent_fd)
    if expected_parent is not None:
        _fsync_transaction_parent_at(
            parent_fd,
            expected_parent,
            f"{stage}_fsync",
            require_private=require_private_parent,
            require_current_path=require_current_path,
        )
    else:
        os.fsync(parent_fd)


def _unlink_verified_candidate_at(
    parent_fd: int,
    name: str,
    expected: object,
    *,
    expected_parent: _HeldParent,
    stage: str,
    require_private_parent: bool = True,
    require_current_path: bool = True,
) -> None:
    snapshot = _read_optional_safe_file_at(parent_fd, name)
    if snapshot is None:
        raise _UnsafeConfigPath
    file_stat, content = snapshot
    if not _candidate_descriptor_matches(file_stat, content, expected):
        raise _UnsafeConfigPath
    _transaction_boundary(
        stage,
        parent_fd,
        expected_parent,
        require_private=require_private_parent,
        require_current_path=require_current_path,
    )
    inspected = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if _safe_file_version(inspected) != _safe_file_version(file_stat):
        raise _UnsafeConfigPath
    _transaction_boundary(
        f"{stage}_commit",
        parent_fd,
        expected_parent,
        require_private=require_private_parent,
        require_current_path=require_current_path,
    )
    os.unlink(name, dir_fd=parent_fd)
    _fsync_transaction_parent_at(
        parent_fd,
        expected_parent,
        f"{stage}_fsync",
        require_private=require_private_parent,
        require_current_path=require_current_path,
    )


def _transaction_marker_bytes(
    temporary_name: str,
    expected: dict[str, object],
    candidate: dict[str, object],
) -> bytes:
    payload = {
        "version": _NTFY_TRANSACTION_VERSION,
        "temporary_name": temporary_name,
        "expected": expected,
        "candidate": candidate,
    }
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _parse_transaction_marker(
    content: bytes, expected_temporary_name: str
) -> dict[str, object]:
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _UnsafeConfigPath from exc
    if (
        not isinstance(payload, dict)
        or set(payload)
        != {"version", "temporary_name", "expected", "candidate"}
        or payload["version"] != _NTFY_TRANSACTION_VERSION
        or payload["temporary_name"] != expected_temporary_name
        or not _valid_fingerprint(payload["expected"])
        or not _valid_candidate_descriptor(payload["candidate"])
    ):
        raise _UnsafeConfigPath
    return payload


def _displaced_guard(
    file_stat: os.stat_result,
    content: bytes | None,
) -> dict[str, object]:
    return {
        "device": file_stat.st_dev,
        "inode": file_stat.st_ino,
        "mode": file_stat.st_mode,
        "links": file_stat.st_nlink,
        "uid": file_stat.st_uid,
        "gid": file_stat.st_gid,
        "size": file_stat.st_size,
        "mtime_ns": file_stat.st_mtime_ns,
        "revision": _revision(content) if content is not None else None,
    }


def _valid_displaced_guard(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != _FINGERPRINT_KEYS:
        return False
    integer_keys = _FINGERPRINT_KEYS - {"revision"}
    if any(type(value[key]) is not int or value[key] < 0 for key in integer_keys):
        return False
    revision = value["revision"]
    return revision is None or (
        isinstance(revision, str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", revision) is not None
    )


def _disposition_bytes(
    temporary_name: str,
    expected: dict[str, object],
    candidate: dict[str, object],
    phase: str,
    displaced: dict[str, object],
) -> bytes:
    payload = {
        "version": _NTFY_DISPOSITION_VERSION,
        "temporary_name": temporary_name,
        "expected": expected,
        "candidate": candidate,
        "phase": phase,
        "displaced": displaced,
    }
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _parse_disposition(
    content: bytes,
    expected_temporary_name: str,
) -> dict[str, object]:
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _UnsafeConfigPath from exc
    if (
        not isinstance(payload, dict)
        or set(payload)
        != {
            "version",
            "temporary_name",
            "expected",
            "candidate",
            "phase",
            "displaced",
        }
        or payload["version"] != _NTFY_DISPOSITION_VERSION
        or payload["temporary_name"] != expected_temporary_name
        or not _valid_fingerprint(payload["expected"])
        or not _valid_candidate_descriptor(payload["candidate"])
        or payload["phase"] not in {"commit", "rollback"}
        or not _valid_displaced_guard(payload["displaced"])
    ):
        raise _UnsafeConfigPath
    return payload


def _persist_disposition_at(
    parent_fd: int,
    parent: _HeldParent,
    disposition_name: str,
    content: bytes,
) -> dict[str, object]:
    disposition_fd, disposition_stat = _prepare_unnamed_candidate_at(
        parent_fd,
        parent,
        content,
    )
    descriptor = _candidate_descriptor(disposition_stat, content)
    linked = False
    try:
        _transaction_boundary("before_disposition_link", parent_fd, parent)
        _link_unnamed_candidate_at(disposition_fd, parent_fd, disposition_name)
        linked = True
        linked_snapshot = _read_optional_safe_file_at(parent_fd, disposition_name)
        if (
            linked_snapshot is None
            or not _candidate_descriptor_matches(*linked_snapshot, descriptor)
        ):
            raise _UnsafeConfigPath
        _fsync_transaction_parent_at(
            parent_fd,
            parent,
            "before_disposition_directory_fsync",
        )
        _transaction_probe("after_disposition_durable")
        return _file_fingerprint(*linked_snapshot)
    except Exception:
        if linked:
            with suppress(OSError, _UnsafeConfigPath):
                _unlink_verified_candidate_at(
                    parent_fd,
                    disposition_name,
                    descriptor,
                    expected_parent=parent,
                    stage="before_failed_disposition_cleanup",
                    require_private_parent=False,
                    require_current_path=False,
                )
        raise
    finally:
        os.close(disposition_fd)


def _unsupported_marker_bytes(expected: dict[str, object]) -> bytes:
    payload = {
        "version": _NTFY_UNSUPPORTED_VERSION,
        "expected": expected,
    }
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _parse_unsupported_marker(content: bytes) -> dict[str, object]:
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _UnsafeConfigPath from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"version", "expected"}
        or payload["version"] != _NTFY_UNSUPPORTED_VERSION
        or not _valid_fingerprint(payload["expected"])
    ):
        raise _UnsafeConfigPath
    return payload["expected"]


def _cleanup_active_transaction_at(
    parent_fd: int,
    expected_parent: _HeldParent,
    marker_name: str,
    marker_fingerprint: dict[str, object] | None,
    candidate_name: str,
    candidate_fingerprint: dict[str, object] | None,
) -> None:
    if candidate_fingerprint is not None:
        _unlink_verified_candidate_at(
            parent_fd,
            candidate_name,
            candidate_fingerprint,
            expected_parent=expected_parent,
            stage="before_active_candidate_cleanup",
            require_private_parent=False,
            require_current_path=False,
        )
    if marker_fingerprint is not None:
        _unlink_verified_file_at(
            parent_fd,
            marker_name,
            marker_fingerprint,
            expected_parent=expected_parent,
            stage="before_active_marker_cleanup",
            require_private_parent=False,
            require_current_path=False,
        )


def _displaced_guard_matches_at(
    parent_fd: int,
    name: str,
    expected: object,
) -> bool:
    if not _valid_displaced_guard(expected):
        return False
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return False
    actual = _displaced_guard(current, None)
    revision = expected["revision"]
    actual["revision"] = revision
    if actual != expected:
        return False
    if revision is None:
        return True
    try:
        snapshot = _read_optional_safe_file_at(parent_fd, name)
    except (OSError, _UnsafeConfigPath):
        return False
    return snapshot is not None and _revision(snapshot[1]) == revision


def _ensure_disposition_at(
    parent_fd: int,
    parent: _HeldParent,
    disposition_name: str,
    content: bytes,
) -> dict[str, object]:
    snapshot = _read_optional_safe_file_at(parent_fd, disposition_name)
    if snapshot is not None:
        if snapshot[1] != content:
            raise _UnsafeConfigPath
        return _file_fingerprint(*snapshot)
    return _persist_disposition_at(
        parent_fd,
        parent,
        disposition_name,
        content,
    )


def _entry_version_without_ctime(
    file_stat: os.stat_result,
) -> tuple[int, int, int, int, int, int, int, int]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_mode,
        file_stat.st_nlink,
        file_stat.st_uid,
        file_stat.st_gid,
        file_stat.st_size,
        file_stat.st_mtime_ns,
    )


def _rollback_mismatched_exchange_at(
    parent_fd: int,
    parent: _HeldParent,
    name: str,
    candidate_name: str,
    candidate: dict[str, object],
    marker_name: str,
    marker_fingerprint: dict[str, object] | None,
    disposition_name: str,
    disposition_fingerprint: dict[str, object],
    displaced: dict[str, object],
    displaced_snapshot: tuple[os.stat_result, bytes] | None,
) -> str:
    try:
        displaced_stat = os.stat(
            candidate_name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise _UnsafeConfigPath from exc
    displaced_version = _entry_version_without_ctime(displaced_stat)
    displaced_fingerprint = (
        _file_fingerprint(*displaced_snapshot)
        if displaced_snapshot is not None
        else None
    )
    if not _displaced_guard_matches_at(parent_fd, candidate_name, displaced):
        raise _UnsafeConfigPath

    def entry_matches_displaced(entry_name: str) -> bool:
        try:
            current = os.stat(
                entry_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except OSError:
            return False
        if _entry_version_without_ctime(current) != displaced_version:
            return False
        if displaced_fingerprint is None:
            return True
        try:
            snapshot = _read_optional_safe_file_at(parent_fd, entry_name)
        except (OSError, _UnsafeConfigPath):
            return False
        return snapshot is not None and _fingerprint_matches(
            *snapshot,
            displaced_fingerprint,
        )

    def entry_is_candidate(entry_name: str) -> bool:
        try:
            snapshot = _read_optional_safe_file_at(parent_fd, entry_name)
        except (OSError, _UnsafeConfigPath):
            return False
        return snapshot is not None and _candidate_descriptor_matches(
            *snapshot,
            candidate,
        )

    rollback_complete = False
    for attempt in range(2):
        _transaction_boundary(
            f"before_mismatch_rollback_exchange_{attempt + 1}",
            parent_fd,
            parent,
        )
        try:
            _rename_exchange_at(parent_fd, candidate_name, name)
        except OSError as rollback_error:
            if entry_matches_displaced(name) and entry_is_candidate(candidate_name):
                rollback_complete = True
                break
            if not (
                entry_is_candidate(name)
                and entry_matches_displaced(candidate_name)
            ):
                raise _UnsafeConfigPath from rollback_error
        else:
            rollback_complete = True
            break

    if not rollback_complete:
        if not entry_is_candidate(name) or not entry_matches_displaced(candidate_name):
            raise _UnsafeConfigPath
        _transaction_boundary(
            "before_mismatch_rollback_replace",
            parent_fd,
            parent,
        )
        os.replace(
            candidate_name,
            name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )

    _transaction_probe("after_mismatch_rollback")
    _fsync_transaction_parent_at(
        parent_fd,
        parent,
        "before_mismatch_rollback_directory_fsync",
    )
    live_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if displaced_fingerprint is None:
        if _entry_version_without_ctime(live_stat) != displaced_version:
            raise _UnsafeConfigPath
    else:
        live_snapshot = _read_optional_safe_file_at(parent_fd, name)
        if (
            live_snapshot is None
            or not _fingerprint_matches(
                *live_snapshot,
                displaced_fingerprint,
            )
        ):
            raise _UnsafeConfigPath

    isolated_candidate = _read_optional_safe_file_at(parent_fd, candidate_name)
    if isolated_candidate is not None:
        if not _candidate_descriptor_matches(*isolated_candidate, candidate):
            raise _UnsafeConfigPath
        _unlink_verified_candidate_at(
            parent_fd,
            candidate_name,
            candidate,
            expected_parent=parent,
            stage="before_mismatch_candidate_cleanup",
        )
    _unlink_verified_file_at(
        parent_fd,
        disposition_name,
        disposition_fingerprint,
        expected_parent=parent,
        stage="before_mismatch_disposition_cleanup",
    )
    if marker_fingerprint is not None:
        _unlink_verified_file_at(
            parent_fd,
            marker_name,
            marker_fingerprint,
            expected_parent=parent,
            stage="before_mismatch_marker_cleanup",
        )
    return "manual_target"


def _recover_ntfy_transaction_at(
    parent_fd: int,
    name: str,
    expected_parent: _HeldParent | None = None,
    *,
    parent_path: Path | None = None,
) -> str:
    parent = _validate_held_parent(
        parent_fd,
        expected_parent,
        parent_path=parent_path,
    )
    marker_name, candidate_name, unsupported_name = _transaction_names(name)
    disposition_name = _transaction_disposition_name(name)
    marker_snapshot = _read_optional_safe_file_at(parent_fd, marker_name)
    disposition_snapshot = _read_optional_safe_file_at(
        parent_fd,
        disposition_name,
    )
    candidate_read_error = False
    candidate_unsafe = False
    try:
        candidate_snapshot = _read_optional_safe_file_at(
            parent_fd, candidate_name
        )
    except _UnsafeConfigPath:
        candidate_snapshot = None
        candidate_unsafe = True
    except OSError:
        candidate_snapshot = None
        candidate_read_error = True
    unsupported_snapshot = _read_optional_safe_file_at(parent_fd, unsupported_name)

    if (
        marker_snapshot is None
        and disposition_snapshot is None
        and unsupported_snapshot is None
    ):
        if candidate_snapshot is not None or candidate_unsafe or candidate_read_error:
            raise _UnsafeConfigPath
        return "none"

    target_unsafe = False
    try:
        target_snapshot = _read_optional_safe_file_at(parent_fd, name)
    except _UnsafeConfigPath:
        target_snapshot = None
        target_unsafe = True
    if unsupported_snapshot is not None:
        if disposition_snapshot is not None:
            raise _UnsafeConfigPath
        _unsupported_stat, unsupported_content = unsupported_snapshot
        unsupported_expected = _parse_unsupported_marker(unsupported_content)
        if marker_snapshot is not None:
            marker_stat, marker_content = marker_snapshot
            marker = _parse_transaction_marker(marker_content, candidate_name)
            if marker["expected"] != unsupported_expected:
                raise _UnsafeConfigPath
            marker_fingerprint = _file_fingerprint(marker_stat, marker_content)
            candidate = marker["candidate"]
            if candidate_snapshot is not None:
                if not _candidate_descriptor_matches(
                    *candidate_snapshot, candidate
                ):
                    return "unsupported"
                _unlink_verified_candidate_at(
                    parent_fd,
                    candidate_name,
                    candidate,
                    expected_parent=parent,
                    stage="before_unsupported_candidate_cleanup",
                )
            elif candidate_unsafe or candidate_read_error:
                return "unsupported"
            _unlink_verified_file_at(
                parent_fd,
                marker_name,
                marker_fingerprint,
                expected_parent=parent,
                stage="before_unsupported_marker_cleanup",
            )
        return "unsupported"

    marker_fingerprint: dict[str, object] | None = None
    marker: dict[str, object] | None = None
    if marker_snapshot is not None:
        marker_stat, marker_content = marker_snapshot
        marker = _parse_transaction_marker(marker_content, candidate_name)
        marker_fingerprint = _file_fingerprint(marker_stat, marker_content)

    disposition_fingerprint: dict[str, object] | None = None
    disposition: dict[str, object] | None = None
    if disposition_snapshot is not None:
        disposition_stat, disposition_content = disposition_snapshot
        disposition = _parse_disposition(disposition_content, candidate_name)
        disposition_fingerprint = _file_fingerprint(
            disposition_stat,
            disposition_content,
        )
        if marker is not None and (
            marker["expected"] != disposition["expected"]
            or marker["candidate"] != disposition["candidate"]
        ):
            raise _UnsafeConfigPath

    transaction = marker if marker is not None else disposition
    if transaction is None:
        raise _UnsafeConfigPath
    expected = transaction["expected"]
    candidate = transaction["candidate"]
    phase = disposition["phase"] if disposition is not None else "prepared"
    displaced = disposition["displaced"] if disposition is not None else None

    def cleanup_metadata(stage: str) -> None:
        if marker_fingerprint is not None:
            _unlink_verified_file_at(
                parent_fd,
                marker_name,
                marker_fingerprint,
                expected_parent=parent,
                stage=f"before_{stage}_marker_cleanup",
            )
        if disposition_fingerprint is not None:
            _unlink_verified_file_at(
                parent_fd,
                disposition_name,
                disposition_fingerprint,
                expected_parent=parent,
                stage=f"before_{stage}_disposition_cleanup",
            )

    def ensure_disposition(
        requested_phase: str,
        requested_displaced: dict[str, object],
    ) -> tuple[dict[str, object], dict[str, object]]:
        content = _disposition_bytes(
            candidate_name,
            expected,
            candidate,
            requested_phase,
            requested_displaced,
        )
        fingerprint = _ensure_disposition_at(
            parent_fd,
            parent,
            disposition_name,
            content,
        )
        return fingerprint, requested_displaced
    target_is_expected = (
        target_snapshot is not None
        and _fingerprint_matches(*target_snapshot, expected)
    )
    target_is_candidate = (
        target_snapshot is not None
        and _candidate_descriptor_matches(*target_snapshot, candidate)
    )
    temporary_is_expected = (
        candidate_snapshot is not None
        and _fingerprint_matches(*candidate_snapshot, expected)
    )
    temporary_is_candidate = (
        candidate_snapshot is not None
        and _candidate_descriptor_matches(*candidate_snapshot, candidate)
    )

    if target_is_expected:
        if phase != "prepared":
            if (
                phase != "rollback"
                or displaced is None
                or not _displaced_guard_matches_at(parent_fd, name, displaced)
                or not temporary_is_candidate
            ):
                raise _UnsafeConfigPath
            _unlink_verified_candidate_at(
                parent_fd,
                candidate_name,
                candidate,
                expected_parent=parent,
                stage="before_exact_rollback_candidate_cleanup",
            )
            cleanup_metadata("exact_rollback")
            return "manual_target"
        if temporary_is_candidate:
            _unlink_verified_candidate_at(
                parent_fd,
                candidate_name,
                candidate,
                expected_parent=parent,
                stage="before_aborted_candidate_cleanup",
            )
        elif candidate_snapshot is not None:
            cleanup_metadata("collision")
            return "manual_artifact"
        elif candidate_unsafe or candidate_read_error:
            raise _UnsafeConfigPath
        cleanup_metadata("aborted")
        return "aborted"

    if temporary_is_expected:
        if phase == "rollback":
            raise _UnsafeConfigPath
        if phase == "prepared":
            disposition_fingerprint, displaced = ensure_disposition(
                "commit",
                expected,
            )
            phase = "commit"
        if target_snapshot is None and not target_unsafe:
            raise _UnsafeConfigPath
        _unlink_verified_file_at(
            parent_fd,
            candidate_name,
            expected,
            expected_parent=parent,
            stage="before_displaced_original_cleanup",
        )
        cleanup_metadata("committed")
        return "committed" if target_is_candidate else "manual_target"

    if target_is_candidate and candidate_snapshot is None:
        if candidate_unsafe or candidate_read_error:
            if phase == "commit":
                raise _UnsafeConfigPath
            if phase == "prepared":
                displaced_stat = os.stat(
                    candidate_name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                disposition_fingerprint, displaced = ensure_disposition(
                    "rollback",
                    _displaced_guard(displaced_stat, None),
                )
                phase = "rollback"
            assert disposition_fingerprint is not None
            assert displaced is not None
            return _rollback_mismatched_exchange_at(
                parent_fd,
                parent,
                name,
                candidate_name,
                candidate,
                marker_name,
                marker_fingerprint,
                disposition_name,
                disposition_fingerprint,
                displaced,
                None,
            )
        if phase == "commit":
            cleanup_metadata("response_loss_commit")
            return "committed"
        _unlink_verified_candidate_at(
            parent_fd,
            name,
            candidate,
            expected_parent=parent,
            stage="before_missing_displaced_ack_cleanup",
        )
        cleanup_metadata("missing_displaced")
        return "manual_target"

    if target_is_candidate and candidate_snapshot is not None:
        if temporary_is_candidate:
            raise _UnsafeConfigPath
        if phase == "commit":
            raise _UnsafeConfigPath
        if phase == "prepared":
            disposition_fingerprint, displaced = ensure_disposition(
                "rollback",
                _displaced_guard(*candidate_snapshot),
            )
            phase = "rollback"
        assert disposition_fingerprint is not None
        assert displaced is not None
        return _rollback_mismatched_exchange_at(
            parent_fd,
            parent,
            name,
            candidate_name,
            candidate,
            marker_name,
            marker_fingerprint,
            disposition_name,
            disposition_fingerprint,
            displaced,
            candidate_snapshot,
        )

    if temporary_is_candidate and (target_snapshot is not None or target_unsafe):
        if phase == "commit":
            raise _UnsafeConfigPath
        if phase == "rollback" and (
            displaced is None
            or not _displaced_guard_matches_at(parent_fd, name, displaced)
        ):
            raise _UnsafeConfigPath
        _unlink_verified_candidate_at(
            parent_fd,
            candidate_name,
            candidate,
            expected_parent=parent,
            stage="before_completed_rollback_candidate_cleanup",
        )
        cleanup_metadata("completed_rollback")
        return "manual_target"

    if candidate_snapshot is None and target_snapshot is not None:
        if phase == "commit":
            cleanup_metadata("commit_with_manual_target")
            return "manual_target"
        cleanup_metadata("marker_only_manual")
        return "manual_target"

    raise _UnsafeConfigPath


def _durable_replace_at(
    parent_fd: int,
    name: str,
    content: bytes,
    *,
    before_replace: Callable[[], None] | None = None,
) -> None:
    temporary_name = f".{name}.{secrets.token_hex(16)}.tmp"
    temporary_fd: int | None = None
    replaced = False
    try:
        temporary_fd = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_fd,
        )
        os.fchmod(temporary_fd, 0o600)
        remaining = memoryview(content)
        while remaining:
            written = os.write(temporary_fd, remaining)
            if written <= 0:
                raise OSError("candidate write did not make progress")
            remaining = remaining[written:]
        os.fsync(temporary_fd)
        os.close(temporary_fd)
        temporary_fd = None
        if before_replace is not None:
            before_replace()
        os.replace(
            temporary_name,
            name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        replaced = True
        try:
            os.fsync(parent_fd)
        except OSError as exc:
            raise _PostReplaceDurabilityError from exc
    except Exception as exc:
        if replaced and not isinstance(exc, _PostReplaceDurabilityError):
            raise _PostReplaceDurabilityError from exc
        raise
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        with suppress(OSError):
            os.unlink(temporary_name, dir_fd=parent_fd)


def _unsupported_exchange_error(error: OSError) -> bool:
    return error.errno in {
        errno.ENOSYS,
        errno.EOPNOTSUPP,
        getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
        errno.EINVAL,
    }


def _unsupported_unnamed_candidate_error(error: OSError) -> bool:
    return error.errno in {
        errno.ENOSYS,
        errno.EOPNOTSUPP,
        getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
        errno.EINVAL,
        errno.EISDIR,
        errno.ENOENT,
        errno.EPERM,
    }


def _persist_unsupported_transaction_at(
    parent_fd: int,
    expected_parent: _HeldParent,
    expected: dict[str, object],
    unsupported_name: str,
    *,
    marker_name: str | None = None,
    marker_fingerprint: dict[str, object] | None = None,
    candidate_name: str | None = None,
    candidate: dict[str, object] | None = None,
) -> None:
    unsupported_content = _unsupported_marker_bytes(expected)
    _create_private_file_at(
        parent_fd,
        unsupported_name,
        unsupported_content,
        expected_parent=expected_parent,
        stage="before_unsupported_marker_create",
    )
    _fsync_transaction_parent_at(
        parent_fd,
        expected_parent,
        "before_unsupported_marker_directory_fsync",
    )
    _transaction_probe("after_unsupported_marker_durable")
    if candidate_name is not None and candidate is not None:
        _unlink_verified_candidate_at(
            parent_fd,
            candidate_name,
            candidate,
            expected_parent=expected_parent,
            stage="before_unsupported_active_candidate_cleanup",
        )
    if marker_name is not None and marker_fingerprint is not None:
        _unlink_verified_file_at(
            parent_fd,
            marker_name,
            marker_fingerprint,
            expected_parent=expected_parent,
            stage="before_unsupported_transaction_marker_cleanup",
        )


def _durable_exchange_at(
    parent_fd: int,
    name: str,
    content: bytes,
    expected_stat: os.stat_result,
    expected_content: bytes,
    *,
    parent_path: Path,
    before_replace: Callable[[], None],
) -> None:
    if not _rename_exchange_available() or not _unnamed_candidate_available():
        raise _RenameExchangeUnsupported(
            errno.ENOSYS, "required atomic filesystem primitives are unavailable"
        )
    parent = _validate_held_parent(
        parent_fd,
        parent_path=parent_path,
    )
    marker_name, candidate_name, unsupported_name = _transaction_names(name)
    disposition_name = _transaction_disposition_name(name)
    if any(
        _read_optional_safe_file_at(parent_fd, reserved) is not None
        for reserved in (
            marker_name,
            candidate_name,
            disposition_name,
            unsupported_name,
        )
    ):
        raise _UnsafeConfigPath

    expected = _file_fingerprint(expected_stat, expected_content)
    candidate_fd: int | None = None
    candidate_descriptor: dict[str, object] | None = None
    marker_content: bytes | None = None
    marker_fingerprint: dict[str, object] | None = None
    candidate_linked = False

    try:
        try:
            candidate_fd, candidate_stat = _prepare_unnamed_candidate_at(
                parent_fd,
                parent,
                content,
            )
        except OSError as candidate_error:
            if _unsupported_unnamed_candidate_error(candidate_error):
                _persist_unsupported_transaction_at(
                    parent_fd,
                    parent,
                    expected,
                    unsupported_name,
                )
                raise _RenameExchangeUnsupported(
                    candidate_error.errno,
                    "unnamed candidate creation is unsupported",
                ) from candidate_error
            raise

        candidate_descriptor = _candidate_descriptor(candidate_stat, content)
        marker_content = _transaction_marker_bytes(
            candidate_name,
            expected,
            candidate_descriptor,
        )
        marker_stat = _create_private_file_at(
            parent_fd,
            marker_name,
            marker_content,
            expected_parent=parent,
            stage="before_marker_create",
        )
        marker_fingerprint = _file_fingerprint(marker_stat, marker_content)
        _fsync_transaction_parent_at(
            parent_fd,
            parent,
            "before_marker_directory_fsync",
        )
        _transaction_probe("after_marker_durable")
        _transaction_probe("after_candidate_marker_durable")

        _transaction_boundary("before_candidate_link", parent_fd, parent)
        try:
            _link_unnamed_candidate_at(candidate_fd, parent_fd, candidate_name)
        except OSError as link_error:
            if _unsupported_unnamed_candidate_error(link_error):
                _persist_unsupported_transaction_at(
                    parent_fd,
                    parent,
                    expected,
                    unsupported_name,
                    marker_name=marker_name,
                    marker_fingerprint=marker_fingerprint,
                )
                marker_fingerprint = None
                raise _RenameExchangeUnsupported(
                    link_error.errno,
                    "linking an unnamed candidate is unsupported",
                ) from link_error
            raise
        candidate_linked = True
        _transaction_probe("after_candidate_link")
        linked_candidate = _read_optional_safe_file_at(parent_fd, candidate_name)
        if (
            linked_candidate is None
            or not _candidate_descriptor_matches(
                *linked_candidate,
                candidate_descriptor,
            )
        ):
            raise _UnsafeConfigPath
        _fsync_transaction_parent_at(
            parent_fd,
            parent,
            "before_candidate_directory_fsync",
        )
        _transaction_probe("after_candidate_durable")
        os.close(candidate_fd)
        candidate_fd = None
        before_replace()
        _transaction_boundary("before_exchange", parent_fd, parent)
    except Exception:
        if candidate_fd is not None:
            os.close(candidate_fd)
            candidate_fd = None
        _cleanup_active_transaction_at(
            parent_fd,
            parent,
            marker_name,
            marker_fingerprint,
            candidate_name,
            candidate_descriptor if candidate_linked else None,
        )
        raise

    try:
        _rename_exchange_at(parent_fd, candidate_name, name)
    except OSError as exchange_error:
        if (
            _unsupported_exchange_error(exchange_error)
            and candidate_descriptor is not None
            and marker_content is not None
        ):
            target_snapshot = _read_optional_safe_file_at(parent_fd, name)
            temporary_snapshot = _read_optional_safe_file_at(
                parent_fd, candidate_name
            )
            if (
                target_snapshot is not None
                and _fingerprint_matches(*target_snapshot, expected)
                and temporary_snapshot is not None
                and _candidate_descriptor_matches(
                    *temporary_snapshot, candidate_descriptor
                )
            ):
                _persist_unsupported_transaction_at(
                    parent_fd,
                    parent,
                    expected,
                    unsupported_name,
                    marker_name=marker_name,
                    marker_fingerprint=marker_fingerprint,
                    candidate_name=candidate_name,
                    candidate=candidate_descriptor,
                )
                marker_fingerprint = None
                candidate_linked = False
                raise _RenameExchangeUnsupported(
                    exchange_error.errno,
                    "atomic rename exchange is unsupported",
                ) from exchange_error
        try:
            disposition = _recover_ntfy_transaction_at(
                parent_fd, name, parent
            )
        except (OSError, _MissingConfigPath, _UnsafeConfigPath) as recovery_error:
            raise _PostReplaceDurabilityError from recovery_error
        if disposition == "committed":
            return
        if disposition == "manual_target":
            raise NtfyDisclosureConflictError(
                "Configuration revision changed"
            ) from exchange_error
        raise exchange_error

    try:
        _transaction_probe("after_exchange")
    except Exception as probe_error:
        try:
            disposition = _recover_ntfy_transaction_at(
                parent_fd, name, parent
            )
        except (OSError, _MissingConfigPath, _UnsafeConfigPath) as recovery_error:
            raise _PostReplaceDurabilityError from recovery_error
        if disposition == "committed":
            return
        if disposition == "manual_target":
            raise NtfyDisclosureConflictError(
                "Configuration revision changed"
            ) from probe_error
        raise _PostReplaceDurabilityError from probe_error

    try:
        disposition = _recover_ntfy_transaction_at(parent_fd, name, parent)
    except (OSError, _MissingConfigPath, _UnsafeConfigPath) as exc:
        raise _PostReplaceDurabilityError from exc
    if disposition == "committed":
        return
    if disposition == "manual_target":
        raise NtfyDisclosureConflictError("Configuration revision changed")
    raise _PostReplaceDurabilityError



def acknowledge_ntfy_disclosure(
    path: Path, expected_revision: str
) -> None:
    if os.name != "posix":
        raise NtfyDisclosureConflictError(
            "Disclosure acknowledgement is unavailable"
        )
    try:
        absolute, parent_fd, name = _open_safe_parent(path)
    except (_MissingConfigPath, _UnsafeConfigPath, OSError) as exc:
        raise NtfyDisclosureConflictError(
            "Configuration is not eligible for acknowledgement"
        ) from exc

    current_fd: int | None = None
    replacement_completed = False
    try:
        with _config_serialization_lock():
            try:
                recovery = _recover_ntfy_transaction_at(
                    parent_fd,
                    name,
                    parent_path=absolute.parent,
                )
                if (
                    recovery in {"unsupported", "manual_artifact"}
                    or not _rename_exchange_available()
                    or not _unnamed_candidate_available()
                ):
                    raise _UnsafeConfigPath
            except (
                OSError,
                _MissingConfigPath,
                _UnsafeConfigPath,
            ) as exc:
                raise NtfyDisclosureConflictError(
                    "Configuration is not eligible for acknowledgement"
                ) from exc
            try:
                current_fd, current_identity, current = (
                    _read_safe_file_at(parent_fd, name)
                )
            except (
                _MissingConfigPath,
                _UnsafeConfigPath,
                OSError,
            ) as exc:
                raise NtfyDisclosureConflictError(
                    "Configuration is not eligible for acknowledgement"
                ) from exc
            if _revision(current) != expected_revision:
                raise NtfyDisclosureConflictError(
                    "Configuration revision changed"
                )
            status, candidate = _classify_ntfy_disclosure(
                current, absolute
            )
            if (
                status.state != "acknowledgement_required"
                or candidate is None
            ):
                raise NtfyDisclosureConflictError(
                    "Configuration is not eligible for acknowledgement"
                )
            try:
                _load_config_bytes(candidate, absolute)
            except ConfigError as exc:
                raise NtfyDisclosureConflictError(
                    "Configuration is not eligible for acknowledgement"
                ) from exc

            def verify_precommit() -> None:
                try:
                    verify_fd, verify_identity, verify_content = (
                        _read_safe_file_at(parent_fd, name)
                    )
                except (
                    _MissingConfigPath,
                    _UnsafeConfigPath,
                    OSError,
                ) as exc:
                    raise NtfyDisclosureConflictError(
                        "Configuration revision changed"
                    ) from exc
                try:
                    if (
                        _safe_file_version(verify_identity)
                        != _safe_file_version(current_identity)
                        or verify_content != current
                    ):
                        raise NtfyDisclosureConflictError(
                            "Configuration revision changed"
                        )
                finally:
                    os.close(verify_fd)

            try:
                _durable_exchange_at(
                    parent_fd,
                    name,
                    candidate,
                    current_identity,
                    current,
                    parent_path=absolute.parent,
                    before_replace=verify_precommit,
                )
                replacement_completed = True
            except NtfyDisclosureConflictError:
                raise
            except _PostReplaceDurabilityError as exc:
                raise NtfyDisclosureOutcomeUnknownError(
                    "Disclosure acknowledgement outcome is unknown"
                ) from exc
            except OSError as exc:
                raise NtfyDisclosureWriteError(
                    "Disclosure acknowledgement was not written"
                ) from exc

            try:
                final_handle = _open_safe_config(absolute)
                try:
                    if final_handle.content != candidate:
                        raise ConfigError(
                            "final configuration did not match candidate"
                        )
                finally:
                    final_handle.close()
                _load_runtime_config(absolute, lock_held=True)
                confirmed_handle = _open_safe_config(absolute)
                try:
                    if confirmed_handle.content != candidate:
                        raise ConfigError(
                            "final configuration did not match candidate"
                        )
                finally:
                    confirmed_handle.close()
            except (
                ConfigError,
                OSError,
                _MissingConfigPath,
                _UnsafeConfigPath,
            ) as exc:
                raise NtfyDisclosureOutcomeUnknownError(
                    "Disclosure acknowledgement outcome is unknown"
                ) from exc
    except (
        NtfyDisclosureConflictError,
        NtfyDisclosureOutcomeUnknownError,
        NtfyDisclosureWriteError,
    ):
        raise
    except Exception as exc:
        if replacement_completed:
            raise NtfyDisclosureOutcomeUnknownError(
                "Disclosure acknowledgement outcome is unknown"
            ) from exc
        raise NtfyDisclosureWriteError(
            "Disclosure acknowledgement was not written"
        ) from exc
    finally:
        if current_fd is not None:
            os.close(current_fd)
        os.close(parent_fd)



def _windows_stat_version(
    file_stat: os.stat_result,
) -> tuple[int, int, int, int, int, int, int]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_nlink,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
        int(getattr(file_stat, "st_file_attributes", 0)),
    )


def _windows_replacement_identity(
    file_stat: os.stat_result,
) -> tuple[int, int, int, int, int, int]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_nlink,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        int(getattr(file_stat, "st_file_attributes", 0)),
    )


def _is_safe_windows_file_stat(file_stat: os.stat_result) -> bool:
    reparse_point = 0x400
    return (
        stat.S_ISREG(file_stat.st_mode)
        and file_stat.st_nlink == 1
        and not int(getattr(file_stat, "st_file_attributes", 0)) & reparse_point
    )


def _read_safe_windows_file(path: Path) -> tuple[os.stat_result, bytes]:
    try:
        inspected = os.lstat(path)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise _UnsafeConfigPath from exc
    if not _is_safe_windows_file_stat(inspected):
        raise _UnsafeConfigPath
    try:
        file_fd = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise _UnsafeConfigPath from exc
    try:
        opened = os.fstat(file_fd)
        if not _is_safe_windows_file_stat(opened) or _windows_stat_version(
            opened
        ) != _windows_stat_version(inspected):
            raise _UnsafeConfigPath
        content = _read_fd_bytes(file_fd)
        completed = os.fstat(file_fd)
        if not _is_safe_windows_file_stat(completed) or _windows_stat_version(
            completed
        ) != _windows_stat_version(opened):
            raise _UnsafeConfigPath
        return completed, content
    finally:
        os.close(file_fd)


_WINDOWS_PUBLICATION_VERSION = 1
_WINDOWS_PUBLICATION_SUFFIX = ".config-publication-transaction"
_WINDOWS_MARKER_STAGING_SUFFIX = ".config-publication-marker-staging"
_WINDOWS_CANDIDATE_PREFIX = ".config-publication-candidate-"
_WINDOWS_BACKUP_PREFIX = ".config-publication-backup-"


def _windows_publication_descriptor(
    file_stat: os.stat_result,
    content: bytes,
) -> dict[str, object]:
    return {
        "identity": list(_windows_replacement_identity(file_stat)),
        "revision": _revision(content),
    }


def _valid_windows_publication_descriptor(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"identity", "revision"}:
        return False
    identity = value["identity"]
    revision = value["revision"]
    return (
        isinstance(identity, list)
        and len(identity) == 6
        and all(type(item) is int and item >= 0 for item in identity)
        and isinstance(revision, str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", revision) is not None
    )


def _windows_candidate_descriptor(
    content: bytes,
    file_stat: os.stat_result | None = None,
) -> dict[str, object]:
    return {
        "size": len(content),
        "revision": _revision(content),
        "identity": (
            list(_windows_replacement_identity(file_stat))
            if file_stat is not None
            else None
        ),
    }


def _valid_windows_candidate_descriptor(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"size", "revision", "identity"}:
        return False
    identity = value["identity"]
    return (
        type(value["size"]) is int
        and value["size"] >= 0
        and isinstance(value["revision"], str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", value["revision"]) is not None
        and (
            identity is None
            or isinstance(identity, list)
            and len(identity) == 6
            and all(type(item) is int and item >= 0 for item in identity)
        )
    )


def _windows_publication_matches(
    snapshot: tuple[os.stat_result, bytes] | None,
    expected: object,
) -> bool:
    return (
        snapshot is not None
        and _valid_windows_publication_descriptor(expected)
        and _windows_publication_descriptor(*snapshot) == expected
    )


def _windows_candidate_matches(
    snapshot: tuple[os.stat_result, bytes] | None,
    expected: object,
) -> bool:
    if snapshot is None or not _valid_windows_candidate_descriptor(expected):
        return False
    file_stat, content = snapshot
    if len(content) != expected["size"] or _revision(content) != expected["revision"]:
        return False
    identity = expected["identity"]
    return identity is None or list(_windows_replacement_identity(file_stat)) == identity


def _windows_publication_marker_bytes(
    candidate_name: str,
    backup_name: str,
    expected: dict[str, object],
    candidate: dict[str, object],
) -> bytes:
    payload = {
        "version": _WINDOWS_PUBLICATION_VERSION,
        "candidate_name": candidate_name,
        "backup_name": backup_name,
        "expected": expected,
        "candidate": candidate,
    }
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _parse_windows_publication_marker(path: Path, content: bytes) -> dict[str, object]:
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _UnsafeConfigPath from exc
    if (
        not isinstance(payload, dict)
        or set(payload)
        != {"version", "candidate_name", "backup_name", "expected", "candidate"}
        or payload["version"] != _WINDOWS_PUBLICATION_VERSION
        or not isinstance(payload["candidate_name"], str)
        or not payload["candidate_name"].startswith(
            f".{path.name}{_WINDOWS_CANDIDATE_PREFIX}"
        )
        or not isinstance(payload["backup_name"], str)
        or not payload["backup_name"].startswith(f".{path.name}{_WINDOWS_BACKUP_PREFIX}")
        or Path(payload["candidate_name"]).name != payload["candidate_name"]
        or Path(payload["backup_name"]).name != payload["backup_name"]
        or payload["candidate_name"] == payload["backup_name"]
        or not _valid_windows_publication_descriptor(payload["expected"])
        or not _valid_windows_candidate_descriptor(payload["candidate"])
    ):
        raise _UnsafeConfigPath
    return payload


def _read_optional_safe_windows_file(
    path: Path,
) -> tuple[os.stat_result, bytes] | None:
    try:
        return _read_safe_windows_file(path)
    except FileNotFoundError:
        return None


def _write_private_windows_file(path: Path, content: bytes) -> tuple[os.stat_result, bytes]:
    file_fd: int | None = None
    try:
        file_fd = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        os.chmod(path, 0o600)
        remaining = memoryview(content)
        while remaining:
            written = os.write(file_fd, remaining)
            if written <= 0:
                raise OSError("publication write did not make progress")
            remaining = remaining[written:]
        os.fsync(file_fd)
    finally:
        if file_fd is not None:
            os.close(file_fd)
    snapshot = _read_safe_windows_file(path)
    if snapshot[1] != content:
        raise _UnsafeConfigPath
    return snapshot


def _windows_replace_file(target: Path, replacement: Path, backup: Path) -> None:
    if os.name != "nt":
        os.replace(target, backup)
        os.replace(replacement, target)
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    replace_file = kernel32.ReplaceFileW
    replace_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    replace_file.restype = ctypes.c_int
    if not replace_file(
        str(target),
        str(replacement),
        str(backup),
        0,
        None,
        None,
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _windows_move_file(source: Path, destination: Path, *, replace: bool) -> None:
    if os.name != "nt":
        if replace:
            os.replace(source, destination)
        else:
            os.rename(source, destination)
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    move_file = kernel32.MoveFileExW
    move_file.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    move_file.restype = ctypes.c_int
    flags = 0x00000008 | (0x00000001 if replace else 0)
    if not move_file(str(source), str(destination), flags):
        raise ctypes.WinError(ctypes.get_last_error())


def _windows_flush_parent(parent: Path) -> None:
    if os.name != "nt":
        parent_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return
    # Windows has no documented directory FlushFileBuffers contract. File
    # contents are flushed before publication, durable renames use
    # MoveFileExW, and every metadata transition remains replayable.


def _unlink_matching_windows_file(path: Path, expected: object) -> None:
    snapshot = _read_optional_safe_windows_file(path)
    if snapshot is None:
        return
    if not _windows_publication_matches(snapshot, expected):
        raise _UnsafeConfigPath
    os.unlink(path)


def _unlink_matching_windows_candidate(path: Path, expected: object) -> None:
    snapshot = _read_optional_safe_windows_file(path)
    if snapshot is None:
        return
    if not _windows_candidate_matches(snapshot, expected):
        raise _UnsafeConfigPath
    os.unlink(path)


def _unlink_owned_windows_candidate(path: Path) -> None:
    if _read_optional_safe_windows_file(path) is not None:
        os.unlink(path)


def _recover_windows_publication(path: Path) -> Literal[
    "none", "aborted", "committed", "manual_target"
]:
    marker_path = path.parent / f".{path.name}{_WINDOWS_PUBLICATION_SUFFIX}"
    staging_path = path.parent / f".{path.name}{_WINDOWS_MARKER_STAGING_SUFFIX}"
    marker_snapshot = _read_optional_safe_windows_file(marker_path)
    if marker_snapshot is None:
        staging_snapshot = _read_optional_safe_windows_file(staging_path)
        if staging_snapshot is not None:
            os.unlink(staging_path)
            _windows_flush_parent(path.parent)
            return "aborted"
        return "none"
    if _read_optional_safe_windows_file(staging_path) is not None:
        _unlink_owned_windows_candidate(staging_path)
        _windows_flush_parent(path.parent)
    marker_stat, marker_content = marker_snapshot
    marker = _parse_windows_publication_marker(path, marker_content)
    marker_descriptor = _windows_publication_descriptor(marker_stat, marker_content)
    candidate_path = path.parent / str(marker["candidate_name"])
    backup_path = path.parent / str(marker["backup_name"])
    try:
        target = _read_optional_safe_windows_file(path)
    except _UnsafeConfigPath:
        target = None
        target_is_manual = path.exists() or path.is_symlink()
    else:
        target_is_manual = target is not None and not _windows_publication_matches(
            target, marker["expected"]
        ) and not _windows_candidate_matches(target, marker["candidate"])
    candidate = _read_optional_safe_windows_file(candidate_path)
    try:
        backup = _read_optional_safe_windows_file(backup_path)
    except _UnsafeConfigPath:
        backup = None
        backup_exists = backup_path.exists() or backup_path.is_symlink()
    else:
        backup_exists = backup is not None

    target_is_expected = _windows_publication_matches(target, marker["expected"])
    target_is_candidate = _windows_candidate_matches(target, marker["candidate"])
    candidate_is_candidate = _windows_candidate_matches(candidate, marker["candidate"])
    backup_is_expected = _windows_publication_matches(backup, marker["expected"])

    if target_is_expected and not backup_exists:
        if candidate is not None:
            _unlink_owned_windows_candidate(candidate_path)
        _unlink_matching_windows_file(marker_path, marker_descriptor)
        _windows_flush_parent(path.parent)
        return "aborted"

    if target_is_candidate and backup_is_expected:
        _unlink_matching_windows_file(backup_path, marker["expected"])
        _unlink_matching_windows_file(marker_path, marker_descriptor)
        _windows_flush_parent(path.parent)
        return "committed"

    if target_is_candidate and not backup_exists:
        _unlink_matching_windows_file(marker_path, marker_descriptor)
        _windows_flush_parent(path.parent)
        return "committed"

    if target_is_candidate and backup_exists:
        if candidate is not None:
            raise _UnsafeConfigPath
        _windows_move_file(backup_path, path, replace=True)
        try:
            restored_target = _read_optional_safe_windows_file(path)
        except _UnsafeConfigPath:
            restored_target = None
            restored_manual_exists = path.exists() or path.is_symlink()
        else:
            restored_manual_exists = restored_target is not None
        if not restored_manual_exists or _windows_candidate_matches(
            restored_target, marker["candidate"]
        ):
            raise _UnsafeConfigPath
        _unlink_matching_windows_file(marker_path, marker_descriptor)
        _windows_flush_parent(path.parent)
        return "manual_target"

    if target_is_manual and candidate_is_candidate and not backup_exists:
        _unlink_matching_windows_candidate(candidate_path, marker["candidate"])
        _unlink_matching_windows_file(marker_path, marker_descriptor)
        _windows_flush_parent(path.parent)
        return "manual_target"

    if target_is_manual and candidate is None and not backup_exists:
        _unlink_matching_windows_file(marker_path, marker_descriptor)
        _windows_flush_parent(path.parent)
        return "manual_target"

    if target_is_manual and candidate is None and backup_is_expected:
        _unlink_matching_windows_file(backup_path, marker["expected"])
        _unlink_matching_windows_file(marker_path, marker_descriptor)
        _windows_flush_parent(path.parent)
        return "manual_target"

    raise _UnsafeConfigPath


def _atomic_write_windows(
    path: Path,
    content: bytes,
    source: _ConfigMutationSource,
) -> None:
    parent_stat = os.lstat(path.parent)
    if (
        not stat.S_ISDIR(parent_stat.st_mode)
        or int(getattr(parent_stat, "st_file_attributes", 0)) & 0x400
    ):
        raise _UnsafeConfigPath
    recovery = _recover_windows_publication(path)
    if recovery == "manual_target":
        raise ConfigAdmissionStaleError("Configuration admission snapshot changed")
    _verify_config_mutation_source(source)
    token = secrets.token_hex(16)
    candidate = path.parent / f".{path.name}{_WINDOWS_CANDIDATE_PREFIX}{token}"
    backup = path.parent / f".{path.name}{_WINDOWS_BACKUP_PREFIX}{token}"
    marker = path.parent / f".{path.name}{_WINDOWS_PUBLICATION_SUFFIX}"
    marker_staging = path.parent / f".{path.name}{_WINDOWS_MARKER_STAGING_SUFFIX}"
    candidate_descriptor = _windows_candidate_descriptor(content)
    marker_descriptor: dict[str, object] | None = None
    try:
        marker_content = _windows_publication_marker_bytes(
            candidate.name,
            backup.name,
            _windows_publication_descriptor(source.identity, source.content),
            candidate_descriptor,
        )
        marker_snapshot = _write_private_windows_file(marker_staging, marker_content)
        marker_descriptor = _windows_publication_descriptor(*marker_snapshot)
        _windows_flush_parent(path.parent)
        _config_mutation_probe("after_windows_marker_staged", path)
        _windows_move_file(marker_staging, marker, replace=False)
        marker_snapshot = _read_safe_windows_file(marker)
        if not _windows_publication_matches(marker_snapshot, marker_descriptor):
            raise _UnsafeConfigPath
        _windows_flush_parent(path.parent)
        _config_mutation_probe("after_windows_marker_durable", path)
        candidate_snapshot = _write_private_windows_file(candidate, content)
        if not _windows_candidate_matches(candidate_snapshot, candidate_descriptor):
            raise _UnsafeConfigPath
        _windows_flush_parent(path.parent)
        candidate_descriptor = _windows_candidate_descriptor(
            content,
            candidate_snapshot[0],
        )
        ready_marker_content = _windows_publication_marker_bytes(
            candidate.name,
            backup.name,
            _windows_publication_descriptor(source.identity, source.content),
            candidate_descriptor,
        )
        ready_marker_snapshot = _write_private_windows_file(
            marker_staging,
            ready_marker_content,
        )
        ready_marker_descriptor = _windows_publication_descriptor(
            *ready_marker_snapshot
        )
        _windows_flush_parent(path.parent)
        _config_mutation_probe("after_windows_ready_marker_staged", path)
        _windows_move_file(marker_staging, marker, replace=True)
        marker_snapshot = _read_safe_windows_file(marker)
        if not _windows_publication_matches(
            marker_snapshot,
            ready_marker_descriptor,
        ):
            raise _UnsafeConfigPath
        marker_descriptor = ready_marker_descriptor
        _windows_flush_parent(path.parent)
        _config_mutation_probe("after_windows_candidate_durable", path)
        current_parent = os.lstat(path.parent)
        if (
            not stat.S_ISDIR(current_parent.st_mode)
            or _safe_file_identity(current_parent) != _safe_file_identity(parent_stat)
            or int(getattr(current_parent, "st_file_attributes", 0)) & 0x400
        ):
            raise _UnsafeConfigPath
        _windows_replace_file(path, candidate, backup)
        _config_mutation_probe("after_windows_replace", path)
        disposition = _recover_windows_publication(path)
        if disposition == "committed":
            return
        if disposition == "manual_target":
            raise ConfigAdmissionStaleError("Configuration admission snapshot changed")
        raise _PostReplaceDurabilityError
    except ConfigAdmissionStaleError:
        raise
    except Exception as exc:
        try:
            disposition = _recover_windows_publication(path)
        except (_UnsafeConfigPath, OSError) as recovery_error:
            raise _PostReplaceDurabilityError from recovery_error
        if disposition == "committed":
            return
        if disposition == "manual_target":
            raise ConfigAdmissionStaleError(
                "Configuration admission snapshot changed"
            ) from exc
        raise
    finally:
        if marker_descriptor is None:
            with suppress(OSError, _UnsafeConfigPath):
                _unlink_matching_windows_candidate(candidate, candidate_descriptor)


def _publish_config_mutation(source: _ConfigMutationSource, content: bytes) -> None:
    if os.name == "nt":
        try:
            _atomic_write_windows(source.path, content, source)
        except _UnsafeConfigPath as exc:
            raise ConfigError("Configuration path is unsafe") from exc
        return
    assert source.parent_fd is not None
    assert source.name is not None
    assert source.parent is not None
    try:
        _durable_exchange_at(
            source.parent_fd,
            source.name,
            content,
            source.identity,
            source.content,
            parent_path=source.parent.path,
            before_replace=lambda: _verify_config_mutation_source(source),
        )
    except NtfyDisclosureConflictError as exc:
        raise ConfigAdmissionStaleError("Configuration admission snapshot changed") from exc


def _atomic_create(path: Path, content: str) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise ConfigAlreadyExistsError("Configuration already exists") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _initialization_probe(_stage: str) -> None:
    """Test seam for first-run parent replacement probes."""


def _unlink_exact_at(parent_fd: int, name: str, identity: tuple[int, int]) -> None:
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return
    if _safe_file_identity(current) != identity:
        return
    try:
        os.unlink(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except OSError:
        pass


def _atomic_create_at(
    parent_fd: int,
    parent: _HeldParent,
    name: str,
    content: bytes,
) -> tuple[int, int]:
    candidate_fd: int | None = None
    candidate_identity: tuple[int, int] | None = None
    published = False
    try:
        candidate_fd, candidate_stat = _prepare_unnamed_candidate_at(
            parent_fd,
            parent,
            content,
        )
        candidate_identity = _safe_file_identity(candidate_stat)
        _initialization_probe("before_publish")
        _validate_held_parent(parent_fd, parent)
        try:
            _link_unnamed_candidate_at(candidate_fd, parent_fd, name)
        except FileExistsError as exc:
            raise ConfigAlreadyExistsError("Configuration already exists") from exc
        published = True
        _initialization_probe("after_publish")
        _validate_held_parent(parent_fd, parent)
        os.fsync(parent_fd)
        _initialization_probe("after_publish_durable")
        return candidate_identity
    except Exception:
        if published and candidate_identity is not None:
            _unlink_exact_at(parent_fd, name, candidate_identity)
        raise
    finally:
        if candidate_fd is not None:
            os.close(candidate_fd)


def initialize_config(
    path: Path,
    *,
    timezone: str,
    model_base_url: str,
    model_name: str,
    desktop_initialization_receipt: str | None = None,
) -> Config:
    normalized_timezone = timezone.strip()
    if not normalized_timezone:
        raise InvalidConfigInitializationError("timezone must be a non-empty string")
    try:
        ZoneInfo(normalized_timezone)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise InvalidConfigInitializationError(f"Unknown timezone: {normalized_timezone}") from exc

    try:
        normalized_base_url = validate_model_base_url(model_base_url)
    except ConfigError as exc:
        raise InvalidConfigInitializationError(str(exc)) from exc
    normalized_model_name = model_name.strip()
    if not normalized_model_name or any(
        not character.isprintable() for character in normalized_model_name
    ):
        raise InvalidConfigInitializationError("model_name must be a non-empty printable string")
    initialization_receipt = (
        secrets.token_hex(16)
        if desktop_initialization_receipt is None
        else desktop_initialization_receipt
    )
    if INITIALIZATION_RECEIPT_RE.fullmatch(initialization_receipt) is None:
        raise InvalidConfigInitializationError(
            "desktop_initialization_receipt must be 32 lowercase hexadecimal characters"
        )

    initial = document()
    initial["timezone"] = normalized_timezone
    initial["poll_interval_minutes"] = DEFAULT_POLL_INTERVAL_MINUTES
    initial["retention_days"] = DEFAULT_RETENTION_DAYS
    initial["model_backend"] = "loopback"
    initial["model_base_url"] = normalized_base_url
    initial["model_name"] = normalized_model_name
    initial["model_require_auth"] = False
    initial["notifications_enabled"] = True
    initial["desktop_initialization_receipt"] = initialization_receipt
    content = dumps(initial).encode("utf-8")

    if os.name != "posix":
        config_path = path.expanduser().resolve()
        config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with _config_serialization_lock():
            _atomic_create(config_path, content.decode("utf-8"))
        return load_config(config_path)

    parent_fd: int | None = None
    created_identity: tuple[int, int] | None = None
    try:
        with _config_serialization_lock():
            config_path, parent_fd, name = _open_or_create_safe_parent(path)
            held_parent = _validate_held_parent(
                parent_fd,
                parent_path=config_path.parent,
            )
            _initialization_probe("before_create")
            _validate_held_parent(parent_fd, held_parent)
            created_identity = _atomic_create_at(parent_fd, held_parent, name, content)
            try:
                _validate_held_parent(parent_fd, held_parent)
                file_fd, file_stat, written = _read_safe_file_at(parent_fd, name)
                try:
                    if (
                        _safe_file_identity(file_stat) != created_identity
                        or written != content
                    ):
                        raise _UnsafeConfigPath
                finally:
                    os.close(file_fd)
                _validate_held_parent(parent_fd, held_parent)
                return _load_config_bytes(written, config_path)
            except Exception:
                _unlink_exact_at(parent_fd, name, created_identity)
                raise
    except ConfigAlreadyExistsError:
        raise
    except (_MissingConfigPath, _UnsafeConfigPath) as exc:
        raise ConfigError(
            "Configuration is unavailable or requires manual repair"
        ) from exc
    finally:
        if parent_fd is not None:
            os.close(parent_fd)


def _sender_table(sender: Sender, *, inline: bool):
    item = inline_table() if inline else table()
    item["email"] = sender.email
    if sender.name:
        item["name"] = sender.name
    return item


def add_sender(path: Path, email: str, name: str | None = None) -> Sender:
    sender = _sender(email, name, invalid_message="email must be a valid email address")
    with _config_serialization_lock(), _config_mutation_source(path) as source:
        config = _load_config_bytes(source.content, source.path)
        if sender.email in config.allowlist:
            raise DuplicateSenderError(f"Sender is already watched: {sender.email}")
        document = parse(source.content.decode("utf-8"))
        sender_items = document.get("senders")
        if sender_items is None or isinstance(sender_items, Array) and not sender_items:
            sender_items = aot()
            document["senders"] = sender_items
        if isinstance(sender_items, AoT):
            sender_items.append(_sender_table(sender, inline=False))
        elif isinstance(sender_items, Array):
            sender_items.append(_sender_table(sender, inline=True))
        else:
            raise ConfigError("senders must be a list of tables")
        _publish_config_mutation(source, dumps(document).encode("utf-8"))
    return sender


def remove_sender(path: Path, email: str) -> Sender:
    requested = _sender(
        email,
        None,
        invalid_message="email must be a valid email address",
        enforce_selector_limit=False,
    )
    with _config_serialization_lock(), _config_mutation_source(path) as source:
        config = _load_config_bytes(source.content, source.path)
        try:
            index = next(
                index
                for index, sender in enumerate(config.senders)
                if sender.email == requested.email
            )
        except StopIteration as exc:
            raise SenderNotFoundError(f"Sender is not watched: {requested.email}") from exc
        removed = config.senders[index]
        document = parse(source.content.decode("utf-8"))
        sender_items = document.get("senders")
        if not isinstance(sender_items, (AoT, Array)):
            raise ConfigError("senders must be a list of tables")
        del sender_items[index]
        _publish_config_mutation(source, dumps(document).encode("utf-8"))
    return removed


def update_settings(path: Path, updates: Mapping[str, object]) -> Config:
    if not updates:
        raise InvalidSettingsUpdateError("At least one setting must be provided")
    unknown = set(updates) - MUTABLE_DESKTOP_SETTINGS
    if unknown:
        fields = ", ".join(sorted(str(field) for field in unknown))
        raise InvalidSettingsUpdateError(f"Unsupported settings: {fields}")

    poll_interval = updates.get("poll_interval_minutes")
    if "poll_interval_minutes" in updates:
        if type(poll_interval) is not int:
            raise InvalidSettingsUpdateError("poll_interval_minutes must be an integer")
        if not 1 <= poll_interval <= 1440:
            raise InvalidSettingsUpdateError("poll_interval_minutes must be between 1 and 1440")

    retention = updates.get("retention_days")
    if "retention_days" in updates:
        if type(retention) is not int:
            raise InvalidSettingsUpdateError("retention_days must be an integer")
        if not MIN_RETENTION_DAYS <= retention <= MAX_RETENTION_DAYS:
            raise InvalidSettingsUpdateError(
                f"retention_days must be between {MIN_RETENTION_DAYS} and {MAX_RETENTION_DAYS}"
            )

    notifications = updates.get("notifications_enabled")
    if "notifications_enabled" in updates and type(notifications) is not bool:
        raise InvalidSettingsUpdateError("notifications_enabled must be a boolean")

    normalized_updates = dict(updates)
    model_base_url = updates.get("model_base_url")
    if "model_base_url" in updates:
        if not isinstance(model_base_url, str):
            raise InvalidSettingsUpdateError("model_base_url must be a string")
        try:
            normalized_updates["model_base_url"] = validate_model_base_url(model_base_url)
        except ConfigError as exc:
            raise InvalidSettingsUpdateError(str(exc)) from exc

    model_name = updates.get("model_name")
    if "model_name" in updates:
        if not isinstance(model_name, str):
            raise InvalidSettingsUpdateError("model_name must be a string")
        normalized_model_name = model_name.strip()
        if not normalized_model_name or any(
            not character.isprintable() for character in normalized_model_name
        ):
            raise InvalidSettingsUpdateError("model_name must be a non-empty printable string")
        normalized_updates["model_name"] = normalized_model_name

    with _config_serialization_lock(), _config_mutation_source(path) as source:
        config = _load_config_bytes(source.content, source.path)
        if {
            "model_base_url",
            "model_name",
        } & updates.keys() and config.model_backend != "loopback":
            raise InvalidSettingsUpdateError(
                "Model endpoint and identifier are managed by the inference gateway"
            )
        document = parse(source.content.decode("utf-8"))
        for key, value in normalized_updates.items():
            document[key] = value
        candidate = dumps(document).encode("utf-8")
        updated = _load_config_bytes(candidate, source.path)
        _publish_config_mutation(source, candidate)
        return updated


def secure_runtime_paths(
    config: Config, *, secure_config_path: bool = True
) -> None:
    for path in {
        config.path.parent,
        config.gmail_token_file.parent,
        config.gmail_send_token_file.parent,
        config.database_file.parent,
    }:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if stat.S_IMODE(path.stat().st_mode) != 0o700:
            path.chmod(0o700)
    if (
        secure_config_path
        and config.path.exists()
        and stat.S_IMODE(config.path.stat().st_mode) != 0o600
    ):
        config.path.chmod(0o600)
