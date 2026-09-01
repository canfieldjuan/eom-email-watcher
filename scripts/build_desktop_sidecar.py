#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
PROJECT_DIRECTORY = SCRIPT_DIRECTORY.parent
BUILD_DIRECTORY = PROJECT_DIRECTORY / ".sidecar-build"
OUTPUT_DIRECTORY = PROJECT_DIRECTORY / "desktop" / "src-tauri" / "binaries"
ENGINE_NAME = "eom-mail-engine"
TARGET_TRIPLE_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
OAUTH_REQUIRED_FIELDS = ("auth_uri", "client_id", "client_secret", "token_uri")
OAUTH_TOKEN_FIELDS = frozenset({"access_token", "refresh_token"})


class SidecarBuildError(RuntimeError):
    pass


def _target_family(target_triple: str) -> str:
    components = target_triple.split("-")
    if "windows" in components:
        return "windows"
    if components[-2:] == ["apple", "darwin"]:
        return "macos"
    if "linux" in components:
        return "linux"
    raise SidecarBuildError(f"Unsupported Rust target triple: {target_triple}")


def validate_target_triple(target_triple: str, host_target_triple: str) -> str:
    for triple in (target_triple, host_target_triple):
        if not triple or TARGET_TRIPLE_PATTERN.fullmatch(triple) is None:
            raise SidecarBuildError(f"Unsupported Rust target triple: {triple}")
        _target_family(triple)
    if target_triple != host_target_triple:
        raise SidecarBuildError(
            "PyInstaller must build for the native Rust host target; "
            f"cannot label a {host_target_triple} sidecar as {target_triple}"
        )
    return target_triple


def determine_target_triple() -> str:
    result = subprocess.run(
        ["rustc", "--print", "host-tuple"],
        check=True,
        capture_output=True,
        text=True,
    )
    host_target_triple = result.stdout.strip()
    configured = os.environ.get("CARGO_BUILD_TARGET", host_target_triple)
    return validate_target_triple(configured, host_target_triple)


def executable_suffix(target_triple: str) -> str:
    return ".exe" if _target_family(target_triple) == "windows" else ""


def sidecar_output_path(target_triple: str) -> Path:
    suffix = executable_suffix(target_triple)
    return OUTPUT_DIRECTORY / f"{ENGINE_NAME}-{target_triple}{suffix}"


def _document_keys(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from _document_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _document_keys(child)


def validate_oauth_client(path: Path) -> None:
    if not path.is_file():
        raise SidecarBuildError("Google OAuth Desktop client file is not a regular file")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SidecarBuildError("Google OAuth Desktop client file is not valid JSON") from exc
    installed = document.get("installed") if isinstance(document, dict) else None
    if not isinstance(installed, dict):
        raise SidecarBuildError("Google OAuth input must be a downloaded Desktop client JSON file")
    if not all(
        isinstance(installed.get(field), str) and installed[field].strip()
        for field in OAUTH_REQUIRED_FIELDS
    ):
        raise SidecarBuildError("Google OAuth Desktop client JSON is missing required fields")
    if OAUTH_TOKEN_FIELDS.intersection(_document_keys(document)):
        raise SidecarBuildError("Google OAuth Desktop client input must not contain account tokens")


def validate_entitlement_keyring(path: Path) -> None:
    if not path.is_file():
        raise SidecarBuildError("Connect entitlement public-key ring is not a regular file")
    from eom_email_watcher.entitlement import _parse_keyring

    try:
        keys = _parse_keyring(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise SidecarBuildError("Connect entitlement public-key ring is invalid") from exc
    if not keys:
        raise SidecarBuildError(
            "Connect-enabled release key ring must contain at least one public key"
        )


def validate_entitlement_keyring_target(target_triple: str) -> None:
    if _target_family(target_triple) == "windows":
        raise SidecarBuildError(
            "Connect entitlement activation storage is not supported on Windows; "
            "unset LOCAL_CONNECT_ENTITLEMENT_KEYRING_FILE to build the public package"
        )


@contextmanager
def _staged_build_input(source: Path, filename: str, prefix: str) -> Iterator[Path]:
    directory = Path(tempfile.mkdtemp(prefix=prefix, dir=BUILD_DIRECTORY))
    destination = directory / filename
    try:
        shutil.copyfile(source, destination)
        if os.name != "nt":
            destination.chmod(0o600)
        yield destination
    finally:
        destination.unlink(missing_ok=True)
        with suppress(OSError):
            directory.rmdir()


def _publish_sidecar(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise SidecarBuildError(f"PyInstaller did not produce the expected engine: {source}")
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    try:
        shutil.copy2(source, temporary)
        if os.name != "nt":
            temporary.chmod(0o755)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def build_sidecar() -> Path:
    target_triple = determine_target_triple()
    suffix = executable_suffix(target_triple)
    output_path = sidecar_output_path(target_triple)
    for directory in (
        BUILD_DIRECTORY,
        BUILD_DIRECTORY / "cache",
        BUILD_DIRECTORY / "dist",
        BUILD_DIRECTORY / "spec",
        BUILD_DIRECTORY / "work",
        OUTPUT_DIRECTORY,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    pyinstaller_arguments = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        "--onefile",
        "--name",
        ENGINE_NAME,
        "--distpath",
        str(BUILD_DIRECTORY / "dist"),
        "--workpath",
        str(BUILD_DIRECTORY / "work"),
        "--specpath",
        str(BUILD_DIRECTORY / "spec"),
        "--collect-all",
        "tzdata",
    ]

    with ExitStack() as stack:
        oauth_source_value = os.environ.get("EOM_EMAIL_WATCHER_GOOGLE_OAUTH_CLIENT_FILE")
        if oauth_source_value:
            oauth_source = Path(oauth_source_value)
            validate_oauth_client(oauth_source)
            staged_oauth = stack.enter_context(
                _staged_build_input(
                    oauth_source,
                    "google-oauth-client.json",
                    "oauth-client.",
                )
            )
            pyinstaller_arguments.extend(
                ["--add-data", f"{staged_oauth}:eom_email_watcher_data"]
            )

        keyring_source_value = os.environ.get("LOCAL_CONNECT_ENTITLEMENT_KEYRING_FILE")
        if keyring_source_value:
            validate_entitlement_keyring_target(target_triple)
            keyring_source = Path(keyring_source_value)
            validate_entitlement_keyring(keyring_source)
            staged_keyring = stack.enter_context(
                _staged_build_input(
                    keyring_source,
                    "connect-entitlement-keyring.json",
                    "connect-keyring.",
                )
            )
            pyinstaller_arguments.extend(
                ["--add-data", f"{staged_keyring}:eom_email_watcher_data"]
            )

        pyinstaller_arguments.append(str(PROJECT_DIRECTORY / "packaging" / "engine_entry.py"))
        build_environment = os.environ.copy()
        build_environment["PYINSTALLER_CONFIG_DIR"] = str(BUILD_DIRECTORY / "cache")
        subprocess.run(
            pyinstaller_arguments,
            check=True,
            cwd=PROJECT_DIRECTORY,
            env=build_environment,
        )

    built_path = BUILD_DIRECTORY / "dist" / f"{ENGINE_NAME}{suffix}"
    _publish_sidecar(built_path, output_path)
    subprocess.run(
        [
            sys.executable,
            str(SCRIPT_DIRECTORY / "smoke_packaged_engine.py"),
            str(output_path),
        ],
        check=True,
        cwd=PROJECT_DIRECTORY,
    )
    return output_path


def main() -> None:
    try:
        output_path = build_sidecar()
    except SidecarBuildError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
    except FileNotFoundError as exc:
        print(f"Required sidecar build tool is unavailable: {exc.filename}", file=sys.stderr)
        raise SystemExit(2) from exc
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode or 1) from exc
    print(output_path)


if __name__ == "__main__":
    main()
