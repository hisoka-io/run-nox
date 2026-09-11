# Node Registration

NOX nodes must be registered in the current Arbitrum Sepolia `NoxRegistry` before they join the mixnet. Its
address comes from the committed operator deployment manifest. Historical registry addresses are not compatible
with the current complete topology profile verification. Testnet registrations are reviewed by the Hisoka team.

## 1. Generate Keys

Copy the signed release record to `deployment.json` and export its exact `noxImage` and `preflightImage` values before generating keys:

```bash
git clone https://github.com/hisoka-io/run-nox.git
cd run-nox
cp /secure/path/to/signed-deployment.json deployment.json
export NOX_IMAGE='ghcr.io/hisoka-io/nox@sha256:...'
export NOX_PREFLIGHT_IMAGE='python@sha256:...'
docker run --rm "$NOX_IMAGE" keygen > .env
chmod 600 .env
```

Store `.env` through the approved secret-management path. Do not attach it to a registration request or paste it into logs.

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

An exit operator must insert the committed `NoxEntryPoint` deployment address into a local copy of `configs/exit.toml`, fund the generated wallet, and pass `scripts/preflight.sh exit ... deployment.json` before starting. Compose will repeat this validation before it starts either service.

## 3. Submit the Public Registration Values

Open a [node registration request](https://github.com/hisoka-io/run-nox/issues/new?template=node-registration-request.yml) with only these public values:

| Field | Source |
|---|---|
| Sphinx public key | The public-key line produced by `keygen` |
| ETH address | The public address produced by `keygen` |
| P2P multiaddr | `/ip4/YOUR_PUBLIC_IP/tcp/15000` |
| Node role | `relay` or `exit` |
| Peer ID | The public Peer ID produced by `keygen` or node startup logs |

Never include a routing key, P2P private key, wallet private key, RPC credential, or complete `.env` file.

## 4. Verify Registration

A maintainer registers the node with `nox-ctl`. After approval, peers should appear in the topology response:

```bash
curl --fail --silent http://127.0.0.1:15001/topology | python3 -m json.tool
docker compose logs --tail 100 nox
```

Confirm that TCP port `15000` is reachable from outside the host and that the registered multiaddr matches the public address.

## Exit Funding

Exit nodes require Arbitrum Sepolia ETH for gas. Fund only the public wallet address emitted by the same key generation run. Monitor the configured minimum gas balance and replenish before it is reached.

## Recovery

If validation fails, rerun preflight and inspect the named field without printing `.env`. If peers remain absent after registration, verify the public multiaddr, firewall, RPC access, registry address, and scan start block.

Do not delete Docker volumes during troubleshooting. The identity and data volumes are required for stable peer identity and safe transaction recovery.
