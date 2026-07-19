#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

mkdir -p "$unit_dir"
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
echo "It will begin succeeding after: uv run eom-mail-watch setup"
