from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

PAIRED_CLI_PROTOCOL = "eom-mail-engine-paired-cli-v1"
SCHEDULED_COMMANDS = {
    "eom-email-watcher.service": "check",
    "eom-monthly-hours.service": "send-hours",
}


class DeploymentError(RuntimeError):
    pass


def service_unit_directory() -> Path:
    """One XDG resolver for installation and both packaged entrypoints."""
    configured = os.environ.get("XDG_CONFIG_HOME")
    root = (
        Path(configured)
        if configured and Path(configured).is_absolute()
        else Path.home() / ".config"
    )
    if not root.is_absolute() or "\n" in str(root) or "\r" in str(root):
        raise DeploymentError("Scheduled unit directory must be one absolute path")
    return root / "systemd/user"


def _service_property(unit: str, name: str) -> object:
    if name == "LoadState":
        try:
            result = subprocess.run(
                ["systemctl", "--user", "show", unit, "--property=LoadState", "--value"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DeploymentError("Cannot verify the configured scheduled reader") from exc
        if result.returncode != 0 or result.stdout.strip() not in {"loaded", "not-found"}:
            raise DeploymentError("Cannot verify the configured scheduled reader")
        return result.stdout.strip()
    # systemd DBus escaping: these fixed unit names contain only letters and '-.'.
    escaped = unit.replace("-", "_2d").replace(".", "_2e")
    try:
        result = subprocess.run(
            [
                "busctl",
                "--user",
                "--json=short",
                "get-property",
                "org.freedesktop.systemd1",
                "/org/freedesktop/systemd1/unit/" + escaped,
                "org.freedesktop.systemd1.Service",
                name,
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0:
            raise DeploymentError("Cannot verify the configured scheduled reader")
        value = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise DeploymentError("Cannot verify the configured scheduled reader") from exc
    if not isinstance(value, dict) or set(value) != {"type", "data"}:
        raise DeploymentError("Invalid scheduled reader metadata")
    expected_type = "u" if name == "MainPID" else "a(sasbttttuii)"
    if value["type"] != expected_type:
        raise DeploymentError("Invalid scheduled reader metadata")
    return value["data"]


def _same_executable(value: object, binary: Path) -> bool:
    if not isinstance(value, str) or not Path(value).is_absolute():
        return False
    try:
        return Path(value).samefile(binary)
    except OSError:
        return False


def _service_main_pid(unit: str) -> int:
    pid = _service_property(unit, "MainPID")
    if type(pid) is not int or pid < 0:
        raise DeploymentError("Invalid scheduled reader metadata")
    return pid


def verify_scheduled_readers(
    binary: Path,
) -> None:
    """Bind manager configuration and running readers to this packaged owner."""
    for unit, command in SCHEDULED_COMMANDS.items():
        load_state = _service_property(unit, "LoadState")
        pid = _service_main_pid(unit)
        if load_state == "not-found" and pid == 0:
            continue
        if load_state != "loaded":
            raise DeploymentError("Cannot verify the configured scheduled reader")
        entries = _service_property(unit, "ExecStart")
        if not isinstance(entries, list) or len(entries) != 1:
            raise DeploymentError("Scheduled intake must use the paired desktop engine")
        entry = entries[0]
        if not isinstance(entry, list) or len(entry) != 10:
            raise DeploymentError("Invalid scheduled reader metadata")
        executable, argv, ignore_failure = entry[:3]
        if (
            not _same_executable(executable, binary)
            or not isinstance(argv, list)
            or len(argv) != 2
            or not _same_executable(argv[0], binary)
            or Path(argv[0]).name != "eom-mail-watch"
            or argv[1] != command
            or ignore_failure is not False
        ):
            raise DeploymentError(
                "Scheduled intake must use the paired desktop engine; "
                "rerun install-user-services.sh"
            )
        # ExecStart may name an updated binary while the old inode still runs.
        # Admit an observed worker exit only after a typed zero MainPID report.
        if (
            pid
            and not _same_executable(f"/proc/{pid}/exe", binary)
            and _service_main_pid(unit) != 0
        ):
            raise DeploymentError(
                "An incompatible scheduled worker is still active; "
                "let it finish before updating"
            )


def _dispatch_entrypoint() -> None:
    from . import cli, engine_api

    args = sys.argv[1:]
    if args == ["--paired-cli-version"]:
        print(PAIRED_CLI_PROTOCOL)
        return
    if args == ["--service-unit-directory"]:
        print(service_unit_directory())
        return
    alias = Path(sys.argv[0]).name in {"eom-mail-watch", "eom-mail-watch.exe"}
    cli_mode = args[:1] == ["--cli"] or alias
    cli_args = args[1:] if args[:1] == ["--cli"] else args
    readonly_version = cli_mode and cli_args == ["--version"]
    if getattr(sys, "frozen", False) and sys.platform == "linux" and not readonly_version:
        verify_scheduled_readers(Path(sys.executable))
    if cli_mode:
        cli.main(cli_args)
    else:
        engine_api.main()


def main() -> None:
    try:
        _dispatch_entrypoint()
    except DeploymentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
