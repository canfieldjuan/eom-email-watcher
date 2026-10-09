"""Explicit systemd substitution shared by isolated packaged proof scripts.

Production never imports this fixture and never waives deployment admission.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from eom_email_watcher.deployment import _MANAGER_FIELDS, SCHEDULED_JOBS, UNIT_NAMES

_ACCOUNT_FIXTURE = r"""
#define _GNU_SOURCE
#include <dlfcn.h>
#include <pwd.h>
#include <stdlib.h>
#include <unistd.h>
struct passwd *getpwuid(uid_t uid) {
    struct passwd *(*real)(uid_t) = dlsym(RTLD_NEXT, "getpwuid");
    struct passwd *value = real(uid);
    const char *home = getenv("EOM_PUBLIC_PROOF_HOME");
    static _Thread_local struct passwd copy;
    if (!value || !home || uid != getuid()) return value;
    copy = *value; copy.pw_dir = (char *)home; return &copy;
}
int getpwuid_r(uid_t uid, struct passwd *value, char *buf, size_t size,
              struct passwd **result) {
    int (*real)(uid_t, struct passwd *, char *, size_t, struct passwd **) =
        dlsym(RTLD_NEXT, "getpwuid_r");
    int status = real(uid, value, buf, size, result);
    const char *home = getenv("EOM_PUBLIC_PROOF_HOME");
    if (!status && *result && home && uid == getuid()) value->pw_dir = (char *)home;
    return status;
}
"""


def with_account_home(root: Path, environment: dict[str, str]) -> dict[str, str]:
    """External NSS substitution for isolated Linux proofs, never an app waiver."""
    if sys.platform != "linux":
        return dict(environment)
    folder = root / "public-account-fixture"
    folder.mkdir(mode=0o700, exist_ok=True)
    source, library = folder / "account.c", folder / "account.so"
    source.write_text(_ACCOUNT_FIXTURE)
    source.chmod(0o600)
    subprocess.run(["cc", "-shared", "-fPIC", "-Wall", "-Werror", str(source),
                    "-ldl", "-o", str(library)], check=True, capture_output=True, timeout=30)
    library.chmod(0o600)
    result = dict(environment, LD_PRELOAD=str(library),
                  EOM_PUBLIC_PROOF_HOME=environment["HOME"])
    return result


def systemd_quote(value: str, *, posix: bool = True) -> str:
    """Independent fixture encoding of the two qualified systemd wire modes."""
    special = " \t\r\n\v\f\\'\"$`*?[]()<>|&;!"
    if all(ord(c) >= 32 and ord(c) != 127 and c not in special for c in value):
        return value
    escaped = []
    controls = {7: "a", 8: "b", 9: "t", 10: "n", 11: "v", 12: "f", 13: "r"}
    for char in value:
        number = ord(char)
        if number in controls:
            escaped.append("\\" + controls[number])
        elif number < 32 or number == 127:
            escaped.append(f"\\{number:03o}")
        elif char in ("\\'" if posix else '\\"$`'):
            escaped.append("\\" + char)
        else:
            escaped.append(char)
    return ("$'" if posix else '"') + "".join(escaped) + ("'" if posix else '"')


def unit_records(
    directory: Path, *, loaded: bool, home: Path | None = None
) -> dict[str, dict[str, str]]:
    home = home or directory.parents[2]
    records = {}
    for name in UNIT_NAMES:
        item = dict(
            Id=name,
            Names=name, Transient="no",
            Requires="app.slice basic.target" if loaded and name.endswith(".service") else "",
            Requisite="",
            Wants="",
            BindsTo="",
            Upholds="",
            OnSuccess="",
            OnFailure="",
            Triggers=SCHEDULED_JOBS[name][0] if loaded and name.endswith(".timer") else "",
            LoadState="loaded" if loaded else "not-found",
            ActiveState="inactive",
            FragmentPath=str(directory / name) if loaded else "",
            DropInPaths="",
            NeedDaemonReload="no",
        )
        if loaded and name in ("eom-email-watcher.service", "eom-monthly-hours.service"):
            item["Wants"] = "network-online.target"
            if name == "eom-email-watcher.service":
                item["Wants"] += " eom-email-lmstudio.service"
        if name.endswith(".service"):
            item.update(MainPID="0", ControlPID="0", ExecStart="")
            if loaded:
                command = f"{home}/.lmstudio/bin/lms"
                # Scheduled services have the same alias; helper commands are
                # retained in shipped payloads and never admitted as readers.
                if name != "eom-email-lmstudio.service":
                    command = f"{home}/.local/bin/eom-mail-watch"
                item["ExecStart"] = "{ path=" + command + " ; argv[]=" + command + " ; }"
                if name == "eom-email-lmstudio.service":
                    item["ExecStart"] = "\n".join(
                        "{ path=" + command + " ; argv[]=" + command + " " + action + " ; }"
                        for action in ("daemon up", "server start --bind 127.0.0.1 -p 1234")
                    )
        else:
            item["Unit"] = SCHEDULED_JOBS[name][0] if loaded else ""
        records[name] = item
    for name in ("basic.target", "app.slice", "network-online.target"):
        item = dict(Id=name, Names=name, Transient="no", LoadState="loaded", ActiveState="inactive",
                    FragmentPath="/usr/lib/systemd/user/" + name,
                    DropInPaths="", NeedDaemonReload="no",
                    Requires="", Requisite="", Wants="", BindsTo="", Upholds="", OnSuccess="",
                    OnFailure="", Triggers="")
        if name == "network-online.target":
            item.update(LoadState="not-found", FragmentPath="")
        records[name] = item
    return records


def render_unit_records(records: dict[str, dict[str, str]]) -> str:
    return (
        "\n\n".join(
            "\n".join(
                f"{key}={part}" for key, value in item.items()
                for part in (value.split("\n") if key == "ExecStart" else [value])
            ) for item in records.values()
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
        "#!"
        + sys.executable
        + "\nimport json,sys\nfrom pathlib import Path\n"
        + "sys.path.insert(0,"
        + repr(str(Path(__file__).parent))
        + ")\n"
        + "from packaged_proof_environment import render_unit_records,unit_records,systemd_quote\n"
        + "names="
        + repr(UNIT_NAMES)
        + "\nfields="
        + repr(_MANAGER_FIELDS)
        + "\ninstalled="
        + repr(installed)
        + "\njobs="
        + repr(SCHEDULED_JOBS)
        + "\nmanager_home=Path("
        + repr(str(manager_home))
        + ")"
        + "\ndirectory=Path("
        + repr(str(manager_config / "systemd/user"))
        + ")"
        + "\nenvironment="
        + repr(
            {
                "HOME": str(manager_home),
                "XDG_CONFIG_HOME": str(manager_config),
                "XDG_DATA_HOME": manager_data,
            }
        )
        + "\noverrides=Path("
        + repr(str(root / "manager-overrides.json"))
        + ")\n"
        + "args=sys.argv[1:]\n"
        + "if args==['--user','show-environment']:\n"
        + " print('\\n'.join(k+'='+systemd_quote(v) for k,v in environment.items()));sys.exit(0)\n"
        + "if args==['--user','show','--property=UnitPath','--value']:\n"
        + " print(systemd_quote(str(directory),posix=False));sys.exit(0)\n"
        + "if installed and (args==['--user','daemon-reload'] or "
        + "args in [['--user','enable',timer] for timer in jobs]):sys.exit(0)\n"
        + "if len(args)<5 or args[:4]!=['--user','show','--all','--no-pager'] "
        + "or not args[4].startswith('--property='):sys.exit(2)\n"
        + "requested=args[4][len('--property='):].split(',');selectors=args[5:]\n"
        + "if not set(requested)<=set(fields) or selectors not in "
        + "[list(names),list(names)+['*.target','*.slice']]:sys.exit(2)\n"
        + "loaded=installed and all((directory/name).is_file() for name in names)\n"
        + "records=unit_records(directory,loaded=loaded,home=manager_home)\n"
        + "changes=json.loads(overrides.read_text()) if overrides.exists() else {}\n"
        + "for name,item in records.items():item.update(changes.get(name,{}))\n"
        + "for name,item in changes.items():\n"
        + " if name not in records:records[name]=item\n"
        + "records={n:{k:v for k,v in item.items() if k in requested} "
        + "for n,item in records.items() "
        + "if n in names or '*.target' in selectors}\n"
        + "print(render_unit_records(records))\n"
    )
    path = manager / "systemctl"
    path.write_text(source)
    path.chmod(0o700)
    result = dict(environment)
    result.setdefault("HOME", str(root))
    result = with_account_home(root, result)
    result["PATH"] = str(manager) + os.pathsep + result.get("PATH", "")
    return result


def with_empty_user_manager(root: Path, environment: dict[str, str]) -> dict[str, str]:
    return _with_manager(root, environment, installed=False)


def with_shipped_user_manager(root: Path, environment: dict[str, str]) -> dict[str, str]:
    return _with_manager(root, environment, installed=True)
