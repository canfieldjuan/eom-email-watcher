"""Private evidence destination admission for both COI proof tools.

This dependency-free module is also a CLI for the Node rendering generator.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def evidence_directory(path: Path) -> Path:
    resolved = path.resolve()
    for ancestor in (resolved, *resolved.parents):
        marker = ancestor / ".git"
        if marker.exists():
            raise RuntimeError(
                "Private evidence must be outside every Git worktree; "
                f"Git marker: {marker}"
            )
    return resolved


def create_evidence_directory(path: Path) -> Path:
    resolved = evidence_directory(path)
    os.umask(0o077)
    resolved.mkdir(mode=0o700, parents=True, exist_ok=False)
    return resolved


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("One new evidence directory is required")
    print(json.dumps(str(create_evidence_directory(Path(sys.argv[1])))))
