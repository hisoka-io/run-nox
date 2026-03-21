# Node Registration

NOX nodes must be registered on-chain in the NoxRegistry contract before they can participate in the mixnet. During the testnet phase, registration is managed by the Hisoka team.

## Steps

### 1. Generate Your Keys

```bash
docker run --rm ghcr.io/hisoka-io/nox:0.1.2-testnet keygen
```

Save the full output. You'll need:
- **Private keys** → paste into your `.env` file (never share these)
- **Public values** → share in your registration request

### 2. Start Your Node

```bash
# Copy a config template
cp configs/relay.toml config.toml

# Edit config.toml — set your public IP address
# Replace any instance of YOUR_PUBLIC_IP with your server's IPv4

# Copy keygen output to .env
docker run --rm ghcr.io/hisoka-io/nox:0.1.2-testnet keygen > .env

# Start the node
docker compose up -d
```

Your node will boot and begin polling the chain for topology updates. It won't find peers yet — that's expected until you're registered.

### 3. Verify Your Node is Running

```bash
# Check health
curl http://localhost:15001/topology

# Check logs
docker compose logs -f nox
```

You should see the node polling blocks and waiting for peer connections.

### 4. Submit a Registration Request

Open an issue using the [Node Registration Request](https://github.com/hisoka-io/run-nox/issues/new?template=node-registration-request.yml) template.

You'll need to provide:

| Field | Where to Find It |
|-------|-----------------|
| **Sphinx Public Key** | `nox keygen` output: line `# Public key (for registration): ...` |
| **ETH Address** | `nox keygen` output: line `# Address (for registration): 0x...` |
| **P2P Multiaddr** | `/ip4/YOUR_PUBLIC_IP/tcp/15000` |
| **Node Role** | `relay` or `exit` |
| **PeerId** (optional) | `nox keygen` output or from node logs |

### 5. Wait for Approval

A maintainer will register your node on-chain using `nox-ctl`. You'll be notified on the issue when registration is complete.

### 6. Verify Registration

After registration, your node should automatically discover peers within 1-2 block poll intervals (~10 seconds on Arbitrum Sepolia).

```bash
# Check that your node sees other peers
curl http://localhost:15001/topology | python3 -m json.tool
```

You should see a list of nodes including your own.

## Funding Exit Nodes

Exit nodes submit on-chain transactions and need ETH for gas. For the Arbitrum Sepolia testnet:

1. Get your ETH address from `nox keygen` output
2. Get testnet ETH from the [Arbitrum Sepolia faucet](https://faucet.quicknode.com/arbitrum/sepolia)
3. Send 0.1 ETH to your node's address (lasts weeks on testnet)

The node checks profitability before submitting transactions: it only submits if `revenue / gas_cost >= 1.10` (10% profit margin). You can adjust this via `min_profit_margin_percent` in your config.

## Troubleshooting

**"No peers found after registration"**
- Verify your P2P port (15000) is publicly reachable: `nc -zv YOUR_IP 15000`
- Check that your multiaddr in the registration matches your actual IP and port
- Wait 1-2 minutes for topology propagation

**"Config validation failed"**
- Ensure `.env` file exists and is in the same directory as `docker-compose.yml`
- Verify `routing_private_key` is not empty (check with `grep NOX__ROUTING .env`)

**Node registered but can't connect to peers**
- Check firewall: `sudo ufw allow 15000/tcp`
- Ensure `p2p_listen_addr = "0.0.0.0"` (not `127.0.0.1`)
- Check Docker uses `network_mode: host`
