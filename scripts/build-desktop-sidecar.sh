#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIRECTORY="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIRECTORY="$(cd -- "$SCRIPT_DIRECTORY/.." && pwd)"

exec uv run --project "$PROJECT_DIRECTORY" python \
  "$SCRIPT_DIRECTORY/build_desktop_sidecar.py" "$@"
