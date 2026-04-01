# Node Registration

NOX nodes must be registered on-chain in the NoxRegistry contract to join the mixnet. During testnet, registration is handled by the Hisoka team.

## 1. Generate Keys and Start

```bash
git clone https://github.com/hisoka-io/run-nox.git && cd run-nox

# Generate keys (save this output, private keys can't be recovered)
docker run --rm ghcr.io/hisoka-io/nox:0.2.0-testnet keygen | tee .env

# Pick a config template
cp configs/relay.toml config.toml   # or configs/exit.toml

docker compose up -d
```

First start takes about 60 seconds. The node will poll the chain for topology but won't find peers until registered.

## 2. Check It's Running

```bash
curl http://localhost:15001/topology
docker compose logs -f nox
```

You should see block polling and no peer connections yet.

## 3. Request Registration

Open an issue: [Node Registration Request](https://github.com/hisoka-io/run-nox/issues/new?template=node-registration-request.yml)

Provide:

| Field | Where |
|-------|-------|
| Sphinx Public Key | keygen output, `# Public key (for registration):` line |
| ETH Address | keygen output, `# Address (for registration):` line |
| P2P Multiaddr | `/ip4/YOUR_PUBLIC_IP/tcp/15000` |
| Node Role | `relay` or `exit` |
| PeerId (optional) | keygen output or `docker compose logs nox | grep PeerId` |

## 4. Wait for Approval

A maintainer registers your node on-chain with `nox-ctl`. You'll get notified on the issue.

## 5. Verify

After registration, peers should appear within ~10 seconds:

```bash
curl -s http://localhost:15001/topology | python3 -m json.tool
```

## Funding Exit Nodes

Exit nodes need ETH for gas on Arbitrum Sepolia:

1. Get your ETH address from keygen output
2. Faucet: https://faucet.quicknode.com/arbitrum/sepolia
3. Send 0.1 ETH (lasts weeks on testnet)

The node only submits if `revenue / gas_cost >= 1.10`. Adjust with `min_profit_margin_percent`.

## Troubleshooting

**No peers after registration:** Check port 15000 is reachable (`nc -zv YOUR_IP 15000`), verify multiaddr matches your IP, wait a minute.

**Config validation failed:** Check `.env` exists next to docker-compose.yml. Run `grep NOX__ROUTING .env` to verify keys are set.

**Can't connect to peers:** `sudo ufw allow 15000/tcp`, check `p2p_listen_addr = "0.0.0.0"`, check `network_mode: host` in docker-compose.

**Won't start after role change:** `docker compose down -v && docker compose up -d`
