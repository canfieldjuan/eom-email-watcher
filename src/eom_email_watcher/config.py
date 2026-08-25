from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from email.utils import parseaddr
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_CONFIG = Path("~/.config/eom-email-watcher/config.toml").expanduser()
DEFAULT_STATE = Path("~/.local/state/eom-email-watcher").expanduser()
NTFY_TOPIC_RE = re.compile(r"^[-_A-Za-z0-9]{20,64}$")


class ConfigError(ValueError):
    """Configuration is missing or unsafe."""


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
    gmail_credentials_file: Path
    gmail_token_file: Path
    gmail_send_token_file: Path
    monthly_hours_recipient: str | None
    database_file: Path
    model_base_url: str
    model_name: str
    model_api_token_file: Path | None
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
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} must be a non-empty path")
    return Path(os.path.expandvars(value)).expanduser()


def normalize_address(value: str) -> str:
    _name, address = parseaddr(value)
    return address.strip().casefold()


def _validate_model_base_url(value: object) -> str:
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
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"Unknown timezone: {timezone}") from exc

    raw_senders = data.get("senders")
    if not isinstance(raw_senders, list) or not raw_senders:
        raise ConfigError("At least one [[senders]] entry is required")
    senders: list[Sender] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_senders, start=1):
        if not isinstance(raw, dict):
            raise ConfigError(f"senders entry {index} must be a table")
        email = normalize_address(str(raw.get("email", "")))
        if not email or "@" not in email:
            raise ConfigError(f"senders entry {index} has an invalid email")
        if email in seen:
            raise ConfigError(f"Duplicate sender: {email}")
        seen.add(email)
        name = raw.get("name")
        if name is not None and not isinstance(name, str):
            raise ConfigError(f"senders entry {index} name must be a string")
        senders.append(Sender(email=email, name=name.strip() if name else None))

    body_limit = int(data.get("body_char_limit", 20_000))
    retention = int(data.get("retention_days", 180))
    timeout = float(data.get("model_timeout_seconds", 60))
    if not 1_000 <= body_limit <= 100_000:
        raise ConfigError("body_char_limit must be between 1000 and 100000")
    if not 1 <= retention <= 3650:
        raise ConfigError("retention_days must be between 1 and 3650")
    if not 1 <= timeout <= 300:
        raise ConfigError("model_timeout_seconds must be between 1 and 300")

    base_url = _validate_model_base_url(
        data.get("model_base_url", "http://127.0.0.1:1234/v1")
    )
    model_name = data.get("model_name")
    if not isinstance(model_name, str) or not model_name.strip():
        raise ConfigError("model_name must be set")
    require_auth = bool(data.get("model_require_auth", True))
    raw_token_file = data.get("model_api_token_file")
    token_file = _path(raw_token_file, "model_api_token_file") if raw_token_file else None
    if require_auth and token_file is None:
        raise ConfigError("model_api_token_file is required when model_require_auth is true")

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
        model_base_url=base_url,
        model_name=model_name.strip(),
        model_api_token_file=token_file,
        model_require_auth=require_auth,
        model_timeout_seconds=timeout,
        notifications_enabled=bool(data.get("notifications_enabled", True)),
        ntfy_topic=ntfy_topic,
        ntfy_url=ntfy_url,
        senders=tuple(senders),
    )


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
