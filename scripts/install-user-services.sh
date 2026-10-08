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
# Installation mode is explicit; PATH cannot identify the desktop's sidecar.
packaged_engine=""
is_native_artifact() {
  local artifact_magic
  artifact_magic="$(od -An -N4 -tx1 "$1")" || {
    echo "Cannot read the artifact identity; repair it before installation." >&2
    exit 2
  }
  artifact_magic="${artifact_magic//[[:space:]]/}"
  [[ "$artifact_magic" == 7f454c46 ]]
}
if [[ "$#" == 2 && "$1" == --engine && "$2" == /* ]]; then
  packaged_engine="$2"
  if [[ ! -f "$packaged_engine" || ! -x "$packaged_engine" ]]; then
    echo "The concrete desktop engine must be an executable file." >&2; exit 2
  fi
  if ! is_native_artifact "$packaged_engine"; then
    echo "Select the concrete native desktop engine, not a source shim." >&2; exit 2
  fi
  if [[ -n "$release_keyring_source" ]]; then
    echo "The packaged engine embeds its approved authority; supply it at build time." >&2; exit 2
  fi
  if ! paired_version="$("$packaged_engine" --paired-cli-version)"; then
    echo "Installed desktop engine has no paired CLI; rebuild it." >&2; exit 2
  fi
  if [[ "$paired_version" != "eom-mail-engine-paired-cli-v1" ]]; then
    echo "Installed desktop engine has no compatible paired CLI." >&2; exit 2
  fi
  "$packaged_engine" --install-user-services
  exit 0
elif [[ "$#" != 1 || "$1" != --source ]]; then
  echo "Usage: install-user-services.sh --engine /absolute/desktop/sidecar OR --source" >&2
  exit 2
fi

# uv publishes this same alias. Refuse before it can replace a native desktop
# deployment; source installation remains independent of native admission.
if [[ -e "$tool_bin_dir/eom-mail-watch" ]]; then
  if [[ ! -f "$tool_bin_dir/eom-mail-watch" || ! -r "$tool_bin_dir/eom-mail-watch" ]]; then
    echo "Cannot inspect the existing scheduled alias; repair it before source installation." >&2
    exit 2
  fi
  if is_native_artifact "$tool_bin_dir/eom-mail-watch"; then
    echo "Source installation would replace the paired native alias. Use --engine with the desktop sidecar." >&2
    exit 2
  fi
elif [[ -L "$tool_bin_dir/eom-mail-watch" ]]; then
  echo "The scheduled alias is unresolved; repair it before source installation." >&2
  exit 2
fi

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

PYTHONPATH="$repo_dir/src" "$snapshot_python" -c \
  'from eom_email_watcher.deployment import install_source_units; install_source_units()'

echo "Installed and enabled eom-email-watcher.timer."
echo "Installed and enabled eom-monthly-hours.timer."
echo "Installed a stable eom-mail-watch snapshot at $tool_bin_dir/eom-mail-watch."
if [[ -n "$release_keyring_target" && -f "$release_keyring_target" ]]; then
  echo "Installed the approved Connect release authority for the service snapshot."
else
  echo "Connect remains unavailable to the service snapshot until an approved release authority is installed."
fi
echo "It will begin succeeding after: $tool_bin_dir/eom-mail-watch setup"
