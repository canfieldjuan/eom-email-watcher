"""Explicit manager substitution shared by isolated packaged proof scripts.

This module is not imported by production and never waives its admission policy.
"""

from __future__ import annotations

import os
from pathlib import Path


def with_empty_user_manager(root: Path, environment: dict[str, str]) -> dict[str, str]:
    manager = root / "empty-manager-fixture"
    manager.mkdir(mode=0o700, exist_ok=True)
    scripts = {
        "systemctl": """#!/bin/sh
if [ "$#" -ne 5 ] || [ "$1" != --user ] || [ "$2" != show ] ||
   [ "$4" != --property=LoadState ] || [ "$5" != --value ]; then exit 2; fi
printf '%s\\n' not-found
""",
        "busctl": """#!/bin/sh
if [ "$#" -ne 7 ] || [ "$1" != --user ] || [ "$2" != --json=short ] ||
   [ "$3" != get-property ] || [ "$4" != org.freedesktop.systemd1 ]; then exit 2; fi
case "$7" in
  ActiveState) [ "$6" = org.freedesktop.systemd1.Unit ] || exit 2
    printf '%s\\n' '{"type":"s","data":"inactive"}' ;;
  MainPID|ControlPID) [ "$6" = org.freedesktop.systemd1.Service ] || exit 2
    printf '%s\\n' '{"type":"u","data":0}' ;;
  ExecCondition|ExecStart|ExecStartPre|ExecStartPost|ExecReload|ExecStop|ExecStopPost)
    [ "$6" = org.freedesktop.systemd1.Service ] || exit 2
    printf '%s\\n' '{"type":"a(sasbttttuii)","data":[]}' ;;
  *) exit 2 ;;
esac
""",
    }
    for name, text in scripts.items():
        path = manager / name
        path.write_text(text)
        path.chmod(0o700)
    result = dict(environment)
    result["PATH"] = str(manager) + os.pathsep + result.get("PATH", "")
    return result
