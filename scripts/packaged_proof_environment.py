"""Explicit systemd substitution shared by isolated packaged proof scripts.

Production never imports this fixture and never waives deployment admission.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from eom_email_watcher.deployment import _MANAGER_FIELDS, SCHEDULED_JOBS, UNIT_NAMES


def unit_records(directory: Path, *, loaded: bool) -> dict[str, dict[str, str]]:
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
            item.update(MainPID="0", ControlPID="0")
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
    source = (
        "#!"
        + sys.executable
        + "\nimport json,os,sys\nfrom pathlib import Path\n"
        + "names="
        + repr(UNIT_NAMES)
        + "\nfields="
        + repr(_MANAGER_FIELDS)
        + "\ninstalled="
        + repr(installed)
        + "\njobs="
        + repr(SCHEDULED_JOBS)
        + "\noverrides=Path("
        + repr(str(root / "manager-overrides.json"))
        + ")\n"
        + "args=sys.argv[1:]\n"
        + "if installed and (args==['--user','daemon-reload'] or "
        "args in [['--user','enable',timer] for timer in jobs]):sys.exit(0)\n"
        + "if args!=['--user','show','--all','--no-pager',"
        "'--property='+','.join(fields),*names]:sys.exit(2)\n"
        + "home=Path(os.environ['HOME'])\n"
        + "configured=os.environ.get('XDG_CONFIG_HOME','')\n"
        + "config=Path(configured) if configured and Path(configured).is_absolute() "
        "else home/'.config'\n"
        + "directory=config/'systemd/user'\n"
        + "loaded=installed and all((directory/name).is_file() for name in names)\n"
        + "changes=json.loads(overrides.read_text()) if overrides.exists() else {}\n"
        + "for name in names:\n"
        + " record=dict(Id=name,LoadState='loaded' if loaded else 'not-found',"
        "ActiveState='inactive',FragmentPath=str(directory/name) if loaded else '',"
        "DropInPaths='',NeedDaemonReload='no')\n"
        + " if name.endswith('.service'):record.update(MainPID='0',ControlPID='0')\n"
        + " else:record['Unit']=jobs[name][0] if loaded else ''\n"
        + " record.update(changes.get(name,{}))\n"
        + " print('\\n'.join(key+'='+value for key,value in record.items()))\n"
        + " print()\n"
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
