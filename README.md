# Run NOX

This repository is the canonical operator kit for a [NOX](https://github.com/hisoka-io/nox) mixnet node on [Hisoka Protocol](https://hisoka.io). NOX provides network-layer privacy for protocol-neutral paid transactions on Arbitrum Sepolia.

## Requirements

- Docker Engine 20.10 or newer and Docker Compose v2.20 or newer
- Python 3.11 or newer for TOML preflight validation
- A public IPv4 address with TCP ports `15000` (libp2p) and `15001` (metrics, read-only) open
- An Arbitrum Sepolia RPC endpoint. Exits need one that serves `eth_simulateV1`, such as
  `https://arbitrum-sepolia-rpc.publicnode.com`
- The committed deployment manifest, [`configs/arbitrum-sepolia.deployment.json`](configs/arbitrum-sepolia.deployment.json),
  which pins the Nox and preflight image digests
- 1 vCPU and 1 GB RAM for relay nodes, or 2 vCPU and 2 GB RAM for exit nodes

The node and price server use the same Nox digest. The preflight service uses its separately pinned digest. Do not deploy a mutable tag.

## Current Deployment

The network moved to a new contract set on 2026-09-25. The April 2026 registry
`0x8626aF80db409BeD3C19871FAdf9b0Ce7Aa641Bc` is **retired**. Nodes still configured for it are not part of the
mixnet.

| Item | Value |
|---|---|
| Chain | Arbitrum Sepolia (`421614`) |
| `NoxRegistry` (proxy) | `0xF7BFf88A1412054a001Dc4b8aCBddAd6F9b26cB6` |
| `NoxRewardPool` (proxy) | `0xA487BAa4f2C3fAA01C70066EE88b6F7fD6f1361D` |
| `NoxEntryPoint` | `0xad911Ca217C6dC779fCE6A6538bDda3071c38E7E` |
| `HowlPaymentAdapter` | `0xfC874B702F8D59B60B35582855505F4f60cE766D` |
| SOKA (staking and fee asset, 18 decimals) | `0x0F69cf1c9F4FF72471701036dd789c934458e630` |
| `chain_start_block` | `312414608` |
| Nox image | `ghcr.io/hisoka-io/nox@sha256:709155c5fa11f1fb37f82ca1c5b31952730fe0ce3c00192a8a398802bfdc887e` (`0.4.0-rc.3`) |
| Preflight image | `python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9` |

The role templates in `configs/` already carry these values. The manifest is the source of truth: preflight
refuses to start a node whose config or images differ from it.

## Relay Quick Start

Copy the committed manifest to `deployment.json` and export its exact `noxImage` and `preflightImage` values:

```bash
git clone https://github.com/hisoka-io/run-nox.git
cd run-nox
cp configs/arbitrum-sepolia.deployment.json deployment.json
export NOX_IMAGE="$(python3 -c 'import json; print(json.load(open("deployment.json"))["release"]["noxImage"])')"
export NOX_PREFLIGHT_IMAGE="$(python3 -c 'import json; print(json.load(open("deployment.json"))["release"]["preflightImage"])')"
docker run --rm "$NOX_IMAGE" nox keygen > .env
chmod 600 .env
grep -c '^NOX__' .env   # must print 3
cp configs/relay.toml config.toml
set -a
. ./.env
set +a
scripts/preflight.sh relay config.toml "$NOX_IMAGE" deployment.json
docker compose up -d
curl --fail http://127.0.0.1:15001/topology
```

The generated secrets remain in `.env`. Do not print or paste that file into logs, issues, or shell history. `grep 'for registration' .env` prints only the public values. Register the node after it starts by following [REGISTRATION.md](REGISTRATION.md). `docker compose up` runs the same preflight itself, so neither the node nor the price server starts if the manifest is absent, an image differs from the release record, or on-chain verification fails.

## Exit Nodes

Exit nodes request and verify signed execution quotes, receive the corresponding paid transaction through the selected mixnet route, enforce configured-token and profitability policy, and submit the approved `NoxEntryPoint` call. The durable transaction outbox reconciles submissions and replacements after restart.

An exit additionally requires:

- A funded secp256k1 wallet in `NOX__ETH_WALLET_PRIVATE_KEY`
- A price source for every configured fee asset. The bundled price server serves `ethereum`, `usd-coin` and
  `bitcoin`; the template values SOKA through `usd-coin`
- An RPC endpoint that serves `eth_simulateV1`; without it every paid transaction is rejected. The template uses
  `https://arbitrum-sepolia-rpc.publicnode.com`, as the Hisoka exits do. `https://sepolia-rollup.arbitrum.io/rpc`
  answered `-32603 method handler crashed` during the September 2026 cutover but simulated a recorded paid
  transaction correctly on 2026-10-02. If you use it, watch the exit logs for simulation errors

The exit template already carries the committed `NoxEntryPoint`, `NoxRewardPool`, `HowlPaymentAdapter` and SOKA
fee-asset values. The price server belongs to the Compose `exit` profile, so an exit enables that profile once in
`.env`; every later `docker compose` command (`up`, `ps`, `logs`, `down`) then includes it. An exit set up before
the profile existed must add the same line, or `docker compose up` starts it without a price server:

```bash
cp configs/exit.toml config.toml
echo 'COMPOSE_PROFILES=exit' >> .env
set -a
. ./.env
set +a
scripts/preflight.sh exit config.toml "$NOX_IMAGE" deployment.json
docker compose up -d
curl --fail http://127.0.0.1:15004/health
curl --fail http://127.0.0.1:15001/topology
```

Never expose the price server publicly: Compose binds it to `127.0.0.1`. Relays do not run it. The node starts
after the price server container without waiting for a fresh price, so a price-API outage does not keep an exit
off the mixnet; the exit refuses paid requests until it can read fresh prices. The price response consumed by an exit is `{price_e8, observed_at_unix, asset_id, source}`. The exit rejects stale, future-dated, mismatched, unsupported, or malformed observations and performs profitability decisions with integer E8 arithmetic.

### Claiming Exit Credit

Exit credit accrues in `NoxRewardPool` to the exit wallet, and only that wallet can claim it with
`claimExitCredit(asset, recipient, amount)`. **Stop the node before you claim and start it again afterwards; never
claim while it runs.** The node reads its wallet nonce when it starts, so any transaction sent from the exit wallet
by another tool while the node runs leaves the node with a stale nonce. Its next paid submission then stays stuck
in the outbox and blocks later ones, and a restart does not clear it. The same applies to any other transaction
from the exit wallet, such as moving funds.

```bash
curl --fail --silent http://127.0.0.1:15001/metrics | grep '^nox_eth_tx_pending '   # wait until it reads 0
docker compose stop nox
set -a; . ./.env; set +a
POOL=0xA487BAa4f2C3fAA01C70066EE88b6F7fD6f1361D
SOKA=0x0F69cf1c9F4FF72471701036dd789c934458e630
RPC=https://arbitrum-sepolia-rpc.publicnode.com
EXIT=$(sed -n 's/^# Address (for registration): //p' .env)
cast call "$POOL" 'claimableExit(address,address)(uint256)' "$EXIT" "$SOKA" --rpc-url "$RPC"
cast send "$POOL" 'claimExitCredit(address,address,uint256)' "$SOKA" RECIPIENT AMOUNT_WEI \
  --private-key "$NOX__ETH_WALLET_PRIVATE_KEY" --rpc-url "$RPC"
docker compose up -d
```

`cast send` waits for the receipt. Start the node only after the claim succeeded; on start it reads the new nonce.

## KPS Entry (optional)

`nox-kps` lets wallets and browsers reach your node directly over [KPS](https://github.com/ethereum/kps):
WebRTC for browsers and QUIC for native clients, both on one UDP port, without a domain name or TLS
certificate to manage. Clients pin the sidecar's certificate hash, which is part of the address they dial. The
sidecar runs next to the node under the Compose profile `kps`. It forwards each request on an allowlisted route
(packet submit, response claim, topology, health) to the node's loopback ingress and topology API, and enforces
its own size, connection and time limits. Packet policy (PoW, validity, SURB IDs) stays in the node, so both
entry paths share one policy.

You need:

- UDP `15005` open inbound in your cloud firewall and host firewall (see Host Firewall below)
- `NOX_KPS_IMAGE`: the immutable `ghcr.io/hisoka-io/nox-kps@sha256:` digest from the `nox-kps` release record
- The node ingress and topology API enabled on loopback, with a client IP header shared by the node and the sidecar

1. Enable the loopback ports and the header in `config.toml`, then restart the node:

   ```toml
   ingress_port = 15002
   topology_api_port = 15003

   [ingress]
   client_ip_header = "x-real-ip"
   ```

   The node binds these ports on all interfaces. Keep `15002` and `15003` closed to the internet with the host
   firewall; the sidecar reaches them on `127.0.0.1`. The node reads the client IP header only on loopback
   connections, so each KPS client gets its own rate-limit bucket.

2. Create the sidecar config and set your public IP (cloud hosts are NATed, so it cannot be detected):

   ```bash
   cp configs/nox-kps.toml nox-kps.toml
   sed -i 's/"203.0.113.10"/"<your public IP>"/' nox-kps.toml
   ```

3. Enable the profile and start it (`COMPOSE_PROFILES=exit,kps` on an exit):

   ```bash
   export NOX_KPS_IMAGE='ghcr.io/hisoka-io/nox-kps@sha256:<digest from the release record>'
   echo 'COMPOSE_PROFILES=kps' >> .env
   docker compose up -d
   docker compose ps nox-kps            # healthy
   docker compose exec nox-kps nox-kps address   # prints <public ip>:15005:<certhash>
   ```

   `nox-kps-preflight` runs first and refuses to start the sidecar if the image is not a digest, the public IP is
   missing or private, an upstream is not loopback, the ingress or topology port is disabled, the client IP
   headers differ, the metrics listener is public, or another process holds UDP `15005`. `nox-kps-init` gives the
   two `nox-kps` volumes to UID `10002`.

4. Note your KPS address, `<public ip>:15005:<certhash>`, and dial it from another network with a KPS client
   (`@kpstreams/quic-client` or `@kpstreams/webrtc-client`), requesting `GET /health` and `GET /topology`.

5. Back up the identity key. The certhash in your address is derived from it, and the key stays in the
   `nox-kps-identity` volume across restarts and upgrades:

   ```bash
   (umask 077; docker compose cp nox-kps:/var/lib/nox-kps/kps.key ./kps.key.backup)
   ```

   Store the copy with the node's other secrets. Never delete the `nox-kps-identity` volume: a new key changes
   your address.

6. Publish the address in the registry. `updateMetadataUrl` is self-service: it must be sent by your registered
   node address, which is the key in `NOX__ETH_WALLET_PRIVATE_KEY`. Import that key into an encrypted Foundry
   keystore once (the command prompts for it, so it never appears in your shell history), then send:

   ```bash
   cast wallet import nox-node --interactive
   cast send 0xF7BFf88A1412054a001Dc4b8aCBddAd6F9b26cB6 "updateMetadataUrl(string)" \
     "kps:<public ip>:15005:<certhash>/metadata.json" \
     --account nox-node --rpc-url https://sepolia-rollup.arbitrum.io/rpc
   cast call 0xF7BFf88A1412054a001Dc4b8aCBddAd6F9b26cB6 "relayers(address)(bytes32,string,string,string,uint256,uint256,bool,uint8,bool)" \
     <node address> --rpc-url https://sepolia-rollup.arbitrum.io/rpc   # 4th value is your metadataUrl
   ```

   The transaction costs about 130,000 gas. Publish only after step 4 succeeds from another network, and
   keep the public IP stable: the IP and certhash together are the address clients pin.

To stop serving KPS, clear the published address first, then stop the sidecar. The identity volume stays, so
re-enabling later keeps the same address:

```bash
cast send 0xF7BFf88A1412054a001Dc4b8aCBddAd6F9b26cB6 "updateMetadataUrl(string)" "" \
  --account nox-node --rpc-url https://sepolia-rollup.arbitrum.io/rpc
docker compose stop nox-kps
docker compose rm -f nox-kps nox-kps-init nox-kps-preflight
```

Then remove `kps` from `COMPOSE_PROFILES` in `.env`. Restoring `ingress_port = 0` and `topology_api_port = 0`
is optional and needs a node restart.

### Host Firewall

Open UDP `15005` and keep the loopback ports closed to the internet. With `ufw` (default deny incoming):

```bash
# keep your existing SSH rule
sudo ufw allow 15005/udp
sudo ufw allow 15000/tcp
sudo ufw allow 15001/tcp
sudo ufw status numbered   # 15002, 15003, 15004 and 15006 must not appear
```

With plain `iptables`, put the rules in a script that a systemd oneshot unit runs at boot, so they persist:

```bash
iptables -N NOX-FW 2>/dev/null || iptables -F NOX-FW
iptables -A NOX-FW -i lo -j RETURN
iptables -A NOX-FW -p udp --dport 15005 -j RETURN
iptables -A NOX-FW -p tcp -m multiport --dports 15002,15003,15004,15006 -j DROP
iptables -C INPUT -j NOX-FW 2>/dev/null || iptables -I INPUT 1 -j NOX-FW
```

Repeat with `ip6tables` on hosts with IPv6. Check from another network that UDP `15005` answers a KPS dial and
that TCP `15002`, `15003` and `15006` refuse connections.

## Target Network Configuration

The checked-in templates target Arbitrum Sepolia:

| Setting | Value |
|---|---|
| Chain ID | `421614` |
| Benchmark mode | `false` |
| Native price asset | `ethereum`, 18 decimals |

`configs/arbitrum-sepolia.deployment.json` is generated from the contracts deploy record by
`scripts/make-deployment-manifest.py` and carries the
registry and paid-execution addresses, registry start block, runtime-code hashes, proxy implementation slots and
hashes, fee-asset list, and image digests. Copy it to the ignored `deployment.json` path before Compose startup.
Preflight compares the role config and both runtime image digests to that record, verifies the configured
RPC chain, checks every recorded runtime `codeHash` through `eth_getProof`, verifies each proxy implementation,
checks EntryPoint, sandbox, adapter, BundleExecutor, RewardPool role and asset wiring, and compares token
`decimals()` on chain. Do not infer or substitute an address or price mapping.

### After a Contract Upgrade

Preflight compares each upgradeable contract's live implementation with the manifest. After a governance upgrade
of `NoxRegistry` or `NoxRewardPool`, `docker compose up` fails on every node until the manifest is updated.
Running containers keep running. A `DarkPool` upgrade only prints a warning on relays, which never call it, and
still blocks exits. Maintainers finish every upgrade by regenerating the manifest from the new deploy record:

```bash
python3 scripts/make-deployment-manifest.py PATH/TO/deploy-record/deployment.json \
  --nox-image "$NOX_IMAGE" --preflight-image "$NOX_PREFLIGHT_IMAGE" \
  --check-rpc https://arbitrum-sepolia-rpc.publicnode.com \
  --out configs/arbitrum-sepolia.deployment.json
bash scripts/test-preflight.sh
```

`--check-rpc` runs the same on-chain checks as preflight before the file is written. After the change merges,
operators run `git pull` and copy the manifest to `deployment.json` again.

The retired April 2026 registry exposes an older profile ABI and is not compatible with the current complete
topology verification. Registry, indexer, SDK, node image, and operator manifest roll out as one release.

The indexer must use the same registry address and exact nonzero deployment start block from the manifest. Its
`/seed/topology` endpoint returns 503 until replayed membership proves the registry count and fingerprint at a
single processed block. Do not substitute persisted database rows for this proof.

Relay nodes do not use the paid-execution addresses (the template sets them to the committed deployment anyway)
and do not need an exit wallet or oracle. Exit nodes must have the committed paid-execution addresses and pass
preflight.

`bootstrap_topology_urls` is empty in both templates. The node replays the registry from `chain_start_block`,
which takes only a few `eth_getLogs` calls.

## Configuration

Copy one checked-in role template to `config.toml`. Environment variables with the `NOX__` prefix override TOML fields. Keep secrets in `.env`; keep public network and policy settings in `config.toml`.

The preflight gate parses TOML by section and checks the role, release-pinned deployment manifest, Nox and preflight image equality, chain, registry, scan start, benchmark setting,
oracle freshness policy, gas buffers, quote capacity and loss limits, payment adapters,
configured fee assets, live contract relationships, data-fee mode, and the presence of role-specific key variables. It validates presence
without displaying values.

Exit token entries are an allowlist. Each configured address must appear in the deployment record's `feeAssets`,
match on-chain decimals, and have explicit symbol and oracle identifiers in operator config. An unconfigured token
is rejected rather than priced with a fallback.

The checked-in 20,000,000 transaction-gas ceiling (`quote_maximum_transaction_gas`) leaves headroom over a
Howl-paid execution, which needs a gas limit of about 10.8M on Arbitrum because payment and action gas are
reserved up front. Operators must remeasure every enabled payment adapter and action class, then price the full signed reservation. `quote_max_pending_sponsored_gas` remains the aggregate exposure limit,
so it can reject a quote even when that quote is below the per-transaction ceiling.

## Ports

| Port | Purpose | Exposure |
|---|---|---|
| `15000/tcp` | libp2p | Public |
| `15001/tcp` | Metrics and topology (read-only) | Public: the indexer probes it |
| `15002/tcp` | Client ingress | Off in the templates. Entry nodes only, behind an https proxy or `nox-kps` (loopback) |
| `15003/tcp` | Topology API | Off in the templates. Loopback for `nox-kps` |
| `15004/tcp` | Price server | Exits only, bound to `127.0.0.1` |
| `15005/udp` | KPS entry (`nox-kps`, WebRTC + QUIC) | Public, profile `kps` only |
| `15006/tcp` | `nox-kps` metrics and health | Bound to `127.0.0.1`, profile `kps` only |

`metrics_port` must equal `p2p_port + 1`. The Hisoka indexer derives the metrics URL from your registered
multiaddr (TCP port + 1) and polls `/topology` and `/metrics/json` there. If it cannot reach the port, the seed
lists your node as offline and clients do not route through it. The port serves read-only metrics, topology and
events; the admin write endpoint exists only in `benchmark_mode`, which preflight rejects.

## Health and Monitoring

```bash
docker compose ps
docker compose logs --tail 100 nox
curl --fail http://127.0.0.1:15001/topology
# Exits only:
docker compose logs --tail 100 price-server
curl --fail http://127.0.0.1:15004/health
```

For exits, alert on wallet balance, stale-price and unsupported-token rejections, rejected profitability decisions, submission ambiguity, replacement exhaustion, and unreconciled outbox records. Preserve the outbox volume across restarts and upgrades.

## Upgrade, Canary, and Rollback

Before upgrading:

1. Record the current `NOX_IMAGE`, `NOX_PREFLIGHT_IMAGE`, manifest release record, and `docker compose ps` output.
2. Verify both target digests against the committed manifest (`configs/arbitrum-sepolia.deployment.json`), copy it to `deployment.json`, and export the matching values.
3. Stop one canary node cleanly and snapshot its `nox-data`, `nox-identity`, and `nox-logs` volumes using the host or cloud volume-snapshot facility. Record the three snapshot identifiers before continuing.
4. Run the one-time ownership migration below while the node remains stopped.
5. Run preflight against the unchanged role config, target Nox digest, and target manifest.
6. Start the canary with `docker compose up -d` and verify topology, price health, peer recovery, and exit outbox reconciliation before continuing.

Images after the cutover run as `nox` with fixed UID:GID `10001:10001`; Compose also drops every Linux
capability and sets `no-new-privileges`. Existing AWS volumes were written by root. On each host, resolve the
three exact Compose volume names with `docker volume ls`, inspect each target, then migrate only those explicit
volumes after the snapshots exist:

```bash
export NOX_DATA_VOLUME=run-nox_nox-data
export NOX_IDENTITY_VOLUME=run-nox_nox-identity
export NOX_LOGS_VOLUME=run-nox_nox-logs

for volume in "$NOX_DATA_VOLUME" "$NOX_IDENTITY_VOLUME" "$NOX_LOGS_VOLUME"; do
  test -n "$volume"
  docker volume inspect "$volume" >/dev/null
done
for volume in "$NOX_DATA_VOLUME" "$NOX_IDENTITY_VOLUME" "$NOX_LOGS_VOLUME"; do
  docker run --rm --user 0:0 --entrypoint /bin/chown \
    --volume "$volume:/mnt/nox-volume" \
    "$NOX_IMAGE" -R 10001:10001 /mnt/nox-volume
done
```

Do not run this loop against an unresolved, empty, or newly created volume name. The live evidence before
migration is root-owned volume roots and contents, with the Bloom file mode `0600`. Only the data, identity, and
log volumes are migrated. `/etc/nox` remains `root:root` mode `0755` in the image, and `config.toml` remains a
read-only bind mount. `/var/lib/nox` is `10001:10001` mode `0750`. The live config is mode `0644` and owned by
UID 1000, so the runtime only needs read access. Do not chown the config or `/etc/nox`. After migration, verify
all three writable mounts report numeric owner `10001:10001` from the target image before starting the canary.

Promote relays first, then one exit, then the remaining exits. Keep the previous digest and snapshots until the observation window completes.

To roll back, stop the affected node, restore the recorded `NOX_IMAGE`, `NOX_PREFLIGHT_IMAGE`, and matching `deployment.json`, then run `docker compose up -d`.
The previous root-running image can use the `10001:10001` files, so ownership does not need to be reversed. Do
not downgrade across an outbox schema change unless the release record explicitly declares backward
compatibility. Restore the pre-upgrade volume snapshot when compatibility is not declared.

Never delete the Docker volumes to resolve a startup or role-change failure. They contain the durable identity and transaction state required for safe recovery.

## Security

- Restrict `.env` to the node operator and back it up through the approved secret-management path.
- Keep `allow_private_ips = false` on exits.
- Use bounded token approvals and verify they return to zero in client workflows.
- Do not enable `benchmark_mode` in an operator configuration.
- Do not expose RPC credentials, private keys, raw signed payloads, or quote signatures in logs.
- Use only committed contract addresses and immutable container digests.

## Architecture

```text
Client -> selected entry -> mix hops -> selected exit -> NoxEntryPoint -> Ethereum
   ^                              |
   +--------- SURB response ------+
```

The client obtains a quote through one selected exit route, verifies the EIP-712 quote, and sends the paid transaction through that same route. A direct-IP quote request is not part of the operator flow because it would reveal the client-to-exit relationship.

## Links

- [Hisoka Protocol](https://hisoka.io)
- [Node registration](REGISTRATION.md)
- [Issue tracker](https://github.com/hisoka-io/run-nox/issues)
