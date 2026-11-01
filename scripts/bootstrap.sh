#!/usr/bin/env bash
set -euo pipefail

studio_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
studio_action="${1:-check}"
if [[ $# -gt 0 ]]; then shift; fi
if [[ "$studio_action" != check && "$studio_action" != install ]]; then
  printf '%s\n' '{"ready":false,"agent_action_required":"Unknown bootstrap action"}'
  exit 2
fi
studio_data="${XDG_DATA_HOME:-$HOME/.local/share}/story-studio"
studio_python=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -I -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' >/dev/null 2>&1; then
    studio_python="$(command -v "$candidate")"
    break
  fi
done
if [[ -z "$studio_python" && "$studio_action" == check ]]; then
  printf '%s\n' '{"ready":false,"configuration_plan":["Managed Python 3.11","Then check tools required by the requested stages"],"agent_runs_installation":true,"manual_commands_required":false}'
  exit 1
fi
if [[ -z "$studio_python" ]]; then
  case "$(uname -s)" in
    Darwin) studio_os=apple-darwin ;;
    Linux) studio_os=unknown-linux-gnu ;;
    *) printf '%s\n' '{"ready":false,"agent_action_required":"Use the native Windows bootstrap on Windows"}'; exit 2 ;;
  esac
  case "$(uname -m)" in
    arm64|aarch64) studio_arch=aarch64 ;;
    x86_64|amd64) studio_arch=x86_64 ;;
    *) printf '%s\n' '{"ready":false,"agent_action_required":"Agent must select an installer for this CPU architecture"}'; exit 2 ;;
  esac
  studio_uv_version=0.13.5
  studio_asset="uv-${studio_arch}-${studio_os}.tar.gz"
  studio_url="https://github.com/astral-sh/uv/releases/download/${studio_uv_version}/${studio_asset}"
  mkdir -p "$studio_data/downloads" "$studio_data/tools/uv/$studio_uv_version"
  curl --fail --location --proto '=https' --tlsv1.2 "$studio_url.sha256" --output "$studio_data/downloads/$studio_asset.sha256"
  curl --fail --location --proto '=https' --tlsv1.2 "$studio_url" --output "$studio_data/downloads/$studio_asset"
  studio_expected="$(awk '{print $1}' "$studio_data/downloads/$studio_asset.sha256")"
  if command -v sha256sum >/dev/null 2>&1; then
    studio_actual="$(sha256sum "$studio_data/downloads/$studio_asset" | awk '{print $1}')"
  else
    studio_actual="$(shasum -a 256 "$studio_data/downloads/$studio_asset" | awk '{print $1}')"
  fi
  if [[ ! "$studio_expected" =~ ^[a-fA-F0-9]{64}$ || "$studio_actual" != "$studio_expected" ]]; then
    printf '%s\n' '{"ready":false,"agent_action_required":"uv checksum mismatch; nothing was executed"}'
    exit 2
  fi
  if tar -tzf "$studio_data/downloads/$studio_asset" | grep -E '(^/|(^|/)\.\.(/|$))' >/dev/null; then
    printf '%s\n' '{"ready":false,"agent_action_required":"Unsafe archive path"}'
    exit 2
  fi
  tar -xzf "$studio_data/downloads/$studio_asset" -C "$studio_data/tools/uv/$studio_uv_version"
  studio_uv="$(find "$studio_data/tools/uv/$studio_uv_version" -type f -name uv -print -quit)"
  export UV_PYTHON_INSTALL_DIR="$studio_data/python"
  export UV_PYTHON_BIN_DIR="$studio_data/python-bin"
  export UV_NO_MODIFY_PATH=1
  "$studio_uv" python install 3.11
  studio_python="$("$studio_uv" python find --managed-python 3.11)"
fi
exec "$studio_python" -I -X utf8 -B "$studio_script_dir/bootstrap_environment.py" "$studio_action" "$@"
