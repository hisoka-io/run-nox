#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
image="ghcr.io/hisoka-io/nox@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
deployment_fixture="$repo_dir/scripts/fixtures/deployment.json"
export NOX_PREFLIGHT_RPC_FIXTURE="$repo_dir/scripts/fixtures/rpc.json"
export NOX__ROUTING_PRIVATE_KEY="$(printf '11%.0s' {1..32})"
export NOX__P2P_PRIVATE_KEY="$(printf '22%.0s' {1..32})"
export NOX__ETH_WALLET_PRIVATE_KEY="$(printf '33%.0s' {1..32})"

[[ "$(grep -c 'user: "10001:10001"' "$repo_dir/docker-compose.yml")" -eq 2 ]]
[[ "$(grep -c 'cap_drop: \["ALL"\]' "$repo_dir/docker-compose.yml")" -eq 3 ]]
[[ "$(grep -c 'no-new-privileges:true' "$repo_dir/docker-compose.yml")" -eq 3 ]]

python3 - "$repo_dir/docker-compose.yml" <<'PY'
from pathlib import Path
import re
import sys


compose = Path(sys.argv[1]).read_text(encoding="utf-8")


def service(name: str) -> str:
    match = re.search(
        rf"^  {re.escape(name)}:\n(.*?)(?=^  [A-Za-z0-9_-]+:|^volumes:|\\Z)",
        compose,
        flags=re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise SystemExit(f"compose is missing {name} service")
    return match.group(1)


preflight = service("preflight")
if "image: ${NOX_PREFLIGHT_IMAGE:?" not in preflight:
    raise SystemExit("preflight does not require an immutable preflight image")
if 'user: "65532:65532"' not in preflight or "read_only: true" not in preflight:
    raise SystemExit("preflight is not isolated as a read-only non-root service")
if "source: ./deployment.json" not in preflight or "target: /etc/nox-release/deployment.json" not in preflight:
    raise SystemExit("preflight does not use the release-pinned deployment manifest")
if preflight.count("create_host_path: false") != 3:
    raise SystemExit("preflight can create an empty host path instead of failing closed")
if "${NOX_IMAGE:?" not in preflight:
    raise SystemExit("preflight does not receive the configured Nox image")
if "service_completed_successfully" in preflight:
    raise SystemExit("preflight cannot depend on its own completion")

for name in ("price-server", "nox"):
    body = service(name)
    required = "preflight:\n        condition: service_completed_successfully"
    if required not in body:
        raise SystemExit(f"{name} can start without a successful preflight")

nox = service("nox")
if "source: ./config.toml" not in nox or "target: /etc/nox/config.toml" not in nox:
    raise SystemExit("nox does not mount the operator configuration explicitly")
if nox.count("create_host_path: false") != 1:
    raise SystemExit("nox can create an empty configuration path")
PY

python3 - "$repo_dir/configs" <<'PY'
import json
import sys
import tomllib
from pathlib import Path

configs = Path(sys.argv[1])
manifest = json.loads((configs / "arbitrum-sepolia.deployment.json").read_text(encoding="utf-8"))
contracts = {name: value.lower() for name, value in manifest["contracts"].items()}
fee_assets = {value.lower() for value in manifest["feeAssets"]}
for role in ("relay", "exit"):
    config = tomllib.loads((configs / f"{role}.toml").read_text(encoding="utf-8"))
    expected = {
        "chain_id": manifest["meta"]["chainId"],
        "chain_start_block": manifest["meta"]["startBlock"],
        "registry_contract_address": contracts["noxRegistry"],
        "nox_reward_pool_address": contracts["noxRewardPool"],
        "nox_entry_point_address": contracts["noxEntryPoint"],
        "metrics_port": config.get("p2p_port", 0) + 1,
    }
    for field, value in expected.items():
        actual = config.get(field)
        if isinstance(actual, str):
            actual = actual.lower()
        if actual != value:
            raise SystemExit(f"configs/{role}.toml {field} does not match the committed manifest")
exit_config = tomllib.loads((configs / "exit.toml").read_text(encoding="utf-8"))
if {token["address"].lower() for token in exit_config["tokens"]} != fee_assets:
    raise SystemExit("configs/exit.toml tokens do not match the committed feeAssets")
adapter = exit_config["payment_adapters"][0]
if adapter["address"].lower() != contracts["howlPaymentAdapter"]:
    raise SystemExit("configs/exit.toml payment adapter does not match the committed manifest")
if {asset.lower() for asset in adapter["fee_assets"]} != fee_assets:
    raise SystemExit("configs/exit.toml adapter fee_assets do not match the committed feeAssets")
PY

relay_config="$(mktemp)"
sed \
  -e 's/^registry_contract_address = .*/registry_contract_address = "0x5555555555555555555555555555555555555555"/' \
  -e 's/^chain_start_block = .*/chain_start_block = 123/' \
  "$repo_dir/configs/relay.toml" >"$relay_config"
relay_output="$("$repo_dir/scripts/preflight.sh" relay "$relay_config" "$image" "$deployment_fixture")"
[[ "$relay_output" == *"preflight passed for relay"* ]]
[[ "$relay_output" != *"$NOX__ROUTING_PRIVATE_KEY"* ]]

if "$repo_dir/scripts/preflight.sh" relay "$relay_config" \
  "ghcr.io/hisoka-io/nox@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc" \
  "$deployment_fixture" >/dev/null 2>&1; then
  echo "relay preflight accepted an image outside the deployment release record" >&2
  exit 1
fi

if python3 "$repo_dir/scripts/preflight_config.py" auto "$relay_config" "$deployment_fixture" "$image" \
  "python@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc" \
  >/dev/null 2>&1; then
  echo "compose preflight accepted an image outside the deployment release record" >&2
  exit 1
fi

if python3 "$repo_dir/scripts/preflight_config.py" auto "$relay_config" "$deployment_fixture" "$image" \
  "python@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" \
  >/dev/null 2>&1; then
  echo "compose preflight accepted the test-only RPC fixture" >&2
  exit 1
fi

wrong_asset_config="$(mktemp)"
exit_config="$(mktemp)"
missing_quote_config="$(mktemp)"
zero_adapter_config="$(mktemp)"
bad_hash_manifest="$(mktemp)"
bad_secret_config="$(mktemp)"
unset_entry_config="$(mktemp)"
trap 'rm -f "$bad_hash_manifest" "$bad_secret_config" "$exit_config" "$missing_quote_config" "$relay_config" "$unset_entry_config" "$wrong_asset_config" "$zero_adapter_config"' EXIT

sed '0,/0x07ad118d6cc8642c86c03827f276d8b791a65e5c99a3845faf186be720a1455d/s//0xffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff/' \
  "$deployment_fixture" >"$bad_hash_manifest"
if "$repo_dir/scripts/preflight.sh" relay "$relay_config" "$image" "$bad_hash_manifest" >/dev/null 2>&1; then
  echo "relay preflight accepted a mismatched committed code hash" >&2
  exit 1
fi

sed 's/^routing_private_key = ""/routing_private_key = ["not", "a", "key"]/' \
  "$relay_config" >"$bad_secret_config"
if "$repo_dir/scripts/preflight.sh" relay "$bad_secret_config" "$image" "$deployment_fixture" >/dev/null 2>&1; then
  echo "relay preflight accepted secret material in TOML" >&2
  exit 1
fi
sed \
  -e 's/^registry_contract_address = .*/registry_contract_address = "0x5555555555555555555555555555555555555555"/' \
  -e 's/^chain_start_block = .*/chain_start_block = 123/' \
  -e 's/^nox_reward_pool_address = .*/nox_reward_pool_address = "0x2222222222222222222222222222222222222222"/' \
  -e 's/^nox_entry_point_address = .*/nox_entry_point_address = "0x1111111111111111111111111111111111111111"/' \
  -e '/^\[\[tokens\]\]/,/^\[\[payment_adapters\]\]/ { s/^address = .*/address = "0x5555555555555555555555555555555555555555"/; s/^symbol = .*/symbol = "TEST"/; s/^decimals = .*/decimals = 18/; s/^price_id = .*/price_id = "usd-coin"/; }' \
  -e '/^\[\[payment_adapters\]\]/,$ { s/^address = .*/address = "0x4444444444444444444444444444444444444444"/; s/^fee_assets = .*/fee_assets = ["0x5555555555555555555555555555555555555555"]/; }' \
  "$repo_dir/configs/exit.toml" >"$wrong_asset_config"
if "$repo_dir/scripts/preflight.sh" exit "$wrong_asset_config" "$image" "$deployment_fixture" >/dev/null 2>&1; then
  echo "exit preflight accepted an unverified fee asset" >&2
  exit 1
fi

sed \
  -e 's/^registry_contract_address = .*/registry_contract_address = "0x5555555555555555555555555555555555555555"/' \
  -e 's/^chain_start_block = .*/chain_start_block = 123/' \
  -e 's/^nox_reward_pool_address = .*/nox_reward_pool_address = "0x2222222222222222222222222222222222222222"/' \
  -e 's/^nox_entry_point_address = .*/nox_entry_point_address = "0x1111111111111111111111111111111111111111"/' \
  -e '/^\[\[tokens\]\]/,/^\[\[payment_adapters\]\]/ { s/^address = .*/address = "0x3333333333333333333333333333333333333333"/; s/^symbol = .*/symbol = "TEST"/; s/^decimals = .*/decimals = 18/; s/^price_id = .*/price_id = "usd-coin"/; }' \
  -e '/^\[\[payment_adapters\]\]/,$ { s/^address = .*/address = "0x4444444444444444444444444444444444444444"/; s/^fee_assets = .*/fee_assets = ["0x3333333333333333333333333333333333333333"]/; }' \
  "$repo_dir/configs/exit.toml" >"$exit_config"
exit_output="$("$repo_dir/scripts/preflight.sh" exit "$exit_config" "$image" "$deployment_fixture")"
[[ "$exit_output" == *"preflight passed for exit"* ]]
[[ "$exit_output" != *"$NOX__ETH_WALLET_PRIVATE_KEY"* ]]
grep -q '^quote_maximum_transaction_gas = 20000000$' "$exit_config"

sed 's/^nox_entry_point_address = .*/nox_entry_point_address = "0x0000000000000000000000000000000000000000"/' \
  "$exit_config" >"$unset_entry_config"
if "$repo_dir/scripts/preflight.sh" exit "$unset_entry_config" "$image" "$deployment_fixture" >/dev/null 2>&1; then
  echo "exit preflight accepted an unset EntryPoint address" >&2
  exit 1
fi

valid_eth_key="$NOX__ETH_WALLET_PRIVATE_KEY"
NOX__ETH_WALLET_PRIVATE_KEY="$(printf 'ff%.0s' {1..32})"
if "$repo_dir/scripts/preflight.sh" exit "$exit_config" "$image" "$deployment_fixture" >/dev/null 2>&1; then
  echo "exit preflight accepted a scalar outside the secp256k1 order" >&2
  exit 1
fi
NOX__ETH_WALLET_PRIVATE_KEY="$valid_eth_key"

valid_routing_key="$NOX__ROUTING_PRIVATE_KEY"
NOX__ROUTING_PRIVATE_KEY="$(printf 'AA%.0s' {1..32})"
if "$repo_dir/scripts/preflight.sh" relay "$relay_config" "$image" "$deployment_fixture" >/dev/null 2>&1; then
  echo "relay preflight accepted a non-canonical uppercase routing key" >&2
  exit 1
fi
NOX__ROUTING_PRIVATE_KEY="$valid_routing_key"

sed '/^quote_max_outstanding = /d' "$exit_config" >"$missing_quote_config"
if "$repo_dir/scripts/preflight.sh" exit "$missing_quote_config" "$image" "$deployment_fixture" >/dev/null 2>&1; then
  echo "exit preflight accepted a missing quote capacity" >&2
  exit 1
fi

sed '/^\[\[payment_adapters\]\]/,$ s/address = "0x4444444444444444444444444444444444444444"/address = "0x0000000000000000000000000000000000000000"/' "$exit_config" >"$zero_adapter_config"
if "$repo_dir/scripts/preflight.sh" exit "$zero_adapter_config" "$image" "$deployment_fixture" >/dev/null 2>&1; then
  echo "exit preflight accepted an unset payment adapter" >&2
  exit 1
fi
