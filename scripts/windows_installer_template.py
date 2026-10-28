"""Derive cutover ownership from the pinned installer's actual File entries."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.request
from pathlib import Path

CLI_VERSION = "2.11.4"
UPSTREAM_SHA256 = "20f4ecc730defb71f1342eaeaec4021df13be3d843abba0effe88ea5835fa079"
UPSTREAM_URL = (
    "https://raw.githubusercontent.com/tauri-apps/tauri/"
    "8909f221d1515955fc843808032bdc5d62209c96/"
    "crates/tauri-bundler/src/bundle/windows/nsis/installer.nsi"
)


def catalog_macro(template: str) -> str:
    section = re.search(r"(?ms)^Section Install\s*\n(.*?)^SectionEnd", template)
    if section is None:
        raise ValueError("Installer has no application Install section")
    output = [
        "Var EW_PACKAGE_PROGRAM_CATALOG",
        "Var EW_PACKAGE_CATALOG_HANDLE",
        "!macro EW_PREPARE_PACKAGE_CATALOG",
        "  Push $0",
        "  InitPluginsDir",
        '  StrCpy $EW_PACKAGE_PROGRAM_CATALOG "$PLUGINSDIR\\ew-package-files-v1.txt"',
        "  ClearErrors",
        '  FileOpen $EW_PACKAGE_CATALOG_HANDLE "$EW_PACKAGE_PROGRAM_CATALOG" w',
        "  IfErrors 0 +2",
        '    Abort "Unable to write package file catalog"',
        '  FileWriteUTF16LE /BOM $EW_PACKAGE_CATALOG_HANDLE "EW-PACKAGE-FILES-V1$\\r$\\n"',
    ]
    files = 0
    loops = []
    for raw in section[1].splitlines():
        line = raw.strip()
        # Keep the bundler's exact renderer loops/conditions; no executable list.
        if line.startswith("{{") and line.endswith("}}"):
            output.append(raw)
            each = re.match(r"{{#each\s+(\w+)", line)
            if each:
                loops.append(each[1])
            elif line.startswith("{{/each"):
                loops.pop()
        elif line.startswith("File "):
            oname = re.search(r'"/oname=([^"]+)"', line)
            if oname:
                value = oname[1]
            else:
                source = re.search(r'"([^"]+)"$', line)
                if source is None:
                    raise ValueError("Cannot derive a packaged File destination")
                output.append('  ${GetFileName} "' + source[1] + '" $0')
                value = "$0"
            output.append('  FileWriteUTF16LE $EW_PACKAGE_CATALOG_HANDLE "' + value + '$\\r$\\n"')
            # Main application / declared sidecars, not generic resource programs.
            if "binaries" in loops or "MAINBINARYSRCPATH" in line:
                output.append(
                    '  FileWriteUTF16LE $EW_PACKAGE_CATALOG_HANDLE "EW-APP-IMAGE|'
                    + value
                    + '$\\r$\\n"'
                )
            files += 1
        elif line.startswith("WriteUninstaller "):
            match = re.fullmatch(r'WriteUninstaller "\$INSTDIR\\([^\"]+)"', line)
            if match is None:
                raise ValueError("Uninstaller destination is not installation-relative")
            output.append(
                '  FileWriteUTF16LE $EW_PACKAGE_CATALOG_HANDLE "' + match[1] + '$\\r$\\n"'
            )
            files += 1
    if files == 0:
        raise ValueError("Installer has no package files")
    output += ["  FileClose $EW_PACKAGE_CATALOG_HANDLE", "  Pop $0", "!macroend"]
    return "\n".join(output) + "\n"


def prepare(root: Path, build: Path) -> Path:
    package = json.loads((root / "desktop/package.json").read_text())
    if package["devDependencies"]["@tauri-apps/cli"] != CLI_VERSION:
        raise ValueError("Installer template pin must match the Tauri CLI")
    folder = build / "nsis"
    folder.mkdir(parents=True, exist_ok=True)
    upstream = folder / "upstream-installer.nsi"
    if not upstream.exists():
        with urllib.request.urlopen(UPSTREAM_URL, timeout=30) as response:
            upstream.write_bytes(response.read())
    content = upstream.read_bytes()
    if hashlib.sha256(content).hexdigest() != UPSTREAM_SHA256:
        raise ValueError("Pinned installer template checksum differs")
    template = content.decode("utf-8")
    marker = "{{#if installer_hooks}}"
    if template.count(marker) != 1:
        raise ValueError("Installer hook seam changed")
    generated = folder / "installer.nsi"
    generated.write_text(template.replace(marker, catalog_macro(template) + "\n" + marker))
    return generated
