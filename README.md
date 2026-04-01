# Run NOX

Run a [NOX](https://github.com/hisoka-io/nox) mixnet node on [Hisoka Protocol](https://hisoka.io). NOX is a 3-layer Sphinx mixnet that provides network-layer privacy for on-chain transactions on Ethereum.

`v0.2.0-testnet` | Arbitrum Sepolia | `ghcr.io/hisoka-io/nox:0.2.0-testnet`

## Prerequisites

- Docker Engine 20.10+ and Docker Compose v2
- 1 vCPU / 1 GB RAM minimum (2 vCPU / 2 GB for exit nodes)
- Public IPv4 with TCP port `15000` open
- Ethereum RPC endpoint (public Arbitrum Sepolia RPC works: `https://sepolia-rollup.arbitrum.io/rpc`)

> **Apple Silicon:** The image is `linux/amd64` only. Docker Desktop handles emulation via Rosetta. Add `--platform linux/amd64` if you see platform warnings.

## Quick Start

```bash
# Pull the image
docker pull ghcr.io/hisoka-io/nox:0.2.0-testnet

# Generate keys
docker run --rm ghcr.io/hisoka-io/nox:0.2.0-testnet keygen > .env

# Clone and configure
git clone https://github.com/hisoka-io/run-nox.git && cd run-nox
cp configs/relay.toml config.toml

# Start
docker compose up -d

# Verify
curl http://localhost:15001/topology
```

Your node won't find peers until it's registered on-chain. See [REGISTRATION.md](REGISTRATION.md).

First startup takes about 60 seconds while the price server healthcheck passes.

## Exit Nodes

Exit nodes are the final mixnet hop. They decrypt the innermost Sphinx layer, extract the transaction payload, and submit it on-chain. They need an ETH wallet with gas and a price oracle.

### Quick Start

```bash
docker pull ghcr.io/hisoka-io/nox:0.2.0-testnet
docker run --rm ghcr.io/hisoka-io/nox:0.2.0-testnet keygen > .env

git clone https://github.com/hisoka-io/run-nox.git && cd run-nox
cp configs/exit.toml config.toml

# Fund your wallet: get address from .env, send 0.1 ETH on Arb Sepolia
# Faucet: https://faucet.quicknode.com/arbitrum/sepolia

docker compose up -d
```

### How It Works

1. Receive Sphinx packets from mix nodes via P2P
2. Decrypt the final layer using the X25519 routing key
3. Extract the `RelayerPayload` (multicall bundle: ZK proof + DeFi action)
4. Simulate via `eth_simulateV1`
5. Check profitability (gas cost vs fee revenue)
6. Submit on-chain if profitable via `RelayerMulticall.multicall()`
7. Return result to client via SURB through the mixnet

### Requirements

| | Details |
|-|---------|
| **ETH wallet** | secp256k1 key in `.env` (`NOX__ETH_WALLET_PRIVATE_KEY`) |
| **Gas** | ~0.1 ETH on Arb Sepolia lasts weeks |
| **Price oracle** | Runs as docker-compose sidecar on port 15004 |
| **RPC** | Must support `eth_simulateV1` or `debug_traceCall` |
| **Contracts** | `registry_contract_address`, `relayer_multicall_address`, `nox_reward_pool_address` |
| **Config** | `node_role = "exit"` and `oracle_url = "http://127.0.0.1:15004"` |

### Profitability

Exit nodes only submit transactions that are profitable:

```
Revenue = fee_amount * token_price_usd / 10^decimals
Gas Cost = gas_used * gas_price * eth_price_usd / 10^18
Margin  = Revenue / Gas Cost

>= 1.10 -> SUBMIT
<  1.10 -> DROP
```

Adjust with `min_profit_margin_percent` (default 10%). "Unprofitable TX dropped" in logs is normal.

### Monitoring

```bash
cast balance YOUR_ETH_ADDRESS --rpc-url https://sepolia-rollup.arbitrum.io/rpc
docker compose logs nox | grep -i "submit\|confirm\|revert\|profitab"
curl http://localhost:15004/health
```

### Security

- SSRF protection on by default (`allow_private_ips = false`)
- Private keys never logged, zeroized on drop
- Your ETH address is registered on-chain and visible to the network

## Key Generation

```bash
docker run --rm ghcr.io/hisoka-io/nox:0.2.0-testnet keygen
```

Outputs:
```
NOX__ROUTING_PRIVATE_KEY=a573f439...c3d832b6
# Public key (for registration): 509a3761...95acd317

NOX__P2P_PRIVATE_KEY=89f3438a...fcc3f8fa
# PeerId (for registration): 12D3KooWELRY...

NOX__ETH_WALLET_PRIVATE_KEY=396fdae4...ffd6c384
# Address (for registration): 0xb192f9ed...
```

| Key | Algorithm | Purpose |
|-----|-----------|---------|
| Routing Key | X25519 | Sphinx packet encryption per hop |
| P2P Key | Ed25519 | libp2p identity |
| ETH Wallet Key | secp256k1 | Signs on-chain transactions (exit nodes) |

Fallback without Docker: `bash scripts/generate-keys.sh > .env` (generates private keys only, no public key derivation).

## Node Roles

| Role | What It Does | Requires |
|------|-------------|----------|
| `relay` | Forwards Sphinx packets. Entry nodes accept client packets, mix nodes add latency. | Routing key, P2P port |
| `exit` | Submits on-chain transactions. Needs wallet and gas. | Routing key, P2P port, ETH wallet, gas |
| `full` | Both relay and exit. Default. | Everything |

Start as `relay` unless you have a reason to run an exit node.

### Required Config by Role

| Field | Relay | Exit |
|-------|:-----:|:----:|
| `eth_rpc_url` | Yes | Yes |
| `chain_id` | Yes | Yes |
| `routing_private_key` | Yes | Yes |
| `registry_contract_address` | Yes | Yes |
| `eth_wallet_private_key` | No | Yes |
| `oracle_url` | No | Yes |
| `relayer_multicall_address` | No | Yes |
| `nox_reward_pool_address` | No | Yes |

## Configuration

Config loads in order (later overrides earlier):

1. Built-in defaults
2. TOML config file (`config.toml`)
3. Environment variables (`NOX__` prefix)

### Environment Variables

Top-level: `NOX__FIELD_NAME`
Nested: `NOX__SECTION__FIELD_NAME`

```bash
NOX__ETH_RPC_URL=https://...
NOX__CHAIN_ID=421614
NOX__NETWORK__MAX_CONNECTIONS=1000
NOX__RELAYER__MIX_DELAY_MS=500.0
```

### Core

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `eth_rpc_url` | String | `http://127.0.0.1:8545` | Ethereum JSON-RPC. Reads NoxRegistry events (all nodes), submits TXs (exit nodes). |
| `oracle_url` | String | `http://127.0.0.1:3000` | Price oracle URL. docker-compose runs `price_server` on 15004. |
| `chain_id` | u64 | `0` | Chain ID. `421614` for Arbitrum Sepolia. |
| `node_role` | String | `"full"` | `"relay"`, `"exit"`, or `"full"`. |
| `benchmark_mode` | bool | `false` | Skip production validations. Never enable in production. |

### Contracts (Arbitrum Sepolia)

| Contract | Address |
|----------|---------|
| DarkPool | `0xd1CDd9474b5Caf67F95F871503E5774Fd6aD0F16` |
| NoxRegistry | `0x5e00d71a66804f58dAd2dFa6dA6857F6B1F1F4F2` |
| NoxRewardPool | `0x89277aD4519d62AC9C26E431eb6236C30C893956` |
| StakingToken | `0x50716a09f40cB9c1eA7aCA86255bAf02513B0238` |
| RelayerMulticall | `0xCc09Fe53bC36c0F34996A6AD3088E937Ef44C94E` |

Config fields: `registry_contract_address`, `relayer_multicall_address`, `nox_reward_pool_address`

### Keys

| Field | Default | Description |
|-------|---------|-------------|
| `routing_private_key` | `""` | X25519 private key (hex). Required. |
| `p2p_private_key` | `""` | Ed25519 seed (hex). Auto-generates if empty. |
| `p2p_identity_path` | `./data/p2p_id.key` | Persists P2P identity. Docker: `/var/lib/nox/identity/p2p_id.key`. |
| `eth_wallet_private_key` | `""` | secp256k1 key (hex). Required for exit nodes. |

### Networking

| Field | Default | Description |
|-------|---------|-------------|
| `p2p_port` | `9000` | libp2p port. Must be public. Testnet: `15000`. |
| `p2p_listen_addr` | `0.0.0.0` | Bind address. |
| `metrics_port` | `9090` | Admin/metrics, localhost only. Testnet: `15001`. |
| `topology_api_port` | `0` | Public topology endpoint. `15003` for seed nodes. |
| `ingress_port` | `0` | HTTP packet injection. `15002` for entry nodes. |
| `bootstrap_topology_urls` | `[]` | Seed URLs. Default: `["https://api.hisoka.io/seed/topology"]`. |

### Economics

| Field | Default | Description |
|-------|---------|-------------|
| `min_gas_balance` | `"10000000000000000"` | Min ETH balance before warning (0.01 ETH). |
| `min_profit_margin_percent` | `10` | Min profit margin for TX execution. |

### Relay Pipeline

| Field | Default | Description |
|-------|---------|-------------|
| `min_pow_difficulty` | `3` | PoW difficulty (0-63). |
| `db_path` | `./data/nox_db` | Sled database. Docker: `/var/lib/nox/data/db`. |
| `block_poll_interval_secs` | `12` | Block polling interval. `5` for Arb Sepolia. |
| `chain_start_block` | `0` | Start scanning from this block. |
| `max_broadcast_tx_size` | `131072` | Max broadcast TX size (bytes). `262144` for L2. |
| `mix_delay_ms` | `500.0` | Poisson mixing delay (ms). Higher = more privacy, more latency. |
| `cover_traffic_rate` | `0.05` | Loop cover packets/sec. |
| `drop_traffic_rate` | `0.05` | Drop cover packets/sec. |
| `replay_window` | `3600` | Replay tag TTL (seconds). |
| `bloom_capacity` | `100000` | Bloom filter capacity per window. |

### Network

| Field | Default | Description |
|-------|---------|-------------|
| `max_connections` | `1000` | Max total P2P connections. |
| `max_connections_per_peer` | `2` | Max substreams per peer. |
| `ping_interval_secs` | `15` | Heartbeat interval. |
| `idle_connection_timeout_secs` | `3600` | Close idle connections after this. |

### Rate Limiting (`[network.rate_limit]`)

Peers progress through tiers: unknown -> trusted (after 1hr good behavior) -> penalized (after 5 violations in 60s).

| Field | Default |
|-------|---------|
| `burst_unknown` / `rate_unknown` | 50 / 100 |
| `burst_trusted` / `rate_trusted` | 100 / 200 |
| `burst_penalized` / `rate_penalized` | 10 / 25 |
| `violations_before_disconnect` | 5 |
| `trust_promotion_time_secs` | 3600 |

### HTTP Proxy (`[http]`, exit nodes)

| Field | Default | Description |
|-------|---------|-------------|
| `allow_private_ips` | `false` | Never enable in production (SSRF). |
| `request_timeout_secs` | `10` | Proxied request timeout. |
| `max_response_bytes` | `1048576` | Max response (1 MB). |

## Ports

| Port | Service | Required | Notes |
|------|---------|----------|-------|
| 15000 | libp2p P2P | Yes | Must be publicly reachable |
| 15001 | Admin + metrics | No | Localhost only |
| 15002 | HTTP ingress | Entry only | Clients send Sphinx packets here |
| 15003 | Topology API | Seed only | Other nodes bootstrap from this |
| 15004 | Price oracle | Internal | Sidecar, not exposed |

### Firewall

| Role | Open Ports |
|------|-----------|
| Mix-only relay | `15000/tcp` |
| Entry node | `15000/tcp`, `15002/tcp` |
| Seed node | `15000/tcp`, `15002/tcp`, `15003/tcp` |
| Exit node | `15000/tcp` |

```bash
# Example: entry + seed node
sudo ufw allow 15000/tcp
sudo ufw allow 15002/tcp
sudo ufw allow 15003/tcp
```

## Monitoring

```bash
# Topology
curl -s http://localhost:15001/topology | python3 -m json.tool

# Logs
docker compose logs -f nox
docker compose logs --tail 100 nox
docker compose logs price-server
```

Log level via `RUST_LOG` in docker-compose.yml: `error`, `warn`, `info` (default), `debug`, or per-crate like `nox_node=debug,info`.

## Funding Exit Nodes

1. Get your ETH address from `nox keygen` output
2. Get testnet ETH from the [Arbitrum Sepolia faucet](https://faucet.quicknode.com/arbitrum/sepolia)
3. Send 0.1 ETH to your node's address

The profitability engine only submits transactions where revenue exceeds gas cost by at least 10% (configurable). Dropped transactions are logged but not an error.

## Upgrading

```bash
docker compose pull
docker compose up -d
```

Data persists in Docker volumes: `nox-identity` (P2P key), `nox-data` (database), `nox-logs`.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Stuck after `docker compose up` | Normal, first start takes ~60s. Check `docker compose logs nox`. |
| No peers found | Node not registered. See [REGISTRATION.md](REGISTRATION.md). |
| `routing_private_key is empty` | `.env` missing or not in same dir as docker-compose.yml. |
| Connection refused on 15000 | `sudo ufw allow 15000/tcp` |
| Node restart loop | Check `docker compose logs nox` for config errors. |
| Unprofitable TX dropped | Normal for exit nodes. Gas cost > fee revenue. |
| Lagged(N) in logs | Temporary, recovers automatically. Increase `relayer.queue_size` if frequent. |
| Empty topology | Check `eth_rpc_url` and `registry_contract_address`. |
| Won't start after role change | `docker compose down -v && docker compose up -d` |

## Architecture

```
Client -> [Entry Node] -> [Mix Node] -> [Exit Node] -> Ethereum
              ^                              |
              +------ SURB Response ---------+
```

- **Entry nodes** accept client Sphinx packets via HTTP, inject into P2P mixnet
- **Mix nodes** add Poisson-distributed delay to resist timing analysis
- **Exit nodes** decrypt final layer, extract payload, execute on-chain
- **SURBs** carry responses back through the mixnet
- **Cover traffic** maintains constant rate regardless of real activity
- **Reed-Solomon FEC** handles up to 30% packet loss on SURB responses
- **PoW** prevents spam

## Links

- [Hisoka Protocol](https://hisoka.io)
- [GitHub](https://github.com/hisoka-io)
- [Registration](REGISTRATION.md)
- [Report a Bug](https://github.com/hisoka-io/run-nox/issues/new?template=bug-report.yml)
