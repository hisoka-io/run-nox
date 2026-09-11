#!/usr/bin/env bash
# Generate NOX node private keys using OpenSSL.
#
# This script generates random private keys for a NOX node.
# For full key generation including public key derivation and PeerId,
# use the Docker-based approach instead:
#
#   docker run --rm "$NOX_IMAGE" keygen > .env
#
# This script is a fallback for environments without Docker.

set -euo pipefail

generate_secp256k1_key() {
  local key
  local order="fffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364141"
  while true; do
    key="$(openssl rand -hex 32)"
    if [[ ! "$key" =~ ^0+$ && "$key" < "$order" ]]; then
      printf '%s\n' "$key"
      return
    fi
  done
}

echo "# NOX Node Keys"
echo "# Generated: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "# SAVE THIS OUTPUT. Private keys cannot be recovered."
echo "#"
echo "# To derive public keys (sphinx key, PeerId, ETH address),"
echo '# use: docker run --rm "$NOX_IMAGE" keygen'
echo ""
echo "# === Sphinx Routing Key (X25519) ==="
echo "NOX__ROUTING_PRIVATE_KEY=$(openssl rand -hex 32)"
echo ""
echo "# === P2P Identity (Ed25519) ==="
echo "NOX__P2P_PRIVATE_KEY=$(openssl rand -hex 32)"
echo ""
echo "# === ETH Wallet (secp256k1) ==="
echo "# Required for exit nodes. Relay nodes can leave empty."
echo "NOX__ETH_WALLET_PRIVATE_KEY=$(generate_secp256k1_key)"
