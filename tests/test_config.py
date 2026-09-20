import os
import stat
from pathlib import Path
from zoneinfo import ZoneInfo, reset_tzpath

import pytest

import eom_email_watcher.config as config_module
from eom_email_watcher.config import (
    ConfigAdmissionStaleError,
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
        """[[senders]]
email = "Trusted@Example.com"
name = "Trusted Person"
"""
        if include_sender
        else ""
    )
    path.write_text(
        f'''model_base_url = "{base_url}"
model_name = "local-model"
model_api_token_file = "{(path.parent / "lm-token").as_posix()}"
database_file = "{(path.parent / "db.sqlite3").as_posix()}"
gmail_credentials_file = "{(path.parent / "credentials.json").as_posix()}"
microsoft_credentials_file = "{(path.parent / "microsoft-oauth-client.json").as_posix()}"
gmail_token_file = "{(path.parent / "token.json").as_posix()}"
{extra}
{sender}
''',
        encoding="utf-8",
    )
    path.parent.chmod(0o700)
    path.chmod(0o600)


def test_config_normalizes_exact_sender(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    config = load_config(path)
    assert config.allowlist == frozenset({"trusted@example.com"})
    assert normalize_address("Person <TRUSTED@example.com>") == "trusted@example.com"


def test_packaged_tzdata_supports_default_zone_without_system_database(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    ZoneInfo.clear_cache()
    reset_tzpath(())
    try:
        assert load_config(path).timezone == "America/Chicago"
    finally:
        reset_tzpath()
        ZoneInfo.clear_cache()


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
def test_deceptive_or_incomplete_local_model_url_is_rejected(tmp_path: Path, base_url: str) -> None:
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

    def fail_link(source: Path, destination: Path, **_kwargs: object) -> None:
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
    assert load_config(path).allowlist == frozenset({"trusted@example.com", "new@example.com"})

    removed = remove_sender(path, "NEW@example.com")

    assert removed == added
    assert load_config(path).allowlist == frozenset({"trusted@example.com"})


def test_windows_settings_and_watchlist_mutations_avoid_posix_only_apis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    real_open = os.open
    real_replace = os.replace
    real_fsync = os.fsync

    def windows_open(name, flags, mode=0o777, *, dir_fd=None):
        assert dir_fd is None, "Windows mutation used a dir_fd-relative open"
        return real_open(name, flags, mode)

    def windows_replace(source, destination, **kwargs):
        assert not kwargs, "Windows mutation used dir_fd-relative replace"
        return real_replace(source, destination)

    def windows_fsync(fd: int) -> None:
        assert not stat.S_ISDIR(os.fstat(fd).st_mode), "Windows mutation attempted directory fsync"
        real_fsync(fd)

    class WindowsOsProxy:
        name = "nt"
        open = staticmethod(windows_open)
        replace = staticmethod(windows_replace)
        fsync = staticmethod(windows_fsync)
        fchmod = staticmethod(lambda *_args: pytest.fail("Windows mutation called os.fchmod"))

        def __getattr__(self, name: str):
            return getattr(os, name)

    monkeypatch.setattr(config_module, "os", WindowsOsProxy())

    updated = update_settings(path, {"poll_interval_minutes": 45})
    added = add_sender(path, "new@example.com", "New")
    removed = remove_sender(path, "new@example.com")

    assert updated.poll_interval_minutes == 45
    assert added.email == "new@example.com"
    assert removed == added
    assert load_config(path).allowlist == frozenset({"trusted@example.com"})

    original = path.read_bytes()
    os.link(path, tmp_path / "second-link.toml")
    with pytest.raises(
        ConfigError,
        match="Configuration is unavailable or requires manual repair",
    ):
        update_settings(path, {"poll_interval_minutes": 60})
    assert path.read_bytes() == original


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

    def fail_replace(_parent_fd: int, _source: str, _destination: str) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr("eom_email_watcher.config._rename_exchange_at", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        add_sender(path, "new@example.com", "New")

    assert path.read_bytes() == original
    assert not list(path.parent.glob(f".{path.name}.ntfy-disclosure-*"))


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
    value = "http://127.0.0.1:8080/v1" if model_update == "model_base_url" else "replacement-model"

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

    def fail_replace(_parent_fd: int, _source: str, _destination: str) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr("eom_email_watcher.config._rename_exchange_at", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        update_settings(path, {"poll_interval_minutes": 60})

    assert path.read_bytes() == original
    assert not list(path.parent.glob(f".{path.name}.ntfy-disclosure-*"))


def test_watchlist_mutation_rejects_symlinked_config_alias(tmp_path: Path) -> None:
    target = tmp_path / "managed" / "config.toml"
    target.parent.mkdir()
    write_config(target)
    link = tmp_path / "config.toml"
    link.symlink_to(target)

    original = target.read_bytes()

    with pytest.raises(ConfigError, match="unavailable or requires manual repair"):
        add_sender(link, "new@example.com", "New")

    assert link.is_symlink()
    assert target.read_bytes() == original


@pytest.mark.skipif(os.name != "posix", reason="safe config publication is POSIX-only")
@pytest.mark.parametrize("mutation", ["settings", "add", "remove"])
@pytest.mark.parametrize("same_bytes", [False, True])
def test_config_mutation_never_overwrites_editor_atomic_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    same_bytes: bool,
) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    original = path.read_bytes()
    manual = original if same_bytes else original + b"\n# operator replacement\n"
    replaced_identity: tuple[int, int] | None = None

    def replace_before_commit(stage: str, observed_path: Path) -> None:
        nonlocal replaced_identity
        if stage != "before_replace" or replaced_identity is not None:
            return
        assert observed_path == path
        replacement = path.with_name("operator-config.toml")
        replacement.write_bytes(manual)
        replacement.chmod(0o600)
        os.replace(replacement, path)
        current = path.stat()
        replaced_identity = (current.st_dev, current.st_ino)

    monkeypatch.setattr(
        config_module,
        "_config_mutation_probe",
        replace_before_commit,
        raising=False,
    )

    with pytest.raises(ConfigAdmissionStaleError, match="snapshot changed"):
        if mutation == "settings":
            update_settings(path, {"poll_interval_minutes": 45})
        elif mutation == "add":
            add_sender(path, "new@example.com", "New")
        else:
            remove_sender(path, "trusted@example.com")

    assert replaced_identity is not None
    assert (path.stat().st_dev, path.stat().st_ino) == replaced_identity
    assert path.read_bytes() == manual
    assert list(path.parent.glob(".config.toml.*.tmp")) == []


@pytest.mark.skipif(os.name != "posix", reason="exchange publication is POSIX-only")
@pytest.mark.parametrize("mutation", ["settings", "add", "remove"])
@pytest.mark.parametrize("same_bytes", [False, True])
def test_config_mutation_preserves_editor_replacement_at_final_exchange(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    same_bytes: bool,
) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    original = path.read_bytes()
    manual = original if same_bytes else original + b"\n# final editor replacement\n"
    replaced_identity: tuple[int, int] | None = None

    def replace_at_exchange(stage: str) -> None:
        nonlocal replaced_identity
        if stage != "before_exchange" or replaced_identity is not None:
            return
        replacement = path.with_name("operator-final.toml")
        replacement.write_bytes(manual)
        replacement.chmod(0o600)
        os.replace(replacement, path)
        current = path.stat()
        replaced_identity = (current.st_dev, current.st_ino)

    monkeypatch.setattr(config_module, "_transaction_probe", replace_at_exchange)

    with pytest.raises(ConfigAdmissionStaleError, match="snapshot changed"):
        if mutation == "settings":
            update_settings(path, {"poll_interval_minutes": 45})
        elif mutation == "add":
            add_sender(path, "new@example.com", "New")
        else:
            remove_sender(path, "trusted@example.com")

    assert replaced_identity is not None
    assert (path.stat().st_dev, path.stat().st_ino) == replaced_identity
    assert path.read_bytes() == manual
    assert not list(path.parent.glob(f".{path.name}.ntfy-disclosure-*"))


@pytest.mark.skipif(os.name != "posix", reason="exchange publication is POSIX-only")
@pytest.mark.parametrize("mutation", ["settings", "add", "remove"])
def test_config_mutation_preserves_symlink_alias_at_final_exchange(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    target = tmp_path / "operator.toml"
    target.write_bytes(path.read_bytes() + b"\n# operator alias target\n")
    target.chmod(0o600)
    injected = False

    def replace_at_exchange(stage: str) -> None:
        nonlocal injected
        if stage != "before_exchange" or injected:
            return
        path.unlink()
        path.symlink_to(target)
        injected = True

    monkeypatch.setattr(config_module, "_transaction_probe", replace_at_exchange)

    with pytest.raises(ConfigAdmissionStaleError, match="snapshot changed"):
        if mutation == "settings":
            update_settings(path, {"poll_interval_minutes": 45})
        elif mutation == "add":
            add_sender(path, "new@example.com", "New")
        else:
            remove_sender(path, "trusted@example.com")

    assert injected
    assert path.is_symlink()
    assert path.resolve() == target
    assert b"operator alias target" in target.read_bytes()
    assert not list(path.parent.glob(f".{path.name}.ntfy-disclosure-*"))


def _apply_config_mutation(path: Path, mutation: str) -> None:
    if mutation == "settings":
        update_settings(path, {"poll_interval_minutes": 45})
    elif mutation == "add":
        add_sender(path, "new@example.com", "New")
    else:
        remove_sender(path, "trusted@example.com")


def _assert_config_mutation_state(path: Path, mutation: str, *, applied: bool) -> None:
    loaded = load_config(path)
    if mutation == "settings":
        assert loaded.poll_interval_minutes == (45 if applied else 120)
    elif mutation == "add":
        assert ("new@example.com" in loaded.allowlist) is applied
    else:
        assert ("trusted@example.com" not in loaded.allowlist) is applied


@pytest.mark.skipif(os.name != "posix", reason="exchange recovery is POSIX-only")
@pytest.mark.parametrize("mutation", ["settings", "add", "remove"])
@pytest.mark.parametrize(
    ("crash_stage", "applied"),
    [("before_exchange", False), ("after_exchange", True)],
)
def test_config_mutation_recovers_crash_before_or_after_exchange(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    crash_stage: str,
    applied: bool,
) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    child = os.fork()
    if child == 0:

        def crash(stage: str) -> None:
            if stage == crash_stage:
                os._exit(86)

        config_module._transaction_probe = crash
        _apply_config_mutation(path, mutation)
        os._exit(87)
    waited, status = os.waitpid(child, 0)

    assert waited == child
    assert os.WIFEXITED(status)
    assert os.WEXITSTATUS(status) == 86
    _assert_config_mutation_state(path, mutation, applied=applied)
    assert not list(path.parent.glob(f".{path.name}.ntfy-disclosure-*"))


@pytest.mark.skipif(os.name != "posix", reason="exchange recovery is POSIX-only")
@pytest.mark.parametrize("mutation", ["settings", "add", "remove"])
def test_config_mutation_recovers_crash_after_external_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    path = tmp_path / "config.toml"
    write_config(path)
    original = path.read_bytes()
    manual = original + b"\n# external replacement survived crash\n"
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    child = os.fork()
    if child == 0:
        replaced = False

        def replace_then_crash(stage: str) -> None:
            nonlocal replaced
            if stage == "before_exchange" and not replaced:
                replacement = path.with_name("operator-crash.toml")
                replacement.write_bytes(manual)
                replacement.chmod(0o600)
                os.replace(replacement, path)
                replaced = True
            if stage == "after_mismatch_rollback":
                os._exit(86)

        config_module._transaction_probe = replace_then_crash
        _apply_config_mutation(path, mutation)
        os._exit(87)
    waited, status = os.waitpid(child, 0)

    assert waited == child
    assert os.WIFEXITED(status)
    assert os.WEXITSTATUS(status) == 86
    _assert_config_mutation_state(path, mutation, applied=False)
    assert path.read_bytes() == manual
    assert not list(path.parent.glob(f".{path.name}.ntfy-disclosure-*"))


@pytest.mark.skipif(os.name != "posix", reason="state lock concurrency is POSIX-only")
def test_concurrent_app_mutations_serialize_without_lost_updates(tmp_path: Path) -> None:
    import threading

    path = tmp_path / "config.toml"
    write_config(path, include_sender=False)
    start = threading.Barrier(3)
    failures: list[BaseException] = []

    def add(address: str) -> None:
        try:
            start.wait()
            add_sender(path, address)
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    threads = [
        threading.Thread(target=add, args=("one@example.com",)),
        threading.Thread(target=add, args=("two@example.com",)),
    ]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=5)

    assert failures == []
    assert all(not thread.is_alive() for thread in threads)
    assert load_config(path).allowlist == frozenset({"one@example.com", "two@example.com"})


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


@pytest.mark.parametrize(("minutes", "valid"), [(0, False), (1, True), (1440, True), (1441, False)])
def test_poll_interval_boundaries(tmp_path: Path, minutes: int, valid: bool) -> None:
    path = tmp_path / "config.toml"
    write_config(path, extra=f"poll_interval_minutes = {minutes}")
    if valid:
        assert load_config(path).poll_interval_minutes == minutes
    else:
        with pytest.raises(ConfigError, match="poll_interval_minutes"):
            load_config(path)


@pytest.mark.parametrize("timezone", ["", "/tmp/foo"])
def test_invalid_timezone_keys_raise_config_error(tmp_path: Path, timezone: str) -> None:
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
    assert config.ntfy_content_disclosure_acknowledged is False


def test_root_boolean_scanner_keeps_odd_escaped_triple_inside_multiline_string() -> None:
    content = (
        b'notes = """prefix\\\"""\n'
        b"ntfy_content_disclosure_acknowledged = false\n"
        b'"""\n'
        b"ntfy_content_disclosure_acknowledged = false\n"
    )

    spans = config_module._root_boolean_token_spans(
        content,
        "ntfy_content_disclosure_acknowledged",
    )

    expected = content.rindex(b"false")
    assert spans == [(expected, expected + len(b"false"))]


def test_root_boolean_scanner_closes_multiline_string_after_even_backslashes() -> None:
    content = (
        b'notes = """prefix\\\\"""\n'
        b"ntfy_content_disclosure_acknowledged = false\n"
    )

    spans = config_module._root_boolean_token_spans(
        content,
        "ntfy_content_disclosure_acknowledged",
    )

    expected = content.index(b"false")
    assert spans == [(expected, expected + len(b"false"))]


def test_short_ntfy_topic_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(
        path,
        extra=('ntfy_topic = "too-short"\nntfy_content_disclosure_acknowledged = true'),
    )
    with pytest.raises(ConfigError, match="ntfy_topic"):
        load_config(path)


@pytest.mark.parametrize(
    "acknowledgement",
    [
        "",
        "ntfy_content_disclosure_acknowledged = false",
        'ntfy_content_disclosure_acknowledged = "true"',
    ],
)
def test_ntfy_topic_requires_literal_true_disclosure_acknowledgement(
    tmp_path: Path,
    acknowledgement: str,
) -> None:
    path = tmp_path / "config.toml"
    write_config(
        path,
        extra=(f'ntfy_topic = "eom-email-watch-0123456789ab"\n{acknowledgement}'),
    )
    with pytest.raises(ConfigError, match="ntfy_content_disclosure_acknowledged"):
        load_config(path)


def test_valid_ntfy_topic_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(
        path,
        extra=(
            'ntfy_topic = "eom-email-watch-0123456789ab"\n'
            "ntfy_content_disclosure_acknowledged = true"
        ),
    )
    config = load_config(path)
    assert config.ntfy_topic == "eom-email-watch-0123456789ab"
    assert config.ntfy_content_disclosure_acknowledged is True


def test_ntfy_url_must_be_https(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config(
        path,
        extra=(
            'ntfy_topic = "eom-email-watch-0123456789ab"\n'
            "ntfy_content_disclosure_acknowledged = true\n"
            'ntfy_url = "http://ntfy.sh"'
        ),
    )
    with pytest.raises(ConfigError, match="https"):
        load_config(path)


@pytest.mark.skipif(os.name != "posix", reason="state lock ordering is POSIX-only")
def test_initialization_locks_before_creating_config_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "new-config" / "config.toml"
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    observed: list[str] = []

    def observe_lock(stage: str) -> None:
        if stage == "after_acquire":
            assert not path.parent.exists()
            observed.append(stage)

    monkeypatch.setattr(config_module, "_config_serialization_lock_probe", observe_lock)

    initialize_config(
        path,
        timezone="America/Chicago",
        model_base_url="http://127.0.0.1:1234/v1",
        model_name="local-model",
    )

    assert observed == ["after_acquire"]
    assert path.is_file()
    assert normalize_address("Person <TRUSTED@example.com>") == "trusted@example.com"
