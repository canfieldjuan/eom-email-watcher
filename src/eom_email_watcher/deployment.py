from __future__ import annotations

import os
import selectors
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

PAIRED_CLI_PROTOCOL = "eom-mail-engine-paired-cli-v1"
SCHEDULED_JOBS = {
    "eom-email-watcher.timer": ("eom-email-watcher.service", "check"),
    "eom-monthly-hours.timer": ("eom-monthly-hours.service", "send-hours"),
}
SCHEDULED_COMMANDS = {service: command for service, command in SCHEDULED_JOBS.values()}
UNIT_NAMES = tuple(
    sorted(set(SCHEDULED_JOBS) | set(SCHEDULED_COMMANDS) | {"eom-email-lmstudio.service"})
)
_MANAGER_FIELDS = (
    "Id",
    "LoadState",
    "ActiveState",
    "FragmentPath",
    "DropInPaths",
    "NeedDaemonReload",
    "MainPID",
    "ControlPID",
    "Unit",
)
MAX_MANAGER_BYTES = 64 * 1024
MANAGER_TIMEOUT_SECONDS = 5


class DeploymentError(Exception):
    """Deployment refusal, distinct from stale state and concurrent updates."""


@dataclass(frozen=True)
class UnitPayload:
    name: str
    content: bytes


@dataclass(frozen=True)
class DeploymentDescription:
    binary: Path
    executable_identity: tuple[int, int]
    alias: Path
    unit_directory: Path
    units: tuple[UnitPayload, ...]


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


def _unit_payloads() -> tuple[UnitPayload, ...]:
    try:
        root = (
            Path(sys._MEIPASS) / "eom_email_watcher_data/systemd"
            if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parents[2] / "systemd"
        )
        if {path.name for path in root.iterdir()} != set(UNIT_NAMES):
            raise DeploymentError("The engine has an incomplete shipped deployment")
        units = tuple(UnitPayload(name, (root / name).read_bytes()) for name in UNIT_NAMES)
    except (OSError, AttributeError) as exc:
        raise DeploymentError("The engine has no shipped deployment payloads") from exc
    if any(not unit.content or len(unit.content) > MAX_MANAGER_BYTES for unit in units):
        raise DeploymentError("The engine has invalid shipped deployment payloads")
    return units


def _inode(path: Path) -> tuple[int, int]:
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise OSError("Executable is not a regular file")
    return info.st_dev, info.st_ino


def deployment_description(binary: Path) -> DeploymentDescription:
    alias = Path.home() / ".local/bin/eom-mail-watch"
    if not binary.is_absolute() or not alias.is_absolute() or any(c in str(alias) for c in "\r\n"):
        raise DeploymentError("The deployment must name an absolute desktop engine and alias")
    try:
        identity = _inode(binary)
    except OSError as exc:
        raise DeploymentError("The desktop engine cannot be identified") from exc
    return DeploymentDescription(
        binary, identity, alias, service_unit_directory(), _unit_payloads()
    )


def _same_executable(value: object, binary: Path) -> bool:
    if not isinstance(value, str) or not Path(value).is_absolute():
        return False
    try:
        return Path(value).samefile(binary)
    except OSError:
        return False


def _current_engine(description: DeploymentDescription) -> None:
    try:
        unchanged = _inode(description.binary) == description.executable_identity
    except OSError:
        unchanged = False
    if not unchanged or not _same_executable("/proc/self/exe", description.binary):
        raise DeploymentError("The running engine was replaced; restart it before database access")


def _manager_output(names: tuple[str, ...]) -> str:
    """One fixed-set system read, with a deadline and a bounded stdout pipe."""
    command = [
        "systemctl",
        "--user",
        "show",
        "--all",
        "--no-pager",
        "--property=" + ",".join(_MANAGER_FIELDS),
        *names,
    ]
    try:
        with subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        ) as process:
            try:
                assert process.stdout is not None
                output = bytearray()
                deadline = time.monotonic() + MANAGER_TIMEOUT_SECONDS
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or not selector.select(remaining):
                            raise DeploymentError(
                                "The user manager did not respond before the deadline"
                            )
                        chunk = os.read(
                            process.stdout.fileno(), min(8192, MAX_MANAGER_BYTES + 1 - len(output))
                        )
                        if not chunk:
                            break
                        output.extend(chunk)
                        if len(output) > MAX_MANAGER_BYTES:
                            raise DeploymentError(
                                "The user manager returned too much deployment metadata"
                            )
                if process.wait(timeout=max(0, deadline - time.monotonic())) != 0:
                    raise DeploymentError("The user manager is unavailable; repair the deployment")
                return output.decode("utf-8", errors="strict")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        raise DeploymentError("The user manager is unavailable; repair the deployment") from exc


def _manager_snapshot(names: tuple[str, ...]) -> dict[str, dict[str, str]]:
    raw = _manager_output(names)
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_MANAGER_BYTES:
        raise DeploymentError("Invalid user-manager deployment metadata")
    records: dict[str, dict[str, str]] = {}
    for block in raw.strip("\n").split("\n\n"):
        record: dict[str, str] = {}
        for line in block.splitlines():
            key, separator, value = line.partition("=")
            if separator != "=" or key not in _MANAGER_FIELDS or key in record:
                raise DeploymentError("Invalid user-manager deployment metadata")
            record[key] = value
        name = record.get("Id")
        common = set(_MANAGER_FIELDS) - {"MainPID", "ControlPID", "Unit"}
        required = common | (
            {"Unit"} if name and name.endswith(".timer") else {"MainPID", "ControlPID"}
        )
        if name not in names or name in records or required != set(record):
            raise DeploymentError("Incomplete or duplicate user-manager unit metadata")
        if record["ActiveState"] not in {
            "active",
            "inactive",
            "failed",
            "activating",
            "deactivating",
            "reloading",
            "maintenance",
            "refreshing",
        }:
            raise DeploymentError("Invalid user-manager activity state")
        if record["NeedDaemonReload"] not in {"yes", "no"}:
            raise DeploymentError("Invalid user-manager reload state")
        for key in ("MainPID", "ControlPID"):
            if key in record and (
                not record[key].isascii()
                or not record[key].isdigit()
                or len(record[key]) > 10
                or int(record[key]) > 2**32 - 1
            ):
                raise DeploymentError("Invalid user-manager process identity")
        records[name] = record
    if set(records) != set(names):
        raise DeploymentError("Incomplete user-manager deployment graph")
    return records


def _installed_files(description: DeploymentDescription) -> tuple[tuple[str, object], ...]:
    """Capture the installed payload and alias identities around manager observation."""
    result: list[tuple[str, object]] = []
    for unit in description.units:
        path = description.unit_directory / unit.name
        try:
            with path.open("rb") as file:
                info = os.fstat(file.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or file.read(len(unit.content) + 1) != unit.content
                ):
                    raise DeploymentError(
                        "A scheduled unit differs from the shipped deployment; reinstall it"
                    )
                identity: object = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)
        except FileNotFoundError:
            if path.is_symlink():
                raise DeploymentError("A scheduled unit has an unresolved redirect") from None
            identity = None
        except OSError as exc:
            raise DeploymentError("A scheduled unit cannot be verified") from exc
        result.append((unit.name, identity))
    try:
        alias_identity: object = _inode(description.alias)
    except FileNotFoundError:
        if description.alias.is_symlink():
            raise DeploymentError("The paired scheduled alias is unresolved") from None
        alias_identity = None
    except OSError as exc:
        raise DeploymentError("The paired scheduled alias cannot be verified") from exc
    result.append(("alias", alias_identity))
    return tuple(result)


def _configured_graph(
    description: DeploymentDescription, snapshot: dict[str, dict[str, str]]
) -> bool:
    """One graph identity validator for pre-installation and database admission."""
    absent = all(
        record["LoadState"] == "not-found"
        and record["ActiveState"] == "inactive"
        and record["FragmentPath"] == ""
        and record["DropInPaths"] == ""
        and record["NeedDaemonReload"] == "no"
        and all(record.get(key, "0") == "0" for key in ("MainPID", "ControlPID"))
        and (name not in SCHEDULED_JOBS or record["Unit"] in {"", SCHEDULED_JOBS[name][0]})
        for name, record in snapshot.items()
    )
    if absent:
        return False
    for unit in description.units:
        record = snapshot[unit.name]
        if (
            record["LoadState"] != "loaded"
            or record["DropInPaths"] != ""
            or record["NeedDaemonReload"] != "no"
            or record["FragmentPath"] != str(description.unit_directory / unit.name)
        ):
            raise DeploymentError("The scheduled graph is not the shipped deployment; reinstall it")
        if unit.name in SCHEDULED_JOBS and record["Unit"] != SCHEDULED_JOBS[unit.name][0]:
            raise DeploymentError("The scheduled timer does not target its shipped service")
        if unit.name.endswith(".service"):
            for key in ("MainPID", "ControlPID"):
                pid = int(record[key])
                if pid and (
                    unit.name not in SCHEDULED_COMMANDS
                    or not _same_executable(f"/proc/{pid}/exe", description.binary)
                ):
                    # A later check can admit an exit only by observing typed zero.
                    raise DeploymentError(
                        "An incompatible scheduled worker is still active; let it finish"
                    )
    return True


def _verify_description(description: DeploymentDescription) -> None:
    before = _installed_files(description)
    snapshot = _manager_snapshot(tuple(unit.name for unit in description.units))
    loaded = _configured_graph(description, snapshot)
    files = dict(before)
    if loaded:
        if any(files[unit.name] is None for unit in description.units):
            raise DeploymentError("The shipped deployment has missing installed units")
        if files["alias"] != description.executable_identity:
            raise DeploymentError("Scheduled intake must use the concrete paired desktop engine")
    elif any(files[unit.name] is not None for unit in description.units):
        raise DeploymentError("Scheduled unit files are not loaded; reload the user manager")
    if _installed_files(description) != before:
        raise DeploymentError("The deployment changed during admission; retry after repairing it")


def verify_scheduled_readers(binary: Path) -> None:
    _verify_description(deployment_description(binary))


def verify_database_admission() -> None:
    """A fresh deployment check at every packaged Linux database acquisition."""
    if not getattr(sys, "frozen", False) or sys.platform != "linux":
        return
    description = deployment_description(Path(sys.executable))
    _current_engine(description)
    _verify_description(description)
    _current_engine(description)


def _atomic_unit(path: Path, content: bytes) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".unit-", delete=False) as file:
            temporary = Path(file.name)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
            os.fchmod(file.fileno(), 0o644)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _install_units(description: DeploymentDescription) -> None:
    try:
        description.unit_directory.mkdir(parents=True, exist_ok=True)
        for unit in description.units:
            _atomic_unit(description.unit_directory / unit.name, unit.content)
        _manager_action(["daemon-reload"])
    except (OSError, subprocess.SubprocessError) as exc:
        raise DeploymentError("The scheduled deployment could not be installed") from exc


def _manager_action(arguments: list[str]) -> None:
    try:
        result = subprocess.run(
            ["systemctl", "--user", *arguments],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=MANAGER_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            raise DeploymentError(
                "Scheduled units were installed but the user manager needs repair"
            )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DeploymentError("The user manager could not complete installation") from exc


def _enable_timers() -> None:
    for timer in SCHEDULED_JOBS:
        _manager_action(["enable", timer])


def install_user_services() -> None:
    """The explicitly selected running native engine owns its entire deployment."""
    if not getattr(sys, "frozen", False) or sys.platform != "linux":
        raise DeploymentError("Select the concrete Linux desktop engine to install paired services")
    description = deployment_description(Path(sys.executable))
    _current_engine(description)
    _configured_graph(
        description, _manager_snapshot(tuple(unit.name for unit in description.units))
    )
    description.alias.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=description.alias.parent, prefix=".paired-cli-") as stage:
        alias = Path(stage) / "eom-mail-watch"
        alias.symlink_to(description.binary)
        _current_engine(description)
        os.replace(alias, description.alias)
    _install_units(description)
    _verify_description(description)
    _current_engine(description)
    _enable_timers()
    print(f"Paired scheduled intake with {description.binary}.")


def install_source_units() -> None:
    """Explicit source-only installation uses the same shipped unit payload owner."""
    _install_units(deployment_description(Path(sys.executable)))
    _enable_timers()


def _dispatch_entrypoint() -> None:
    from . import cli, engine_api

    args = sys.argv[1:]
    if args == ["--paired-cli-version"]:
        print(PAIRED_CLI_PROTOCOL)
        return
    if args == ["--service-unit-directory"]:
        print(service_unit_directory())
        return
    if args == ["--install-user-services"]:
        install_user_services()
        return
    alias = Path(sys.argv[0]).name in {"eom-mail-watch", "eom-mail-watch.exe"}
    cli_mode = args[:1] == ["--cli"] or alias
    cli_args = args[1:] if args[:1] == ["--cli"] else args
    readonly_version = cli_mode and cli_args == ["--version"]
    if not readonly_version:
        verify_database_admission()
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
