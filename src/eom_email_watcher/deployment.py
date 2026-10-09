from __future__ import annotations

import codecs
import os
import re
import selectors
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
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
    "ExecStart",
)
MAX_MANAGER_BYTES = 64 * 1024
MANAGER_TIMEOUT_SECONDS = 5
LEASE_TIMEOUT_SECONDS = 5
_lease_state_lock = threading.Lock()
_shared_leases = 0


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
    manager: ManagerView


@dataclass(frozen=True)
class ManagerView:
    home: Path
    unit_directory: Path
    data_home: Path
    alias: Path

    @property
    def lease_anchor(self) -> Path:
        return self.home.resolve(strict=True)


def _manager_words(raw: str) -> tuple[str, ...]:
    """Decode systemd shell_maybe_quote words without evaluating shell input."""
    words = []
    end = 0
    for match in re.finditer(r"\$'(?:[^'\\]|\\.)*'|'[^']*'|[^\s'\"\\]+", raw):
        if raw[end:match.start()].strip():
            raise DeploymentError("Invalid manager path encoding")
        token = match.group()
        if token.startswith("$'"):
            body = token[2:-1]
            if not re.fullmatch(
                r"(?:[^'\\]|\\(?:[abfnrtv\\'\"]|[0-3][0-7]{2}|x[0-9a-fA-F]{2}))*", body
            ):
                raise DeploymentError("Invalid manager path escape")
            try:
                token = codecs.escape_decode(body.encode("utf-8"))[0].decode("utf-8")
            except (ValueError, UnicodeError) as exc:
                raise DeploymentError("Invalid manager path encoding") from exc
        elif token.startswith("'"):
            token = token[1:-1]
        words.append(token)
        end = match.end()
    if raw[end:].strip():
        raise DeploymentError("Invalid manager path encoding")
    return tuple(words)


def _account_home() -> Path:
    import pwd

    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError) as exc:
        raise DeploymentError("The account home cannot be identified") from exc


def _manager_unit_paths() -> tuple[Path, ...]:
    raw = _read_manager_command(["systemctl", "--user", "show", "--property=UnitPath", "--value"])
    paths = tuple(Path(word) for word in _manager_words(raw))
    if not paths or any(
        not path.is_absolute() or any(c in str(path) for c in "\r\n\0") for path in paths
    ):
        raise DeploymentError("The manager UnitPath is invalid; repair it")
    return paths


def _manager_environment() -> dict[str, str]:
    raw = _read_manager_command(["systemctl", "--user", "show-environment"])
    selected: dict[str, str] = {}
    for line in raw.splitlines():
        if not any(
            line.startswith(key + "=") for key in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME")
        ):
            continue
        key, _, raw_value = line.partition("=")
        words = _manager_words(raw_value) if raw_value else ("",)
        if len(words) != 1:
            raise DeploymentError("The manager environment needs repair")
        value = words[0]
        if key in selected:
            raise DeploymentError("The manager environment has duplicate paths")
        selected[key] = value
    return selected


def manager_view() -> ManagerView:
    environment = _manager_environment()
    home = Path(environment.get("HOME", ""))
    configured = Path(environment.get("XDG_CONFIG_HOME", ""))
    configured_data = Path(environment.get("XDG_DATA_HOME", ""))
    config = configured if configured.is_absolute() else home / ".config"
    data = configured_data if configured_data.is_absolute() else home / ".local/share"
    if any(not path.is_absolute() or any(c in str(path) for c in "\r\n\0")
           for path in (home, config, data)):
        raise DeploymentError("The manager must report absolute HOME and XDG paths; repair it")
    account = _account_home()
    if not account.is_absolute() or home != account:
        raise DeploymentError("The manager HOME disagrees with the account home; repair it")
    directory = config / "systemd/user"
    if directory not in _manager_unit_paths():
        raise DeploymentError(
            "The configured unit directory is absent from manager UnitPath; repair it"
        )
    return ManagerView(account, directory, data, home / ".local/bin/eom-mail-watch")


def service_unit_directory() -> Path:
    """The user manager owns installation paths, including for shell publishers."""
    return manager_view().unit_directory


def _verify_lease_identity(descriptor: int, path: Path) -> None:
    identity = os.fstat(descriptor)
    named = path.stat()
    if (
        not stat.S_ISDIR(identity.st_mode) or identity.st_uid != os.getuid()
        or not stat.S_ISDIR(named.st_mode)
        or (identity.st_dev, identity.st_ino) != (named.st_dev, named.st_ino)
    ):
        raise DeploymentError("The deployment lock cannot be identified; repair it")


@contextmanager
def deployment_lease(
    view: ManagerView, *, exclusive: bool, inherited_fd: int | None = None
) -> Iterator[int]:
    """One bounded flock owner; inherited source publishers retain the same lease."""
    import fcntl

    global _shared_leases
    with _lease_state_lock:
        if exclusive and _shared_leases:
            raise DeploymentError("Close this process's database connections before installation")
    message = (
        "Close the app or stop the timers, then retry installation"
        if exclusive else "Deployment is being updated; retry after installation"
    )
    descriptor: int | None = None
    counted = False
    try:
        try:
            if inherited_fd is not None:
                if not exclusive:
                    raise DeploymentError("A publication lease cannot become a database lease")
                descriptor = inherited_fd
            else:
                descriptor = os.open(
                    view.lease_anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                )
            _verify_lease_identity(descriptor, view.lease_anchor)
            deadline = time.monotonic() + LEASE_TIMEOUT_SECONDS
            while True:
                try:
                    fcntl.flock(
                        descriptor,
                        (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB,
                    )
                    break
                except BlockingIOError:
                    if inherited_fd is not None or time.monotonic() >= deadline:
                        raise DeploymentError(message) from None
                    time.sleep(min(0.05, max(0, deadline - time.monotonic())))
            _verify_lease_identity(descriptor, view.lease_anchor)
        except OSError as exc:
            raise DeploymentError("The deployment lock is unavailable; repair it") from exc
        if not exclusive:
            with _lease_state_lock:
                _shared_leases += 1
                counted = True
        yield descriptor
    finally:
        if counted:
            with _lease_state_lock:
                _shared_leases -= 1
        if descriptor is not None and inherited_fd is None:
            # Never replace the home anchor or unlock an inherited publisher descriptor.
            os.close(descriptor)


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


def deployment_description(binary: Path, view: ManagerView | None = None) -> DeploymentDescription:
    view = view or manager_view()
    if not binary.is_absolute():
        raise DeploymentError("The deployment must name an absolute desktop engine")
    try:
        # Resolve parents and each final symlink separately: resolving the alias
        # fully would lose the fact that the selected path traverses that target.
        target = view.alias.parent.resolve() / view.alias.name
        selected = binary
        visited: set[Path] = set()
        while True:
            selected = selected.parent.resolve() / selected.name
            if selected == target:
                raise DeploymentError("Select the distinct bundle sidecar, not the scheduled alias")
            if selected in visited:
                raise DeploymentError("Select an engine without a cyclic redirect")
            visited.add(selected)
            if not selected.is_symlink():
                break
            redirect = selected.readlink()
            selected = redirect if redirect.is_absolute() else selected.parent / redirect
        identity = _inode(selected)
    except OSError as exc:
        raise DeploymentError("The desktop engine cannot be identified") from exc
    return DeploymentDescription(
        selected, identity, view.alias, view.unit_directory, _unit_payloads(), view
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
    return _read_manager_command(command)


def _read_manager_command(command: list[str]) -> str:
    """Shared deadline/output bound for manager environment and unit reads."""
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
        common = set(_MANAGER_FIELDS) - {"MainPID", "ControlPID", "Unit", "ExecStart"}
        required = common | (
            {"Unit"} if name and name.endswith(".timer") else {"MainPID", "ControlPID", "ExecStart"}
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
        and record.get("ExecStart", "") == ""
        and (name not in SCHEDULED_JOBS or record["Unit"] in {"", SCHEDULED_JOBS[name][0]})
        for name, record in snapshot.items()
    )
    if absent:
        return False
    for unit in description.units:
        record = snapshot[unit.name]
        if record["LoadState"] == "masked":
            raise DeploymentError(
                f"Scheduled unit {unit.name} is masked; run systemctl --user unmask {unit.name}, "
                "then systemctl --user daemon-reload before paired installation"
            )
        if record["DropInPaths"]:
            raise DeploymentError(
                f"Scheduled unit {unit.name} has effective drop-ins; remove its user/vendor "
                "overrides and run systemctl --user daemon-reload before paired installation"
            )
        if record["NeedDaemonReload"] != "no":
            raise DeploymentError("Run systemctl --user daemon-reload before paired installation")
        if record["LoadState"] != "loaded":
            raise DeploymentError(
                "The scheduled graph is partial; remove the partial five-unit installation, "
                "run systemctl --user daemon-reload, then install the complete paired deployment"
            )
        if record["FragmentPath"] != str(description.unit_directory / unit.name):
            raise DeploymentError(
                f"Scheduled unit {unit.name} uses a foreign fragment; remove that fragment, "
                "run systemctl --user daemon-reload, then install the paired deployment"
            )
        if unit.name in SCHEDULED_JOBS and record["Unit"] != SCHEDULED_JOBS[unit.name][0]:
            raise DeploymentError("The scheduled timer does not target its shipped service")
        if unit.name in SCHEDULED_COMMANDS:
            paths = re.findall(r"\{ path=(.*?) ;", record["ExecStart"])
            if paths != [str(description.alias)]:
                raise DeploymentError(
                    "The manager's ExecStart must use its paired alias; reinstall"
                )
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


def _verify_description(description: DeploymentDescription, *, paired: bool = True) -> None:
    before = _installed_files(description)
    snapshot = _manager_snapshot(tuple(unit.name for unit in description.units))
    loaded = _configured_graph(description, snapshot)
    files = dict(before)
    if loaded:
        if any(files[unit.name] is None for unit in description.units):
            raise DeploymentError("The shipped deployment has missing installed units")
        if paired and files["alias"] != description.executable_identity:
            raise DeploymentError("Scheduled intake must use the concrete paired desktop engine")
    elif any(files[unit.name] is not None for unit in description.units):
        raise DeploymentError("Scheduled unit files are not loaded; reload the user manager")
    if _installed_files(description) != before:
        raise DeploymentError("The deployment changed during admission; retry after repairing it")


def verify_scheduled_readers(binary: Path) -> None:
    _verify_description(deployment_description(binary))


@contextmanager
def database_admission() -> Iterator[None]:
    """Hold admission through the complete packaged Linux connection lifetime."""
    if not getattr(sys, "frozen", False) or sys.platform != "linux":
        yield
        return
    description = deployment_description(Path(sys.executable))
    with deployment_lease(description.manager, exclusive=False):
        _current_engine(description)
        _verify_description(description)
        _current_engine(description)
        yield


def verify_database_admission() -> None:
    """Read-only startup check; Store.connection owns its longer database lease."""
    with database_admission():
        pass


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


def install_user_services(selected_engine: Path | None = None) -> None:
    """The explicitly selected running native engine owns its entire deployment."""
    if not getattr(sys, "frozen", False) or sys.platform != "linux":
        raise DeploymentError("Select the concrete Linux desktop engine to install paired services")
    # A frozen runtime resolves sys.executable; retain the launch path from the
    # dispatcher so an alias cannot become its own publication source.
    description = deployment_description(selected_engine or Path(sys.executable))
    with deployment_lease(description.manager, exclusive=True):
        _current_engine(description)
        _configured_graph(
            description, _manager_snapshot(tuple(unit.name for unit in description.units))
        )
        description.alias.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            dir=description.alias.parent, prefix=".paired-cli-"
        ) as stage:
            alias = Path(stage) / "eom-mail-watch"
            alias.symlink_to(description.binary)
            _current_engine(description)
            os.replace(alias, description.alias)
        _install_units(description)
        _verify_description(description)
        _current_engine(description)
        _enable_timers()
    print(f"Paired scheduled intake with {description.binary}.")


def install_source_units(inherited_fd: int | None = None) -> None:
    """The shell's snapshot publication and these unit writes share one lease."""
    description = deployment_description(Path(sys.executable))
    with deployment_lease(description.manager, exclusive=True, inherited_fd=inherited_fd):
        _configured_graph(description, _manager_snapshot(UNIT_NAMES))
        _install_units(description)
        _verify_description(description, paired=False)
        _enable_timers()


def confirm_source_publication(inherited_fd: int) -> None:
    description = deployment_description(Path(sys.executable))
    with deployment_lease(description.manager, exclusive=True, inherited_fd=inherited_fd):
        _configured_graph(description, _manager_snapshot(UNIT_NAMES))


def publish_source_snapshot(script: Path) -> None:
    """Keep the exclusive open-file description across all shell/uv publication."""
    view = manager_view()
    with deployment_lease(view, exclusive=True) as descriptor:
        environment = dict(os.environ, HOME=str(view.home),
                           XDG_CONFIG_HOME=str(view.unit_directory.parent.parent),
                           XDG_DATA_HOME=str(view.data_home))
        result = subprocess.run(
            ["bash", str(script), "--source-locked", str(descriptor)],
            env=environment, pass_fds=(descriptor,), check=False,
        )
        if result.returncode:
            raise SystemExit(result.returncode)


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
        install_user_services(Path(sys.argv[0]).absolute())
        return
    alias = Path(sys.argv[0]).name in {"eom-mail-watch", "eom-mail-watch.exe"}
    cli_mode = args[:1] == ["--cli"] or alias
    cli_args = args[1:] if args[:1] == ["--cli"] else args
    readonly_version = cli_mode and cli_args == ["--version"]
    if cli_mode:
        if not readonly_version:
            verify_database_admission()
        cli.main(cli_args)
    else:
        engine_api.main()


def main() -> None:
    try:
        _dispatch_entrypoint()
    except DeploymentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
