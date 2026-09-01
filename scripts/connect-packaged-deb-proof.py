#!/usr/bin/env python3
from __future__ import annotations

import argparse
import errno
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from eom_email_watcher.db import Store
from eom_email_watcher.mime import AttachmentDescriptor

PROTOCOL_VERSION = 1
CAPABILITY_ID = "document.summarize"
CAPABILITY_VERSION = "1.0"
PROVIDER_APP_ID = "document-summarizer"
MESSAGE_ID = "packaged-connect-proof-message"
PART_ID = "packaged-connect-proof-pdf"
ENGINE_TIMEOUT_SECONDS = 30


class PackagedConnectProofError(RuntimeError):
    pass


def positive_seconds(value: str) -> int:
    try:
        seconds = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if seconds <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return seconds


def require_tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise PackagedConnectProofError(f"Required packaged-proof tool is unavailable: {name}")
    return path


def extract_binary(deb: Path, destination: Path, relative_binary: str) -> Path:
    try:
        source = deb.resolve(strict=True)
    except FileNotFoundError as exc:
        raise PackagedConnectProofError(f"Debian package does not exist: {deb}") from exc
    if not source.is_file():
        raise PackagedConnectProofError(f"Debian package is not a regular file: {source}")
    destination.mkdir(mode=0o700)
    result = subprocess.run(
        [require_tool("dpkg-deb"), "-x", str(source), str(destination)],
        capture_output=True,
        text=True,
        timeout=ENGINE_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise PackagedConnectProofError(
            f"Could not extract Debian package {source.name} (exit {result.returncode})"
        )
    binary = destination / relative_binary
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise PackagedConnectProofError(
            f"Debian package {source.name} does not contain executable {relative_binary}"
        )
    return binary


def isolated_environment(root: Path) -> dict[str, str]:
    environment = {
        name: value
        for name in ("LANG", "LC_ALL", "PATH", "TZ")
        if (value := os.environ.get(name)) is not None
    }
    environment.update(
        {
            "DOC_SUM_MODEL_BASE_URL": "http://127.0.0.1:9/v1",
            "DOC_SUM_MODEL_NAME": "packaged-connect-proof",
            "DOC_SUM_MODEL_TIMEOUT_SECONDS": "1",
            "GDK_BACKEND": "x11",
            "GIO_USE_VFS": "local",
            "GTK_USE_PORTAL": "0",
            "LIBGL_ALWAYS_SOFTWARE": "1",
            "NO_AT_BRIDGE": "1",
            "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_RUNTIME_DIR": str(root / "runtime"),
        }
    )
    return environment


def prepare_private_directories(root: Path) -> None:
    for name in ("cache", "config", "data", "runtime", "state"):
        directory = root / name
        directory.mkdir(mode=0o700)
        directory.chmod(0o700)


def _toml_string(value: Path | str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def write_isolated_config(config_path: Path, state_directory: Path) -> Path:
    state_directory = state_directory.resolve(strict=True)
    config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_path.parent.chmod(0o700)
    database_path = state_directory / "watcher.sqlite3"
    values = {
        "timezone": "UTC",
        "gmail_credentials_file": state_directory / "credentials.json",
        "gmail_token_file": state_directory / "token.json",
        "gmail_send_token_file": state_directory / "send-token.json",
        "database_file": database_path,
        "model_base_url": "http://127.0.0.1:9/v1",
        "model_name": "packaged-connect-proof",
    }
    content = "\n".join(f"{name} = {_toml_string(value)}" for name, value in values.items())
    content += "\nmodel_require_auth = false\nnotifications_enabled = false\n"
    config_path.write_text(content, encoding="utf-8")
    config_path.chmod(0o600)
    return database_path


def seed_attachment(database_path: Path) -> None:
    store = Store(database_path)
    store.initialize()
    inserted = store.add_message(
        message_id=MESSAGE_ID,
        thread_id=None,
        sender="fixture@example.com",
        sender_name="Packaged proof",
        subject="Packaged Local Connect proof",
        received_at="2026-01-01T00:00:00+00:00",
    )
    if not inserted:
        raise PackagedConnectProofError("Packaged proof fixture message already exists")
    store.replace_attachments(
        MESSAGE_ID,
        (
            AttachmentDescriptor(
                PART_ID,
                "fixture-attachment",
                "fixture.pdf",
                "application/pdf",
                4,
                0,
            ),
        ),
    )
    database_path.chmod(0o600)


def engine_request(
    binary: Path,
    environment: dict[str, str],
    config_path: Path,
    operation: str,
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    request = {
        "config_path": str(config_path),
        "operation": operation,
        "payload": payload or {},
        "protocol": PROTOCOL_VERSION,
    }
    try:
        result = subprocess.run(
            [str(binary)],
            input=json.dumps(request, separators=(",", ":")),
            capture_output=True,
            env=environment,
            text=True,
            timeout=ENGINE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise PackagedConnectProofError(f"Packaged engine {operation} timed out") from exc
    if result.returncode != 0:
        raise PackagedConnectProofError(
            f"Packaged engine {operation} exited {result.returncode}"
        )
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PackagedConnectProofError(
            f"Packaged engine {operation} returned invalid JSON"
        ) from exc
    if (
        not isinstance(response, dict)
        or response.get("ok") is not True
        or response.get("operation") != operation
        or response.get("protocol") != PROTOCOL_VERSION
        or not isinstance(response.get("data"), dict)
    ):
        raise PackagedConnectProofError(
            f"Packaged engine {operation} did not return a successful v1 response"
        )
    return response["data"]


def summary_capabilities(data: dict[str, object]) -> list[dict[str, object]]:
    items = data.get("items")
    if not isinstance(items, list):
        raise PackagedConnectProofError("Capability discovery did not return an item list")
    matches: list[dict[str, object]] = []
    for item in items:
        if not isinstance(item, dict):
            raise PackagedConnectProofError("Capability discovery returned a malformed item")
        capability = item.get("capability")
        provider = item.get("provider")
        if not isinstance(capability, dict) or not isinstance(provider, dict):
            raise PackagedConnectProofError("Capability discovery returned a malformed declaration")
        if (
            capability.get("id") == CAPABILITY_ID
            and capability.get("version") == CAPABILITY_VERSION
        ):
            if (
                item.get("protocol_version") != 2
                or provider.get("available") is not True
                or provider.get("app_id") != PROVIDER_APP_ID
            ):
                raise PackagedConnectProofError(
                    "Document summary capability was not an available protocol-v2 declaration"
                )
            matches.append(item)
    return matches


def capability_state_satisfied(matches: list[dict[str, object]], available: bool) -> bool:
    return len(matches) == 1 if available else len(matches) == 0


@dataclass
class ProviderProcess:
    process: subprocess.Popen[bytes]
    log_stream: BinaryIO

    def stop(self) -> None:
        if self.process.poll() is None:
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
        self.log_stream.close()

    def failure_detail(self) -> str:
        return_code = self.process.poll()
        return f"provider exited with status {return_code}; provider logs remain private"


def start_provider(binary: Path, environment: dict[str, str], log_path: Path) -> ProviderProcess:
    log_stream = log_path.open("ab")
    try:
        process = subprocess.Popen(
            [
                require_tool("dbus-run-session"),
                "--",
                require_tool("xvfb-run"),
                "-a",
                str(binary),
            ],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log_stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except Exception:
        log_stream.close()
        raise
    return ProviderProcess(process, log_stream)


def discover_attachment_capabilities(
    engine: Path, environment: dict[str, str], config_path: Path
) -> dict[str, object]:
    return engine_request(
        engine,
        environment,
        config_path,
        "connect.attachment.capabilities",
        {"message_id": MESSAGE_ID, "part_id": PART_ID},
    )


def wait_for_summary_capability(
    engine: Path,
    environment: dict[str, str],
    config_path: Path,
    *,
    available: bool,
    timeout_seconds: int,
    provider: ProviderProcess | None,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    deadline = time.monotonic() + timeout_seconds
    latest: dict[str, object] = {"items": []}
    matches: list[dict[str, object]] = []
    while time.monotonic() < deadline:
        if available and provider is not None and provider.process.poll() is not None:
            raise PackagedConnectProofError(
                "Packaged provider exited before advertising a capability: "
                f"{provider.failure_detail()}"
            )
        latest = discover_attachment_capabilities(engine, environment, config_path)
        matches = summary_capabilities(latest)
        if capability_state_satisfied(matches, available):
            return latest, matches
        time.sleep(0.2)
    raise PackagedConnectProofError(
        f"Packaged capability availability did not become {available}; "
        f"observed {len(matches)} matching declarations"
    )


def require_checks(checks: dict[str, bool]) -> None:
    failed = sorted(name for name, passed in checks.items() if not passed)
    if failed:
        raise PackagedConnectProofError(f"Packaged Connect proof failed: {', '.join(failed)}")


def wait_for_runtime_release(runtime_directory: Path, timeout_seconds: int = 10) -> None:
    """Wait until desktop-session helpers release their test-owned runtime mounts."""
    runtime_prefix = f"{runtime_directory}{os.sep}"
    deadline = time.monotonic() + timeout_seconds
    stable_observations = 0
    while time.monotonic() < deadline:
        mount_targets = []
        for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
            fields = line.split()
            if len(fields) < 5:
                continue
            target = re.sub(
                r"\\([0-7]{3})",
                lambda match: chr(int(match.group(1), 8)),
                fields[4],
            )
            if target == str(runtime_directory) or target.startswith(runtime_prefix):
                mount_targets.append(target)
        try:
            pending = [runtime_directory]
            while pending:
                directory = pending.pop()
                with os.scandir(directory) as entries:
                    for entry in entries:
                        entry.stat(follow_symlinks=False)
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(Path(entry.path))
        except OSError as exc:
            if exc.errno not in {
                errno.ECONNABORTED,
                errno.EIO,
                errno.ENOTCONN,
                errno.ESTALE,
            }:
                raise PackagedConnectProofError(
                    "Could not inspect the isolated desktop runtime directory"
                ) from exc
            stable_observations = 0
        else:
            stable_observations = stable_observations + 1 if not mount_targets else 0
            if stable_observations == 3:
                return
        time.sleep(0.1)
    raise PackagedConnectProofError(
        "Isolated desktop helpers did not release their runtime mount after provider shutdown"
    )


def run_proof(
    consumer_deb: Path,
    provider_deb: Path,
    active_entitlement: Path,
    timeout_seconds: int,
) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="connect-packaged-deb-proof-") as temporary_value:
        root = Path(temporary_value)
        prepare_private_directories(root)
        environment = isolated_environment(root)
        consumer = extract_binary(
            consumer_deb,
            root / "consumer-package",
            "usr/bin/eom-mail-engine",
        )
        provider_binary = extract_binary(
            provider_deb,
            root / "provider-package",
            "usr/bin/document-summarizer",
        )
        config_path = root / "config" / "email-watcher" / "config.toml"
        database_path = write_isolated_config(config_path, root / "state")
        seed_attachment(database_path)

        entitlement_path = active_entitlement.resolve(strict=True)
        installed = engine_request(
            consumer,
            environment,
            config_path,
            "connect.entitlement.install",
            {"source_path": str(entitlement_path)},
        )
        before = discover_attachment_capabilities(consumer, environment, config_path)
        before_matches = summary_capabilities(before)

        provider: ProviderProcess | None = None
        restarted: ProviderProcess | None = None
        try:
            provider = start_provider(provider_binary, environment, root / "provider-first.log")
            _during, during_matches = wait_for_summary_capability(
                consumer,
                environment,
                config_path,
                available=True,
                timeout_seconds=timeout_seconds,
                provider=provider,
            )
            first_provider = during_matches[0]["provider"]
            if not isinstance(first_provider, dict):
                raise PackagedConnectProofError("Packaged provider identity was malformed")
            first_instance = first_provider.get("instance_id")
            first_app = first_provider.get("app_id")

            provider.stop()
            provider = None
            _absent, absent_matches = wait_for_summary_capability(
                consumer,
                environment,
                config_path,
                available=False,
                timeout_seconds=timeout_seconds,
                provider=None,
            )
            inbox = engine_request(
                consumer,
                environment,
                config_path,
                "inbox.recent",
                {"limit": 10},
            )
            inbox_items = inbox.get("items")

            restarted = start_provider(
                provider_binary,
                environment,
                root / "provider-restarted.log",
            )
            _restored, restored_matches = wait_for_summary_capability(
                consumer,
                environment,
                config_path,
                available=True,
                timeout_seconds=timeout_seconds,
                provider=restarted,
            )
            restored_provider = restored_matches[0]["provider"]
            if not isinstance(restored_provider, dict):
                raise PackagedConnectProofError("Restarted provider identity was malformed")

            checks = {
                "consumer_entitlement_active": installed.get("active") is True,
                "before_provider_absent": not before_matches,
                "packaged_v2_capability_discovered": len(during_matches) == 1,
                "provider_removed": not absent_matches,
                "consumer_inbox_healthy_without_provider": (
                    isinstance(inbox_items, list)
                    and any(
                        isinstance(item, dict) and item.get("message_id") == MESSAGE_ID
                        for item in inbox_items
                    )
                ),
                "provider_restored": len(restored_matches) == 1,
                "provider_identity_expected": first_app == PROVIDER_APP_ID,
                "provider_instance_stable": (
                    isinstance(first_instance, str)
                    and first_instance != ""
                    and restored_provider.get("instance_id") == first_instance
                ),
            }
            require_checks(checks)
            return {
                "checks": checks,
                "counts": {
                    "before": len(before_matches),
                    "during": len(during_matches),
                    "after_stop": len(absent_matches),
                    "after_restart": len(restored_matches),
                },
                "provider": {
                    "app_id": first_app,
                    "capability_id": CAPABILITY_ID,
                    "capability_version": CAPABILITY_VERSION,
                },
            }
        finally:
            if provider is not None:
                provider.stop()
            if restarted is not None:
                restarted.stop()
            wait_for_runtime_release(root / "runtime")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Prove generic Local Connect discovery between extracted Email Watcher and "
            "Document Summarizer Debian packages"
        )
    )
    parser.add_argument("--consumer-deb", type=Path, required=True)
    parser.add_argument("--provider-deb", type=Path, required=True)
    parser.add_argument("--active-entitlement", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=positive_seconds, default=30)
    args = parser.parse_args()
    try:
        result = run_proof(
            args.consumer_deb,
            args.provider_deb,
            args.active_entitlement,
            args.timeout_seconds,
        )
    except (FileNotFoundError, PackagedConnectProofError) as exc:
        parser.exit(1, f"Packaged Connect proof failed: {exc}\n")
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))


if __name__ == "__main__":
    main()
