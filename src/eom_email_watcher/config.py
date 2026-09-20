from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import re
import secrets
import stat
import tempfile
import tomllib
from collections.abc import Callable, Mapping
from contextlib import suppress
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
DEFAULT_STATE = Path("~/.local/state/eom-email-watcher").expanduser()
DEFAULT_POLL_INTERVAL_MINUTES = 120
DEFAULT_RETENTION_DAYS = 180
MIN_RETENTION_DAYS = 1
MAX_RETENTION_DAYS = 3650
NTFY_TOPIC_RE = re.compile(r"^[-_A-Za-z0-9]{20,64}$")
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


class _MissingConfigPath(Exception):
    """The configured path does not exist."""


class _UnsafeConfigPath(Exception):
    """The configured path cannot be accessed under the migration policy."""


class _PostReplaceDurabilityError(OSError):
    """Atomic replacement returned, but a later durability step failed."""


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

    @property
    def allowlist(self) -> frozenset[str]:
        return frozenset(sender.email for sender in self.senders)

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


def _path(value: object, key: str) -> Path:
    if isinstance(value, Path):
        return value.expanduser()
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ConfigError(f"{key} must be a non-empty path")
    return Path(os.path.expandvars(value)).expanduser()


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


def _sender(email_value: str, name_value: str | None, *, invalid_message: str) -> Sender:
    try:
        email = normalize_validated_address(email_value)
    except ValueError as exc:
        raise InvalidSenderError(invalid_message) from exc
    if name_value is not None and any(
        character in "\r\n" or not character.isprintable() for character in name_value
    ):
        raise InvalidSenderError("sender name must not contain control characters")
    name = name_value.strip() if name_value and name_value.strip() else None
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
            )
        except InvalidSenderError as exc:
            raise ConfigError(str(exc)) from exc
        if sender.email in seen:
            raise ConfigError(f"Duplicate sender: {sender.email}")
        seen.add(sender.email)
        senders.append(sender)

    body_limit = _integer_setting(data, "body_char_limit", 20_000)
    retention = _integer_setting(data, "retention_days", DEFAULT_RETENTION_DAYS)
    poll_interval = _integer_setting(
        data, "poll_interval_minutes", DEFAULT_POLL_INTERVAL_MINUTES
    )
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

    return Config(
        path=config_path,
        timezone=timezone,
        body_char_limit=body_limit,
        retention_days=retention,
        poll_interval_minutes=poll_interval,
        gmail_credentials_file=_path(
            data.get("gmail_credentials_file", DEFAULT_STATE / "credentials.json"),
            "gmail_credentials_file",
        ),
        microsoft_credentials_file=_path(
            data.get(
                "microsoft_credentials_file",
                DEFAULT_STATE / "microsoft-oauth-client.json",
            ),
            "microsoft_credentials_file",
        ),
        gmail_token_file=_path(
            data.get("gmail_token_file", DEFAULT_STATE / "token.json"),
            "gmail_token_file",
        ),
        gmail_send_token_file=_path(
            data.get("gmail_send_token_file", DEFAULT_STATE / "send-token.json"),
            "gmail_send_token_file",
        ),
        monthly_hours_recipient=(
            normalize_address(str(data["monthly_hours_recipient"]))
            if data.get("monthly_hours_recipient")
            else None
        ),
        database_file=_path(
            data.get("database_file", DEFAULT_STATE / "watcher.sqlite3"),
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
    )


def load_config(path: Path | None = None) -> Config:
    config_path = (path or DEFAULT_CONFIG).expanduser()
    try:
        content = config_path.read_bytes()
    except FileNotFoundError as exc:
        raise ConfigError(
            f"Configuration not found: {config_path}. Copy config.example.toml and edit it."
        ) from exc
    return _load_config_bytes(content, config_path)


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


def _open_safe_parent(path: Path) -> tuple[Path, int, str]:
    if os.name != "posix" or not hasattr(os, "getuid"):
        raise _UnsafeConfigPath
    absolute = _absolute_lexical_path(path)
    if not absolute.name:
        raise _UnsafeConfigPath
    current_fd = os.open("/", _directory_open_flags())
    try:
        for component in absolute.parent.parts[1:]:
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


def _read_fd_bytes(file_fd: int) -> bytes:
    os.lseek(file_fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while chunk := os.read(file_fd, 64 * 1024):
        chunks.append(chunk)
    return b"".join(chunks)


def _safe_file_identity(file_stat: os.stat_result) -> tuple[int, int]:
    return file_stat.st_dev, file_stat.st_ino


def _safe_file_version(file_stat: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )


def _read_safe_file_at(parent_fd: int, name: str) -> tuple[int, os.stat_result, bytes]:
    try:
        inspected = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise _MissingConfigPath from exc
    except OSError as exc:
        raise _UnsafeConfigPath from exc
    if (
        not stat.S_ISREG(inspected.st_mode)
        or inspected.st_nlink != 1
        or inspected.st_uid != os.geteuid()
        or stat.S_IMODE(inspected.st_mode) != 0o600
    ):
        raise _UnsafeConfigPath
    try:
        file_fd = os.open(name, _file_open_flags(), dir_fd=parent_fd)
    except OSError as exc:
        raise _UnsafeConfigPath from exc
    try:
        opened = os.fstat(file_fd)
        if _safe_file_identity(opened) != _safe_file_identity(inspected):
            raise _UnsafeConfigPath
        content = _read_fd_bytes(file_fd)
        completed = os.fstat(file_fd)
        if _safe_file_version(completed) != _safe_file_version(opened):
            raise _UnsafeConfigPath
        return file_fd, completed, content
    except Exception:
        os.close(file_fd)
        raise


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
        parsed = tomllib.loads(
            statement_key.decode("utf-8") + " = false"
        )
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
            if multiline and content[index : index + 3] == marker:
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
                    spans.append(
                        (statement_start + value_start, statement_start + value_start + 5)
                    )
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
        not isinstance(raw_topic, str)
        or NTFY_TOPIC_RE.fullmatch(raw_topic.strip()) is None
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
        return NtfyDisclosureStatus(
            "acknowledgement_required", _revision(content)
        ), candidate

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
        status, _candidate = _classify_ntfy_disclosure(content, config_path)
        if status.state == "acknowledgement_required":
            return NtfyDisclosureStatus("manual_repair_required")
        return status
    try:
        handle = _open_safe_config(path)
    except _MissingConfigPath:
        return NtfyDisclosureStatus("missing")
    except (OSError, _UnsafeConfigPath):
        return NtfyDisclosureStatus("manual_repair_required")
    try:
        try:
            status, _candidate = _classify_ntfy_disclosure(handle.content, handle.path)
            return status
        except Exception:
            return NtfyDisclosureStatus("manual_repair_required")
    finally:
        handle.close()


def _rename_exchange_at(parent_fd: int, first: str, second: str) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(library, "renameat2", None)
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


def _reconcile_failed_exchange_rollback(
    parent_fd: int,
    name: str,
    temporary_name: str,
    candidate_identity: tuple[int, int],
    candidate_content: bytes,
) -> bool:
    destination_fd: int | None = None
    temporary_fd: int | None = None
    try:
        destination_fd, destination_stat, destination_content = _read_safe_file_at(
            parent_fd, name
        )
        temporary_fd, temporary_stat, temporary_content = _read_safe_file_at(
            parent_fd, temporary_name
        )
        destination_is_candidate = (
            _safe_file_identity(destination_stat) == candidate_identity
            and destination_content == candidate_content
        )
        temporary_is_candidate = (
            _safe_file_identity(temporary_stat) == candidate_identity
            and temporary_content == candidate_content
        )
        if destination_is_candidate and not temporary_is_candidate:
            destination_now = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            temporary_now = os.stat(
                temporary_name, dir_fd=parent_fd, follow_symlinks=False
            )
            if (
                _safe_file_version(destination_now)
                != _safe_file_version(destination_stat)
                or _safe_file_version(temporary_now)
                != _safe_file_version(temporary_stat)
            ):
                return False
            os.replace(
                temporary_name,
                name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            os.fsync(parent_fd)
            return True
        if temporary_is_candidate and not destination_is_candidate:
            temporary_now = os.stat(
                temporary_name, dir_fd=parent_fd, follow_symlinks=False
            )
            if _safe_file_version(temporary_now) != _safe_file_version(temporary_stat):
                return False
            os.unlink(temporary_name, dir_fd=parent_fd)
            os.fsync(parent_fd)
            return True
        return False
    except (OSError, _MissingConfigPath, _UnsafeConfigPath):
        return False
    finally:
        if destination_fd is not None:
            os.close(destination_fd)
        if temporary_fd is not None:
            os.close(temporary_fd)


def _durable_replace_at(
    parent_fd: int,
    name: str,
    content: bytes,
    *,
    before_replace: Callable[[], None] | None = None,
    validate_displaced: Callable[[int, str], None] | None = None,
) -> None:
    temporary_name = f".{name}.{secrets.token_hex(16)}.tmp"
    temporary_fd: int | None = None
    replaced = False
    preserve_temporary = False
    candidate_identity: tuple[int, int] | None = None
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
        candidate_identity = _safe_file_identity(os.fstat(temporary_fd))
        os.close(temporary_fd)
        temporary_fd = None
        if before_replace is not None:
            before_replace()
        if validate_displaced is None:
            os.replace(
                temporary_name,
                name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            replaced = True
        else:
            _rename_exchange_at(parent_fd, temporary_name, name)
            replaced = True
            try:
                validate_displaced(parent_fd, temporary_name)
            except Exception as validation_error:
                try:
                    _rename_exchange_at(parent_fd, temporary_name, name)
                    replaced = False
                    os.fsync(parent_fd)
                except Exception as rollback_error:
                    if candidate_identity is None or not _reconcile_failed_exchange_rollback(
                        parent_fd,
                        name,
                        temporary_name,
                        candidate_identity,
                        content,
                    ):
                        preserve_temporary = True
                    else:
                        replaced = False
                    raise _PostReplaceDurabilityError from rollback_error
                if isinstance(validation_error, NtfyDisclosureConflictError):
                    raise
                raise _PostReplaceDurabilityError from validation_error
        try:
            os.fsync(parent_fd)
        except OSError as exc:
            raise _PostReplaceDurabilityError from exc
        if validate_displaced is not None:
            os.unlink(temporary_name, dir_fd=parent_fd)
            replaced = False
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
        if not preserve_temporary:
            with suppress(OSError):
                os.unlink(temporary_name, dir_fd=parent_fd)


def acknowledge_ntfy_disclosure(path: Path, expected_revision: str) -> None:
    if os.name != "posix":
        raise NtfyDisclosureConflictError("Disclosure acknowledgement is unavailable")
    try:
        handle = _open_safe_config(path)
    except (_MissingConfigPath, _UnsafeConfigPath, OSError) as exc:
        raise NtfyDisclosureConflictError(
            "Configuration is not eligible for acknowledgement"
        ) from exc
    replacement_completed = False
    try:
        with FileLock(f"{handle.path}.lock"):
            os.close(handle.file_fd)
            handle.file_fd = -1
            try:
                current_fd, current_identity, current = _read_safe_file_at(
                    handle.parent_fd, handle.name
                )
            except (_MissingConfigPath, _UnsafeConfigPath, OSError) as exc:
                raise NtfyDisclosureConflictError(
                    "Configuration is not eligible for acknowledgement"
                ) from exc
            handle.file_fd = current_fd
            handle.identity = current_identity
            handle.content = current
            if _revision(current) != expected_revision:
                raise NtfyDisclosureConflictError("Configuration revision changed")
            status, candidate = _classify_ntfy_disclosure(current, handle.path)
            if status.state != "acknowledgement_required" or candidate is None:
                raise NtfyDisclosureConflictError(
                    "Configuration is not eligible for acknowledgement"
                )
            try:
                _load_config_bytes(candidate, handle.path)
            except ConfigError as exc:
                raise NtfyDisclosureConflictError(
                    "Configuration is not eligible for acknowledgement"
                ) from exc

            def verify_precommit() -> None:
                try:
                    verify_fd, verify_identity, verify_content = _read_safe_file_at(
                        handle.parent_fd, handle.name
                    )
                except (_MissingConfigPath, _UnsafeConfigPath, OSError) as exc:
                    raise NtfyDisclosureConflictError(
                        "Configuration revision changed"
                    ) from exc
                try:
                    if (
                        _safe_file_identity(verify_identity)
                        != _safe_file_identity(handle.identity)
                        or verify_content != current
                    ):
                        raise NtfyDisclosureConflictError("Configuration revision changed")
                finally:
                    os.close(verify_fd)

            def validate_displaced(parent_fd: int, displaced_name: str) -> None:
                try:
                    displaced_fd, displaced_identity, displaced_content = (
                        _read_safe_file_at(parent_fd, displaced_name)
                    )
                except _UnsafeConfigPath as exc:
                    raise NtfyDisclosureConflictError(
                        "Configuration revision changed"
                    ) from exc
                except (_MissingConfigPath, OSError) as exc:
                    raise NtfyDisclosureOutcomeUnknownError(
                        "Disclosure acknowledgement outcome is unknown"
                    ) from exc
                try:
                    if (
                        _safe_file_identity(displaced_identity)
                        != _safe_file_identity(handle.identity)
                        or displaced_content != current
                        or _revision(displaced_content) != expected_revision
                    ):
                        raise NtfyDisclosureConflictError(
                            "Configuration revision changed"
                        )
                finally:
                    os.close(displaced_fd)

            try:
                _durable_replace_at(
                    handle.parent_fd,
                    handle.name,
                    candidate,
                    before_replace=verify_precommit,
                    validate_displaced=validate_displaced,
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
                final_handle = _open_safe_config(handle.path)
                try:
                    if final_handle.content != candidate:
                        raise ConfigError("final configuration did not match candidate")
                finally:
                    final_handle.close()
                load_config(handle.path)
                confirmed_handle = _open_safe_config(handle.path)
                try:
                    if confirmed_handle.content != candidate:
                        raise ConfigError("final configuration did not match candidate")
                finally:
                    confirmed_handle.close()
            except (ConfigError, OSError, _MissingConfigPath, _UnsafeConfigPath) as exc:
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
        if handle.file_fd >= 0:
            os.close(handle.file_fd)
        os.close(handle.parent_fd)


def _atomic_write(path: Path, content: str) -> None:
    parent_fd = os.open(path.parent, _directory_open_flags())
    try:
        _durable_replace_at(parent_fd, path.name, content.encode("utf-8"))
    finally:
        os.close(parent_fd)


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


def initialize_config(
    path: Path,
    *,
    timezone: str,
    model_base_url: str,
    model_name: str,
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

    config_path = path.expanduser().resolve()
    config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    initial = document()
    initial["timezone"] = normalized_timezone
    initial["poll_interval_minutes"] = DEFAULT_POLL_INTERVAL_MINUTES
    initial["retention_days"] = DEFAULT_RETENTION_DAYS
    initial["model_backend"] = "loopback"
    initial["model_base_url"] = normalized_base_url
    initial["model_name"] = normalized_model_name
    initial["model_require_auth"] = False
    initial["notifications_enabled"] = True
    with FileLock(f"{config_path}.lock"):
        _atomic_create(config_path, dumps(initial))
    return load_config(config_path)


def _sender_table(sender: Sender, *, inline: bool):
    item = inline_table() if inline else table()
    item["email"] = sender.email
    if sender.name:
        item["name"] = sender.name
    return item


def add_sender(path: Path, email: str, name: str | None = None) -> Sender:
    sender = _sender(email, name, invalid_message="email must be a valid email address")
    config_path = path.expanduser().resolve()
    with FileLock(f"{config_path}.lock"):
        config = load_config(config_path)
        if sender.email in config.allowlist:
            raise DuplicateSenderError(f"Sender is already watched: {sender.email}")
        document = parse(config_path.read_text(encoding="utf-8"))
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
        _atomic_write(config_path, dumps(document))
    return sender


def remove_sender(path: Path, email: str) -> Sender:
    requested = _sender(email, None, invalid_message="email must be a valid email address")
    config_path = path.expanduser().resolve()
    with FileLock(f"{config_path}.lock"):
        config = load_config(config_path)
        try:
            index = next(
                index
                for index, sender in enumerate(config.senders)
                if sender.email == requested.email
            )
        except StopIteration as exc:
            raise SenderNotFoundError(f"Sender is not watched: {requested.email}") from exc
        removed = config.senders[index]
        document = parse(config_path.read_text(encoding="utf-8"))
        sender_items = document.get("senders")
        if not isinstance(sender_items, (AoT, Array)):
            raise ConfigError("senders must be a list of tables")
        del sender_items[index]
        _atomic_write(config_path, dumps(document))
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
            raise InvalidSettingsUpdateError(
                "poll_interval_minutes must be between 1 and 1440"
            )

    retention = updates.get("retention_days")
    if "retention_days" in updates:
        if type(retention) is not int:
            raise InvalidSettingsUpdateError("retention_days must be an integer")
        if not MIN_RETENTION_DAYS <= retention <= MAX_RETENTION_DAYS:
            raise InvalidSettingsUpdateError(
                f"retention_days must be between {MIN_RETENTION_DAYS} "
                f"and {MAX_RETENTION_DAYS}"
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
            raise InvalidSettingsUpdateError(
                "model_name must be a non-empty printable string"
            )
        normalized_updates["model_name"] = normalized_model_name

    config_path = path.expanduser().resolve()
    try:
        with FileLock(f"{config_path}.lock"):
            config = load_config(config_path)
            if (
                {"model_base_url", "model_name"} & updates.keys()
                and config.model_backend != "loopback"
            ):
                raise InvalidSettingsUpdateError(
                    "Model endpoint and identifier are managed by the inference gateway"
                )
            document = parse(config_path.read_text(encoding="utf-8"))
            for key, value in normalized_updates.items():
                document[key] = value
            _atomic_write(config_path, dumps(document))
            return load_config(config_path)
    except FileNotFoundError:
        load_config(config_path)
        raise


def secure_runtime_paths(config: Config) -> None:
    for path in {
        config.path.parent,
        config.gmail_token_file.parent,
        config.gmail_send_token_file.parent,
        config.database_file.parent,
    }:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
    if config.path.exists():
        config.path.chmod(0o600)
