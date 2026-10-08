"""Explicit systemd substitution shared by isolated packaged proof scripts.

Production never imports this fixture and never waives deployment admission.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from eom_email_watcher.deployment import _MANAGER_FIELDS, SCHEDULED_JOBS, UNIT_NAMES


def unit_records(
    directory: Path, *, loaded: bool, home: Path | None = None
) -> dict[str, dict[str, str]]:
    home = home or directory.parents[2]
    records = {}
    for name in UNIT_NAMES:
        item = dict(
            Id=name,
            LoadState="loaded" if loaded else "not-found",
            ActiveState="inactive",
            FragmentPath=str(directory / name) if loaded else "",
            DropInPaths="",
            NeedDaemonReload="no",
        )
        if name.endswith(".service"):
            item.update(MainPID="0", ControlPID="0", ExecStart="")
            if loaded:
                command = f"{home}/.lmstudio/bin/lms"
                # Scheduled services have the same alias; helper commands are
                # retained in shipped payloads and never admitted as readers.
                if name != "eom-email-lmstudio.service":
                    command = f"{home}/.local/bin/eom-mail-watch"
                item["ExecStart"] = "{ path=" + command + " ; argv[]=" + command + " ; }"
        else:
            item["Unit"] = SCHEDULED_JOBS[name][0] if loaded else ""
        records[name] = item
    return records


def render_unit_records(records: dict[str, dict[str, str]]) -> str:
    return (
        "\n\n".join(
            "\n".join(f"{key}={value}" for key, value in item.items()) for item in records.values()
        )
        + "\n"
    )


def _with_manager(root: Path, environment: dict[str, str], *, installed: bool) -> dict[str, str]:
    manager = root / ("shipped-manager-fixture" if installed else "empty-manager-fixture")
    manager.mkdir(mode=0o700, exist_ok=True)
    # This is an external test manager, not a production environment waiver.
    # Reading the isolated installed files simulates daemon-reload in this fixture.
    manager_home = Path(environment.get("HOME", str(root)))
    configured = environment.get("XDG_CONFIG_HOME")
    manager_config = Path(configured) if configured else manager_home / ".config"
    manager_data = environment.get("XDG_DATA_HOME", str(manager_home / ".local/share"))
    source = (
        "#!" + sys.executable + "\nimport json,shlex,sys\nfrom pathlib import Path\n"
        + "sys.path.insert(0," + repr(str(Path(__file__).parent)) + ")\n"
        + "from packaged_proof_environment import render_unit_records,unit_records\n"
        + "names=" + repr(UNIT_NAMES) + "\nfields=" + repr(_MANAGER_FIELDS)
        + "\ninstalled=" + repr(installed) + "\njobs=" + repr(SCHEDULED_JOBS)
        + "\nmanager_home=Path(" + repr(str(manager_home)) + ")"
        + "\ndirectory=Path(" + repr(str(manager_config / "systemd/user")) + ")"
        + "\nenvironment=" + repr({"HOME": str(manager_home),
                                  "XDG_CONFIG_HOME": str(manager_config),
                                  "XDG_DATA_HOME": manager_data})
        + "\noverrides=Path(" + repr(str(root / "manager-overrides.json")) + ")\n"
        + "args=sys.argv[1:]\n"
        + "if args==['--user','show-environment']:\n"
        + " print('\\n'.join(k+'='+shlex.quote(v) for k,v in environment.items()));sys.exit(0)\n"
        + "if installed and (args==['--user','daemon-reload'] or "
        + "args in [['--user','enable',timer] for timer in jobs]):sys.exit(0)\n"
        + "if args!=['--user','show','--all','--no-pager',"
        + "'--property='+','.join(fields),*names]:sys.exit(2)\n"
        + "loaded=installed and all((directory/name).is_file() for name in names)\n"
        + "records=unit_records(directory,loaded=loaded,home=manager_home)\n"
        + "changes=json.loads(overrides.read_text()) if overrides.exists() else {}\n"
        + "for name,item in records.items():item.update(changes.get(name,{}))\n"
        + "print(render_unit_records(records))\n"
    )
    path = manager / "systemctl"
    path.write_text(source)
    path.chmod(0o700)
    result = dict(environment)
    result.setdefault("HOME", str(root))
    result["PATH"] = str(manager) + os.pathsep + result.get("PATH", "")
    return result


def with_empty_user_manager(root: Path, environment: dict[str, str]) -> dict[str, str]:
    return _with_manager(root, environment, installed=False)


def with_shipped_user_manager(root: Path, environment: dict[str, str]) -> dict[str, str]:
    return _with_manager(root, environment, installed=True)
