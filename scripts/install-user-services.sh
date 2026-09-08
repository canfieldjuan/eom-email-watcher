#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
tool_bin_dir="$HOME/.local/bin"
tool_dir="${XDG_DATA_HOME:-$HOME/.local/share}/uv/tools"
release_keyring_source="${LOCAL_CONNECT_ENTITLEMENT_KEYRING_FILE:-}"
release_keyring_dir="${XDG_DATA_HOME:-$HOME/.local/share}/eom-email-watcher"
release_keyring_target="$release_keyring_dir/connect-entitlement-keyring.json"
constraints_file="$(mktemp)"
trap 'rm -f "$constraints_file"' EXIT

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required to install the systemd service CLI snapshot." >&2
  exit 1
fi

mkdir -p "$unit_dir" "$tool_bin_dir" "$tool_dir"
uv export --project "$repo_dir" --locked --no-dev --no-emit-project --format requirements.txt \
  --output-file "$constraints_file" >/dev/null
UV_TOOL_BIN_DIR="$tool_bin_dir" UV_TOOL_DIR="$tool_dir" \
  uv tool install --force --reinstall --constraints "$constraints_file" "$repo_dir"
test -x "$tool_bin_dir/eom-mail-watch"

if [[ -n "$release_keyring_source" ]]; then
  if [[ "$release_keyring_source" != /* ]]; then
    echo "LOCAL_CONNECT_ENTITLEMENT_KEYRING_FILE must be absolute." >&2
    exit 1
  fi
  if [[ "$release_keyring_dir" != /* ]]; then
    echo "XDG_DATA_HOME must be absolute when installing the Connect authority." >&2
    exit 1
  fi
  PYTHONPATH="$repo_dir" RELEASE_KEYRING_SOURCE="$release_keyring_source" \
    uv run --project "$repo_dir" python -c \
    'import os; from pathlib import Path; from scripts.build_desktop_sidecar import validate_entitlement_keyring; validate_entitlement_keyring(Path(os.environ["RELEASE_KEYRING_SOURCE"]))'
  install -d -m 0700 "$release_keyring_dir"
  install -m 0600 "$release_keyring_source" "$release_keyring_target"
fi

install -m 0644 "$repo_dir/systemd/eom-email-watcher.service" "$unit_dir/"
install -m 0644 "$repo_dir/systemd/eom-email-watcher.timer" "$unit_dir/"
install -m 0644 "$repo_dir/systemd/eom-email-lmstudio.service" "$unit_dir/"
install -m 0644 "$repo_dir/systemd/eom-monthly-hours.service" "$unit_dir/"
install -m 0644 "$repo_dir/systemd/eom-monthly-hours.timer" "$unit_dir/"
systemctl --user daemon-reload
systemctl --user enable eom-email-watcher.timer
systemctl --user enable eom-monthly-hours.timer

echo "Installed and enabled eom-email-watcher.timer."
echo "Installed and enabled eom-monthly-hours.timer."
echo "Installed a stable eom-mail-watch snapshot at $tool_bin_dir/eom-mail-watch."
if [[ -f "$release_keyring_target" ]]; then
  echo "Installed the approved Connect release authority for the service snapshot."
else
  echo "Connect remains unavailable to the service snapshot until an approved release authority is installed."
fi
echo "It will begin succeeding after: $tool_bin_dir/eom-mail-watch setup"
