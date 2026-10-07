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


def _service_property(unit: str, name: str) -> object:
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


def verify_scheduled_readers(
    binary: Path,
    unit_directory: Path,
    own_worker_pids: frozenset[int] = frozenset(),
) -> None:
    """Before API request handling, bind configured timers to this packaged owner."""
    for unit, command in SCHEDULED_COMMANDS.items():
        if not (unit_directory / unit).exists() and not (unit_directory / (unit + ".d")).exists():
            continue
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
        pid = _service_property(unit, "MainPID")
        if type(pid) is not int or pid < 0:
            raise DeploymentError("Invalid scheduled reader metadata")
        if pid and pid not in own_worker_pids:
            raise DeploymentError(
                "A scheduled worker is still active; let it finish before updating"
            )


def main() -> None:
    from . import cli, engine_api

    args = sys.argv[1:]
    if args == ["--paired-cli-version"]:
        print(PAIRED_CLI_PROTOCOL)
        return
    alias = Path(sys.argv[0]).name in {"eom-mail-watch", "eom-mail-watch.exe"}
    cli_mode = args[:1] == ["--cli"] or alias
    cli_args = args[1:] if args[:1] == ["--cli"] else args
    readonly_version = cli_mode and cli_args == ["--version"]
    if getattr(sys, "frozen", False) and sys.platform == "linux" and not readonly_version:
        unit_directory = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        # PyInstaller's parent bootloader is the systemd MainPID. Its application
        # child is this process. Other active workers must finish before migration.
        own_worker_pids = frozenset({os.getpid(), os.getppid()}) if cli_mode else frozenset()
        try:
            verify_scheduled_readers(
                Path(sys.executable),
                unit_directory / "systemd/user",
                own_worker_pids,
            )
        except DeploymentError as exc:
            print(f"error: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
    if cli_mode:
        cli.main(cli_args)
    else:
        engine_api.main()
