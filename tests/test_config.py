from pathlib import Path

import pytest

from eom_email_watcher.config import ConfigError, load_config, normalize_address


def write_config(path: Path, *, base_url: str = "http://127.0.0.1:1234/v1") -> None:
    path.write_text(
        f'''model_base_url = "{base_url}"
model_name = "local-model"
model_api_token_file = "{path.parent / "lm-token"}"
database_file = "{path.parent / "db.sqlite3"}"
gmail_credentials_file = "{path.parent / "credentials.json"}"
gmail_token_file = "{path.parent / "token.json"}"
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
