from pathlib import Path

import pytest

from eom_email_watcher.config import ConfigError, load_config, normalize_address


def write_config(
    path: Path, *, base_url: str = "http://127.0.0.1:1234/v1", extra: str = ""
) -> None:
    path.write_text(
        f'''model_base_url = "{base_url}"
model_name = "local-model"
model_api_token_file = "{path.parent / "lm-token"}"
database_file = "{path.parent / "db.sqlite3"}"
gmail_credentials_file = "{path.parent / "credentials.json"}"
gmail_token_file = "{path.parent / "token.json"}"
{extra}
[[senders]]
email = "Trusted@Example.com"
name = "Trusted Person"
''',
        encoding="utf-8",
    )


def test_config_normalizes_exact_sender(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    config = load_config(path)
    assert config.allowlist == frozenset({"trusted@example.com"})
    assert normalize_address("Person <TRUSTED@example.com>") == "trusted@example.com"


def test_remote_model_url_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path, base_url="https://api.example.com/v1")
    with pytest.raises(ConfigError, match="localhost"):
        load_config(path)


def test_duplicate_sender_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    with path.open("a", encoding="utf-8") as stream:
        stream.write('[[senders]]\nemail = "trusted@example.com"\n')
    with pytest.raises(ConfigError, match="Duplicate"):
        load_config(path)


def test_ntfy_defaults_to_disabled(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    config = load_config(path)
    assert config.ntfy_topic is None
    assert config.ntfy_url == "https://ntfy.sh"


def test_short_ntfy_topic_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path, extra='ntfy_topic = "too-short"')
    with pytest.raises(ConfigError, match="ntfy_topic"):
        load_config(path)


def test_valid_ntfy_topic_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path, extra='ntfy_topic = "eom-email-watch-0123456789ab"')
    config = load_config(path)
    assert config.ntfy_topic == "eom-email-watch-0123456789ab"


def test_ntfy_url_must_be_https(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(
        path,
        extra=(
            'ntfy_topic = "eom-email-watch-0123456789ab"\n'
            'ntfy_url = "http://ntfy.sh"'
        ),
    )
    with pytest.raises(ConfigError, match="https"):
        load_config(path)
