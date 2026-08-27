from pathlib import Path

import pytest

from eom_email_watcher.config import (
    ConfigError,
    DuplicateSenderError,
    InvalidSenderError,
    SenderNotFoundError,
    add_sender,
    load_config,
    normalize_address,
    remove_sender,
)


def write_config(
    path: Path,
    *,
    base_url: str = "http://127.0.0.1:1234/v1",
    extra: str = "",
    include_sender: bool = True,
) -> None:
    sender = (
        '''[[senders]]
email = "Trusted@Example.com"
name = "Trusted Person"
'''
        if include_sender
        else ""
    )
    path.write_text(
        f'''model_base_url = "{base_url}"
model_name = "local-model"
model_api_token_file = "{path.parent / "lm-token"}"
database_file = "{path.parent / "db.sqlite3"}"
gmail_credentials_file = "{path.parent / "credentials.json"}"
gmail_token_file = "{path.parent / "token.json"}"
{extra}
{sender}
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


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:1234@evil.example/v1",
        "http://evil.example@localhost:1234/v1",
        "https://localhost:1234/v1",
        "http://localhost/v1",
        "http://localhost:not-a-port/v1",
        "http://127.0.0.2:1234/v1",
        " http://localhost:1234/v1",
        "http://local\\thost:1234/v1",
        "http://local\\nhost:1234/v1",
    ],
)
def test_deceptive_or_incomplete_local_model_url_is_rejected(
    tmp_path: Path, base_url: str
) -> None:
    path = tmp_path / "config.toml"
    write_config(path, base_url=base_url)
    with pytest.raises(ConfigError, match="model_base_url"):
        load_config(path)


def test_localhost_model_url_with_explicit_port_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path, base_url="http://localhost:1234/v1/")
    assert load_config(path).model_base_url == "http://localhost:1234/v1"


@pytest.mark.parametrize("port", [1, 65_535])
def test_local_model_url_accepts_valid_port_boundaries(tmp_path: Path, port: int) -> None:
    path = tmp_path / "config.toml"
    write_config(path, base_url=f"http://127.0.0.1:{port}/v1")
    assert load_config(path).model_base_url == f"http://127.0.0.1:{port}/v1"


@pytest.mark.parametrize("port", [0, 65_536])
def test_local_model_url_rejects_invalid_port_boundaries(tmp_path: Path, port: int) -> None:
    path = tmp_path / "config.toml"
    write_config(path, base_url=f"http://127.0.0.1:{port}/v1")
    with pytest.raises(ConfigError, match="model_base_url"):
        load_config(path)


def test_duplicate_sender_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    with path.open("a", encoding="utf-8") as stream:
        stream.write('[[senders]]\nemail = "trusted@example.com"\n')
    with pytest.raises(ConfigError, match="Duplicate"):
        load_config(path)


def test_config_allows_zero_senders_for_first_run(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path, include_sender=False)

    config = load_config(path)

    assert config.senders == ()
    assert config.allowlist == frozenset()


def test_watchlist_round_trip_preserves_config_and_normalizes_addresses(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    write_config(path, extra="# operator comment\nretention_days = 90")

    added = add_sender(path, "New Person <NEW@Example.com>", "  New Person  ")

    assert added.email == "new@example.com"
    assert added.name == "New Person"
    text = path.read_text(encoding="utf-8")
    assert "# operator comment" in text
    assert "retention_days = 90" in text
    assert "Trusted@Example.com" in text
    assert load_config(path).allowlist == frozenset(
        {"trusted@example.com", "new@example.com"}
    )

    removed = remove_sender(path, "NEW@example.com")

    assert removed == added
    assert load_config(path).allowlist == frozenset({"trusted@example.com"})


def test_watchlist_duplicate_and_missing_removal_do_not_change_config(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    original = path.read_bytes()

    with pytest.raises(DuplicateSenderError, match="already watched"):
        add_sender(path, "Person <TRUSTED@example.com>", None)
    assert path.read_bytes() == original

    with pytest.raises(SenderNotFoundError, match="not watched"):
        remove_sender(path, "missing@example.com")
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "email",
    [
        "",
        "not-an-address",
        "@example.com",
        "person@",
        "a@@example.com",
        "a b@example.com",
        ".a@example.com",
        "a.@example.com",
        "a@example..com",
        "a@-example.com",
        "a@exam/ple.com",
        "a@!",
    ],
)
def test_watchlist_rejects_invalid_addresses_without_changing_config(
    tmp_path: Path, email: str
) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    original = path.read_bytes()

    with pytest.raises(InvalidSenderError, match="valid email"):
        add_sender(path, email, None)

    assert path.read_bytes() == original


def test_watchlist_domain_label_length_boundary(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path)

    accepted = add_sender(path, f"valid@{'a' * 63}.example", None)

    assert accepted.email == f"valid@{'a' * 63}.example"
    with pytest.raises(InvalidSenderError, match="valid email"):
        add_sender(path, f"invalid@{'a' * 64}.example", None)


def test_watchlist_atomic_replace_failure_preserves_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    original = path.read_bytes()

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr("eom_email_watcher.config.os.replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        add_sender(path, "new@example.com", "New")

    assert path.read_bytes() == original
    assert list(tmp_path.glob(".config.toml.*.tmp")) == []


def test_removing_final_sender_leaves_valid_empty_watchlist(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path)

    removed = remove_sender(path, "trusted@example.com")

    assert removed.email == "trusted@example.com"
    assert load_config(path).senders == ()


@pytest.mark.parametrize(
    ("setting", "value", "message"),
    [
        ("retention_days", '"seven"', "retention_days"),
        ("body_char_limit", "{ value = 1000 }", "body_char_limit"),
        ("model_timeout_seconds", '"soon"', "model_timeout_seconds"),
    ],
)
def test_malformed_numeric_settings_raise_config_error(
    tmp_path: Path, setting: str, value: str, message: str
) -> None:
    path = tmp_path / "config.toml"
    write_config(path, extra=f"{setting} = {value}")
    with pytest.raises(ConfigError, match=message):
        load_config(path)


@pytest.mark.parametrize("timezone", ["", "/tmp/foo"])
def test_invalid_timezone_keys_raise_config_error(
    tmp_path: Path, timezone: str
) -> None:
    path = tmp_path / "config.toml"
    write_config(path, extra=f'timezone = "{timezone}"')
    with pytest.raises(ConfigError, match="Unknown timezone"):
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
