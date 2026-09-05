from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest


def _load_builder() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "build_desktop_sidecar.py"
    spec = importlib.util.spec_from_file_location("build_desktop_sidecar", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load desktop sidecar builder")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_desktop_sidecar = _load_builder()


def _load_smoke() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "smoke_packaged_engine.py"
    spec = importlib.util.spec_from_file_location("smoke_packaged_engine", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load packaged-engine smoke script")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


smoke_packaged_engine = _load_smoke()


def test_packaged_smoke_rejects_unknown_expected_authority_state(tmp_path: Path) -> None:
    binary = tmp_path / "engine"
    binary.write_bytes(b"not executed")

    with pytest.raises(
        smoke_packaged_engine.PackagedEngineSmokeError,
        match="authority state is unsupported",
    ):
        smoke_packaged_engine.smoke_packaged_engine(binary, "ambient")


def test_packaged_smoke_rejects_unknown_expected_mail_provider(tmp_path: Path) -> None:
    binary = tmp_path / "engine"
    binary.write_bytes(b"not executed")

    with pytest.raises(
        smoke_packaged_engine.PackagedEngineSmokeError,
        match="mail provider is unsupported",
    ):
        smoke_packaged_engine.smoke_packaged_engine(
            binary,
            "missing",
            ("untrusted-provider",),
        )


@pytest.mark.parametrize("connection_available", [False, None])
def test_packaged_smoke_rejects_unavailable_expected_mail_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    connection_available: object,
) -> None:
    binary = tmp_path / "engine"
    binary.write_bytes(b"engine")
    responses = iter(
        [
            {"data": {"settings": {"timezone": "America/Chicago"}}},
            {"data": {"items": []}},
            {
                "data": {
                    "watchlist_count": 0,
                    "mail": {
                        "providers": [
                            {
                                "provider": "gmail",
                                "connection_available": connection_available,
                            },
                            {
                                "provider": "microsoft365",
                                "connection_available": True,
                            },
                        ]
                    },
                }
            },
        ]
    )
    monkeypatch.setattr(
        smoke_packaged_engine,
        "_request",
        lambda *args, **kwargs: next(responses),
    )

    with pytest.raises(
        smoke_packaged_engine.PackagedEngineSmokeError,
        match="expected gmail identity",
    ):
        smoke_packaged_engine.smoke_packaged_engine(
            binary,
            "missing",
            ("gmail",),
        )


def test_packaged_smoke_accepts_all_expected_mail_providers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = tmp_path / "engine"
    binary.write_bytes(b"engine")
    responses = iter(
        [
            {"data": {"settings": {"timezone": "America/Chicago"}}},
            {"data": {"items": []}},
            {
                "data": {
                    "watchlist_count": 0,
                    "mail": {
                        "providers": [
                            {"provider": "gmail", "connection_available": True},
                            {
                                "provider": "microsoft365",
                                "connection_available": True,
                            },
                        ]
                    },
                }
            },
            {"data": {"state": "missing", "active": False}},
            {"data": {"active": False}},
        ]
    )
    monkeypatch.setattr(
        smoke_packaged_engine,
        "_request",
        lambda *args, **kwargs: next(responses),
    )

    smoke_packaged_engine.smoke_packaged_engine(
        binary,
        "missing",
        ("gmail", "microsoft365"),
    )


@pytest.mark.parametrize(
    ("target_triple", "suffix"),
    [
        ("x86_64-unknown-linux-gnu", ""),
        ("aarch64-apple-darwin", ""),
        ("x86_64-pc-windows-msvc", ".exe"),
    ],
)
def test_sidecar_target_contract(target_triple: str, suffix: str) -> None:
    validated = build_desktop_sidecar.validate_target_triple(target_triple, target_triple)

    assert validated == target_triple
    assert build_desktop_sidecar.executable_suffix(target_triple) == suffix


@pytest.mark.parametrize(
    ("target_triple", "host_target_triple"),
    [
        ("../../outside", "x86_64-unknown-linux-gnu"),
        ("", "x86_64-unknown-linux-gnu"),
        ("x86_64-pc-windows-msvc", "x86_64-unknown-linux-gnu"),
        ("x86_64-unknown-linux-gnu", "x86_64-pc-windows-msvc"),
        ("aarch64-unknown-linux-gnu", "x86_64-unknown-linux-gnu"),
        ("x86_64-unknown-linux-musl", "x86_64-unknown-linux-gnu"),
        ("xwindows-unknown-unknown", "x86_64-unknown-linux-gnu"),
        ("wasm32-unknown-unknown", "x86_64-unknown-linux-gnu"),
    ],
)
def test_sidecar_target_contract_rejects_unsafe_or_cross_platform_builds(
    target_triple: str, host_target_triple: str
) -> None:
    with pytest.raises(build_desktop_sidecar.SidecarBuildError):
        build_desktop_sidecar.validate_target_triple(target_triple, host_target_triple)


def test_configured_target_is_checked_against_rustc_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CARGO_BUILD_TARGET", "aarch64-unknown-linux-gnu")
    monkeypatch.setattr(
        build_desktop_sidecar.subprocess,
        "run",
        lambda *args, **kwargs: build_desktop_sidecar.subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout="x86_64-unknown-linux-gnu\n"
        ),
    )

    with pytest.raises(
        build_desktop_sidecar.SidecarBuildError,
        match="cannot label a x86_64-unknown-linux-gnu sidecar as aarch64-unknown-linux-gnu",
    ):
        build_desktop_sidecar.determine_target_triple()


def test_windows_sidecar_output_uses_tauri_executable_name(monkeypatch: pytest.MonkeyPatch) -> None:
    output_directory = Path("bundle")
    monkeypatch.setattr(build_desktop_sidecar, "OUTPUT_DIRECTORY", output_directory)

    output = build_desktop_sidecar.sidecar_output_path("x86_64-pc-windows-msvc")

    assert output == output_directory / "eom-mail-engine-x86_64-pc-windows-msvc.exe"


def test_sidecar_build_profile_defaults_to_development(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(build_desktop_sidecar.BUILD_PROFILE_ENVIRONMENT, raising=False)

    assert build_desktop_sidecar.determine_build_profile() == "development"


def test_sidecar_build_profile_rejects_unknown_value_before_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(build_desktop_sidecar.BUILD_PROFILE_ENVIRONMENT, "release")
    monkeypatch.setattr(
        build_desktop_sidecar.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("build profile must fail before subprocess"),
    )

    with pytest.raises(
        build_desktop_sidecar.SidecarBuildError,
        match="must be development or public",
    ):
        build_desktop_sidecar.build_sidecar()


@pytest.mark.parametrize(
    ("google_identity", "microsoft_identity", "missing"),
    [
        (None, None, "Google, Microsoft 365"),
        ("google.json", None, "Microsoft 365"),
        (None, "microsoft.json", "Google"),
    ],
)
def test_public_build_rejects_incomplete_mail_identities_before_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    google_identity: str | None,
    microsoft_identity: str | None,
    missing: str,
) -> None:
    monkeypatch.setenv(build_desktop_sidecar.BUILD_PROFILE_ENVIRONMENT, "public")
    monkeypatch.setenv("LOCAL_CONNECT_ENTITLEMENT_KEYRING_FILE", "keyring.json")
    if google_identity is None:
        monkeypatch.delenv("EOM_EMAIL_WATCHER_GOOGLE_OAUTH_CLIENT_FILE", raising=False)
    else:
        monkeypatch.setenv("EOM_EMAIL_WATCHER_GOOGLE_OAUTH_CLIENT_FILE", google_identity)
    if microsoft_identity is None:
        monkeypatch.delenv("EOM_EMAIL_WATCHER_MICROSOFT_OAUTH_CLIENT_FILE", raising=False)
    else:
        monkeypatch.setenv("EOM_EMAIL_WATCHER_MICROSOFT_OAUTH_CLIENT_FILE", microsoft_identity)
    monkeypatch.setattr(
        build_desktop_sidecar.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("public admission must fail before subprocess"),
    )

    with pytest.raises(
        build_desktop_sidecar.SidecarBuildError,
        match=f"both Google and Microsoft 365 OAuth client identities; missing {missing}",
    ):
        build_desktop_sidecar.build_sidecar()


def test_public_build_rejects_missing_keyring_before_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(build_desktop_sidecar.BUILD_PROFILE_ENVIRONMENT, "public")
    monkeypatch.setenv("EOM_EMAIL_WATCHER_GOOGLE_OAUTH_CLIENT_FILE", "google.json")
    monkeypatch.setenv("EOM_EMAIL_WATCHER_MICROSOFT_OAUTH_CLIENT_FILE", "microsoft.json")
    monkeypatch.delenv("LOCAL_CONNECT_ENTITLEMENT_KEYRING_FILE", raising=False)
    monkeypatch.setattr(
        build_desktop_sidecar.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("public admission must fail before subprocess"),
    )

    with pytest.raises(
        build_desktop_sidecar.SidecarBuildError,
        match="approved production Connect entitlement key ring",
    ):
        build_desktop_sidecar.build_sidecar()


def _write_oauth_client(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "installed": {
                    "auth_uri": "https://accounts.example.test/auth",
                    "client_id": "desktop-client",
                    "client_secret": "desktop-secret",
                    "token_uri": "https://accounts.example.test/token",
                }
            }
        ),
        encoding="utf-8",
    )


def test_oauth_build_input_accepts_desktop_client_without_account_tokens(tmp_path: Path) -> None:
    path = tmp_path / "client.json"
    _write_oauth_client(path)

    build_desktop_sidecar.validate_oauth_client(path)


@pytest.mark.parametrize("token_field", ["access_token", "refresh_token"])
def test_oauth_build_input_rejects_nested_account_tokens(
    tmp_path: Path, token_field: str
) -> None:
    path = tmp_path / "client.json"
    _write_oauth_client(path)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["metadata"] = {"nested": [{token_field: "account-grant"}]}
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(
        build_desktop_sidecar.SidecarBuildError,
        match="must not contain account tokens",
    ):
        build_desktop_sidecar.validate_oauth_client(path)


@pytest.mark.parametrize(
    "document",
    [
        {"web": {}},
        {"installed": {"client_id": "missing-fields"}},
        [],
    ],
)
def test_oauth_build_input_rejects_non_desktop_shapes(
    tmp_path: Path, document: object
) -> None:
    path = tmp_path / "client.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(build_desktop_sidecar.SidecarBuildError):
        build_desktop_sidecar.validate_oauth_client(path)


def _write_microsoft_oauth_client(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "client_id": "11111111-2222-4333-8444-555555555555",
                "tenant": "organizations",
            }
        ),
        encoding="utf-8",
    )


def test_microsoft_oauth_build_input_accepts_public_client_without_secrets(
    tmp_path: Path,
) -> None:
    path = tmp_path / "microsoft.json"
    _write_microsoft_oauth_client(path)

    build_desktop_sidecar.validate_microsoft_oauth_client(path)


@pytest.mark.parametrize(
    "document",
    [
        {
            "client_id": "11111111-2222-4333-8444-555555555555",
            "tenant": "organizations",
            "client_secret": "must-not-ship",
        },
        {"client_id": "not-a-uuid", "tenant": "organizations"},
        {"client_id": "11111111-2222-4333-8444-555555555555", "tenant": "common"},
    ],
)
def test_microsoft_oauth_build_input_rejects_secret_or_invalid_shapes(
    tmp_path: Path,
    document: object,
) -> None:
    path = tmp_path / "microsoft.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(build_desktop_sidecar.SidecarBuildError):
        build_desktop_sidecar.validate_microsoft_oauth_client(path)


def test_sidecar_build_stages_microsoft_public_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "microsoft.json"
    _write_microsoft_oauth_client(source)
    build_directory = tmp_path / "build"
    output_directory = tmp_path / "output"
    calls: list[list[str]] = []

    monkeypatch.setattr(build_desktop_sidecar, "BUILD_DIRECTORY", build_directory)
    monkeypatch.setattr(build_desktop_sidecar, "OUTPUT_DIRECTORY", output_directory)
    monkeypatch.setattr(
        build_desktop_sidecar,
        "determine_target_triple",
        lambda: "x86_64-unknown-linux-gnu",
    )
    monkeypatch.delenv("EOM_EMAIL_WATCHER_GOOGLE_OAUTH_CLIENT_FILE", raising=False)
    monkeypatch.delenv("LOCAL_CONNECT_ENTITLEMENT_KEYRING_FILE", raising=False)
    monkeypatch.setenv("EOM_EMAIL_WATCHER_MICROSOFT_OAUTH_CLIENT_FILE", str(source))

    def run(arguments: list[str], **kwargs):
        calls.append(arguments)
        if "PyInstaller" in arguments:
            built = build_directory / "dist" / build_desktop_sidecar.ENGINE_NAME
            built.write_bytes(b"engine")
        return build_desktop_sidecar.subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr(build_desktop_sidecar.subprocess, "run", run)

    output = build_desktop_sidecar.build_sidecar()

    assert output.read_bytes() == b"engine"
    add_data = [
        calls[0][index + 1] for index, argument in enumerate(calls[0]) if argument == "--add-data"
    ]
    assert len(add_data) == 1
    staged_source, destination = add_data[0].rsplit(build_desktop_sidecar.os.pathsep, 1)
    assert Path(staged_source).name == "microsoft-oauth-client.json"
    assert destination == "eom_email_watcher_data"
    assert not Path(staged_source).exists()
    assert calls[1][-2:] == [
        "--expected-mail-provider",
        "microsoft365",
    ]


def _write_entitlement_keyring(
    path: Path,
    *,
    key_id: str = "local-connect-prod-2026-01",
    public_key: bytes | None = None,
) -> None:
    selected_public_key = public_key
    if selected_public_key is None:
        selected_public_key = next(
            key
            for approved_key_id, key in build_desktop_sidecar.APPROVED_RELEASE_AUTHORITIES
            if approved_key_id == "local-connect-prod-2026-01"
        )
    encoded_public_key = (
        base64.urlsafe_b64encode(selected_public_key).rstrip(b"=").decode("ascii")
    )
    path.write_text(
        json.dumps(
            {
                "keys": [
                    {
                        "algorithm": "Ed25519",
                        "key_id": key_id,
                        "public_key_base64url": encoded_public_key,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def _configure_fake_sidecar_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    profile: str = "development",
    google: Path | None = None,
    microsoft: Path | None = None,
    keyring: Path | None = None,
) -> tuple[list[list[str]], list[tuple[str, bytes]]]:
    build_directory = tmp_path / "build"
    output_directory = tmp_path / "output"
    calls: list[list[str]] = []
    staged_files: list[tuple[str, bytes]] = []
    monkeypatch.setattr(build_desktop_sidecar, "BUILD_DIRECTORY", build_directory)
    monkeypatch.setattr(build_desktop_sidecar, "OUTPUT_DIRECTORY", output_directory)
    monkeypatch.setattr(
        build_desktop_sidecar,
        "determine_target_triple",
        lambda: "x86_64-unknown-linux-gnu",
    )
    monkeypatch.setenv(build_desktop_sidecar.BUILD_PROFILE_ENVIRONMENT, profile)
    for environment, value in (
        ("EOM_EMAIL_WATCHER_GOOGLE_OAUTH_CLIENT_FILE", google),
        ("EOM_EMAIL_WATCHER_MICROSOFT_OAUTH_CLIENT_FILE", microsoft),
        ("LOCAL_CONNECT_ENTITLEMENT_KEYRING_FILE", keyring),
    ):
        if value is None:
            monkeypatch.delenv(environment, raising=False)
        else:
            monkeypatch.setenv(environment, str(value))

    def run(arguments: list[str], **kwargs):
        calls.append(arguments)
        if "PyInstaller" in arguments:
            for index, argument in enumerate(arguments):
                if argument == "--add-data":
                    source, _destination = arguments[index + 1].rsplit(
                        build_desktop_sidecar.os.pathsep,
                        1,
                    )
                    source_path = Path(source)
                    staged_files.append((source_path.name, source_path.read_bytes()))
            built = build_directory / "dist" / build_desktop_sidecar.ENGINE_NAME
            built.write_bytes(b"engine")
        return build_desktop_sidecar.subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr(build_desktop_sidecar.subprocess, "run", run)
    return calls, staged_files


def test_development_build_remains_credential_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, staged_files = _configure_fake_sidecar_build(tmp_path, monkeypatch)

    output = build_desktop_sidecar.build_sidecar()

    assert output.read_bytes() == b"engine"
    assert staged_files == []
    assert calls[1][-2:] == [
        "--expected-entitlement-state",
        "authority_unavailable",
    ]


def test_public_build_accepts_complete_provider_identity_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    google = tmp_path / "google.json"
    microsoft = tmp_path / "microsoft.json"
    keyring = tmp_path / "keyring.json"
    _write_oauth_client(google)
    _write_microsoft_oauth_client(microsoft)
    _write_entitlement_keyring(keyring)
    calls, staged_files = _configure_fake_sidecar_build(
        tmp_path,
        monkeypatch,
        profile="public",
        google=google,
        microsoft=microsoft,
        keyring=keyring,
    )

    output = build_desktop_sidecar.build_sidecar()

    assert output.read_bytes() == b"engine"
    smoke_arguments = calls[1]
    declared_providers = [
        smoke_arguments[index + 1]
        for index, argument in enumerate(smoke_arguments)
        if argument == "--expected-mail-provider"
    ]
    assert declared_providers == ["gmail", "microsoft365"]
    staged_names = {name for name, _content in staged_files}
    assert "connect-entitlement-keyring.json" in staged_names
    assert "google-oauth-client.json" in staged_names
    assert "microsoft-oauth-client.json" in staged_names
    assert not list((tmp_path / "build").glob("oauth-client.*"))
    assert not list((tmp_path / "build").glob("microsoft-oauth-client.*"))
    assert not list((tmp_path / "build").glob("connect-keyring.*"))


def test_google_oauth_build_stages_validated_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    google = tmp_path / "google.json"
    _write_oauth_client(google)
    expected = google.read_bytes()
    calls, staged_files = _configure_fake_sidecar_build(
        tmp_path,
        monkeypatch,
        google=google,
    )

    build_desktop_sidecar.build_sidecar()

    assert calls[1][-2:] == ["--expected-mail-provider", "gmail"]
    assert staged_files == [("google-oauth-client.json", expected)]
    assert not list((tmp_path / "build").glob("oauth-client.*"))


def test_public_build_rejects_token_bearing_google_input_before_pyinstaller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    google = tmp_path / "google.json"
    microsoft = tmp_path / "microsoft.json"
    keyring = tmp_path / "keyring.json"
    _write_oauth_client(google)
    _write_microsoft_oauth_client(microsoft)
    document = json.loads(google.read_text(encoding="utf-8"))
    document["installed"]["refresh_token"] = "account-grant"
    google.write_text(json.dumps(document), encoding="utf-8")
    _write_entitlement_keyring(keyring)
    calls, _staged_files = _configure_fake_sidecar_build(
        tmp_path,
        monkeypatch,
        profile="public",
        google=google,
        microsoft=microsoft,
        keyring=keyring,
    )

    with pytest.raises(
        build_desktop_sidecar.SidecarBuildError,
        match="must not contain account tokens",
    ):
        build_desktop_sidecar.build_sidecar()

    assert calls == []


def test_entitlement_build_input_accepts_approved_public_keyring(tmp_path: Path) -> None:
    path = tmp_path / "keyring.json"
    _write_entitlement_keyring(path)
    expected = path.read_bytes()

    assert build_desktop_sidecar.validate_entitlement_keyring(path) == expected


def test_entitlement_build_input_rejects_unapproved_production_authority(
    tmp_path: Path,
) -> None:
    path = tmp_path / "keyring.json"
    _write_entitlement_keyring(path, public_key=b"q" * 32)

    with pytest.raises(
        build_desktop_sidecar.SidecarBuildError,
        match="approved production authority",
    ):
        build_desktop_sidecar.validate_entitlement_keyring(path)


def test_entitlement_build_input_rejects_windows_reparse_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eom_email_watcher import connect_windows

    path = tmp_path / "keyring.json"
    _write_entitlement_keyring(path)
    monkeypatch.setattr(connect_windows, "_is_reparse", lambda _metadata: True)

    with pytest.raises(build_desktop_sidecar.SidecarBuildError):
        build_desktop_sidecar.validate_entitlement_keyring(path)


def test_entitlement_build_input_rejects_non_production_key_id(tmp_path: Path) -> None:
    path = tmp_path / "keyring.json"
    _write_entitlement_keyring(path, key_id="local-connect-test-2026-01")

    with pytest.raises(
        build_desktop_sidecar.SidecarBuildError,
        match="non-production key ID",
    ):
        build_desktop_sidecar.validate_entitlement_keyring(path)


@pytest.mark.parametrize(
    "target_triple",
    [
        "x86_64-unknown-linux-gnu",
        "aarch64-apple-darwin",
        "x86_64-pc-windows-msvc",
    ],
)
def test_entitlement_keyring_target_accepts_supported_platforms(target_triple: str) -> None:
    build_desktop_sidecar.validate_entitlement_keyring_target(target_triple)


def test_windows_build_stages_connect_keyring_with_platform_separator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "keyring.json"
    _write_entitlement_keyring(source)
    build_directory = tmp_path / "build"
    output_directory = tmp_path / "output"
    calls: list[list[str]] = []
    staged_keyring: list[bytes] = []
    validated_keyring = source.read_bytes()

    monkeypatch.setattr(build_desktop_sidecar, "BUILD_DIRECTORY", build_directory)
    monkeypatch.setattr(build_desktop_sidecar, "OUTPUT_DIRECTORY", output_directory)
    monkeypatch.setattr(
        build_desktop_sidecar,
        "determine_target_triple",
        lambda: "x86_64-pc-windows-msvc",
    )
    monkeypatch.setattr(build_desktop_sidecar.os, "pathsep", ";")
    monkeypatch.delenv("EOM_EMAIL_WATCHER_GOOGLE_OAUTH_CLIENT_FILE", raising=False)
    monkeypatch.delenv("EOM_EMAIL_WATCHER_MICROSOFT_OAUTH_CLIENT_FILE", raising=False)
    monkeypatch.setenv("LOCAL_CONNECT_ENTITLEMENT_KEYRING_FILE", str(source))

    validate_keyring = build_desktop_sidecar.validate_entitlement_keyring

    def validate_then_replace(path: Path) -> bytes:
        content = validate_keyring(path)
        path.write_bytes(b"substituted after validation")
        return content

    monkeypatch.setattr(
        build_desktop_sidecar,
        "validate_entitlement_keyring",
        validate_then_replace,
    )

    def run(arguments: list[str], **kwargs):
        calls.append(arguments)
        if "PyInstaller" in arguments:
            add_data = arguments[arguments.index("--add-data") + 1]
            staged_source = Path(add_data.rsplit(";", 1)[0])
            staged_keyring.append(staged_source.read_bytes())
            built = build_directory / "dist" / f"{build_desktop_sidecar.ENGINE_NAME}.exe"
            built.write_bytes(b"engine")
        return build_desktop_sidecar.subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr(build_desktop_sidecar.subprocess, "run", run)

    output = build_desktop_sidecar.build_sidecar()

    assert output.read_bytes() == b"engine"
    add_data = [
        calls[0][index + 1] for index, argument in enumerate(calls[0]) if argument == "--add-data"
    ]
    assert len(add_data) == 1
    staged_source, destination = add_data[0].rsplit(";", 1)
    assert Path(staged_source).name == "connect-entitlement-keyring.json"
    assert destination == "eom_email_watcher_data"
    assert staged_keyring == [validated_keyring]
    assert source.read_bytes() == b"substituted after validation"
    assert not Path(staged_source).exists()
    assert calls[1][-2:] == ["--expected-entitlement-state", "missing"]


@pytest.mark.parametrize("document", [{"keys": []}, {"keys": "not-a-list"}, {}])
def test_entitlement_build_input_rejects_invalid_keyring(
    tmp_path: Path, document: object
) -> None:
    path = tmp_path / "keyring.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(build_desktop_sidecar.SidecarBuildError):
        build_desktop_sidecar.validate_entitlement_keyring(path)


def test_tauri_uses_cross_platform_hook_and_platform_bundle_targets() -> None:
    repository = Path(__file__).parents[1]
    base_config = json.loads(
        (repository / "desktop/src-tauri/tauri.conf.json").read_text(encoding="utf-8")
    )
    windows_config = json.loads(
        (repository / "desktop/src-tauri/tauri.windows.conf.json").read_text(encoding="utf-8")
    )

    assert base_config["build"]["beforeBuildCommand"] == "pnpm build && pnpm build:sidecar"
    assert base_config["bundle"]["targets"] == ["deb"]
    assert windows_config["bundle"]["targets"] == ["nsis"]


def test_windows_package_has_required_icon_and_hidden_console_contract() -> None:
    repository = Path(__file__).parents[1]
    windows_config = json.loads(
        (repository / "desktop/src-tauri/tauri.windows.conf.json").read_text(encoding="utf-8")
    )
    icon = repository / "desktop/src-tauri" / windows_config["bundle"]["icon"][0]

    icon_header = icon.read_bytes()[:6]
    assert icon_header[:4] == b"\x00\x00\x01\x00"
    assert int.from_bytes(icon_header[4:], byteorder="little") > 0

    main_source = (repository / "desktop/src-tauri/src/main.rs").read_text(encoding="utf-8")
    engine_source = (repository / "desktop/src-tauri/src/engine.rs").read_text(
        encoding="utf-8"
    )
    assert 'windows_subsystem = "windows"' in main_source
    assert (
        "command.creation_flags(CREATE_NO_WINDOW | CREATE_SUSPENDED);" in engine_source
    )


def test_release_candidate_workflow_is_private_main_only_and_fail_closed() -> None:
    repository = Path(__file__).parents[1]
    workflow = (repository / ".github/workflows/release-candidate.yml").read_text(
        encoding="utf-8"
    )

    assert "\n  workflow_dispatch:" in workflow
    assert "\n  pull_request:" not in workflow
    assert "\n  push:" not in workflow
    assert '"refs/heads/main"' in workflow
    assert "EMAIL_WATCHER_GOOGLE_OAUTH_DESKTOP_JSON_B64" in workflow
    assert "EMAIL_WATCHER_MICROSOFT_OAUTH_PUBLIC_JSON_B64" in workflow
    assert 'if [ -z "$GOOGLE_OAUTH_JSON_B64" ] || [ -z "$MICROSOFT_OAUTH_JSON_B64" ]' in workflow
    assert workflow.count('--expected-mail-provider\", \"gmail') == 1
    assert workflow.count('--expected-mail-provider\", \"microsoft365') == 1
    assert workflow.count("EOM_EMAIL_WATCHER_BUILD_PROFILE: public") == 3
    linux_cargo_sidecar_profile = (
        "name: Build Linux sidecar for Cargo checks\n"
        "        env:\n"
        "          EOM_EMAIL_WATCHER_BUILD_PROFILE: public\n"
        "        run: pnpm --dir desktop build:sidecar"
    )
    assert linux_cargo_sidecar_profile in workflow
    linux_build_profile = (
        "name: Build Linux DEB\n"
        "        env:\n"
        "          EOM_EMAIL_WATCHER_BUILD_PROFILE: public"
    )
    assert linux_build_profile in workflow
    assert (
        "name: Build Windows NSIS installer\n"
        "        env:\n"
        "          EOM_EMAIL_WATCHER_BUILD_PROFILE: public" in workflow
    )
    assert "3005d82a7be885fba36f8688b5967a5b56a0abea" in workflow
    assert "pnpm --dir desktop tauri build --bundles deb" in workflow
    assert "pnpm --dir desktop tauri build --bundles nsis" in workflow
    assert workflow.count("if-no-files-found: error") == 2
    assert workflow.count("retention-days: 7") == 2
    assert "actions/upload-artifact@v7" in workflow
    assert "gh release" not in workflow
    assert "softprops/action-gh-release" not in workflow

    packaging_test = workflow.index("- run: uv run pytest tests/test_desktop_packaging.py")
    sidecar_build = workflow.index(linux_cargo_sidecar_profile)
    cargo_format = workflow.index(
        "cargo fmt --manifest-path desktop/src-tauri/Cargo.toml --check"
    )
    cargo_clippy = workflow.index(
        "cargo clippy --manifest-path desktop/src-tauri/Cargo.toml --all-targets"
    )
    cargo_test = workflow.index(
        "cargo test --manifest-path desktop/src-tauri/Cargo.toml --all-targets"
    )
    linux_deb = workflow.index(linux_build_profile)

    assert packaging_test < sidecar_build
    assert sidecar_build < cargo_format < cargo_clippy < cargo_test < linux_deb
