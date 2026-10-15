"""Measured production/qualification payload parity; unexpected deltas stop proof."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import zipfile
from pathlib import Path

from windows_cutover_fixture import write_private

ROOT = Path(__file__).resolve().parents[1]
KEYRING = "connect_automate_data/connect-entitlement-keyring.json"
SOURCES = (
    "desktop/src-tauri/windows/hooks.nsh",
    "desktop/src-tauri/windows/cutover.ps1",
    "scripts/build_desktop_sidecar.py",
    "packaging/engine_entry.py",
    "scripts/windows_installer_template.py",
    "desktop/src-tauri/tauri.windows.conf.json",
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_receipt() -> dict:
    return {
        "source_head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "sources": {name: digest(ROOT / name) for name in SOURCES},
    }


def compare_manifests(release: dict, qualification: dict, expected_changes: set) -> list:
    """Closed comparison: same names/types, and exactly the named content deltas."""
    if release.keys() != qualification.keys() or not expected_changes <= release.keys():
        raise ValueError("Payload names differ or required payload is absent")
    changed = []
    for name in sorted(release):
        if release[name]["type"] != qualification[name]["type"]:
            raise ValueError(f"Payload type changed: {name}")
        if release[name] != qualification[name]:
            changed.append(name)
    if set(changed) != expected_changes:
        raise ValueError(
            f"Unexpected payload differences: {changed}; expected {sorted(expected_changes)}"
        )
    return changed


def sidecar_manifest(binary: Path) -> dict:
    from PyInstaller.archive.readers import CArchiveReader

    archive = CArchiveReader(str(binary))
    manifest = {}
    for raw_name, entry in archive.toc.items():
        name = raw_name.replace("\\", "/")
        if name in manifest:
            raise ValueError(f"Duplicate payload name: {name}")
        content = archive.extract(raw_name)
        manifest[name] = {
            "type": entry[-1],
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    for index, option in enumerate(archive.options):
        name = f"OPTION/{index}"
        if name in manifest:
            raise ValueError("Option collides with a payload name")
        manifest[name] = {"type": "o", "value": option}
    return manifest


def base_library_evidence(release: Path, qualification: Path, output: Path) -> None:
    """Retain the exact native archives and byte-level member comparison."""
    from PyInstaller.archive.readers import CArchiveReader

    sides = {}
    for label, binary in (("release", release), ("qualification", qualification)):
        data = CArchiveReader(str(binary)).extract("base_library.zip")
        write_private(output / f"{label}-base-library.zip", data)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise ValueError("Duplicate stdlib ZIP member")
            sides[label] = {
                "archive_sha256": hashlib.sha256(data).hexdigest(),
                "order": names,
                "members": {
                    name: {
                        "size": len(archive.read(name)),
                        "sha256": hashlib.sha256(archive.read(name)).hexdigest(),
                        "header_hex": archive.read(name)[:16].hex(),
                        "timestamp": archive.getinfo(name).date_time,
                    } for name in names
                },
            }
    a, b = (sides[side]["members"] for side in ("release", "qualification"))
    report = {
        "sides": sides,
        "member_names_equal": a.keys() == b.keys(),
        "changed_member_bytes": sorted(
            name for name in a.keys() & b.keys() if a[name]["sha256"] != b[name]["sha256"]
        ),
        "raw_archive_equal": (
            sides["release"]["archive_sha256"] == sides["qualification"]["archive_sha256"]
        ),
    }
    write_private(output / "base-library-diff.json", json.dumps(report, indent=2).encode())


def installer_manifest(installer: Path, destination: Path) -> dict:
    destination.mkdir(mode=0o700)
    result = subprocess.run(
        ["7z", "x", "-y", "-o" + str(destination), str(installer.resolve())],
        check=True,
        capture_output=True,
    )
    write_private(destination.parent / (destination.name + ".log"), result.stdout + result.stderr)
    manifest = {}
    for path in sorted(destination.rglob("*")):
        if path.is_symlink():
            raise ValueError("Installer payload cannot be a symlink")
        if path.is_file():
            manifest[path.relative_to(destination).as_posix()] = {
                "type": "file",
                "size": path.stat().st_size,
                "sha256": digest(path),
            }
    return manifest


def verify(output: Path, installer: Path, engine: Path) -> dict:
    inventory = json.loads((output / "build-inventory.json").read_text())
    release_sources = json.loads((output / "release-sources.json").read_text())
    if (
        source_receipt() != release_sources
        or inventory["source_head"] != release_sources["source_head"]
    ):
        raise ValueError("Production/qualification source inputs changed")
    release_installer, release_engine = (
        output / "release-installer.exe",
        output / "release-engine.exe",
    )
    base_library_evidence(release_engine, engine, output)
    engines = {
        "release": sidecar_manifest(release_engine),
        "qualification": sidecar_manifest(engine),
    }
    engine_changes = compare_manifests(engines["release"], engines["qualification"], {KEYRING})
    installers = {
        "release": installer_manifest(release_installer, output / "release-payload"),
        "qualification": installer_manifest(installer, output / "qualification-payload"),
    }
    sidecars = [name for name in installers["release"] if Path(name).name == "eom-mail-engine.exe"]
    if len(sidecars) != 1:
        raise ValueError("Expected exactly one installer sidecar")
    sidecar = sidecars[0]
    installer_changes = compare_manifests(
        installers["release"], installers["qualification"], {sidecar}
    )
    if installers["release"][sidecar]["sha256"] != digest(release_engine) or installers[
        "qualification"
    ][sidecar]["sha256"] != digest(engine):
        raise ValueError("Extracted installer does not contain the compared engine")
    desktops = [
        name for name in installers["release"] if Path(name).name == "eom-email-watcher-desktop.exe"
    ]
    if len(desktops) != 1:
        raise ValueError("Expected exactly one unchanged desktop payload")
    report = {
        "result": "PASS",
        "source_receipt": release_sources,
        "installer_sha256": digest(installer),
        "release_installer_sha256": digest(release_installer),
        "desktop_sha256": installers["release"][desktops[0]]["sha256"],
        "engine_changes": engine_changes,
        "installer_changes": installer_changes,
        "engine_manifests": engines,
        "installer_manifests": installers,
    }
    write_private(output / "parity.json", json.dumps(report, indent=2).encode())
    inventory["parity"] = {k: v for k, v in report.items() if not k.endswith("manifests")}
    write_private(output / "build-inventory.json", json.dumps(inventory, indent=2).encode())
    print(json.dumps(inventory["parity"], indent=2))
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("record", "verify"))
    parser.add_argument("output", type=Path)
    parser.add_argument("--installer", type=Path)
    parser.add_argument("--engine", type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    if args.phase == "record":
        output.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_private(
            output / "release-sources.json", json.dumps(source_receipt(), indent=2).encode()
        )
    else:
        if args.installer is None or args.engine is None:
            parser.error("verify requires installer and engine")
        verify(output, args.installer, args.engine)


if __name__ == "__main__":
    main()
