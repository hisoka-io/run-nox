# Run NOX

This repository is the canonical operator kit for a [NOX](https://github.com/hisoka-io/nox) mixnet node on [Hisoka Protocol](https://hisoka.io). NOX provides network-layer privacy for protocol-neutral paid transactions on Arbitrum Sepolia.

## Requirements

- Docker Engine 20.10 or newer and Docker Compose v2
- Python 3.11 or newer for TOML preflight validation
- A public IPv4 address with TCP port `15000` open
- An Arbitrum Sepolia RPC endpoint
- A signed deployment record containing immutable Nox and preflight image digests
- 1 vCPU and 1 GB RAM for relay nodes, or 2 vCPU and 2 GB RAM for exit nodes

The node and price server use the same Nox digest. The preflight service uses its separately pinned digest. Do not deploy a mutable tag.

## Relay Quick Start

Copy the signed release record to `deployment.json` and export its exact `noxImage` and `preflightImage` values:

```bash
git clone https://github.com/hisoka-io/run-nox.git
cd run-nox
cp /secure/path/to/signed-deployment.json deployment.json
export NOX_IMAGE='ghcr.io/hisoka-io/nox@sha256:...'
export NOX_PREFLIGHT_IMAGE='python@sha256:...'
docker run --rm "$NOX_IMAGE" keygen > .env
chmod 600 .env
cp configs/relay.toml config.toml
set -a
. ./.env
set +a
scripts/preflight.sh relay config.toml "$NOX_IMAGE" deployment.json
docker compose up -d
curl --fail http://127.0.0.1:15001/topology
```

The generated secrets remain in `.env`. Do not print or paste that file into logs, issues, or shell history. Register the node after it starts by following [REGISTRATION.md](REGISTRATION.md). `docker compose up` runs the same preflight itself, so neither the node nor the price server starts if the manifest is absent, an image differs from the release record, or on-chain verification fails.

## Exit Nodes

Exit nodes request and verify signed execution quotes, receive the corresponding paid transaction through the selected mixnet route, enforce configured-token and profitability policy, and submit the approved `NoxEntryPoint` call. The durable transaction outbox reconciles submissions and replacements after restart.

An exit additionally requires:

- A funded secp256k1 wallet in `NOX__ETH_WALLET_PRIVATE_KEY`
- The committed paid-execution `NoxEntryPoint` address in `nox_entry_point_address`
- The committed paid-execution deployment manifest
- A price source for every configured fee asset
- An RPC endpoint that supports the configured simulation and fee-estimation policy

The checked-in exit template deliberately uses zero addresses for `nox_entry_point_address` and the payment
adapter. Exit preflight remains closed until the paid-execution deployment record supplies both addresses.

After inserting the committed EntryPoint, RewardPool, adapter, and fee-asset values into a local `config.toml`:

```bash
cp configs/exit.toml config.toml
set -a
. ./.env
set +a
scripts/preflight.sh exit config.toml "$NOX_IMAGE" deployment.json
docker compose up -d
curl --fail http://127.0.0.1:15004/health
curl --fail http://127.0.0.1:15001/topology
```

Never expose the price server or admin port publicly. The price response consumed by an exit is `{price_e8, observed_at_unix, asset_id, source}`. The exit rejects stale, future-dated, mismatched, unsupported, or malformed observations and performs profitability decisions with integer E8 arithmetic.

## Target Network Configuration

The checked-in templates target Arbitrum Sepolia:

| Setting | Value |
|---|---|
| Chain ID | `421614` |
| Benchmark mode | `false` |
| Native price asset | `ethereum`, 18 decimals |

The checked-in deployment template leaves the registry and paid-execution addresses, registry start block,
runtime-code hashes, proxy implementation slots and hashes, fee-asset list, and image digests empty until the ABI-compatible contracts are deployed. Copy the signed release record to the ignored `deployment.json` path before Compose startup. Both role templates
therefore fail preflight by design. Preflight compares the role config and both runtime image digests to that record, verifies the configured
RPC chain, checks every recorded runtime `codeHash` through `eth_getProof`, verifies each proxy implementation,
checks EntryPoint, sandbox, adapter, BundleExecutor, RewardPool role and asset wiring, and compares token
`decimals()` on chain. Do not infer or substitute an address or price mapping.

The historical Arbitrum Sepolia registry exposes an older profile ABI and is not compatible with the current
complete topology verification. Registry, indexer, SDK, node image, and operator manifest must roll out as one
audited release.

The indexer must use the same registry address and exact nonzero deployment start block from the manifest. Its
`/seed/topology` endpoint returns 503 until replayed membership proves the registry count and fingerprint at a
single processed block. Do not substitute persisted database rows for this proof.

Relay nodes use zero paid-execution contract addresses and do not need an exit wallet or oracle. Exit nodes must have explicit, nonzero paid-execution addresses and pass preflight.

## Configuration

Copy one checked-in role template to `config.toml`. Environment variables with the `NOX__` prefix override TOML fields. Keep secrets in `.env`; keep public network and policy settings in `config.toml`.

The preflight gate parses TOML by section and checks the role, release-pinned deployment manifest, Nox and preflight image equality, chain, registry, scan start, benchmark setting,
oracle freshness policy, gas buffers, quote capacity and loss limits, payment adapters,
configured fee assets, live contract relationships, data-fee mode, and the presence of role-specific key variables. It validates presence
without displaying values.

Exit token entries are an allowlist. Each configured address must appear in the deployment record's `feeAssets`,
match on-chain decimals, and have explicit symbol and oracle identifiers in operator config. An unconfigured token
is rejected rather than priced with a fallback.

The checked-in 12,000,000 transaction-gas ceiling covers the measured Howl EntryPoint plan of 10,273,982 gas
after the configured 20% estimate buffer. Operators must remeasure every enabled payment adapter and action class,
then price the full signed reservation. `quote_max_pending_sponsored_gas` remains the aggregate exposure limit,
so it can reject a quote even when that quote is below the per-transaction ceiling.

## Ports

| Port | Purpose | Exposure |
|---|---|---|
| `15000/tcp` | libp2p | Public |
| `15001/tcp` | Admin and metrics | Local only |
| `15002/tcp` | Client ingress | Entry nodes only |
| `15003/tcp` | Topology API | Seed nodes only |
| `15004/tcp` | Price server | Local only |

## Health and Monitoring

```bash
docker compose ps
docker compose logs --tail 100 nox
docker compose logs --tail 100 price-server
curl --fail http://127.0.0.1:15001/topology
curl --fail http://127.0.0.1:15004/health
```

For exits, alert on wallet balance, stale-price and unsupported-token rejections, rejected profitability decisions, submission ambiguity, replacement exhaustion, and unreconciled outbox records. Preserve the outbox volume across restarts and upgrades.

## Upgrade, Canary, and Rollback

Before upgrading:

1. Record the current `NOX_IMAGE`, `NOX_PREFLIGHT_IMAGE`, manifest release record, and `docker compose ps` output.
2. Verify both target digests against the signed release record, copy that exact record to `deployment.json`, and export the matching values.
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
