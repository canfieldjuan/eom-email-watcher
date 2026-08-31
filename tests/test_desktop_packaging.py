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


@pytest.mark.parametrize(
    ("target_triple", "host_platform", "suffix"),
    [
        ("x86_64-unknown-linux-gnu", "linux", ""),
        ("aarch64-apple-darwin", "darwin", ""),
        ("x86_64-pc-windows-msvc", "win32", ".exe"),
    ],
)
def test_sidecar_target_contract(
    target_triple: str, host_platform: str, suffix: str
) -> None:
    validated = build_desktop_sidecar.validate_target_triple(target_triple, host_platform)

    assert validated == target_triple
    assert build_desktop_sidecar.executable_suffix(target_triple) == suffix


@pytest.mark.parametrize(
    ("target_triple", "host_platform"),
    [
        ("../../outside", "linux"),
        ("", "linux"),
        ("x86_64-pc-windows-msvc", "linux"),
        ("x86_64-unknown-linux-gnu", "win32"),
        ("xwindows-unknown-unknown", "linux"),
        ("wasm32-unknown-unknown", "linux"),
    ],
)
def test_sidecar_target_contract_rejects_unsafe_or_cross_platform_builds(
    target_triple: str, host_platform: str
) -> None:
    with pytest.raises(build_desktop_sidecar.SidecarBuildError):
        build_desktop_sidecar.validate_target_triple(target_triple, host_platform)


def test_windows_sidecar_output_uses_tauri_executable_name(monkeypatch: pytest.MonkeyPatch) -> None:
    output_directory = Path("bundle")
    monkeypatch.setattr(build_desktop_sidecar, "OUTPUT_DIRECTORY", output_directory)

    output = build_desktop_sidecar.sidecar_output_path("x86_64-pc-windows-msvc")

    assert output == output_directory / "eom-mail-engine-x86_64-pc-windows-msvc.exe"


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


def test_entitlement_build_input_accepts_public_keyring(tmp_path: Path) -> None:
    path = tmp_path / "keyring.json"
    public_key = base64.urlsafe_b64encode(b"k" * 32).rstrip(b"=").decode("ascii")
    path.write_text(
        json.dumps(
            {
                "keys": [
                    {
                        "algorithm": "Ed25519",
                        "key_id": "release-1",
                        "public_key_base64url": public_key,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    build_desktop_sidecar.validate_entitlement_keyring(path)


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
