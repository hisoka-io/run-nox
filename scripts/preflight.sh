#!/usr/bin/env bash
set -euo pipefail

readonly script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "$#" -lt 3 || "$#" -gt 4 ]]; then
  echo "usage: preflight.sh <relay|exit> <config.toml> <immutable-image> [deployment.json]" >&2
  exit 2
fi

readonly role="$1"
readonly config_file="$2"
readonly image="$3"
readonly deployment_manifest="${4:-$script_dir/../configs/arbitrum-sepolia.deployment.json}"

if [[ "$role" != "relay" && "$role" != "exit" ]]; then
  echo "role must be relay or exit" >&2
  exit 2
fi
if [[ ! -f "$config_file" ]]; then
  echo "config file does not exist" >&2
  exit 2
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 with tomllib is required for config preflight" >&2
  exit 1
fi
python3 "$script_dir/preflight_config.py" "$role" "$config_file" "$deployment_manifest" "$image"

echo "preflight passed for $role"
