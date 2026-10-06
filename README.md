# Run NOX

This repository is the canonical operator kit for a [NOX](https://github.com/hisoka-io/nox) mixnet node on [Hisoka Protocol](https://hisoka.io). NOX provides network-layer privacy for protocol-neutral paid transactions on Arbitrum Sepolia.

## Requirements

- Docker Engine 20.10 or newer and Docker Compose v2.20 or newer
- Python 3.11 or newer for TOML preflight validation
- [Foundry](https://getfoundry.sh) `cast` for registry transactions (publishing a KPS address, changing your IP)
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
| Nox image | `ghcr.io/hisoka-io/nox@sha256:65867f613db88989b51e9dd3027c61cf4c64d9557747069d1d453b066e30f3a8` (`0.4.0-rc.6`) |
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
WebRTC for browsers and QUIC for native clients, both on one UDP port. Clients pin the certificate hash that is
part of the address they dial, so the address itself authenticates your entry. `nox-kps` forwards each request on
an allowlisted route (packet submit, response claim, topology, health) to the node's loopback ingress and
topology API and enforces its own size, connection, rate and time limits. Packet policy (PoW, validity, SURB
IDs) stays in the node, so both entry paths share one policy.

`nox-kps` ships in the Nox node image. Under the Compose profile `kps` it runs as its own container from the same
`NOX_IMAGE` digest as the node, with its own UID (`10002`), a 512 MB memory limit and its own restarts. A release
record that carries it names the `nox-kps` version in `release.noxKps`; check yours with:

```bash
python3 -c 'import json; print(json.load(open("deployment.json"))["release"].get("noxKps"))'
```

A version (`nox-kps` is versioned with the node release, for example `0.4.0-rc.6`) means the pinned image
serves KPS. `nox-kps-preflight` checks this, together with `nox-kps.toml` and `config.toml`, every time the
sidecar starts.

You need:

- UDP `15005` open inbound in your cloud firewall and host firewall (see Host Firewall below)
- The node ingress and topology API enabled on loopback, with a client IP header shared by the node and the sidecar
- `lo` carrying only loopback addresses: `ip -brief addr show lo` prints `127.0.0.1/8` and `::1/128`. Browsers
  dial over WebRTC, which gathers candidates from `lo`, and `nox-kps` logs a warning at startup when `lo` holds
  another address
- For publishing the address: your registered node key and a little Arbitrum Sepolia ETH for one transaction of
  about 130,000 gas

1. Enable the loopback ports and the header in `config.toml`, then restart the node:

   ```toml
   ingress_port = 15002
   topology_api_port = 15003

   [ingress]
   client_ip_header = "x-real-ip"
   ```

   ```bash
   docker compose restart nox
   ```

   The node binds these ports on all interfaces. Keep `15002` and `15003` closed to the internet with the host
   firewall; the sidecar reaches them on `127.0.0.1`. The node reads the client IP header only on loopback
   connections, so each KPS client gets its own rate-limit bucket.

2. Create the sidecar config and set your public IP (cloud hosts are NATed, so it cannot be detected):

   ```bash
   cp configs/nox-kps.toml nox-kps.toml
   sed -i 's/"203.0.113.10"/"<your public IP>"/' nox-kps.toml
   ```

   Set `node_address` to your registered node address (`grep 'Address (for registration)' .env`).
   `/metadata.json` shows it, and wallets that dial your entry as a gateway or a learned anchor map the address to
   your node through it:

   ```bash
   sed -i 's/^node_address = ""/node_address = "<your node address>"/' nox-kps.toml
   ```

3. Enable the profile and create the identity key once. The certhash it prints is the stable part of your KPS
   address:

   ```bash
   if grep -q '^COMPOSE_PROFILES=' .env; then
     sed -i 's/^COMPOSE_PROFILES=\(.*\)$/COMPOSE_PROFILES=\1,kps/' .env
   else
     echo 'COMPOSE_PROFILES=kps' >> .env
   fi
   grep '^COMPOSE_PROFILES=' .env   # kps on a relay, exit,kps on an exit
   docker compose run --rm nox-kps-admin init
   ```

   `nox-kps-admin` runs one-off `nox-kps` commands with the sidecar's image, UID, config and volumes, with
   networking turned off. It first runs `nox-kps-init`, which gives the two `nox-kps` volumes to UID `10002`.
   `init` writes the key to the `nox-kps-identity` volume and prints `certhash: <certhash>`, your address and a
   line `config line: expected_certhash = "<certhash>"`. On a host that already has a key, `init` keeps it and
   says so; `address` prints its certhash.

4. Back up the key, put the certhash in `nox-kps.toml`, and check the result:

   ```bash
   (umask 077; docker compose run --rm -T --entrypoint cat nox-kps-admin /var/lib/nox-kps/kps.key > kps.key.backup)
   sed -i 's/^expected_certhash = ""/expected_certhash = "<certhash>"/' nox-kps.toml
   docker compose run --rm nox-kps-admin check-config
   ```

   Store the backup with the node's other secrets and keep the `nox-kps-identity` volume: the certhash in your
   address is derived from this key. `nox-kps run` serves only the identity whose certhash matches
   `expected_certhash`, which keeps your published address bound to this key. `check-config` prints
   `configuration OK`, the limits in effect and the identity's certhash.

5. Start the sidecar and check it:

   ```bash
   docker compose up -d
   docker compose ps nox-kps                          # healthy
   docker compose run --rm nox-kps-admin address      # <public ip>:15005:<certhash> and the metadataUrl
   ```

   `nox-kps-preflight` runs first and starts the sidecar once the image matches the release record and ships
   `nox-kps`, the public IP is set and public, `expected_certhash` holds a certhash, the upstreams are loopback on
   the enabled ingress and topology ports, the client IP headers match, the admin listener is loopback, and UDP
   `15005` is free.

   From another network, dial `<public ip>:15005:<certhash>` with a KPS client (`@kpstreams/quic-client` or
   `@kpstreams/webrtc-client`) and request `GET /health` and `GET /topology`.

6. Publish the address in the registry. `updateMetadataUrl` is self-service: your registered node address sends
   it, which is the key in `NOX__ETH_WALLET_PRIVATE_KEY` (`grep 'Address (for registration)' .env` shows the
   address). Import that key into an encrypted Foundry keystore once (the command prompts for it, so it stays
   out of your shell history):

   ```bash
   cast wallet import nox-node --interactive
   ```

   A relay sends the transaction while it runs. An exit sends transactions from this key itself, so it follows
   the same order as [Claiming Exit Credit](#claiming-exit-credit): wait for `nox_eth_tx_pending 0`, stop the
   node, send, then start it again, so the node reads the new nonce:

   ```bash
   # Exits only, before sending:
   curl --fail --silent http://127.0.0.1:15001/metrics | grep '^nox_eth_tx_pending '   # wait until it reads 0
   docker compose stop nox

   cast send 0xF7BFf88A1412054a001Dc4b8aCBddAd6F9b26cB6 "updateMetadataUrl(string)" \
     "kps:<public ip>:15005:<certhash>/metadata.json" \
     --account nox-node --rpc-url https://sepolia-rollup.arbitrum.io/rpc
   cast call 0xF7BFf88A1412054a001Dc4b8aCBddAd6F9b26cB6 "relayers(address)(bytes32,string,string,string,uint256,uint256,bool,uint8,bool)" \
     <node address> --rpc-url https://sepolia-rollup.arbitrum.io/rpc   # 4th value is your metadataUrl

   # Exits only, after the receipt:
   docker compose up -d
   ```

   Publish once step 5 succeeds from another network. `scripts/change_ip.py` with your current IP sends the same
   transaction after checking the running sidecar (see [Changing Your IP](#changing-your-ip)).

   The certhash is the stable part of your address: keep the `nox-kps-identity` volume and its backup. The IP can
   move whenever you need it to; clients look the address up in the registry.

### Worker Bundles (optional)

`nox-kps` can serve the anon-rpc worker bundle as a `kps:` resolver. The service mounts the bundle volume
read-only; `nox-kps-admin` adds a file under its keccak-256 name and prints the resolver string:

```bash
docker compose run --rm -v "$PWD/anon-rpc-worker.js:/in/anon-rpc-worker.js:ro" \
  nox-kps-admin bundle add /in/anon-rpc-worker.js
docker compose run --rm nox-kps-admin bundle list
```

The running sidecar picks up new bundles within a minute.

### Stop Serving KPS

Clear the published address first (exits stop the node around the transaction, as in step 6), then stop the
sidecar. The identity volume stays, so re-enabling later keeps the same address:

```bash
cast send 0xF7BFf88A1412054a001Dc4b8aCBddAd6F9b26cB6 "updateMetadataUrl(string)" "" \
  --account nox-node --rpc-url https://sepolia-rollup.arbitrum.io/rpc
docker compose stop nox-kps
docker compose rm -f nox-kps nox-kps-init nox-kps-preflight
```

Then remove `kps` from `COMPOSE_PROFILES` in `.env`. Setting `ingress_port = 0` and `topology_api_port = 0`
again is optional and takes a node restart.

### Host Firewall

KPS needs inbound UDP `15005` from anywhere, in two places.

**Cloud firewall.** Allow UDP `15005` from `0.0.0.0/0` (and `::/0` on IPv6 hosts). For an AWS security group:

```bash
aws ec2 authorize-security-group-ingress --group-id <security group id> \
  --ip-permissions 'IpProtocol=udp,FromPort=15005,ToPort=15005,IpRanges=[{CidrIp=0.0.0.0/0,Description=nox-kps}]'
```

**Host firewall.** Open UDP `15005`, keep TCP `15002`-`15004` and `15006` reachable from the host only, and
limit how fast one source can open new KPS sessions (20 per second, burst 40; sessions already open keep
flowing). With `ufw` (default deny incoming), add the limit to `/etc/ufw/before.rules`, inside the `*filter`
section before `COMMIT`:

```text
-A ufw-before-input -p udp --dport 15005 -m conntrack --ctstate NEW -m hashlimit --hashlimit-mode srcip --hashlimit-above 20/second --hashlimit-burst 40 --hashlimit-name nox-kps-new -j DROP
```

```bash
# keep your existing SSH rule
sudo ufw allow 15005/udp
sudo ufw allow 15000/tcp
sudo ufw allow 15001/tcp
sudo ufw reload
sudo ufw status numbered   # lists SSH, 15000/tcp, 15001/tcp and 15005/udp
```

With plain `iptables`, put the rules in a script that a systemd oneshot unit runs at boot, so they persist:

```bash
iptables -N NOX-FW 2>/dev/null || iptables -F NOX-FW
iptables -A NOX-FW -i lo -j RETURN
iptables -A NOX-FW -p udp --dport 15005 -m conntrack --ctstate NEW -m hashlimit --hashlimit-mode srcip \
  --hashlimit-above 20/second --hashlimit-burst 40 --hashlimit-name nox-kps-new -j DROP
iptables -A NOX-FW -p udp --dport 15005 -j ACCEPT
iptables -A NOX-FW -p tcp -m multiport --dports 15002,15003,15004,15006 -j DROP
iptables -C INPUT -j NOX-FW 2>/dev/null || iptables -I INPUT 1 -j NOX-FW
```

Repeat with `ip6tables` on hosts with IPv6. From another network, check that UDP `15005` answers a KPS dial and
that TCP `15002`, `15003` and `15006` refuse connections.

## Changing Your IP

Your node's identity (registered address, Sphinx key, role) lives in `NoxRegistry` and stays the same. Its
location is two registry fields that your node key writes itself, at any time, without governance:

| Field | Value | Setter |
|---|---|---|
| `url` | `/ip4/<ip>/tcp/15000/p2p/<peer id>` | `updateUrl(string)` |
| `metadataUrl` (KPS entries) | `kps:<ip>:15005:<certhash>/metadata.json` | `updateMetadataUrl(string)` |

`scripts/change_ip.py` moves both to a new IP. It keeps the peer ID, ports and certhash and replaces only the IP.
By default it is a dry run: it prints both values, the calldata, a gas estimate and what clients will see, and
signs nothing. `--send` signs with your node key. The tool needs Foundry `cast` and reads the key from your
`.env` (`NOX__ETH_WALLET_PRIVATE_KEY`) or from a hex key file with mode `600`, and hands it to `cast` through a
private terminal, so it stays off the command line, the environment and the output.

1. Give the host its new IP. The node listens on all interfaces and keeps running.
2. KPS entries: set the new IP in `nox-kps.toml` and recreate the sidecar, which re-runs its preflight:

   ```bash
   sed -i 's/^advertise = .*/advertise = ["<new ip>"]/' nox-kps.toml
   docker compose up -d --force-recreate nox-kps
   ```

3. Dry run from the run-nox directory (it reads `deployment.json`, `config.toml` and `nox-kps.toml` there):

   ```bash
   python3 scripts/change_ip.py --ip <new ip> --key-file .env
   ```

   For a KPS entry it checks that `nox-kps.toml` advertises the new IP, that the running sidecar serves
   `<new ip>:15005:<certhash>` (`nox-kps address`) and that `nox-kps healthcheck --kps` succeeds (a QUIC dial of
   the listener with your certhash and `GET /health`). A node without a KPS entry passes `--no-kps`.

4. Send:

   ```bash
   python3 scripts/change_ip.py --ip <new ip> --key-file .env --send
   ```

   The tool sends `updateUrl` and then `updateMetadataUrl` (up to about 130,000 gas each on Arbitrum Sepolia),
   checks every receipt, event and read-back, and confirms that `topologyFingerprint()` and `relayerCount()` are
   unchanged: both cover membership, which an IP change keeps. Values already on chain are skipped, so a second
   run continues where the first stopped. An exit sends paid transactions from the same key, so for an exit the
   tool waits until `nox_eth_tx_pending` reads `0`, stops the node, sends, and starts the node again so it reads
   the new nonce.

5. From another network, dial `<new ip>:15005:<certhash>` with a KPS client (KPS Entry step 5).

What clients see afterwards:

- Peers apply the `RelayerUpdated` event through their chain observer and dial the new `url`.
- Wallets with S1 discovery read the registry through the mixnet (two exits, two RPC providers, one finalized
  block) and adopt the new location at their next check, every 10 minutes by default. They keep it as a learned
  entry for later starts.
- Worker bundles with a pinned snapshot (before S1) use the new location from the next bundle release.
- Your identity, and with it your probation status, stays as it is.

The tool prints the command that moves the node back (`--ip <old ip> --send`). A node registered with a DNS
multiaddr (`/dns4/<name>/...`) moves by updating its A record; the tool then updates only `metadataUrl`.

## Running a Bridge

A bridge helps people whose network blocks the published Nox entries. It is a second `nox-kps` on an extra public
IP of your host, with its own identity, that stays out of the registry. You share its address with the people
it serves, and their wallet dials only bridges (`bridges` in the anon-rpc worker config, S1). Traffic through a
bridge enters the mixnet at your node exactly like traffic through your published entry, and the bridge serves
the same worker bundles, so people can fetch the worker through it as well.

Any relay or exit can run one. You need the node ingress on loopback (KPS Entry step 1) and a second public IP on
the host. The published `kps` profile is optional.

1. Add the extra IP. On AWS, assign a secondary private IP to the instance's network interface and associate a
   second Elastic IP with it:

   ```bash
   aws ec2 assign-private-ip-addresses --network-interface-id <eni id> --secondary-private-ip-address-count 1
   aws ec2 allocate-address --domain vpc
   aws ec2 associate-address --allocation-id <new allocation id> --network-interface-id <eni id> \
     --private-ip-address <secondary private ip>
   ip -brief addr   # the interface lists the secondary private IP
   ```

   If `ip -brief addr` lacks the secondary address, add it with `sudo ip addr add <secondary private ip>/<prefix>
   dev <interface>` and persist it in your network configuration.

2. Create the bridge config. `listen` binds the host address for the bridge IP, so the bridge answers only there:

   ```bash
   cp configs/nox-kps-bridge.toml nox-kps-bridge.toml
   sed -i 's/"203.0.113.20"/"<extra public IP>"/' nox-kps-bridge.toml
   sed -i 's/^listen = .*/listen = "<secondary private ip>:15007"/' nox-kps-bridge.toml
   sed -i 's/^node_address = ""/node_address = "<your node address>"/' nox-kps-bridge.toml
   ```

   On a host whose extra public IP sits directly on an interface, use that IP in `listen`. The bridge keeps UDP
   `15007`, admin `127.0.0.1:15008` and your registered node address in `node_address` (wallets map the bridge to
   your node through its `/metadata.json`); `nox-kps-bridge-preflight` checks all three.

3. Enable the profile, create the bridge identity, back it up and record its certhash:

   ```bash
   if grep -q '^COMPOSE_PROFILES=' .env; then
     sed -i 's/^COMPOSE_PROFILES=\(.*\)$/COMPOSE_PROFILES=\1,kps-bridge/' .env
   else
     echo 'COMPOSE_PROFILES=kps-bridge' >> .env
   fi
   docker compose run --rm nox-kps-bridge-admin init
   (umask 077; docker compose run --rm -T --entrypoint cat nox-kps-bridge-admin /var/lib/nox-kps/kps.key > kps-bridge.key.backup)
   sed -i 's/^expected_certhash = ""/expected_certhash = "<bridge certhash>"/' nox-kps-bridge.toml
   docker compose run --rm nox-kps-bridge-admin check-config
   ```

4. Open UDP `15007` in the cloud firewall, and in the host firewall next to the KPS rules (see Host Firewall). With
   `iptables`, before the final `INPUT` jump:

   ```bash
   iptables -A NOX-FW -p udp --dport 15007 ! -d <secondary private ip> -j DROP
   iptables -A NOX-FW -p udp --dport 15007 -m conntrack --ctstate NEW -m hashlimit --hashlimit-mode srcip \
     --hashlimit-above 20/second --hashlimit-burst 40 --hashlimit-name nox-kps-bridge-new -j DROP
   iptables -A NOX-FW -p udp --dport 15007 -j ACCEPT
   iptables -A NOX-FW -p tcp --dport 15008 -j DROP
   ```

   The first rule keeps the bridge reachable on its own address only, which matters when `listen` is
   `0.0.0.0:15007`.

5. Start it and read the address:

   ```bash
   docker compose up -d
   docker compose ps nox-kps-bridge                     # healthy
   docker compose run --rm nox-kps-bridge-admin address  # address: <extra public IP>:15007:<certhash>
   ```

   `address` also prints a `metadataUrl` line. A bridge keeps it to itself: publish only your entry's address.

6. Check from another network with a KPS client (`GET /health` on `<extra public IP>:15007:<certhash>`), then share
   the address privately with the people it serves. Their wallet configuration for the anon-rpc worker:

   ```json
   { "bridges": ["<extra public IP>:15007:<certhash>"] }
   ```

Bridges serve wallets for nodes in the worker bundle's snapshot; a node that registered later can serve bridges
from the next worker bundle release.

If a censor blocks the bridge IP, associate a fresh Elastic IP with the secondary private IP, update `advertise`,
run `docker compose up -d --force-recreate nox-kps-bridge` and share the new address. For a new certhash as well,
stop the bridge, remove its `nox-kps-bridge-identity` volume and run `init` again.

## Discovery: Default Anchors and Probation

Wallets with S1 discovery take identity from the chain and look locations up at run time. A wallet starts by
dialing an anchor (wallet `bridges`, else wallet `gateways`, then the default anchors, learned entries and the
bundle snapshot), routes as soon as one answers, and then checks the registry through the mixnet: two exits ask
two public Arbitrum Sepolia RPC providers for the same finalized block, and the wallet uses the answer when both
agree exactly. That check is how wallets learn about new members, new IPs and removals.

### Default Anchors

The S1 worker bundle carries three default anchors, Hisoka entry nodes on Elastic IPs. A wallet whose worker
config is empty (`{}`) starts from them:

| Node | KPS address |
|---|---|
| nox-1 | `100.56.0.72:15005:uEiBVDwIs40bsslDkM-BYb2AOHw3PHe70_bj5U_09r7vdIQ` |
| nox-2 | `3.232.137.146:15005:uEiDGVPDwsQ96ri9T5WLR6jZov_9LW-gRAgs-DN9FyKuHuw` |
| nox-8 | `18.215.18.61:15005:uEiCStd3rfGTo0ts0lSUw5f22u93O3PLCZVWWQIv_MXHm7w` |

Anchors provide reachability: wallets treat what an anchor serves as hints and take membership from the
registry check, with the bundle snapshot as the floor. Every operator entry with a published KPS address becomes
an entry candidate straight from that check. With `node_address` set (KPS Entry step 2), wallets can also list your
entry under `gateways` (`{"gateways": ["<ip>:15005:<certhash>"]}`) once your node is in a worker bundle's snapshot.

### Probation for New Nodes

A node that joins after the bundle's snapshot is on probation for 14 days, counted from when each wallet first
sees it in the registry. Wallets place at most one probation node on each route, so every three-hop route keeps at
least two snapshot members; a layer that has only probation nodes still uses them. Probation ends after 14 days,
or earlier when a bundle release adds the node to its snapshot. Changing your IP keeps your identity and your
probation clock.

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

### Pinning a Node Release

Node images are built by the `hisoka-io/nox` release workflow from a version tag; the manifest pins one by
digest. A release that ships `nox-kps` also records its `nox-kps` version, which turns on the Compose `kps`
profile. Maintainers pin a release by regenerating the manifest from itself with the new digest:

```bash
NEW_IMAGE='ghcr.io/hisoka-io/nox@sha256:<digest of the release tag>'
docker pull "$NEW_IMAGE"
docker run --rm "$NEW_IMAGE" nox-kps --version   # a release that ships nox-kps prints "nox-kps X.Y.Z"
python3 scripts/make-deployment-manifest.py configs/arbitrum-sepolia.deployment.json \
  --nox-image "$NEW_IMAGE" --preflight-image "$NOX_PREFLIGHT_IMAGE" \
  --nox-kps-version X.Y.Z \
  --check-rpc https://arbitrum-sepolia-rpc.publicnode.com \
  --out configs/arbitrum-sepolia.deployment.json
bash scripts/test-preflight.sh
```

Pass `--nox-kps-version` only with the version the image printed. Update the image digest in "Current
Deployment" and `.env.example` in the same change. CI then runs `nox keygen`, `check-config` and, for a release
with `release.noxKps`, `nox-kps --version` and `nox-kps check-config` on the pinned image.

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
| `15007/udp` | Bridge (`nox-kps-bridge`) | The extra bridge IP only, profile `kps-bridge` |
| `15008/tcp` | Bridge metrics and health | Bound to `127.0.0.1`, profile `kps-bridge` only |

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
# KPS entry only:
docker compose ps nox-kps
docker compose logs --tail 100 nox-kps
curl --fail --silent http://127.0.0.1:15006/metrics | grep '^nox_kps_connections'
```

`nox-kps` logs counts only (connections, streams, rejections) and serves Prometheus metrics on
`127.0.0.1:15006`. Watch `nox_kps_connections_rejected_total`, `nox_kps_rate_limited_total` and
`nox_kps_upstream_errors_total`; a rising upstream error count means the node's ingress needs attention.

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

`nox-kps` runs from the same `NOX_IMAGE` as the node, so `docker compose up -d` after a pin upgrades both. Keep
the `nox-kps-identity` volume across upgrades and rollbacks: it holds the key behind your published KPS address.

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
