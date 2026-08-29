#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIRECTORY="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIRECTORY="$(cd -- "$SCRIPT_DIRECTORY/.." && pwd)"
TARGET_TRIPLE="${CARGO_BUILD_TARGET:-$(rustc --print host-tuple)}"

if [[ ! "$TARGET_TRIPLE" =~ ^[A-Za-z0-9_.-]+$ ]]; then
  echo "Unsupported Rust target triple: $TARGET_TRIPLE" >&2
  exit 2
fi

BUILD_DIRECTORY="$PROJECT_DIRECTORY/.sidecar-build"
OUTPUT_DIRECTORY="$PROJECT_DIRECTORY/desktop/src-tauri/binaries"
OUTPUT_PATH="$OUTPUT_DIRECTORY/eom-mail-engine-$TARGET_TRIPLE"

mkdir -p -- "$BUILD_DIRECTORY/dist" "$BUILD_DIRECTORY/work" "$BUILD_DIRECTORY/spec"
mkdir -p -- "$OUTPUT_DIRECTORY"

cd -- "$PROJECT_DIRECTORY"
PYINSTALLER_CONFIG_DIR="$BUILD_DIRECTORY/cache" uv run pyinstaller \
  --clean \
  --noconfirm \
  --onefile \
  --name eom-mail-engine \
  --distpath "$BUILD_DIRECTORY/dist" \
  --workpath "$BUILD_DIRECTORY/work" \
  --specpath "$BUILD_DIRECTORY/spec" \
  packaging/engine_entry.py

install -m 0755 -- "$BUILD_DIRECTORY/dist/eom-mail-engine" "$OUTPUT_PATH"
echo "$OUTPUT_PATH"
