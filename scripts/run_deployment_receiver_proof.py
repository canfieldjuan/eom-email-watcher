"""Run the actual native refusal/desktop parser proof in CI or an isolated local run."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

from packaged_proof_environment import with_shipped_user_manager


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("engine", type=Path)
    parser.add_argument("--manifest", type=Path, default=Path("desktop/src-tauri/Cargo.toml"))
    args = parser.parse_args()
    engine = args.engine.resolve(strict=True)
    # Build outside the proof environment; cargo needs its ordinary toolchain.
    result = subprocess.run(
        [
            "cargo",
            "test",
            "--locked",
            "--manifest-path",
            str(args.manifest),
            "--lib",
            "--no-run",
            "--message-format=json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    binaries = []
    for line in result.stdout.splitlines():
        item = json.loads(line)
        if (
            item.get("reason") == "compiler-artifact"
            and item.get("profile", {}).get("test")
            and item.get("executable")
            and item.get("target", {}).get("name") == "eom_email_watcher_desktop"
        ):
            binaries.append(item["executable"])
    if len(binaries) != 1:
        raise RuntimeError("Expected exactly one desktop library test executable")
    with tempfile.TemporaryDirectory(prefix="public-deployment-receiver-") as directory:
        root = Path(directory)
        config = root / "never-created.toml"
        (root / "manager-overrides.json").write_text(
            json.dumps(
                {
                    "eom-email-watcher.service": {"LoadState": "masked"},
                }
            )
        )
        environment = with_shipped_user_manager(
            root,
            dict(
                os.environ,
                HOME=str(root),
                XDG_CONFIG_HOME=str(root / "config"),
                XDG_DATA_HOME=str(root / "data"),
                XDG_STATE_HOME=str(root / "state"),
                XDG_CACHE_HOME=str(root / "cache"),
                XDG_RUNTIME_DIR=str(root / "runtime"),
            ),
        )
        environment.update(EOM_TEST_REFUSAL_ENGINE=str(engine), EOM_TEST_REFUSAL_CONFIG=str(config))
        subprocess.run(
            [
                binaries[0],
                "engine::tests::packaged_deployment_refusal_is_definitive",
                "--exact",
                "--ignored",
                "--nocapture",
            ],
            env=environment,
            check=True,
        )
        if config.exists():
            raise RuntimeError("Refused initialization created configuration")
    print("REAL_NATIVE_RECEIVER deployment_refused/Definitive; no configuration created")


if __name__ == "__main__":
    main()
