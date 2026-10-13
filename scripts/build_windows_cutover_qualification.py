"""Build a separately labelled fixture-authority NSIS input for native proof.

Never modifies release authority validation. Preserve production installer and
sidecar first. This reuses their source entrypoint, timezone data and deployment
assets; the only substituted product input is the bundled public-key ring.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from windows_cutover_fixture import fixture_authority, write_private

from eom_email_watcher.deployment import _unit_payloads


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("engine", type=Path)
    parser.add_argument("installer", type=Path)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("Requires native Windows; not a release build")
    output = args.output.resolve()
    output.mkdir(mode=0o700, parents=True)
    from connect_automate.connect_windows import _protect_windows_directory

    _protect_windows_directory(output)
    for source, name in (
        (args.engine, "release-engine.exe"),
        (args.installer, "release-installer.exe"),
    ):
        shutil.copy2(source, output / name)
    keyring, _ = fixture_authority(output)
    root = Path(__file__).resolve().parents[1]
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        "--onefile",
        "--name",
        "qualification-engine",
        "--collect-all",
        "tzdata",
        "--distpath",
        str(output / "dist"),
        "--workpath",
        str(output / "work"),
        "--specpath",
        str(output),
        "--add-data",
        f"{keyring}{os.pathsep}connect_automate_data",
    ]
    # The runtime's canonical bundled name must match the release entrypoint.
    canonical_keyring = output / "connect-entitlement-keyring.json"
    shutil.copy2(keyring, canonical_keyring)
    command[-1] = f"{canonical_keyring}{os.pathsep}connect_automate_data"
    for unit in _unit_payloads():
        path = output / "units" / unit.name
        write_private(path, unit.content)
        command.extend(["--add-data", f"{path}{os.pathsep}eom_email_watcher_data/systemd"])
    command.append(str(root / "packaging/engine_entry.py"))
    subprocess.run(command, cwd=root, check=True)
    built = output / "dist/qualification-engine.exe"
    shutil.copy2(built, args.engine)
    write_private(
        output / "qualification-config.json",
        json.dumps(
            {
                "build": {"beforeBuildCommand": ""},
            }
        ).encode(),
    )
    inventory = {
        "purpose": "TEST ONLY; fixture authority; not a release installer",
        "source_head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        "entrypoint": "packaging/engine_entry.py",
        "differences": ["Bundled public-key ring uses generated qualification authority"],
        "python": sys.version,
        "release_engine_sha256": hashlib.sha256(
            (output / "release-engine.exe").read_bytes()
        ).hexdigest(),
        "qualification_engine_sha256": hashlib.sha256(built.read_bytes()).hexdigest(),
        "fixture_keyring_sha256": hashlib.sha256(keyring.read_bytes()).hexdigest(),
    }
    write_private(output / "build-inventory.json", json.dumps(inventory, indent=2).encode())
    print(json.dumps(inventory, indent=2))


if __name__ == "__main__":
    main()
