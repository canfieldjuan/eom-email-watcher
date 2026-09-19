"""Enforce that the Connect Automate core stays vendor-neutral.

The whole point of extracting ``connect_automate`` out of the EOM watcher is
that the generic licensed-consumer host depends on nothing EOM-specific and no
vendor integration. This test fails the build if that boundary is ever
crossed, so a future edit cannot quietly reintroduce coupling.

Two independent checks:

1. A static AST scan of every module in the ``connect_automate`` package for a
   top-level import of a forbidden root (the EOM watcher package, or a
   provider/vendor SDK such as the Gmail or Microsoft clients).
2. A live import in a clean subprocess that imports the core's public entry
   modules and asserts that doing so pulled no ``eom_email_watcher`` module
   into ``sys.modules`` (catches dynamic or transitive coupling the static
   scan cannot see).
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import connect_automate

# The EOM watcher package and the concrete vendor SDKs a generic host must
# never import. Generic infrastructure the core legitimately uses (httpx,
# pydantic, cryptography, filelock, tzdata, and the standard library) is not
# listed and stays allowed.
FORBIDDEN_ROOTS = frozenset(
    {
        "eom_email_watcher",
        "google",
        "googleapiclient",
        "google_auth_oauthlib",
        "googleapiclient.discovery",
        "msal",
    }
)


def _core_python_files() -> list[Path]:
    root = Path(connect_automate.__file__).resolve().parent
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _imported_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".", 1)[0])
        # A relative import (level > 0) is intra-package by construction and can
        # never reach outside connect_automate, so it is always allowed.
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def test_core_modules_have_no_forbidden_imports() -> None:
    files = _core_python_files()
    assert files, "expected to find connect_automate source files"
    violations: list[str] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for root in sorted(_imported_roots(tree) & FORBIDDEN_ROOTS):
            violations.append(f"{path.name} imports forbidden root '{root}'")
    assert not violations, "connect_automate must stay vendor-neutral:\n" + "\n".join(
        violations
    )


def test_importing_core_loads_no_eom_watcher_module() -> None:
    # A clean interpreter: import the core's public entry points and prove that
    # nothing under eom_email_watcher was loaded as a side effect.
    program = (
        "import connect_automate\n"
        "import connect_automate.connect\n"
        "import connect_automate.entitlement\n"
        "import connect_automate.locking\n"
        "import connect_automate.automate_connect\n"
        "import connect_automate.automate.runtime\n"
        "import connect_automate.automate.host\n"
        "import sys\n"
        "leaked = sorted(m for m in sys.modules "
        "if m == 'eom_email_watcher' or m.startswith('eom_email_watcher.'))\n"
        "assert not leaked, leaked\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "importing connect_automate must not load eom_email_watcher:\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert result.stdout.strip() == "ok"
