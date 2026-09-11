#!/usr/bin/env python3
import json
import os
import re
import sys
import tomllib
import urllib.request
from pathlib import Path
from typing import NoReturn

ZERO = "0x" + "0" * 40
ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
HASH = re.compile(r"^0x[0-9a-f]{64}$")
NOX_IMAGE = re.compile(r"^ghcr\.io/hisoka-io/nox@sha256:[0-9a-f]{64}$")
IMMUTABLE_IMAGE = re.compile(
    r"^[a-z0-9][a-z0-9._/-]*(?::[a-zA-Z0-9._-]+)?@sha256:[0-9a-f]{64}$"
)
IMPL_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
SELECTOR = {
    "reward_pool": "0x99f91c28",
    "sandbox": "0x104bd5da",
    "quote_version": "0xafcc377c",
    "dark_pool": "0x4e07747d",
    "entry_point": "0x94430fa5",
    "decimals": "0x313ce567",
    "supported": "0x9be918e6",
    "classified": "0x9ed6bf9e",
    "entrypoint_role": "0x5445bd5d",
    "has_role": "0x91d14854",
}
REQUIRED_CONTRACTS = (
    "noxRegistry",
    "noxRewardPool",
    "noxSandboxImplementation",
    "noxEntryPoint",
    "howlPaymentAdapter",
    "bundleExecutor",
    "darkPool",
    "stakingToken",
)
PROXIES = ("darkPool", "noxRegistry", "noxRewardPool")


def fail(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def load_json(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        fail(f"{label} cannot be parsed: {error}")
    return mapping(value, label)


def load_config(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as handle:
            value = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        fail(f"config cannot be parsed: {error}")
    return mapping(value, "config")


def mapping(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        fail(f"{field} must be an object")
    return value


def address(value: object, field: str, allow_zero: bool = False) -> str:
    if not isinstance(value, str) or ADDRESS.fullmatch(value) is None:
        fail(f"{field} must be a 20-byte 0x-prefixed address")
    normalized = value.lower()
    if not allow_zero and normalized == ZERO:
        fail(f"{field} must come from a committed deployment")
    return normalized


def code_hash(value: object, field: str) -> str:
    if not isinstance(value, str) or HASH.fullmatch(value) is None:
        fail(f"{field} must be a lowercase 32-byte 0x-prefixed hash")
    return value


def immutable_image(value: object, field: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        fail(f"{field} must use an immutable sha256 image digest")
    if value.endswith("0" * 64):
        fail(f"{field} must come from a signed release record")
    return value


def validate_release(
    deployment: dict[str, object], nox_image: str, preflight_image: str | None
) -> None:
    release = mapping(deployment.get("release"), "deployment release")
    expected_nox = immutable_image(release.get("noxImage"), "release.noxImage", NOX_IMAGE)
    expected_preflight = immutable_image(
        release.get("preflightImage"), "release.preflightImage", IMMUTABLE_IMAGE
    )
    actual_nox = immutable_image(nox_image, "NOX image", NOX_IMAGE)
    if actual_nox != expected_nox:
        fail("NOX image must equal deployment release.noxImage")
    if preflight_image is not None:
        actual_preflight = immutable_image(
            preflight_image, "preflight image", IMMUTABLE_IMAGE
        )
        if actual_preflight != expected_preflight:
            fail("preflight image must equal deployment release.preflightImage")


def require_private_key(variable_name: str, label: str, secp256k1: bool = False) -> None:
    value = os.environ.get(variable_name, "")
    if re.fullmatch(r"[0-9a-f]{64}", value) is None or value == "0" * 64:
        fail(f"{label} must be exactly 64 lowercase hexadecimal characters and nonzero")
    if secp256k1 and value >= "fffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364141":
        fail(f"{label} must be a canonical secp256k1 scalar")


def validate_runtime_keys(role: str) -> None:
    require_private_key("NOX__ROUTING_PRIVATE_KEY", "routing private key")
    require_private_key("NOX__P2P_PRIVATE_KEY", "P2P private key")
    if role == "exit":
        require_private_key(
            "NOX__ETH_WALLET_PRIVATE_KEY", "exit wallet private key", secp256k1=True
        )


def positive(source: dict[str, object], field: str) -> int:
    value = source.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        fail(f"{field} must be a positive integer")
    return value


class Rpc:
    def __init__(self, url: str) -> None:
        self.url = url
        fixture = os.environ.get("NOX_PREFLIGHT_RPC_FIXTURE")
        self.fixture = load_json(Path(fixture), "RPC fixture") if fixture else None

    def request(self, method: str, params: list[object]) -> object:
        if self.fixture is not None:
            return self.fixture_result(method, params)
        body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        ).encode()
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            fail(f"RPC preflight failed for {method}: {type(error).__name__}")
        if not isinstance(payload, dict) or "result" not in payload:
            fail(f"RPC preflight returned no result for {method}")
        return payload["result"]

    def fixture_result(self, method: str, params: list[object]) -> object:
        assert self.fixture is not None
        block = self.fixture.get("block_number")
        block_index = 2 if method in {"eth_getStorageAt", "eth_getProof"} else 1
        if method in {"eth_getCode", "eth_getProof", "eth_getStorageAt", "eth_call"}:
            if len(params) <= block_index or params[block_index] != block:
                fail("RPC preflight did not pin one fixture block")
        if method == "eth_chainId":
            value = self.fixture.get("chain_id")
        elif method == "eth_blockNumber":
            value = block
        elif method in {"eth_getCode", "eth_getProof", "eth_getStorageAt"}:
            table_name = {
                "eth_getCode": "codes",
                "eth_getProof": "proofs",
                "eth_getStorageAt": "storage",
            }[method]
            table = self.fixture.get(table_name)
            value = table.get(str(params[0]).lower()) if isinstance(table, dict) else None
            if method == "eth_getProof" and isinstance(value, str):
                value = {"codeHash": value}
        elif method == "eth_call":
            call = params[0]
            table = self.fixture.get("calls")
            key = ""
            if isinstance(call, dict):
                key = f"{str(call.get('to', '')).lower()}:{str(call.get('data', '')).lower()}"
            value = table.get(key) if isinstance(table, dict) else None
        else:
            value = None
        if value is None:
            fail(f"RPC fixture has no result for {method}")
        return value


def rpc_hex(probe: Rpc, method: str, params: list[object]) -> str:
    value = probe.request(method, params)
    if not isinstance(value, str) or re.fullmatch(r"0x[0-9a-fA-F]+", value) is None:
        fail(f"RPC {method} did not return hexadecimal bytes")
    return value.lower()


def abi_word(probe: Rpc, target: str, data: str, block: str, label: str) -> str:
    word = rpc_hex(probe, "eth_call", [{"to": target, "data": data}, block])
    if len(word) != 66:
        fail(f"{label} did not return one ABI word")
    return word


def address_word(value: str) -> str:
    return "0" * 24 + value[2:]


def verify_code(
    probe: Rpc, target: str, expected: object, block: str, label: str
) -> None:
    deployed = rpc_hex(probe, "eth_getCode", [target, block])
    if deployed == "0x" or not any(character != "0" for character in deployed[2:]):
        fail(f"{label} has no code on the configured chain")
    proof = probe.request("eth_getProof", [target, [], block])
    if not isinstance(proof, dict):
        fail(f"{label} eth_getProof result must be an object")
    actual = code_hash(proof.get("codeHash"), f"{label} on-chain codeHash")
    if actual != code_hash(expected, f"deployment {label} code hash"):
        fail(f"{label} runtime code does not match the committed deployment")


def verify_deployment(
    deployment: dict[str, object], probe: Rpc, block: str
) -> dict[str, str]:
    raw_contracts = mapping(deployment.get("contracts"), "deployment contracts")
    hashes = mapping(deployment.get("contractCodeHashes"), "contractCodeHashes")
    for name in REQUIRED_CONTRACTS:
        address(raw_contracts.get(name), f"contracts.{name}")
    contracts: dict[str, str] = {}
    for name, value in raw_contracts.items():
        if value == "":
            continue
        target = address(value, f"contracts.{name}")
        verify_code(probe, target, hashes.get(name), block, name)
        contracts[name] = target

    slots = mapping(deployment.get("proxySlots"), "proxySlots")
    implementation_hashes = mapping(
        deployment.get("proxyImplementationCodeHashes"),
        "proxyImplementationCodeHashes",
    )
    for name in PROXIES:
        proxy = mapping(slots.get(name), f"proxySlots.{name}")
        implementation = address(proxy.get("impl"), f"proxySlots.{name}.impl")
        stored = rpc_hex(probe, "eth_getStorageAt", [contracts[name], IMPL_SLOT, block])
        if len(stored) != 66 or stored[-40:] != implementation[2:]:
            fail(f"{name} EIP-1967 implementation does not match the deployment")
        verify_code(
            probe,
            implementation,
            implementation_hashes.get(name),
            block,
            f"{name} implementation",
        )
    return contracts


def validate_common(
    config: dict[str, object], role: str, deployment: dict[str, object]
) -> tuple[dict[str, str], Rpc, str]:
    if config.get("node_role") != role:
        fail("node_role must match the preflight role")
    for field in (
        "routing_private_key",
        "p2p_private_key",
        "eth_wallet_private_key",
    ):
        value = config.get(field)
        if value is not None and value != "":
            fail(f"{field} must be supplied through the process environment, not TOML")
    meta = mapping(deployment.get("meta"), "deployment meta")
    chain_id = positive(meta, "chainId")
    start_block = positive(meta, "startBlock")
    if config.get("chain_id") != chain_id or config.get("chain_start_block") != start_block:
        fail("chain_id and chain_start_block must equal the committed deployment")
    if config.get("benchmark_mode") is not False:
        fail("benchmark_mode must be false")
    if config.get("chain_data_fee_mode") != "rpc_gas_estimate_includes_data_fee":
        fail("chain_data_fee_mode must use the verified RPC total-gas policy")
    if config.get("native_asset_price_id") != "ethereum" or config.get("native_asset_decimals") != 18:
        fail("native asset price configuration must identify 18-decimal ethereum")
    for field in (
        "oracle_cache_ttl_secs",
        "oracle_max_observation_age_secs",
        "gas_limit_buffer_bps",
        "initial_fee_buffer_bps",
        "replacement_step_bps",
    ):
        positive(config, field)
    skew = config.get("oracle_max_future_skew_secs")
    if not isinstance(skew, int) or isinstance(skew, bool) or skew < 0:
        fail("oracle_max_future_skew_secs must be a non-negative integer")
    rpc_url = config.get("eth_rpc_url")
    if not isinstance(rpc_url, str) or not rpc_url.startswith(("http://", "https://")):
        fail("eth_rpc_url must be an absolute HTTP(S) URL")
    probe = Rpc(rpc_url)
    if int(rpc_hex(probe, "eth_chainId", []), 16) != chain_id:
        fail("RPC chain does not match the committed deployment")
    block = rpc_hex(probe, "eth_blockNumber", [])
    contracts = verify_deployment(deployment, probe, block)
    if address(config.get("registry_contract_address"), "registry_contract_address") != contracts["noxRegistry"]:
        fail("registry_contract_address must equal the committed deployment")
    return contracts, probe, block


def validate_exit(
    config: dict[str, object],
    deployment: dict[str, object],
    contracts: dict[str, str],
    probe: Rpc,
    block: str,
) -> None:
    oracle = config.get("oracle_url")
    if not isinstance(oracle, str) or not oracle.startswith(("http://", "https://")):
        fail("oracle_url must be an absolute HTTP(S) URL for an exit")
    entry_point = contracts["noxEntryPoint"]
    reward_pool = contracts["noxRewardPool"]
    if address(config.get("nox_entry_point_address"), "nox_entry_point_address") != entry_point:
        fail("nox_entry_point_address must equal the committed deployment")
    if address(config.get("nox_reward_pool_address"), "nox_reward_pool_address") != reward_pool:
        fail("nox_reward_pool_address must equal the committed deployment")
    if positive(config, "quote_ttl_secs") > 300:
        fail("quote_ttl_secs must be in 1..=300")
    bps = config.get("quote_network_fee_bps")
    if not isinstance(bps, int) or isinstance(bps, bool) or not 0 <= bps <= 10_000:
        fail("quote_network_fee_bps must be in 0..=10000")
    for field in (
        "quote_maximum_transaction_gas",
        "quote_max_outstanding",
        "quote_max_pending_sponsored_gas",
        "quote_rolling_loss_window_secs",
    ):
        positive(config, field)
    loss = config.get("quote_rolling_loss_limit_native")
    if not isinstance(loss, str) or not loss.isdecimal() or int(loss) <= 0:
        fail("quote_rolling_loss_limit_native must be a positive decimal integer")

    raw_assets = deployment.get("feeAssets")
    if not isinstance(raw_assets, list) or not raw_assets:
        fail("deployment feeAssets must contain committed addresses")
    fee_assets = [address(value, f"feeAssets[{index}]") for index, value in enumerate(raw_assets)]
    if len(set(fee_assets)) != len(fee_assets):
        fail("deployment feeAssets must be unique")
    tokens = config.get("tokens")
    if not isinstance(tokens, list) or len(tokens) != len(fee_assets):
        fail("tokens must exactly match deployment feeAssets")
    configured_assets: set[str] = set()
    for index, raw_token in enumerate(tokens):
        token = mapping(raw_token, f"tokens[{index}]")
        token_address = address(token.get("address"), f"tokens[{index}].address")
        decimals = token.get("decimals")
        if not isinstance(decimals, int) or isinstance(decimals, bool) or not 0 <= decimals <= 255:
            fail(f"tokens[{index}].decimals must be in 0..=255")
        if not isinstance(token.get("symbol"), str) or not token["symbol"]:
            fail(f"tokens[{index}].symbol must be explicit")
        if not isinstance(token.get("price_id"), str) or not token["price_id"]:
            fail(f"tokens[{index}].price_id must be explicit")
        if int(abi_word(probe, token_address, SELECTOR["decimals"], block, "decimals"), 16) != decimals:
            fail(f"tokens[{index}].decimals does not match the deployed asset")
        configured_assets.add(token_address)
    if configured_assets != set(fee_assets):
        fail("tokens must exactly match deployment feeAssets")

    adapters = config.get("payment_adapters")
    if not isinstance(adapters, list) or len(adapters) != 1:
        fail("payment_adapters must contain HowlPaymentAdapter exactly once")
    adapter = mapping(adapters[0], "payment_adapters[0]")
    if address(adapter.get("address"), "payment_adapters[0].address") != contracts["howlPaymentAdapter"]:
        fail("payment_adapters[0] must be the deployed HowlPaymentAdapter")
    adapter_assets = adapter.get("fee_assets")
    if not isinstance(adapter_assets, list) or {
        address(value, "payment_adapters[0].fee_assets") for value in adapter_assets
    } != set(fee_assets):
        fail("payment adapter fee_assets must exactly match deployment feeAssets")
    positive(adapter, "maximum_payment_gas")

    if int(abi_word(probe, entry_point, SELECTOR["quote_version"], block, "QUOTE_VERSION"), 16) != 1:
        fail("NoxEntryPoint quote version is not 1")
    links = (
        (entry_point, SELECTOR["reward_pool"], reward_pool, "NoxEntryPoint.REWARD_POOL"),
        (entry_point, SELECTOR["sandbox"], contracts["noxSandboxImplementation"], "NoxEntryPoint.SANDBOX_IMPLEMENTATION"),
        (contracts["howlPaymentAdapter"], SELECTOR["dark_pool"], contracts["darkPool"], "HowlPaymentAdapter.DARK_POOL"),
        (contracts["howlPaymentAdapter"], SELECTOR["entry_point"], entry_point, "HowlPaymentAdapter.ENTRY_POINT"),
        (contracts["bundleExecutor"], SELECTOR["dark_pool"], contracts["darkPool"], "BundleExecutor.DARK_POOL"),
    )
    for target, selector, expected, label in links:
        if abi_word(probe, target, selector, block, label)[-40:] != expected[2:]:
            fail(f"{label} does not match the deployment")
    role = abi_word(probe, reward_pool, SELECTOR["entrypoint_role"], block, "ENTRYPOINT_ROLE")
    has_role = SELECTOR["has_role"] + role[2:] + address_word(entry_point)
    if int(abi_word(probe, reward_pool, has_role, block, "hasRole"), 16) != 1:
        fail("NoxEntryPoint does not hold RewardPool ENTRYPOINT_ROLE")
    for asset in fee_assets:
        encoded = address_word(asset)
        if int(abi_word(probe, reward_pool, SELECTOR["supported"] + encoded, block, "isSupportedAsset"), 16) != 1:
            fail("deployed fee asset is not supported by NoxRewardPool")
        if int(abi_word(probe, reward_pool, SELECTOR["classified"] + encoded, block, "isAssetClassified"), 16) != 1:
            fail("deployed fee asset is not classified by NoxRewardPool")


def main() -> None:
    if len(sys.argv) not in {5, 6} or sys.argv[1] not in {"auto", "relay", "exit"}:
        fail(
            "usage: preflight_config.py <auto|relay|exit> <config.toml> <deployment.json> <nox-image> [preflight-image]"
        )
    if sys.argv[1] == "auto" and "NOX_PREFLIGHT_RPC_FIXTURE" in os.environ:
        fail("NOX_PREFLIGHT_RPC_FIXTURE is test-only and cannot be used by Compose")
    config = load_config(Path(sys.argv[2]))
    configured_role = config.get("node_role")
    role = configured_role if sys.argv[1] == "auto" else sys.argv[1]
    if role not in {"relay", "exit"}:
        fail("node_role must be relay or exit")
    deployment = load_json(Path(sys.argv[3]), "deployment manifest")
    validate_release(deployment, sys.argv[4], sys.argv[5] if len(sys.argv) == 6 else None)
    validate_runtime_keys(role)
    contracts, probe, block = validate_common(config, role, deployment)
    if role == "exit":
        validate_exit(config, deployment, contracts, probe, block)


if __name__ == "__main__":
    main()
