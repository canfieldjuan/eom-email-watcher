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
import sys
from pathlib import Path

import build_desktop_sidecar as builder
from windows_cutover_fixture import QUALIFICATION_AUTHORITY_ID, fixture_authority, write_private
from windows_cutover_parity import source_receipt


def build_test_sidecar(keyring: Path) -> Path:
    from connect_automate.connect_windows import read_bounded_regular_file
    from connect_automate.entitlement import MAX_KEYRING_BYTES, _parse_keyring

    if builder.determine_build_profile() != "development":
        raise builder.SidecarBuildError("Qualification is not a public build")
    content = read_bounded_regular_file(keyring, MAX_KEYRING_BYTES, require_private_acl=False)
    if set(_parse_keyring(content)) != {QUALIFICATION_AUTHORITY_ID}:
        raise builder.SidecarBuildError("Expected labelled qualification authority only")
    target = builder.determine_target_triple()
    builder.validate_entitlement_keyring_target(target)
    return builder._package_sidecar(
        target,
        os.environ.get("EOM_EMAIL_WATCHER_GOOGLE_OAUTH_CLIENT_FILE"),
        os.environ.get("EOM_EMAIL_WATCHER_MICROSOFT_OAUTH_CLIENT_FILE"),
        content,
        build_directory=keyring.parent / "qualification-build",
        output_directory=keyring.parent / "qualification-binaries",
        catalog_entitlement=keyring.parent / "fixture-entitlement.json",
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("engine", type=Path)
    parser.add_argument("installer", type=Path)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("Requires native Windows; not a release build")
    output = args.output.resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    from connect_automate.connect_windows import _protect_windows_directory

    _protect_windows_directory(output)
    release_sources = json.loads((output / "release-sources.json").read_text())
    if release_sources != source_receipt():
        raise RuntimeError("Qualification source differs from recorded release source")
    for source, name in (
        (args.engine, "release-engine.exe"),
        (args.installer, "release-installer.exe"),
    ):
        shutil.copy2(source, output / name)
    keyring, _ = fixture_authority(output)
    built = build_test_sidecar(keyring)
    if built.resolve() == args.engine.resolve():
        raise RuntimeError("Qualification build reused the production output")
    # Tauri consumes this staging path; the independent build output above and
    # the preserved production sidecar remain separate and unchanged.
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
        "source_head": release_sources["source_head"],
        "entrypoint": "packaging/engine_entry.py",
        "parity": "UNVERIFIED until installer and archive manifests pass",
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
