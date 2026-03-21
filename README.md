# Run NOX

Run a NOX mixnet node for **Hisoka Protocol** — a privacy-first DeFi protocol on Ethereum. NOX nodes form a 3-layer Sphinx mixnet that provides network-layer privacy for on-chain transactions, hiding sender identity, recipient, and transaction content from observers.

**Current version:** `v0.1.1-testnet` | **Network:** Arbitrum Sepolia | **Image:** `ghcr.io/hisoka-io/nox:0.1.1-testnet`

---

## Prerequisites

- **Docker Engine** 20.10+ and **Docker Compose** v2
- **1 vCPU / 1 GB RAM** minimum (2 vCPU / 2 GB recommended for exit nodes)
- **Public IPv4** address with TCP port `15000` open
- **Ethereum RPC** endpoint — public Arbitrum Sepolia RPC works: `https://sepolia-rollup.arbitrum.io/rpc`

---

## Quick Start

```bash
# 1. Pull the image
docker pull ghcr.io/hisoka-io/nox:0.1.1-testnet

# 2. Generate keys (save this output!)
docker run --rm ghcr.io/hisoka-io/nox:0.1.1-testnet keygen > .env

# 3. Clone this repo and copy a config template
git clone https://github.com/hisoka-io/run-nox.git && cd run-nox
cp configs/relay.toml config.toml
# Edit config.toml — find "YOUR_PUBLIC_IP" and replace with your server's IPv4

# 4. Start the node
docker compose up -d

# 5. Check it's running
curl http://localhost:15001/topology

# 6. Submit a registration request (see REGISTRATION.md)
```

Your node won't find peers until it's registered on-chain. See [REGISTRATION.md](REGISTRATION.md) for the full registration flow.

---

## Key Generation

The `nox keygen` command generates all cryptographic keys your node needs:

```bash
docker run --rm ghcr.io/hisoka-io/nox:0.1.1-testnet keygen
```

Output:
```
# NOX Node Keys
# Generated: 2026-03-21 08:43:23 UTC
# SAVE THIS OUTPUT. Private keys cannot be recovered.

# === Sphinx Routing Key (X25519) ===
NOX__ROUTING_PRIVATE_KEY=a573f439...c3d832b6
# Public key (for registration): 509a3761...95acd317

# === P2P Identity (Ed25519) ===
NOX__P2P_PRIVATE_KEY=89f3438a...fcc3f8fa
# PeerId (for registration): 12D3KooWELRY...

# === ETH Wallet (secp256k1) ===
NOX__ETH_WALLET_PRIVATE_KEY=396fdae4...ffd6c384
# Address (for registration): 0xb192f9ed...
```

### What Each Key Does

| Key | Algorithm | Purpose | Who Sees It |
|-----|-----------|---------|------------|
| **Routing Key** | X25519 (32 bytes) | Encrypts/decrypts Sphinx packets at each hop | Private — never share |
| **Sphinx Public Key** | X25519 public | Registered on-chain so clients can encrypt packets to your node | Public — share for registration |
| **P2P Key** | Ed25519 (32 bytes) | libp2p identity for peer connections | Private — never share |
| **PeerId** | Ed25519 public hash | Identifies your node in the P2P network | Public — share for registration |
| **ETH Wallet Key** | secp256k1 (32 bytes) | Signs on-chain transactions (exit nodes only) | Private — never share |
| **ETH Address** | secp256k1 public | Your node's Ethereum address | Public — share for registration |

### Fallback: Manual Key Generation

If Docker isn't available for keygen, you can generate private keys with OpenSSL:

```bash
bash scripts/generate-keys.sh > .env
```

This generates random private keys but cannot derive public keys (sphinx key, PeerId, ETH address). Use the Docker `keygen` command for the full output.

---

## Node Roles

| Role | Code | What It Does | Requires |
|------|------|-------------|----------|
| **Relay** | `relay` (1) | Forwards Sphinx packets between nodes. Entry nodes accept client packets; mix nodes add latency. | Routing key, P2P port |
| **Exit** | `exit` (2) | Executes on-chain transactions (ZK proof submission, swaps). Needs an ETH wallet and gas. | Routing key, P2P port, ETH wallet, gas funding |
| **Full** | `full` (3) | All capabilities (relay + exit). Backward-compatible default. | Everything |

**Most operators should start as `relay`.** Exit nodes have additional requirements (funding, oracle, contract addresses) and higher responsibility.

### Config Fields by Role

| Field | Relay | Exit |
|-------|:-----:|:----:|
| `eth_rpc_url` | Required | Required |
| `chain_id` | Required | Required |
| `routing_private_key` | Required | Required |
| `registry_contract_address` | Required | Required |
| `eth_wallet_private_key` | Not needed | Required |
| `oracle_url` | Not needed | Required |
| `relayer_multicall_address` | Not needed | Required |
| `nox_reward_pool_address` | Not needed | Required |
| `min_profit_margin_percent` | Not needed | Required |

---

## Configuration Reference

Configuration loads in order (later values override):
1. Built-in defaults
2. TOML config file (`config.toml`)
3. Environment variables (`NOX__` prefix)

### Environment Variable Format

Top-level fields: `NOX__FIELD_NAME` (double underscore after NOX)
Nested fields: `NOX__SECTION__FIELD_NAME`

Examples:
```bash
NOX__ETH_RPC_URL=https://...
NOX__CHAIN_ID=421614
NOX__NETWORK__MAX_CONNECTIONS=1000
NOX__RELAYER__MIX_DELAY_MS=500.0
```

### Core Settings

| Field | Type | Default | Env Override | Description |
|-------|------|---------|-------------|-------------|
| `eth_rpc_url` | String | `http://127.0.0.1:8545` | `NOX__ETH_RPC_URL` | Ethereum JSON-RPC endpoint. Used for reading NoxRegistry events (all nodes) and submitting transactions (exit nodes). |
| `oracle_url` | String | `http://127.0.0.1:3000` | `NOX__ORACLE_URL` | Price oracle HTTP URL. The docker-compose runs a `price_server` sidecar on port 15004. Used for gas profitability calculations on exit nodes. |
| `chain_id` | u64 | `0` | `NOX__CHAIN_ID` | Ethereum chain ID. Must be non-zero in production. `421614` for Arbitrum Sepolia. |
| `node_role` | String | `"full"` | `NOX__NODE_ROLE` | Node role: `"relay"`, `"exit"`, or `"full"`. Determines which services start. |
| `benchmark_mode` | bool | `false` | `NOX__BENCHMARK_MODE` | Skip production validations. **NEVER enable in production.** |

### Contract Addresses

| Field | Default | Env Override | Description |
|-------|---------|-------------|-------------|
| `registry_contract_address` | `0x000...000` | `NOX__REGISTRY_CONTRACT_ADDRESS` | NoxRegistry contract — node registration, topology, staking. |
| `relayer_multicall_address` | `0x000...000` | `NOX__RELAYER_MULTICALL_ADDRESS` | RelayerMulticall — batched TX execution (gas payment + user action). Exit nodes only. |
| `nox_reward_pool_address` | `0x000...000` | `NOX__NOX_REWARD_POOL_ADDRESS` | NoxRewardPool — ZK gas payment validation and reward deposits. Exit nodes only. |

**Arbitrum Sepolia addresses (v0.1.1-testnet):**
| Contract | Address |
|----------|---------|
| DarkPool | `0xd1CDd9474b5Caf67F95F871503E5774Fd6aD0F16` |
| NoxRegistry | `0x5e00d71a66804f58dAd2dFa6dA6857F6B1F1F4F2` |
| NoxRewardPool | `0x89277aD4519d62AC9C26E431eb6236C30C893956` |
| StakingToken | `0x50716a09f40cB9c1eA7aCA86255bAf02513B0238` |
| RelayerMulticall | `0xCc09Fe53bC36c0F34996A6AD3088E937Ef44C94E` |

### Identity & Keys

| Field | Type | Default | Env Override | Description |
|-------|------|---------|-------------|-------------|
| `routing_private_key` | Hex (64 chars) | `""` | `NOX__ROUTING_PRIVATE_KEY` | X25519 private key for Sphinx packet encryption. 32 bytes, hex-encoded. Required in production. |
| `p2p_private_key` | Hex (64 chars) | `""` | `NOX__P2P_PRIVATE_KEY` | Ed25519 seed for libp2p identity. If empty, auto-generates and persists to `p2p_identity_path`. |
| `p2p_identity_path` | String | `./data/p2p_id.key` | `NOX__P2P_IDENTITY_PATH` | Path to persist P2P identity key. In Docker: `/var/lib/nox/identity/p2p_id.key`. |
| `eth_wallet_private_key` | Hex (64 chars) | `""` | `NOX__ETH_WALLET_PRIVATE_KEY` | secp256k1 private key for signing Ethereum transactions. Required for exit/full nodes. |

### Networking

| Field | Type | Default | Env Override | Description |
|-------|------|---------|-------------|-------------|
| `p2p_port` | u16 | `9000` | `NOX__P2P_PORT` | libp2p listening port. **Must be publicly reachable** (open in firewall). Testnet uses `15000`. |
| `p2p_listen_addr` | String | `0.0.0.0` | `NOX__P2P_LISTEN_ADDR` | P2P bind address. Keep `0.0.0.0` for production. |
| `metrics_port` | u16 | `9090` | `NOX__METRICS_PORT` | Admin API + Prometheus metrics port. Binds to localhost only. Testnet uses `15001`. |
| `topology_api_port` | u16 | `0` (disabled) | `NOX__TOPOLOGY_API_PORT` | Public topology endpoint. Other nodes can bootstrap from this. Set to `15003` for seed nodes, `0` to disable. |
| `ingress_port` | u16 | `0` (disabled) | `NOX__INGRESS_PORT` | HTTP packet injection port. Entry nodes set to `15002`. Clients send Sphinx packets here. |
| `bootstrap_topology_urls` | Vec\<String\> | `[]` | `NOX__BOOTSTRAP_TOPOLOGY_URLS` | Seed node URLs for fast topology bootstrap. JSON array format in env vars. |

### Economics

| Field | Type | Default | Env Override | Description |
|-------|------|---------|-------------|-------------|
| `min_gas_balance` | String (wei) | `"10000000000000000"` | `NOX__MIN_GAS_BALANCE` | Minimum ETH balance before warning. Default: 0.01 ETH. |
| `min_profit_margin_percent` | u64 | `10` | `NOX__MIN_PROFIT_MARGIN_PERCENT` | Minimum profit margin (%) for TX execution. If `revenue / gas_cost < 1.10`, TX is dropped. |

### Relay Pipeline

| Field | Type | Default | Env Override | Description |
|-------|------|---------|-------------|-------------|
| `min_pow_difficulty` | u32 | `3` | `NOX__MIN_POW_DIFFICULTY` | Proof-of-Work difficulty for incoming packets (0-63). Higher = more CPU per packet, fewer spam packets. |
| `db_path` | String | `./data/nox_db` | `NOX__DB_PATH` | Sled database directory. In Docker: `/var/lib/nox/data/db`. |
| `block_poll_interval_secs` | u64 | `12` | `NOX__BLOCK_POLL_INTERVAL_SECS` | How often to poll the chain for new blocks. `12` for Ethereum L1, `5` for Arb Sepolia. |
| `chain_start_block` | u64 | `0` | `NOX__CHAIN_START_BLOCK` | Start scanning from this block. Set to a recent block to skip old history. |
| `max_broadcast_tx_size` | usize | `131072` | `NOX__MAX_BROADCAST_TX_SIZE` | Max signed broadcast TX size in bytes. 128 KB default, 256 KB for L2. |
| `response_prune_interval_secs` | u64 | `60` | `NOX__RESPONSE_PRUNE_INTERVAL_SECS` | How often to prune expired SURB response buffers. |

### `[network]` — P2P Connection Settings

| Field | Type | Default | Env Override | Description |
|-------|------|---------|-------------|-------------|
| `max_connections` | u32 | `1000` | `NOX__NETWORK__MAX_CONNECTIONS` | Max total P2P connections. |
| `max_connections_per_peer` | u32 | `2` | `NOX__NETWORK__MAX_CONNECTIONS_PER_PEER` | Max substreams per peer. |
| `ping_interval_secs` | u64 | `15` | `NOX__NETWORK__PING_INTERVAL_SECS` | Heartbeat ping interval. |
| `ping_timeout_secs` | u64 | `10` | `NOX__NETWORK__PING_TIMEOUT_SECS` | Heartbeat timeout. |
| `gossip_heartbeat_secs` | u64 | `1` | `NOX__NETWORK__GOSSIP_HEARTBEAT_SECS` | Gossip protocol heartbeat. |
| `idle_connection_timeout_secs` | u64 | `3600` | `NOX__NETWORK__IDLE_CONNECTION_TIMEOUT_SECS` | Close idle connections after this duration. |
| `session_ttl_secs` | u64 | `86400` | `NOX__NETWORK__SESSION_TTL_SECS` | Session ticket lifetime (24h). |

### `[network.rate_limit]` — Graduated Rate Limiting

Nodes are classified into reputation tiers: unknown → trusted (after `trust_promotion_time_secs` of good behavior) → penalized (after `violations_before_disconnect` violations).

| Field | Type | Default | Env Override | Description |
|-------|------|---------|-------------|-------------|
| `burst_unknown` | u32 | `50` | `NOX__NETWORK__RATE_LIMIT__BURST_UNKNOWN` | Burst allowance for new peers (packets/sec). |
| `rate_unknown` | u32 | `100` | `NOX__NETWORK__RATE_LIMIT__RATE_UNKNOWN` | Sustained rate for new peers. |
| `burst_trusted` | u32 | `100` | `NOX__NETWORK__RATE_LIMIT__BURST_TRUSTED` | Burst for trusted peers. |
| `rate_trusted` | u32 | `200` | `NOX__NETWORK__RATE_LIMIT__RATE_TRUSTED` | Sustained for trusted peers. |
| `burst_penalized` | u32 | `10` | `NOX__NETWORK__RATE_LIMIT__BURST_PENALIZED` | Burst for penalized peers. |
| `rate_penalized` | u32 | `25` | `NOX__NETWORK__RATE_LIMIT__RATE_PENALIZED` | Sustained for penalized peers. |
| `violations_before_disconnect` | u32 | `5` | `NOX__NETWORK__RATE_LIMIT__VIOLATIONS_BEFORE_DISCONNECT` | Strikes before forced disconnect. |
| `violation_window_secs` | u64 | `60` | `NOX__NETWORK__RATE_LIMIT__VIOLATION_WINDOW_SECS` | Window for counting violations. |
| `trust_promotion_time_secs` | u64 | `3600` | `NOX__NETWORK__RATE_LIMIT__TRUST_PROMOTION_TIME_SECS` | Time to promote unknown → trusted. |

### `[network.connection_filter]` — Anti-Sybil

| Field | Type | Default | Env Override | Description |
|-------|------|---------|-------------|-------------|
| `max_per_subnet` | u32 | `50` | `NOX__NETWORK__CONNECTION_FILTER__MAX_PER_SUBNET` | Max connections per subnet. |
| `subnet_prefix_len` | u32 | `24` | `NOX__NETWORK__CONNECTION_FILTER__SUBNET_PREFIX_LEN` | IPv4 subnet mask (/24). |
| `ipv6_subnet_prefix_len` | u32 | `48` | `NOX__NETWORK__CONNECTION_FILTER__IPV6_SUBNET_PREFIX_LEN` | IPv6 subnet mask (/48). |

### `[relayer]` — Sphinx Relay Pipeline

| Field | Type | Default | Env Override | Description |
|-------|------|---------|-------------|-------------|
| `queue_size` | usize | `10000` | `NOX__RELAYER__QUEUE_SIZE` | Sphinx packet processing queue capacity. |
| `worker_count` | usize | CPU count | `NOX__RELAYER__WORKER_COUNT` | Sphinx peeling worker threads. Auto-detects CPU count if omitted. |
| `replay_window` | u64 | `3600` | `NOX__RELAYER__REPLAY_WINDOW` | Replay tag TTL in seconds. Tags older than this are forgotten. |
| `bloom_capacity` | usize | `100000` | `NOX__RELAYER__BLOOM_CAPACITY` | Bloom filter capacity per replay window. Higher = more memory, fewer false positives. |
| `mix_delay_ms` | f64 | `500.0` | `NOX__RELAYER__MIX_DELAY_MS` | Poisson mixing delay in milliseconds. **Higher = more privacy, more latency.** Set to `0.0` for instant forwarding (testing only). |
| `cover_traffic_rate` | f64 | `0.05` | `NOX__RELAYER__COVER_TRAFFIC_RATE` | Loop cover traffic rate (packets/sec). `0.05` = ~1 packet every 20 seconds. |
| `drop_traffic_rate` | f64 | `0.05` | `NOX__RELAYER__DROP_TRAFFIC_RATE` | Drop cover traffic rate (packets/sec). Same as cover traffic but packets are discarded. |

### `[relayer.fragmentation]` — Reassembly Limits

| Field | Type | Default | Env Override | Description |
|-------|------|---------|-------------|-------------|
| `max_pending_bytes` | usize | `10485760` | `NOX__RELAYER__FRAGMENTATION__MAX_PENDING_BYTES` | Max buffered bytes across all reassemblies (10 MB). |
| `max_concurrent_messages` | usize | `50` | `NOX__RELAYER__FRAGMENTATION__MAX_CONCURRENT_MESSAGES` | Max simultaneous message reassemblies. |
| `timeout_seconds` | u64 | `300` | `NOX__RELAYER__FRAGMENTATION__TIMEOUT_SECONDS` | Timeout for incomplete messages (5 min). |
| `prune_interval_seconds` | u64 | `60` | `NOX__RELAYER__FRAGMENTATION__PRUNE_INTERVAL_SECONDS` | How often to prune expired fragments. |

### `[http]` — HTTP Proxy (Exit Nodes)

| Field | Type | Default | Env Override | Description |
|-------|------|---------|-------------|-------------|
| `allow_private_ips` | bool | `false` | `NOX__HTTP__ALLOW_PRIVATE_IPS` | Allow requests to private/loopback IPs. **NEVER enable in production** (SSRF risk). |
| `request_timeout_secs` | u64 | `10` | `NOX__HTTP__REQUEST_TIMEOUT_SECS` | HTTP request timeout for proxied requests. |
| `max_response_bytes` | usize | `1048576` | `NOX__HTTP__MAX_RESPONSE_BYTES` | Max response body size (1 MB). Larger responses are truncated. |

---

## Ports

| Port | Config Field | Default | Service | Required | Notes |
|------|-------------|---------|---------|----------|-------|
| **15000** | `p2p_port` | 9000 | libp2p P2P | Yes | TCP. **Must be publicly reachable.** |
| **15001** | `metrics_port` | 9090 | Admin + metrics | No | Localhost-only. Serves `/topology`, `/metrics`. |
| **15002** | `ingress_port` | 0 | HTTP ingress | Entry only | Clients send Sphinx packets here. |
| **15003** | `topology_api_port` | 0 | Topology API | Seed only | Other nodes bootstrap topology from this. |
| **15004** | `PRICE_SERVER_PORT` | 3000 | Price oracle | Internal | Sidecar, not exposed externally. |

### Firewall Rules

**Relay node:**
```bash
sudo ufw allow 15000/tcp   # P2P (required)
# Optional for entry/seed nodes:
sudo ufw allow 15002/tcp   # HTTP ingress
sudo ufw allow 15003/tcp   # Topology API
```

**Exit node:**
```bash
sudo ufw allow 15000/tcp   # P2P (required)
```

---

## Monitoring & Health

### Health Check

```bash
# Topology endpoint — returns peer list
curl http://localhost:15001/topology

# Pretty-print
curl -s http://localhost:15001/topology | python3 -m json.tool
```

### Logs

```bash
# Follow live logs
docker compose logs -f nox

# Last 100 lines
docker compose logs --tail 100 nox

# Price server logs
docker compose logs price-server
```

### Log Levels

Set via `RUST_LOG` environment variable in `docker-compose.yml`:

| Level | Use Case |
|-------|----------|
| `error` | Only errors |
| `warn` | Errors + warnings |
| `info` | Normal operation (default) |
| `debug` | Verbose (includes packet processing) |
| `nox_node=debug,info` | Debug for nox-node, info for everything else |

---

## Funding Exit Nodes

Exit nodes submit on-chain transactions and spend gas. On Arbitrum Sepolia testnet:

1. Get your ETH address from `nox keygen` output
2. Get testnet ETH from the [Arbitrum Sepolia faucet](https://faucet.quicknode.com/arbitrum/sepolia)
3. Send **0.1 ETH** to your node's address (lasts weeks on testnet)

The profitability engine ensures your node only submits transactions where revenue exceeds gas cost by at least `min_profit_margin_percent` (default 10%). Unprofitable transactions are dropped with a log message — this is normal behavior, not an error.

---

## Upgrading

```bash
docker compose pull
docker compose up -d
```

Data persists across upgrades via Docker named volumes:
- `nox-identity` — P2P keypair (never changes)
- `nox-data` — Sled database (chain state, sessions)
- `nox-logs` — Log files

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| "No peers found" | Node not registered on-chain | Submit registration request ([REGISTRATION.md](REGISTRATION.md)) |
| "Config validation failed: routing_private_key is empty" | `.env` file missing or not loaded | Ensure `.env` is in the same directory as `docker-compose.yml` |
| "Connection refused" on port 15000 | Firewall blocking P2P | `sudo ufw allow 15000/tcp` |
| Node restarts in a loop | Config error | Check `docker compose logs nox` for the specific error |
| "Unprofitable: revenue=$X, cost=$Y" | Gas cost > fee revenue | Normal for exit nodes — TX is dropped, not submitted |
| "Lagged(N)" in logs | Event bus overflow | Temporary; node recovers automatically. If frequent, increase `relayer.queue_size`. |
| Empty topology (0 nodes) | RPC issue or wrong contract address | Verify `eth_rpc_url` is reachable and `registry_contract_address` is correct |

---

## Architecture

NOX is a 3-layer Sphinx mixnet based on the Loopix design:

```
Client → [Entry Node] → [Mix Node] → [Exit Node] → Ethereum
                ↑                            |
                └──── SURB Response ─────────┘
```

- **Entry nodes** accept client Sphinx packets via HTTP and inject them into the P2P mixnet
- **Mix nodes** add Poisson-distributed latency to resist timing analysis
- **Exit nodes** decrypt the final layer, extract the payload, and execute on-chain transactions
- **SURB responses** travel back through the mixnet to the client using Single-Use Reply Blocks
- **Cover traffic** (loop + drop packets) maintains constant traffic rate regardless of real activity
- **Reed-Solomon FEC** provides error correction for SURB responses (handles up to 30% packet loss)
- **PoW anti-spam** requires computational proof before accepting packets

---

## Links

- [Hisoka Protocol](https://hisoka.io)
- [GitHub Organization](https://github.com/hisoka-io)
- [Registration Guide](REGISTRATION.md)
- [Report a Bug](https://github.com/hisoka-io/run-nox/issues/new?template=bug-report.yml)
