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
[[ "$(grep -c 'cap_drop: \["ALL"\]' "$repo_dir/docker-compose.yml")" -eq 7 ]]
[[ "$(grep -c 'no-new-privileges:true' "$repo_dir/docker-compose.yml")" -eq 7 ]]

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

price_server = service("price-server")
if 'profiles: ["exit"]' not in price_server:
    raise SystemExit("price-server must run only under the exit profile")
if "PRICE_SERVER_BIND=127.0.0.1" not in price_server:
    raise SystemExit("price-server must bind to loopback")
optional_price = (
    "price-server:\n"
    "        condition: service_started\n"
    "        required: false"
)
if optional_price not in nox:
    raise SystemExit("nox must not wait for a healthy price server, and relays run without one")
if "service_healthy" in nox:
    raise SystemExit("nox must not wait for a healthy price server")

# Optional KPS entry: every nox-kps service is opt-in (profile "kps", or
# "kps-admin" for one-off commands), hardened, and runs from the node's release
# image (D-09: nox-kps ships in the node image). The sidecar starts only after
# its own preflight and the volume init.
profiles = {
    "nox-kps-preflight": 'profiles: ["kps"]',
    "nox-kps-init": 'profiles: ["kps", "kps-admin"]',
    "nox-kps": 'profiles: ["kps"]',
    "nox-kps-admin": 'profiles: ["kps-admin"]',
}
for name, profile in profiles.items():
    body = service(name)
    if profile not in body:
        raise SystemExit(f"{name} must run only under {profile}")
    if 'cap_drop: ["ALL"]' not in body or "no-new-privileges:true" not in body or "read_only: true" not in body:
        raise SystemExit(f"{name} is not hardened (cap_drop ALL, no-new-privileges, read-only root)")
    if name != "nox-kps-preflight" and "image: ${NOX_IMAGE:?" not in body:
        raise SystemExit(f"{name} must run from the pinned node image NOX_IMAGE, which ships nox-kps")
if "NOX_KPS_IMAGE" in compose:
    raise SystemExit("nox-kps runs from NOX_IMAGE; a separate NOX_KPS_IMAGE is not part of the kit")
kps = service("nox-kps")
if 'user: "10002:10002"' not in kps or "network_mode: host" not in kps:
    raise SystemExit("nox-kps must run as UID 10002 on the host network (loopback upstreams)")
if 'entrypoint: ["nox-kps"]' not in kps or 'command: ["run"]' not in kps:
    raise SystemExit("nox-kps must run `nox-kps run`, not the image's default node command")
for dependency in ("nox-kps-preflight", "nox-kps-init"):
    if f"{dependency}:\n        condition: service_completed_successfully" not in kps:
        raise SystemExit(f"nox-kps can start without {dependency}")
if "nox-kps-bundles:/var/lib/nox-kps/keccak:ro" not in kps or "create_host_path: false" not in kps:
    raise SystemExit("nox-kps must mount bundles read-only and fail closed without nox-kps.toml")
if 'test: ["CMD", "nox-kps", "healthcheck"]' not in kps or "mem_limit: 512m" not in kps:
    raise SystemExit("nox-kps needs its own healthcheck (the image default probes the node) and a memory limit")
if "stop_grace_period: 15s" not in kps or "timeout: 6s" not in kps:
    raise SystemExit("nox-kps needs 15 s to drain (shutdown grace + linger) and 6 s per healthcheck")
kps_preflight = service("nox-kps-preflight")
if "image: ${NOX_PREFLIGHT_IMAGE:?" not in kps_preflight or kps_preflight.count("create_host_path: false") != 4:
    raise SystemExit("nox-kps-preflight must use the pinned preflight image and fail closed on missing files")
if "target: /etc/nox-release/deployment.json" not in kps_preflight or "- ${NOX_IMAGE:?" not in kps_preflight:
    raise SystemExit("nox-kps-preflight must check NOX_IMAGE against the release record")
if "env_file" in kps_preflight:
    raise SystemExit("nox-kps-preflight needs no secrets and must not load .env")
init = service("nox-kps-init")
if 'cap_add: ["CHOWN", "DAC_READ_SEARCH"]' not in init or "network_mode: none" not in init:
    raise SystemExit("nox-kps-init must keep only CHOWN and DAC_READ_SEARCH and run without a network")
admin = service("nox-kps-admin")
if 'user: "10002:10002"' not in admin or "network_mode: none" not in admin or "env_file" in admin:
    raise SystemExit("nox-kps-admin must run as UID 10002 without a network or secrets")
if "nox-kps-bundles:/var/lib/nox-kps/keccak\n" not in admin or "nox-kps-init:\n        condition: service_completed_successfully" not in admin:
    raise SystemExit("nox-kps-admin must write bundles and run after the volume init")
for name in ("nox-kps", "nox-kps-admin"):
    if "NOX_KPS_CONFIG=/etc/nox-kps/config.toml" not in service(name):
        raise SystemExit(f"{name} must read the mounted nox-kps.toml")
PY

# nox-kps preflight (scripts/preflight_kps.py): one valid config passes, each
# unsafe or inconsistent one is rejected with an actionable message.
python3 - "$repo_dir" <<'PY'
import json
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

repo = Path(sys.argv[1])
work = Path(tempfile.mkdtemp())
script = repo / "scripts" / "preflight_kps.py"
# nox-kps runs from the node image of a release whose record names the nox-kps
# version it ships (release.noxKps).
image = "ghcr.io/hisoka-io/nox@sha256:" + "ab" * 32
manifest = json.loads((repo / "configs" / "arbitrum-sepolia.deployment.json").read_text(encoding="utf-8"))
manifest["release"]["noxImage"] = image
manifest["release"]["noxKps"] = "0.1.0"
deployment = json.dumps(manifest)
without_kps = json.loads(deployment)
del without_kps["release"]["noxKps"]
bad_version = json.loads(deployment)
bad_version["release"]["noxKps"] = "latest"
template = (repo / "configs" / "nox-kps.toml").read_text(encoding="utf-8")
relay = (repo / "configs" / "relay.toml").read_text(encoding="utf-8")


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("0.0.0.0", 0))
        return probe.getsockname()[1]


port = free_udp_port()
listen_line = f'listen = "0.0.0.0:{port}"'
certhash = "uEi" + "A" * 44
certhash_line = f'expected_certhash = "{certhash}"'
good_kps = (
    template.replace('advertise = ["203.0.113.10"]', 'advertise = ["3.239.73.249"]')
    .replace('listen = "0.0.0.0:15005"', listen_line)
    .replace('expected_certhash = ""', certhash_line)
)
if good_kps.count(certhash_line) != 1 or good_kps.count(listen_line) != 1:
    raise SystemExit("configs/nox-kps.toml no longer has the listen, advertise and expected_certhash lines the README edits")
header_block = '\n[ingress]\nclient_ip_header = "x-real-ip"\n'
good_node = (
    relay.replace("ingress_port = 0", "ingress_port = 15002").replace("topology_api_port = 0", "topology_api_port = 15003")
    + header_block
)


def run(
    kps: str, node: str, kps_image: str = image, release: str = deployment
) -> subprocess.CompletedProcess[str]:
    (work / "nox-kps.toml").write_text(kps, encoding="utf-8")
    (work / "config.toml").write_text(node, encoding="utf-8")
    (work / "deployment.json").write_text(release, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(script), str(work / "nox-kps.toml"), str(work / "config.toml"),
         str(work / "deployment.json"), kps_image],
        capture_output=True, text=True, check=False,
    )


ok = run(good_kps, good_node)
if ok.returncode != 0 or "nox-kps 0.1.0 preflight passed" not in ok.stdout:
    raise SystemExit(f"valid nox-kps config rejected: {ok.stderr}")
ipv6_ok = run(good_kps.replace(listen_line, f'listen = "[::]:{port}"'), good_node)
if ipv6_ok.returncode != 0:
    raise SystemExit(f"dual-stack listen rejected: {ipv6_ok.stderr}")
with_address = run(good_kps.replace('node_address = ""', 'node_address = "0x862D6B1105bdE9d64dC5182fe3CD9d09F6F37463"'), good_node)
if with_address.returncode != 0:
    raise SystemExit(f"a registered node_address was rejected: {with_address.stderr}")
valid = 3

sectioned = """[kps]
listen = "0.0.0.0:15005"
public_ips = ["3.239.73.249"]
[upstreams]
ingress = "http://127.0.0.1:15002"
"""
cases = {
    "the template's documentation IP": (
        template.replace('listen = "0.0.0.0:15005"', listen_line).replace('expected_certhash = ""', certhash_line),
        good_node, image, "not a public IP"),
    "the template before nox-kps init": (
        good_kps.replace(certhash_line, 'expected_certhash = ""'), good_node, image, "nox-kps-admin init"),
    "a malformed expected_certhash": (
        good_kps.replace(certhash_line, 'expected_certhash = "uEiShort"'), good_node, image, "expected_certhash"),
    "a mutable image tag": (good_kps, good_node, "ghcr.io/hisoka-io/nox:latest", "immutable"),
    "a separate nox-kps image": (
        good_kps, good_node, "ghcr.io/hisoka-io/nox-kps@sha256:" + "ab" * 32, "ghcr.io/hisoka-io/nox@sha256"),
    "a node image outside the release record": (
        good_kps, good_node, "ghcr.io/hisoka-io/nox@sha256:" + "cd" * 32, "release.noxImage"),
    "a disabled node ingress": (
        good_kps, good_node.replace("ingress_port = 15002", "ingress_port = 0"), image, "ingress_port is 0"),
    "a mismatched client IP header": (
        good_kps, good_node.replace('client_ip_header = "x-real-ip"', 'client_ip_header = "x-forwarded-for"'),
        image, "client_ip_header"),
    "a node without the header": (good_kps, good_node.replace(header_block, "\n"), image, "client_ip_header"),
    "an upstream with a URL scheme": (
        good_kps.replace('"127.0.0.1:15002"', '"http://127.0.0.1:15002"'), good_node, image, "without a scheme"),
    "a non-loopback upstream": (
        good_kps.replace('"127.0.0.1:15002"', '"10.0.0.5:15002"'), good_node, image, "loopback"),
    "an upstream by name": (
        good_kps.replace('"127.0.0.1:15002"', '"localhost:15002"'), good_node, image, "127.0.0.1:<port>"),
    "an IPv6 loopback upstream": (
        good_kps.replace('"127.0.0.1:15003"', '"[::1]:15003"'), good_node, image, "127.0.0.1:<port>"),
    "an expected_certhash with spaces": (
        good_kps.replace(certhash_line, f'expected_certhash = " {certhash} "'), good_node, image, "expected_certhash"),
    "an upstream on the wrong port": (
        good_kps.replace('"127.0.0.1:15002"', '"127.0.0.1:15009"'), good_node, image, "ingress_port"),
    "a topology upstream on no topology port": (
        good_kps.replace('"127.0.0.1:15003"', '"127.0.0.1:15009"'), good_node, image, "serves no topology"),
    "a public admin listener": (
        good_kps.replace('admin_listen = "127.0.0.1:15006"', 'admin_listen = "0.0.0.0:15006"'), good_node, image,
        "admin_listen"),
    "a bind to a specific address": (
        good_kps.replace(listen_line, f'listen = "3.239.73.249:{port}"'), good_node, image, "0.0.0.0"),
    "an identity outside the volume": (
        good_kps.replace('"/var/lib/nox-kps/kps.key"', '"/tmp/kps.key"'), good_node, image, "key_file"),
    "a private advertise override": (
        good_kps + "allow_private_advertise = true\n", good_node, image, "allow_private_advertise"),
    "a malformed node_address": (
        good_kps.replace('node_address = ""', 'node_address = "0x1234"'), good_node, image, "node_address"),
    "a bundle dir outside the volume": (
        good_kps.replace('"/var/lib/nox-kps/keccak"', '"/tmp/keccak"'), good_node, image, "keccak_dir"),
    "an unknown key": (good_kps + "public_ips = []\n", good_node, image, "does not know"),
    "the sectioned schema": (sectioned, good_node, image, "does not know"),
    "a KPS port equal to a node port": (
        good_kps.replace(listen_line, 'listen = "0.0.0.0:15001"'), good_node, image, "collides"),
}
for label, (kps, node, kps_image, needle) in cases.items():
    result = run(kps, node, kps_image)
    if result.returncode == 0 or needle not in result.stderr:
        raise SystemExit(f"nox-kps preflight accepted {label}: rc={result.returncode} {result.stderr}")
release_cases = {
    "a release that ships no nox-kps": (json.dumps(without_kps), "release.noxKps is unset"),
    "a release.noxKps that is not a version": (json.dumps(bad_version), "release.noxKps must be a version"),
    "an unreadable deployment.json": ("{", "cannot be parsed"),
}
for label, (release, needle) in release_cases.items():
    result = run(good_kps, good_node, image, release)
    if result.returncode == 0 or needle not in result.stderr:
        raise SystemExit(f"nox-kps preflight accepted {label}: rc={result.returncode} {result.stderr}")

with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as busy:
    busy.bind(("0.0.0.0", 0))
    busy_port = busy.getsockname()[1]
    result = run(good_kps.replace(listen_line, f'listen = "0.0.0.0:{busy_port}"'), good_node)
    if result.returncode == 0 or "already in use" not in result.stderr:
        raise SystemExit(f"nox-kps preflight accepted a UDP port held by another process: {result.stderr}")
print(
    f"nox-kps preflight: {valid} valid configs passed, "
    f"{len(cases) + len(release_cases) + 1} invalid configs rejected"
)
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

# The committed manifest carries every field the generator reads, so
# regenerating it from itself must reproduce it byte for byte.
manifest="$repo_dir/configs/arbitrum-sepolia.deployment.json"
release_image() {
  python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["release"][sys.argv[2]])' "$manifest" "$1"
}
kps_version_args=()
if kps_version="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["release"]["noxKps"])' "$manifest" 2>/dev/null)"; then
  kps_version_args=(--nox-kps-version "$kps_version")
fi
cmp -s "$manifest" <(python3 "$repo_dir/scripts/make-deployment-manifest.py" "$manifest" \
  --nox-image "$(release_image noxImage)" --preflight-image "$(release_image preflightImage)" \
  "${kps_version_args[@]}")

# release.noxKps: the generator records the nox-kps version shipped in the node
# image, and release validation accepts only a version there.
kps_manifest="$(python3 "$repo_dir/scripts/make-deployment-manifest.py" "$manifest" \
  --nox-image "$(release_image noxImage)" --preflight-image "$(release_image preflightImage)" \
  --nox-kps-version 0.1.0)"
[[ "$(python3 -c 'import json, sys; print(json.loads(sys.argv[1])["release"]["noxKps"])' "$kps_manifest")" == "0.1.0" ]]
for bad_release in '"latest"' '1' 'misspelled'; do
  if python3 - "$manifest" "$bad_release" "$repo_dir/scripts" <<'RELEASE' >/dev/null 2>&1
import json
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[3])
import preflight_config

manifest = json.load(open(sys.argv[1], encoding="utf-8"))
if sys.argv[2] == "misspelled":
    manifest["release"]["noxKPS"] = "0.1.0"
else:
    manifest["release"]["noxKps"] = json.loads(sys.argv[2])
preflight_config.validate_release(manifest, manifest["release"]["noxImage"], None)
RELEASE
  then
    echo "release validation accepted release.noxKps case ${bad_release}" >&2
    exit 1
  fi
done
if python3 "$repo_dir/scripts/make-deployment-manifest.py" "$manifest" \
  --nox-image "$(release_image noxImage)" --preflight-image "$(release_image preflightImage)" \
  --nox-kps-version latest >/dev/null 2>&1; then
  echo "manifest generator accepted a nox-kps version that is not a version" >&2
  exit 1
fi

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

moved_metrics_config="$(mktemp)"
sed 's/^metrics_port = .*/metrics_port = 15005/' "$relay_config" >"$moved_metrics_config"
if "$repo_dir/scripts/preflight.sh" relay "$moved_metrics_config" "$image" "$deployment_fixture" >/dev/null 2>&1; then
  echo "relay preflight accepted a metrics_port other than p2p_port + 1" >&2
  exit 1
fi
moved_both_config="$(mktemp)"
sed -e 's/^p2p_port = .*/p2p_port = 15100/' -e 's/^metrics_port = .*/metrics_port = 15101/' \
  "$relay_config" >"$moved_both_config"
"$repo_dir/scripts/preflight.sh" relay "$moved_both_config" "$image" "$deployment_fixture" >/dev/null
if NOX__P2P_PORT=15200 "$repo_dir/scripts/preflight.sh" relay "$relay_config" "$image" "$deployment_fixture" >/dev/null 2>&1; then
  echo "relay preflight ignored a NOX__P2P_PORT override that breaks p2p_port + 1" >&2
  exit 1
fi
NOX__P2P_PORT=15200 NOX__METRICS_PORT=15201 \
  "$repo_dir/scripts/preflight.sh" relay "$relay_config" "$image" "$deployment_fixture" >/dev/null
rm -f "$moved_metrics_config" "$moved_both_config"

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

upgraded_dir="$(mktemp -d)"
python3 - "$deployment_fixture" "$upgraded_dir" <<'PY'
import json
import sys
from pathlib import Path

source = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
out = Path(sys.argv[2])
cases = {
    "darkpool-impl": ("proxySlots", "darkPool", "impl", "0x9999999999999999999999999999999999999999"),
    "darkpool-hash": ("proxyImplementationCodeHashes", "darkPool", None, "0x" + "e" * 64),
    "registry-impl": ("proxySlots", "noxRegistry", "impl", "0x9999999999999999999999999999999999999999"),
}
for name, (section, contract, field, value) in cases.items():
    manifest = json.loads(json.dumps(source))
    if field is None:
        manifest[section][contract] = value
    else:
        manifest[section][contract][field] = value
    (out / f"{name}.json").write_text(json.dumps(manifest), encoding="utf-8")
PY
for upgraded in darkpool-impl darkpool-hash; do
  if ! relay_warning="$("$repo_dir/scripts/preflight.sh" relay "$relay_config" "$image" "$upgraded_dir/$upgraded.json" 2>&1)"; then
    echo "relay preflight failed on a DarkPool-only upgrade ($upgraded)" >&2
    exit 1
  fi
  [[ "$relay_warning" == *"warning: darkPool was upgraded"* ]]
  if "$repo_dir/scripts/preflight.sh" exit "$exit_config" "$image" "$upgraded_dir/$upgraded.json" >/dev/null 2>&1; then
    echo "exit preflight accepted a DarkPool implementation outside the manifest ($upgraded)" >&2
    exit 1
  fi
done
if "$repo_dir/scripts/preflight.sh" relay "$relay_config" "$image" "$upgraded_dir/registry-impl.json" >/dev/null 2>&1; then
  echo "relay preflight accepted a NoxRegistry implementation outside the manifest" >&2
  exit 1
fi
rm -rf "$upgraded_dir"

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
