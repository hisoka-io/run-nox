# Node Registration

NOX nodes must be registered in the current Arbitrum Sepolia `NoxRegistry` before they join the mixnet:

| Item | Value |
|---|---|
| `NoxRegistry` (proxy) | `0xF7BFf88A1412054a001Dc4b8aCBddAd6F9b26cB6` |
| Deployed | 2026-09-25, `chain_start_block` `312414608` |
| Manifest | [`configs/arbitrum-sepolia.deployment.json`](configs/arbitrum-sepolia.deployment.json) |

The April 2026 registry `0x8626aF80db409BeD3C19871FAdf9b0Ce7Aa641Bc` is **retired**. A registration there does not
carry over: nodes that were registered on it must request registration again with the same public keys.

## How Registration Works

Open a registration issue (step 3 below). Maintainers review it and register approved nodes with `nox-ctl`
through the governance Safe, a 3-of-5 multisig that acts through the Timelock. Nodes registered this way hold no
stake.

Zero-stake self-registration is no longer possible. The permissionless `register()` call requires a stake of
at least `minStakeAmount`, currently 1 SOKA (`1000000000000000000` base units of
`0x0F69cf1c9F4FF72471701036dd789c934458e630`). If you do not hold SOKA, use the issue process.

## 1. Generate Keys

Copy the committed manifest to `deployment.json` and export its exact `noxImage` and `preflightImage` values before
generating keys:

```bash
git clone https://github.com/hisoka-io/run-nox.git
cd run-nox
cp configs/arbitrum-sepolia.deployment.json deployment.json
export NOX_IMAGE="$(python3 -c 'import json; print(json.load(open("deployment.json"))["release"]["noxImage"])')"
export NOX_PREFLIGHT_IMAGE="$(python3 -c 'import json; print(json.load(open("deployment.json"))["release"]["preflightImage"])')"
docker run --rm "$NOX_IMAGE" keygen > .env
chmod 600 .env
grep 'for registration' .env   # public values only
```

Store `.env` through the approved secret-management path. Do not attach it to a registration request or paste it
into logs. If your node was registered on the retired registry, keep your existing `.env`: the new registration
must use the same Sphinx key, PeerId and address.

## 2. Configure and Validate

Start with the relay role unless you are an approved exit operator:

```bash
cp configs/relay.toml config.toml
set -a
. ./.env
set +a
scripts/preflight.sh relay config.toml "$NOX_IMAGE" deployment.json
docker compose up -d
curl --fail http://127.0.0.1:15001/topology
```

Open both of these TCP ports to the internet:

- `15000` (`p2p_port`): libp2p.
- `15001` (`metrics_port`, which must be `p2p_port + 1`): read-only metrics and topology. The Hisoka indexer
  derives this port from your registered multiaddr and probes it. A node it cannot reach is listed as offline,
  and clients do not route through it.

An exit operator uses `configs/exit.toml`, which already carries the committed `NoxEntryPoint`, `NoxRewardPool`,
`HowlPaymentAdapter` and SOKA fee-asset values. An exit also needs:

- a funded wallet (see Exit Funding below);
- an RPC that serves `eth_simulateV1`, such as `https://arbitrum-sepolia-rpc.publicnode.com` (the template
  default). `https://sepolia-rollup.arbitrum.io/rpc` does not serve it, and every paid transaction would be
  rejected;
- a passing `scripts/preflight.sh exit config.toml "$NOX_IMAGE" deployment.json`.

Compose repeats this validation before it starts either service.

## 3. Submit the Public Registration Values

Open a [node registration request](https://github.com/hisoka-io/run-nox/issues/new?template=node-registration-request.yml) with only these public values:

| Field | Source |
|---|---|
| Sphinx public key | `# Public key (for registration):` line from `keygen` |
| ETH address | `# Address (for registration):` line from `keygen` |
| Peer ID | `# PeerId (for registration):` line from `keygen` (starts with `12D3KooW`) |
| P2P multiaddr | `/ip4/YOUR_PUBLIC_IP/tcp/15000/p2p/YOUR_PEER_ID` |
| Node role | `relay` or `exit` |

The multiaddr **must** end in `/p2p/<PeerId>`. Other nodes address packets to that PeerId, so maintainers only
register multiaddrs that include it.

Never include a routing key, P2P private key, wallet private key, RPC credential, or complete `.env` file.

## 4. Verify Registration

After a maintainer confirms the registration, check it on chain and in the seed:

```bash
cast call 0xF7BFf88A1412054a001Dc4b8aCBddAd6F9b26cB6 'isActiveRelayer(address)(bool)' YOUR_ETH_ADDRESS \
  --rpc-url https://arbitrum-sepolia-rpc.publicnode.com
curl --fail --silent http://127.0.0.1:15001/topology | python3 -m json.tool
docker compose logs --tail 100 nox
```

Confirm from outside the host that TCP ports `15000` and `15001` are reachable and that the registered multiaddr
matches the public address and PeerId.

## Exit Funding

Exit nodes require Arbitrum Sepolia ETH for gas. Fund only the public wallet address emitted by the same key
generation run. Monitor the configured minimum gas balance and replenish before it is reached.

## Recovery

If validation fails, rerun preflight and inspect the named field without printing `.env`. If peers remain absent
after registration, verify the public multiaddr and PeerId, the firewall (ports `15000` and `15001`), RPC access,
registry address, and `chain_start_block`.

Do not delete Docker volumes during troubleshooting. The identity and data volumes are required for stable peer
identity and safe transaction recovery.
