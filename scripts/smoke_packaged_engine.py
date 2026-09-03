#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROTOCOL_VERSION = 1
ENGINE_TIMEOUT_SECONDS = 90


class PackagedEngineSmokeError(RuntimeError):
    pass


def _request(
    binary: Path,
    *,
    config_path: Path,
    operation: str,
    payload: dict[str, object] | None,
    working_directory: Path,
    environment: dict[str, str],
) -> dict[str, object]:
    request = {
        "config_path": str(config_path),
        "operation": operation,
        "payload": payload or {},
        "protocol": PROTOCOL_VERSION,
    }
    result = subprocess.run(
        [str(binary)],
        input=json.dumps(request, separators=(",", ":")),
        capture_output=True,
        cwd=working_directory,
        env=environment,
        text=True,
        timeout=ENGINE_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        detail = result.stderr.strip()[-1000:] or "no stderr"
        raise PackagedEngineSmokeError(
            f"Packaged engine {operation} exited {result.returncode}: {detail}"
        )
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PackagedEngineSmokeError(
            f"Packaged engine {operation} returned invalid JSON"
        ) from exc
    if not isinstance(response, dict):
        raise PackagedEngineSmokeError(
            f"Packaged engine {operation} response must be an object"
        )
    if (
        response.get("ok") is not True
        or response.get("operation") != operation
        or response.get("protocol") != PROTOCOL_VERSION
        or not isinstance(response.get("data"), dict)
    ):
        raise PackagedEngineSmokeError(
            f"Packaged engine {operation} did not return a successful v1 response"
        )
    return response


def smoke_packaged_engine(binary: Path) -> None:
    if not binary.is_file():
        raise PackagedEngineSmokeError("Packaged engine is not a regular file")
    with tempfile.TemporaryDirectory(prefix="eom-mail-engine-smoke-") as temporary_value:
        temporary = Path(temporary_value)
        isolated_binary = temporary / binary.name
        shutil.copy2(binary, isolated_binary)
        if os.name != "nt":
            isolated_binary.chmod(0o755)

        private_root = temporary / "private"
        private_root.mkdir()
        config_path = private_root / "config.toml"
        environment = os.environ.copy()
        environment.pop("PYTHONHOME", None)
        environment.pop("PYTHONPATH", None)
        for key in ("APPDATA", "HOME", "LOCALAPPDATA", "USERPROFILE", "XDG_CONFIG_HOME"):
            environment[key] = str(private_root)

        initialized = _request(
            isolated_binary,
            config_path=config_path,
            operation="config.initialize",
            payload={
                "model_base_url": "http://127.0.0.1:9/v1",
                "model_name": "sidecar-build-smoke",
                "timezone": "America/Chicago",
            },
            working_directory=temporary,
            environment=environment,
        )
        settings = initialized["data"].get("settings")
        if not isinstance(settings, dict) or settings.get("timezone") != "America/Chicago":
            raise PackagedEngineSmokeError(
                "Packaged engine did not preserve the initialized timezone"
            )

        watchlist = _request(
            isolated_binary,
            config_path=config_path,
            operation="watchlist.list",
            payload=None,
            working_directory=temporary,
            environment=environment,
        )
        if watchlist["data"].get("items") != []:
            raise PackagedEngineSmokeError("Packaged engine first run did not start empty")

        health = _request(
            isolated_binary,
            config_path=config_path,
            operation="health.get",
            payload=None,
            working_directory=temporary,
            environment=environment,
        )
        if health["data"].get("watchlist_count") != 0:
            raise PackagedEngineSmokeError("Packaged engine health did not report zero senders")
        mail = health["data"].get("mail")
        providers = mail.get("providers") if isinstance(mail, dict) else None
        if not isinstance(providers, list) or "microsoft365" not in {
            item.get("provider") for item in providers if isinstance(item, dict)
        }:
            raise PackagedEngineSmokeError(
                "Packaged engine did not advertise the Microsoft 365 provider"
            )

        entitlement = _request(
            isolated_binary,
            config_path=config_path,
            operation="connect.entitlement.status",
            payload=None,
            working_directory=temporary,
            environment=environment,
        )
        expected_entitlement_state = (
            "missing"
            if os.environ.get("LOCAL_CONNECT_ENTITLEMENT_KEYRING_FILE")
            else "authority_unavailable"
        )
        if entitlement["data"] != {
            "state": expected_entitlement_state,
            "active": False,
        }:
            raise PackagedEngineSmokeError(
                "Packaged engine did not report the expected Connect authority state"
            )

        inactive_check = _request(
            isolated_binary,
            config_path=config_path,
            operation="watcher.check",
            payload={"dry_run": False},
            working_directory=temporary,
            environment=environment,
        )
        if inactive_check["data"].get("active") is not False:
            raise PackagedEngineSmokeError(
                "Packaged engine zero-sender watcher check was not safely inactive"
            )


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: smoke_packaged_engine.py <engine-binary>")
    try:
        smoke_packaged_engine(Path(sys.argv[1]).resolve())
    except PackagedEngineSmokeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
    print("packaged-engine-smoke: ok")


if __name__ == "__main__":
    main()
