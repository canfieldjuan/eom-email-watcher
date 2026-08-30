from __future__ import annotations

import os
import re
import tempfile
import tomllib
from dataclasses import dataclass
from email.utils import parseaddr
from ipaddress import ip_address
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from filelock import FileLock
from tomlkit import aot, dumps, inline_table, parse, table
from tomlkit.items import AoT, Array

DEFAULT_CONFIG = Path("~/.config/eom-email-watcher/config.toml").expanduser()
DEFAULT_STATE = Path("~/.local/state/eom-email-watcher").expanduser()
DEFAULT_POLL_INTERVAL_MINUTES = 120
NTFY_TOPIC_RE = re.compile(r"^[-_A-Za-z0-9]{20,64}$")
DOMAIN_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
GATEWAY_MODEL_LABEL = "Managed by inference gateway"


class ConfigError(ValueError):
    """Configuration is missing or unsafe."""


class InvalidSenderError(ConfigError):
    """A proposed watchlist sender is not valid."""


class DuplicateSenderError(ConfigError):
    """A proposed watchlist sender already exists."""


class SenderNotFoundError(ConfigError):
    """A requested watchlist sender does not exist."""


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
    senders: tuple[Sender, ...]

    @property
    def allowlist(self) -> frozenset[str]:
        return frozenset(sender.email for sender in self.senders)

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def _path(value: object, key: str) -> Path:
    if isinstance(value, Path):
        return value.expanduser()
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ConfigError(f"{key} must be a non-empty path")
    return Path(os.path.expandvars(value)).expanduser()


def normalize_address(value: str) -> str:
    _name, address = parseaddr(value)
    return address.strip().casefold()


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
    email = normalize_address(email_value)
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
        raise InvalidSenderError(invalid_message)
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


def load_config(path: Path | None = None) -> Config:
    config_path = (path or DEFAULT_CONFIG).expanduser()
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(
            f"Configuration not found: {config_path}. Copy config.example.toml and edit it."
        ) from exc
    except tomllib.TOMLDecodeError as exc:
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
    retention = _integer_setting(data, "retention_days", 180)
    poll_interval = _integer_setting(
        data, "poll_interval_minutes", DEFAULT_POLL_INTERVAL_MINUTES
    )
    timeout = _float_setting(data, "model_timeout_seconds", 60)
    if not 1_000 <= body_limit <= 100_000:
        raise ConfigError("body_char_limit must be between 1000 and 100000")
    if not 1 <= retention <= 3650:
        raise ConfigError("retention_days must be between 1 and 3650")
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
        senders=tuple(senders),
    )


def _atomic_write(path: Path, content: str) -> None:
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
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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
