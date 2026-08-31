from pathlib import Path

import pytest

from eom_email_watcher.config import (
    ConfigAlreadyExistsError,
    ConfigError,
    DuplicateSenderError,
    InvalidConfigInitializationError,
    InvalidSenderError,
    InvalidSettingsUpdateError,
    SenderNotFoundError,
    add_sender,
    initialize_config,
    load_config,
    normalize_address,
    remove_sender,
    update_settings,
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


def test_gateway_config_requires_https_trust_and_auth(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    ca_file = tmp_path / "gateway-ca.pem"
    write_config(
        path,
        base_url="https://inference.office.internal:8443",
        extra=f'model_backend = "gateway"\nmodel_ca_file = "{ca_file}"',
    )

    config = load_config(path)

    assert config.model_backend == "gateway"
    assert config.model_base_url == "https://inference.office.internal:8443"
    assert config.model_name == "Managed by inference gateway"
    assert config.model_ca_file == ca_file
    assert config.model_require_auth is True


@pytest.mark.parametrize(
    "base_url",
    [
        "http://inference.office.internal:8080",
        "https://user@inference.office.internal",
        "https://inference.office.internal/v1",
        "https://inference.office.internal?target=elsewhere",
        "https://inference.office.internal#fragment",
        "https://inference.office.internal?",
        "https://inference.office.internal#",
        "https://a..b",
        f"https://{'a' * 64}.internal",
        "https://💩.internal",
        " https://inference.office.internal",
    ],
)
def test_gateway_config_rejects_unsafe_authorities(tmp_path: Path, base_url: str) -> None:
    path = tmp_path / "config.toml"
    write_config(
        path,
        base_url=base_url,
        extra=f'model_backend = "gateway"\nmodel_ca_file = "{tmp_path / "ca.pem"}"',
    )

    with pytest.raises(ConfigError, match="model_base_url"):
        load_config(path)


def test_gateway_config_accepts_maximum_dns_label(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    base_url = f"https://{'a' * 63}.internal"
    write_config(
        path,
        base_url=base_url,
        extra=f'model_backend = "gateway"\nmodel_ca_file = "{tmp_path / "ca.pem"}"',
    )

    assert load_config(path).model_base_url == base_url


@pytest.mark.parametrize(
    "extra",
    [
        'model_backend = "gateway"',
        'model_backend = "gateway"\nmodel_ca_file = "ca.pem"\nmodel_require_auth = false',
        'model_backend = ["gateway"]',
    ],
)
def test_gateway_config_fails_closed_when_security_fields_are_invalid(
    tmp_path: Path, extra: str
) -> None:
    path = tmp_path / "config.toml"
    write_config(path, base_url="https://inference.office.internal", extra=extra)

    with pytest.raises(ConfigError):
        load_config(path)


def test_gateway_config_rejects_nul_in_trust_path(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(
        path,
        base_url="https://inference.office.internal",
        extra='model_backend = "gateway"\nmodel_ca_file = "ca\\u0000.pem"',
    )

    with pytest.raises(ConfigError, match="model_ca_file"):
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


def test_first_run_initialization_creates_private_zero_sender_config(
    tmp_path: Path,
) -> None:
    path = tmp_path / "nested" / "config.toml"

    config = initialize_config(
        path,
        timezone=" UTC ",
        model_base_url="http://localhost:8080/v1/",
        model_name=" local-model ",
    )

    assert config.path == path.resolve()
    assert config.timezone == "UTC"
    assert config.model_base_url == "http://localhost:8080/v1"
    assert config.model_name == "local-model"
    assert config.model_require_auth is False
    assert config.notifications_enabled is True
    assert config.senders == ()
    assert path.stat().st_mode & 0o777 == 0o600
    text = path.read_text(encoding="utf-8")
    assert "gmail_send" not in text
    assert "monthly_hours" not in text
    assert list(path.parent.glob(".config.toml.*.tmp")) == []


def test_first_run_initialization_never_replaces_existing_config(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    original = b"operator-owned config\n"
    path.write_bytes(original)

    with pytest.raises(ConfigAlreadyExistsError, match="already exists"):
        initialize_config(
            path,
            timezone="UTC",
            model_base_url="http://127.0.0.1:8080/v1",
            model_name="local-model",
        )

    assert path.read_bytes() == original


@pytest.mark.parametrize(
    ("timezone", "model_base_url", "model_name"),
    [
        ("Unknown/Timezone", "http://127.0.0.1:8080/v1", "local-model"),
        ("UTC", "https://models.example.com/v1", "local-model"),
        ("UTC", "http://127.0.0.1:8080/v1", "   "),
        ("UTC", "http://127.0.0.1:8080/v1", "bad\nmodel"),
    ],
)
def test_first_run_initialization_rejects_invalid_input_without_creating_config(
    tmp_path: Path, timezone: str, model_base_url: str, model_name: str
) -> None:
    path = tmp_path / "config.toml"

    with pytest.raises(InvalidConfigInitializationError):
        initialize_config(
            path,
            timezone=timezone,
            model_base_url=model_base_url,
            model_name=model_name,
        )

    assert not path.exists()


def test_first_run_atomic_publication_failure_leaves_no_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"

    def fail_link(source: Path, destination: Path) -> None:
        raise OSError("link failed")

    monkeypatch.setattr("eom_email_watcher.config.os.link", fail_link)

    with pytest.raises(OSError, match="link failed"):
        initialize_config(
            path,
            timezone="UTC",
            model_base_url="http://127.0.0.1:8080/v1",
            model_name="local-model",
        )

    assert not path.exists()
    assert list(tmp_path.glob(".config.toml.*.tmp")) == []


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


def test_settings_update_preserves_unrelated_config(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(
        path,
        extra='''# operator comment
poll_interval_minutes = 30
retention_days = 90
notifications_enabled = false
extension_key = "preserve-me"''',
    )

    updated = update_settings(
        path,
        {
            "model_base_url": "http://localhost:8080/v1/",
            "model_name": " replacement-model ",
            "poll_interval_minutes": 60,
            "retention_days": 180,
            "notifications_enabled": True,
        },
    )

    assert updated.poll_interval_minutes == 60
    assert updated.retention_days == 180
    assert updated.notifications_enabled is True
    assert updated.model_base_url == "http://localhost:8080/v1"
    assert updated.model_name == "replacement-model"
    text = path.read_text(encoding="utf-8")
    assert "# operator comment" in text
    assert 'extension_key = "preserve-me"' in text
    assert 'model_name = "replacement-model"' in text
    assert 'email = "Trusted@Example.com"' in text


@pytest.mark.parametrize(
    "updates",
    [
        {"poll_interval_minutes": 1},
        {"poll_interval_minutes": 1440},
        {"retention_days": 1},
        {"retention_days": 3650},
        {"notifications_enabled": False},
        {"model_base_url": "http://localhost:65535/v1"},
        {"model_name": "another-model"},
    ],
)
def test_settings_update_accepts_boundary_values(
    tmp_path: Path, updates: dict[str, object]
) -> None:
    path = tmp_path / "config.toml"
    write_config(path)

    updated = update_settings(path, updates)

    for key, expected in updates.items():
        assert getattr(updated, key) == expected


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({}, "At least one"),
        ({"unknown": 1}, "Unsupported settings"),
        ({"poll_interval_minutes": True}, "must be an integer"),
        ({"poll_interval_minutes": 0}, "between 1 and 1440"),
        ({"poll_interval_minutes": 1441}, "between 1 and 1440"),
        ({"retention_days": False}, "must be an integer"),
        ({"retention_days": 0}, "between 1 and 3650"),
        ({"retention_days": 3651}, "between 1 and 3650"),
        ({"notifications_enabled": 1}, "must be a boolean"),
        ({"notifications_enabled": "false"}, "must be a boolean"),
        ({"model_base_url": 1234}, "must be a string"),
        ({"model_base_url": "https://models.example.com/v1"}, "localhost"),
        ({"model_name": 1234}, "must be a string"),
        ({"model_name": "   "}, "non-empty printable"),
        ({"model_name": "bad\nmodel"}, "non-empty printable"),
        (
            {"poll_interval_minutes": 30, "retention_days": 3651},
            "between 1 and 3650",
        ),
    ],
)
def test_settings_update_rejects_invalid_values_without_changing_config(
    tmp_path: Path, updates: dict[str, object], message: str
) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    original = path.read_bytes()

    with pytest.raises(ConfigError, match=message):
        update_settings(path, updates)

    assert path.read_bytes() == original


@pytest.mark.parametrize("model_update", ["model_base_url", "model_name"])
def test_settings_update_keeps_gateway_model_configuration_read_only(
    tmp_path: Path, model_update: str
) -> None:
    path = tmp_path / "config.toml"
    ca_file = tmp_path / "gateway-ca.pem"
    write_config(
        path,
        base_url="https://inference.office.internal:8443",
        extra=f'model_backend = "gateway"\nmodel_ca_file = "{ca_file}"',
    )
    original = path.read_bytes()
    value = (
        "http://127.0.0.1:8080/v1"
        if model_update == "model_base_url"
        else "replacement-model"
    )

    with pytest.raises(InvalidSettingsUpdateError, match="managed"):
        update_settings(path, {model_update: value})

    assert path.read_bytes() == original
    updated = update_settings(path, {"poll_interval_minutes": 45})
    assert updated.poll_interval_minutes == 45
    assert updated.model_backend == "gateway"


def test_settings_update_atomic_replace_failure_preserves_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    original = path.read_bytes()

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr("eom_email_watcher.config.os.replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        update_settings(path, {"poll_interval_minutes": 60})

    assert path.read_bytes() == original
    assert list(tmp_path.glob(".config.toml.*.tmp")) == []


def test_watchlist_mutation_preserves_symlinked_config_target(tmp_path: Path) -> None:
    target = tmp_path / "managed" / "config.toml"
    target.parent.mkdir()
    write_config(target)
    link = tmp_path / "config.toml"
    link.symlink_to(target)

    added = add_sender(link, "new@example.com", "New")

    assert link.is_symlink()
    assert added.email == "new@example.com"
    assert load_config(target).allowlist == frozenset(
        {"trusted@example.com", "new@example.com"}
    )

    removed = remove_sender(link, "new@example.com")

    assert link.is_symlink()
    assert removed == added
    assert load_config(target).allowlist == frozenset({"trusted@example.com"})


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
        ("poll_interval_minutes", '"often"', "poll_interval_minutes"),
        ("poll_interval_minutes", "true", "poll_interval_minutes"),
        ("poll_interval_minutes", "1.9", "poll_interval_minutes"),
    ],
)
def test_malformed_numeric_settings_raise_config_error(
    tmp_path: Path, setting: str, value: str, message: str
) -> None:
    path = tmp_path / "config.toml"
    write_config(path, extra=f"{setting} = {value}")
    with pytest.raises(ConfigError, match=message):
        load_config(path)


@pytest.mark.parametrize(
    ("minutes", "valid"), [(0, False), (1, True), (1440, True), (1441, False)]
)
def test_poll_interval_boundaries(tmp_path: Path, minutes: int, valid: bool) -> None:
    path = tmp_path / "config.toml"
    write_config(path, extra=f"poll_interval_minutes = {minutes}")
    if valid:
        assert load_config(path).poll_interval_minutes == minutes
    else:
        with pytest.raises(ConfigError, match="poll_interval_minutes"):
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
