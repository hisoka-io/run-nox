#!/usr/bin/env python3
"""Build the run-nox deployment manifest from a contracts deploy record.

The deploy record is the deployment.json written by the darkpool contracts
deploy script (archived in hisoka-io/nox-deployments). The output is the file
committed as configs/arbitrum-sepolia.deployment.json, which preflight uses as
the source of truth.

    scripts/make-deployment-manifest.py DEPLOY_RECORD \\
        --nox-image ghcr.io/hisoka-io/nox@sha256:... \\
        --preflight-image python:3.12-slim@sha256:... \\
        [--check-rpc RPC_URL] [--out configs/arbitrum-sepolia.deployment.json]

With --check-rpc, the generated manifest is verified against the chain with the
same checks preflight runs (chain id, runtime code hashes, proxy
implementations) before anything is written.
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

import preflight_config as preflight  # noqa: E402


def kebab(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"-\1", name).lower()


def build(record: dict[str, object], nox_image: str, preflight_image: str) -> dict[str, object]:
    meta = preflight.mapping(record.get("meta"), "deploy record meta")
    network = meta.get("network")
    if not isinstance(network, str) or not network:
        preflight.fail("deploy record meta.network must be a non-empty string")
    contracts = preflight.mapping(record.get("contracts"), "deploy record contracts")
    hashes = preflight.mapping(record.get("contractCodeHashes"), "deploy record contractCodeHashes")
    slots = preflight.mapping(record.get("proxySlots"), "deploy record proxySlots")
    impl_hashes = preflight.mapping(
        record.get("proxyImplementationCodeHashes"), "deploy record proxyImplementationCodeHashes"
    )
    fee_assets = record.get("feeAssets")
    if not isinstance(fee_assets, list) or not fee_assets:
        preflight.fail("deploy record feeAssets must list at least one asset")

    manifest: dict[str, object] = {
        "meta": {
            "network": kebab(network),
            "chainId": preflight.positive(meta, "chainId"),
            "startBlock": preflight.positive(meta, "startBlock"),
        },
        "release": {"noxImage": nox_image, "preflightImage": preflight_image},
        "contracts": {},
        "contractCodeHashes": {},
        "proxySlots": {},
        "proxyImplementationCodeHashes": {},
        "feeAssets": list(fee_assets),
    }
    for index, value in enumerate(fee_assets):
        preflight.address(value, f"feeAssets[{index}]")
    for name in preflight.REQUIRED_CONTRACTS:
        preflight.address(contracts.get(name), f"contracts.{name}")
        preflight.code_hash(hashes.get(name), f"contractCodeHashes.{name}")
        manifest["contracts"][name] = contracts[name]
        manifest["contractCodeHashes"][name] = hashes[name]
    for name in preflight.PROXIES:
        slot = preflight.mapping(slots.get(name), f"proxySlots.{name}")
        preflight.address(slot.get("impl"), f"proxySlots.{name}.impl")
        preflight.address(slot.get("admin"), f"proxySlots.{name}.admin", allow_zero=True)
        preflight.code_hash(impl_hashes.get(name), f"proxyImplementationCodeHashes.{name}")
        manifest["proxySlots"][name] = {"impl": slot["impl"], "admin": slot["admin"]}
        manifest["proxyImplementationCodeHashes"][name] = impl_hashes[name]
    preflight.validate_release(manifest, nox_image, preflight_image)
    return manifest


def check_chain(manifest: dict[str, object], rpc_url: str) -> None:
    probe = preflight.Rpc(rpc_url)
    meta = preflight.mapping(manifest["meta"], "meta")
    if int(preflight.rpc_hex(probe, "eth_chainId", []), 16) != meta["chainId"]:
        preflight.fail("RPC chain does not match the deploy record")
    block = preflight.rpc_hex(probe, "eth_blockNumber", [])
    # "exit" applies every check strictly, including DarkPool.
    preflight.verify_deployment(manifest, probe, block, "exit")
    print(f"chain check OK at block {int(block, 16)}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("deploy_record", type=Path)
    parser.add_argument("--nox-image", required=True)
    parser.add_argument("--preflight-image", required=True)
    parser.add_argument("--check-rpc", metavar="RPC_URL")
    parser.add_argument("--out", type=Path, help="write here instead of stdout")
    args = parser.parse_args()

    record = preflight.load_json(args.deploy_record, "deploy record")
    manifest = build(record, args.nox_image, args.preflight_image)
    if args.check_rpc:
        check_chain(manifest, args.check_rpc)
    text = json.dumps(manifest, indent=2) + "\n"
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
