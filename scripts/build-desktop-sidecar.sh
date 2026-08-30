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
OAUTH_CLIENT_SOURCE="${EOM_EMAIL_WATCHER_GOOGLE_OAUTH_CLIENT_FILE:-}"
OAUTH_STAGE_DIRECTORY=""
PYINSTALLER_EXTRA_ARGS=()

cleanup() {
  if [[ -n "$OAUTH_STAGE_DIRECTORY" ]]; then
    rm -rf -- "$OAUTH_STAGE_DIRECTORY"
  fi
}
trap cleanup EXIT

mkdir -p -- "$BUILD_DIRECTORY/dist" "$BUILD_DIRECTORY/work" "$BUILD_DIRECTORY/spec"
mkdir -p -- "$OUTPUT_DIRECTORY"

if [[ -n "$OAUTH_CLIENT_SOURCE" ]]; then
  if [[ ! -f "$OAUTH_CLIENT_SOURCE" ]]; then
    echo "Google OAuth Desktop client file is not a regular file" >&2
    exit 2
  fi
  uv run --project "$PROJECT_DIRECTORY" python - "$OAUTH_CLIENT_SOURCE" <<'PY'
import json
import sys
from pathlib import Path

try:
    document = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except (OSError, UnicodeError, json.JSONDecodeError) as exc:
    raise SystemExit("Google OAuth Desktop client file is not valid JSON") from exc

installed = document.get("installed") if isinstance(document, dict) else None
if not isinstance(installed, dict):
    raise SystemExit("Google OAuth input must be a downloaded Desktop client JSON file")
required = ("auth_uri", "client_id", "client_secret", "token_uri")
if not all(isinstance(installed.get(key), str) and installed[key].strip() for key in required):
    raise SystemExit("Google OAuth Desktop client JSON is missing required fields")


def keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from keys(child)


if {"access_token", "refresh_token"}.intersection(keys(document)):
    raise SystemExit("Google OAuth Desktop client input must not contain account tokens")
PY
  OAUTH_STAGE_DIRECTORY="$(mktemp -d "$BUILD_DIRECTORY/oauth-client.XXXXXX")"
  install -m 0600 -- "$OAUTH_CLIENT_SOURCE" \
    "$OAUTH_STAGE_DIRECTORY/google-oauth-client.json"
  PYINSTALLER_EXTRA_ARGS+=(
    --add-data "$OAUTH_STAGE_DIRECTORY/google-oauth-client.json:eom_email_watcher_data"
  )
fi

cd -- "$PROJECT_DIRECTORY"
PYINSTALLER_CONFIG_DIR="$BUILD_DIRECTORY/cache" uv run pyinstaller \
  --clean \
  --noconfirm \
  --onefile \
  --name eom-mail-engine \
  --distpath "$BUILD_DIRECTORY/dist" \
  --workpath "$BUILD_DIRECTORY/work" \
  --specpath "$BUILD_DIRECTORY/spec" \
  "${PYINSTALLER_EXTRA_ARGS[@]}" \
  packaging/engine_entry.py

install -m 0755 -- "$BUILD_DIRECTORY/dist/eom-mail-engine" "$OUTPUT_PATH"
echo "$OUTPUT_PATH"
