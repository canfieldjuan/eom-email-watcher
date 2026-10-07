#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
unit_dir=""
tool_bin_dir="$HOME/.local/bin"
tool_dir="${XDG_DATA_HOME:-$HOME/.local/share}/uv/tools"
release_keyring_source="${LOCAL_CONNECT_ENTITLEMENT_KEYRING_FILE:-}"
# Where installers before the vendor-neutral connect_automate core placed the
# authority. That directory also holds other watcher data, so it is only read.
legacy_release_keyring="$HOME/.local/share/eom-email-watcher/connect-entitlement-keyring.json"
release_keyring_target=""
release_keyring_input=""
release_keyring_stage=""
# An installed desktop owns both readers. Never install a separate snapshot beside it.
packaged_engine=""
# A prior uv snapshot also exports an API console script. It is not a desktop
# bundle. Skip that known source owner, then find the installed desktop on PATH.
while IFS= read -r candidate; do
  if [[ "$candidate" -ef "$tool_dir/eom-email-watcher/bin/eom-mail-engine" ]]; then
    continue
  fi
  packaged_engine="$candidate"
  break
done < <(type -aP eom-mail-engine || true)
if [[ -n "$packaged_engine" ]]; then
  if [[ -n "$release_keyring_source" ]]; then
    echo "The packaged engine embeds its approved authority; supply it at build time." >&2
    exit 2
  fi
  if ! paired_version="$("$packaged_engine" --paired-cli-version)"; then
    echo "Installed desktop engine has no paired CLI; rebuild it before installing services." >&2
    exit 2
  fi
  if [[ "$paired_version" != "eom-mail-engine-paired-cli-v1" ]]; then
    echo "Installed desktop engine has no compatible paired CLI." >&2
    exit 2
  fi
  packaged_engine="$(readlink -f "$packaged_engine")"
  test -x "$packaged_engine"
  "$packaged_engine" --cli --version >/dev/null
  unit_dir="$("$packaged_engine" --service-unit-directory)"
  mkdir -p "$unit_dir" "$tool_bin_dir"
  alias_stage="$(mktemp -d "$tool_bin_dir/.paired-cli.XXXXXX")"
  cleanup_alias() { rm -f "$alias_stage/eom-mail-watch"; rmdir "$alias_stage"; }
  trap cleanup_alias EXIT
  ln -s "$packaged_engine" "$alias_stage/eom-mail-watch"
  mv -Tf "$alias_stage/eom-mail-watch" "$tool_bin_dir/eom-mail-watch"
else
constraints_file="$(mktemp)"
cleanup() {
  rm -f "$constraints_file"
  if [[ -n "$release_keyring_stage" ]]; then
    rm -f "$release_keyring_stage"
  fi
}
trap cleanup EXIT

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required to install the systemd service CLI snapshot." >&2
  exit 1
fi

mkdir -p "$tool_bin_dir" "$tool_dir"
uv export --project "$repo_dir" --locked --no-dev --no-emit-project --format requirements.txt \
  --output-file "$constraints_file" >/dev/null
UV_TOOL_BIN_DIR="$tool_bin_dir" UV_TOOL_DIR="$tool_dir" \
  uv tool install --force --reinstall --constraints "$constraints_file" "$repo_dir"
test -x "$tool_bin_dir/eom-mail-watch"
# The service runs this snapshot, so its interpreter decides where the Connect
# authority is read from and whether it is approved. Using it also keeps the
# installer from creating or syncing an environment inside the source checkout.
snapshot_python="$tool_dir/eom-email-watcher/bin/python"
test -x "$snapshot_python"
unit_dir="$(
  PYTHONPATH="$repo_dir/src" "$snapshot_python" -c \
    'from eom_email_watcher.deployment import service_unit_directory; print(service_unit_directory())'
)"

validate_release_keyring() {
  PYTHONPATH="$repo_dir" RELEASE_KEYRING_SOURCE="$1" "$snapshot_python" -c \
    'import os; from pathlib import Path; from scripts.build_desktop_sidecar import validate_entitlement_keyring; validate_entitlement_keyring(Path(os.environ["RELEASE_KEYRING_SOURCE"]))'
}

# The runtime reader owns the installed authority location; never restate it here.
release_keyring_target="$(
  "$snapshot_python" -c \
    'import os; from connect_automate.entitlement import _installed_release_keyring_path; print(_installed_release_keyring_path(os.environ.get("HOME")) or "")'
)"

if [[ -n "$release_keyring_source" ]]; then
  if [[ "$release_keyring_source" != /* ]]; then
    echo "LOCAL_CONNECT_ENTITLEMENT_KEYRING_FILE must be absolute." >&2
    exit 1
  fi
  if [[ -z "$release_keyring_target" ]]; then
    echo "HOME must be absolute when installing the Connect authority." >&2
    exit 1
  fi
  validate_release_keyring "$release_keyring_source"
  release_keyring_input="$release_keyring_source"
elif [[ -n "$release_keyring_target" && ! -e "$release_keyring_target" && -f "$legacy_release_keyring" ]]; then
  if validate_release_keyring "$legacy_release_keyring"; then
    release_keyring_input="$legacy_release_keyring"
  else
    echo "Did not migrate $legacy_release_keyring: it is not the approved Connect release authority." >&2
  fi
fi

if [[ -n "$release_keyring_input" ]]; then
  release_keyring_dir="$(dirname "$release_keyring_target")"
  install -d -m 0700 "$release_keyring_dir"
  release_keyring_stage="$(mktemp "$release_keyring_dir/.connect-entitlement-keyring.XXXXXX")"
  install -m 0600 "$release_keyring_input" "$release_keyring_stage"
  mv -f "$release_keyring_stage" "$release_keyring_target"
  release_keyring_stage=""
  if [[ "$release_keyring_input" == "$legacy_release_keyring" ]]; then
    echo "Migrated the Connect release authority from $legacy_release_keyring."
  fi
fi

fi

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
if [[ -n "$packaged_engine" ]]; then
  echo "Paired scheduled intake with $packaged_engine."
  echo "It will begin succeeding after: $tool_bin_dir/eom-mail-watch setup"
  exit 0
fi
echo "Installed a stable eom-mail-watch snapshot at $tool_bin_dir/eom-mail-watch."
if [[ -n "$release_keyring_target" && -f "$release_keyring_target" ]]; then
  echo "Installed the approved Connect release authority for the service snapshot."
else
  echo "Connect remains unavailable to the service snapshot until an approved release authority is installed."
fi
echo "It will begin succeeding after: $tool_bin_dir/eom-mail-watch setup"
