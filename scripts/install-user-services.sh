#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
tool_bin_dir="$HOME/.local/bin"
tool_dir="${XDG_DATA_HOME:-$HOME/.local/share}/uv/tools"
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
  uv tool install --force --constraints "$constraints_file" "$repo_dir"
test -x "$tool_bin_dir/eom-mail-watch"

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
echo "It will begin succeeding after: $tool_bin_dir/eom-mail-watch setup"
