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
ENTITLEMENT_KEYRING_SOURCE="${LOCAL_CONNECT_ENTITLEMENT_KEYRING_FILE:-}"
ENTITLEMENT_STAGE_DIRECTORY=""
SIDECAR_SMOKE_DIRECTORY=""
PYINSTALLER_EXTRA_ARGS=()

cleanup() {
  if [[ -n "$OAUTH_STAGE_DIRECTORY" ]]; then
    rm -f -- "$OAUTH_STAGE_DIRECTORY/google-oauth-client.json"
    rmdir -- "$OAUTH_STAGE_DIRECTORY" 2>/dev/null || true
  fi
  if [[ -n "$ENTITLEMENT_STAGE_DIRECTORY" ]]; then
    rm -f -- "$ENTITLEMENT_STAGE_DIRECTORY/connect-entitlement-keyring.json"
    rmdir -- "$ENTITLEMENT_STAGE_DIRECTORY" 2>/dev/null || true
  fi
  if [[ -n "$SIDECAR_SMOKE_DIRECTORY" ]]; then
    rm -f -- \
      "$SIDECAR_SMOKE_DIRECTORY/config.toml" \
      "$SIDECAR_SMOKE_DIRECTORY/config.toml.lock" \
      "$SIDECAR_SMOKE_DIRECTORY/response.json"
    rmdir -- "$SIDECAR_SMOKE_DIRECTORY" 2>/dev/null || true
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

if [[ -n "$ENTITLEMENT_KEYRING_SOURCE" ]]; then
  if [[ ! -f "$ENTITLEMENT_KEYRING_SOURCE" ]]; then
    echo "Connect entitlement public-key ring is not a regular file" >&2
    exit 2
  fi
  uv run --project "$PROJECT_DIRECTORY" python - "$ENTITLEMENT_KEYRING_SOURCE" <<'PY'
import sys
from pathlib import Path

from eom_email_watcher.entitlement import _parse_keyring

try:
    keys = _parse_keyring(Path(sys.argv[1]).read_bytes())
except (OSError, ValueError) as exc:
    raise SystemExit("Connect entitlement public-key ring is invalid") from exc
if not keys:
    raise SystemExit("Connect-enabled release key ring must contain at least one public key")
PY
  ENTITLEMENT_STAGE_DIRECTORY="$(mktemp -d "$BUILD_DIRECTORY/connect-keyring.XXXXXX")"
  install -m 0600 -- "$ENTITLEMENT_KEYRING_SOURCE" \
    "$ENTITLEMENT_STAGE_DIRECTORY/connect-entitlement-keyring.json"
  PYINSTALLER_EXTRA_ARGS+=(
    --add-data "$ENTITLEMENT_STAGE_DIRECTORY/connect-entitlement-keyring.json:eom_email_watcher_data"
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
  --collect-all tzdata \
  "${PYINSTALLER_EXTRA_ARGS[@]}" \
  packaging/engine_entry.py

install -m 0755 -- "$BUILD_DIRECTORY/dist/eom-mail-engine" "$OUTPUT_PATH"

SIDECAR_SMOKE_DIRECTORY="$(mktemp -d "$BUILD_DIRECTORY/sidecar-smoke.XXXXXX")"
SMOKE_CONFIG_PATH="$SIDECAR_SMOKE_DIRECTORY/config.toml"
SMOKE_RESPONSE_PATH="$SIDECAR_SMOKE_DIRECTORY/response.json"
uv run --project "$PROJECT_DIRECTORY" python - "$SMOKE_CONFIG_PATH" <<'PY' | \
  HOME="$SIDECAR_SMOKE_DIRECTORY" \
  XDG_CONFIG_HOME="$SIDECAR_SMOKE_DIRECTORY" \
  "$OUTPUT_PATH" > "$SMOKE_RESPONSE_PATH"
import json
import sys

print(
    json.dumps(
        {
            "protocol": 1,
            "operation": "config.initialize",
            "config_path": sys.argv[1],
            "payload": {
                "model_base_url": "http://127.0.0.1:11434/v1",
                "model_name": "sidecar-build-smoke",
                "timezone": "America/Chicago",
            },
        },
        separators=(",", ":"),
    )
)
PY
uv run --project "$PROJECT_DIRECTORY" python - "$SMOKE_RESPONSE_PATH" <<'PY'
import json
import sys
from pathlib import Path

try:
    response = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except (OSError, UnicodeError, json.JSONDecodeError) as exc:
    raise SystemExit("Desktop sidecar smoke response is invalid") from exc

settings = response.get("data", {}).get("settings", {})
if response.get("ok") is not True or settings.get("timezone") != "America/Chicago":
    raise SystemExit(f"Desktop sidecar timezone smoke failed: {response}")
PY

echo "$OUTPUT_PATH"
