#!/usr/bin/env python3
"""Preflight for the optional nox-kps sidecar (Compose profile "kps").

usage: preflight_kps.py <nox-kps.toml> <config.toml> <deployment.json> <nox-image> [--bridge]

nox-kps ships in the node image and runs from the same digest as the node.
Checks, without network access and without reading any secret:
  - the image is the release record's immutable ghcr.io/hisoka-io/nox@sha256
    digest, and the record names the nox-kps version that image ships
    (release.noxKps)
  - nox-kps.toml uses only keys nox-kps knows (flat schema; nox-kps refuses others)
  - listen is a wildcard UDP socket address and advertise lists only public IPs
    (clients dial these addresses directly; there is no DNS)
  - key_file lives in the nox-kps-identity volume and expected_certhash holds the
    certhash `nox-kps init` printed (nox-kps run serves only that identity)
  - upstream_ingress and upstream_topology are bare 127.0.0.1:<port> on the node's
    enabled ingress_port and topology_api_port (or metrics_port, which also serves /topology)
  - client_ip_header is a valid header name and equals the node's
    [ingress] client_ip_header, so per-IP rate limits see the real client address
  - admin_listen is loopback-only
  - the UDP port is free, or held by the nox-kps UID itself (a restart)

With --bridge (Compose profile "kps-bridge", README "Running a Bridge") the file is
nox-kps-bridge.toml for a second, unpublished nox-kps on an extra public IP:
  - listen may also bind one address of this host (the private address that the extra
    public IP maps to), so the bridge answers only there
  - its UDP and admin ports differ from the published entry's (15005, 15006)
  - node_address stays empty, so /metadata.json does not name the node

Set ingress_port, topology_api_port and [ingress] client_ip_header in config.toml:
this check reads the file, not NOX__ environment overrides.
"""
from __future__ import annotations

import ipaddress
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import NoReturn

NOX_IMAGE = re.compile(r"^ghcr\.io/hisoka-io/nox@sha256:[0-9a-f]{64}$")
VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")
HEADER = re.compile(r"^[a-z0-9!#$%&'*+.^_`|~-]+$")
# nox-kps RawConfig (deny_unknown_fields): top-level keys and the two tables.
KEYS = {
    "listen", "advertise", "allow_private_advertise", "key_file", "expected_certhash", "node_address",
    "upstream_ingress", "upstream_topology", "client_ip_header", "keccak_dir", "admin_listen",
    "log_level", "log_format", "summary_interval_secs", "limits", "shutdown",
}
TABLES = {"limits", "shutdown"}
CERTHASH = re.compile(r"^uEi[A-Za-z0-9_-]{44}$")
NODE_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
IDENTITY_DIR = "/var/lib/nox-kps/"
BUNDLE_DIR = "/var/lib/nox-kps/keccak"
INIT_HINT = (
    "run `docker compose run --rm nox-kps-admin init` once, back up the key, then copy the printed "
    "expected_certhash line into nox-kps.toml (README \"KPS Entry\")"
)
BRIDGE_INIT_HINT = (
    "run `docker compose run --rm nox-kps-bridge-admin init` once, back up the key, then copy the printed "
    "expected_certhash line into nox-kps-bridge.toml (README \"Running a Bridge\")"
)
NOX_KPS_UID = 10002
# The published entry's ports (configs/nox-kps.toml); a bridge uses its own.
ENTRY_UDP_PORT = 15005
ENTRY_ADMIN_PORT = 15006
PROC_NET = (Path("/proc/net/udp"), Path("/proc/net/udp6"))


def fail(message: str) -> NoReturn:
    print(f"nox-kps preflight: {message}", file=sys.stderr)
    raise SystemExit(1)


def load(path: Path, label: str) -> dict[str, object]:
    try:
        with path.open("rb") as handle:
            value = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        fail(f"{label} cannot be parsed: {error}")
    return value


def release_kps_version(deployment_path: Path, image: str) -> str:
    """The nox-kps version the pinned node image ships, from the release record."""
    try:
        deployment = json.loads(deployment_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        fail(f"deployment.json cannot be parsed: {error}")
    release = deployment.get("release") if isinstance(deployment, dict) else None
    if not isinstance(release, dict):
        fail("deployment.json has no release object; copy configs/arbitrum-sepolia.deployment.json again")
    if NOX_IMAGE.fullmatch(image) is None:
        fail(f"NOX_IMAGE must be an immutable ghcr.io/hisoka-io/nox@sha256:<digest> reference (got {image!r})")
    if image != release.get("noxImage"):
        fail("NOX_IMAGE must equal deployment.json release.noxImage: nox-kps runs from the node's release image")
    version = release.get("noxKps")
    if version is None:
        fail(
            "deployment.json release.noxKps is unset. The KPS entry runs from a node release that ships "
            "nox-kps, and its release record names the nox-kps version in release.noxKps. Update run-nox to "
            "such a release, copy the manifest to deployment.json again, then enable the kps profile "
            '(README "KPS Entry")'
        )
    if not isinstance(version, str) or VERSION.fullmatch(version) is None:
        fail(f"deployment.json release.noxKps must be a version such as \"0.1.0\" (got {version!r})")
    return version


def table(config: dict[str, object], name: str) -> dict[str, object]:
    value = config.get(name, {})
    if not isinstance(value, dict):
        fail(f"[{name}] must be a table")
    return value


def socket_address(value: object, field: str) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, int]:
    if not isinstance(value, str):
        fail(f"{field} must be a string such as \"0.0.0.0:15005\"")
    match = re.fullmatch(r"\[([0-9a-fA-F:.]+)\]:([0-9]{1,5})|([0-9.]+):([0-9]{1,5})", value)
    if match is None:
        fail(f"{field} must be <ipv4>:<port> or [<ipv6>]:<port> (got {value!r})")
    host = match.group(1) or match.group(3)
    port = int(match.group(2) or match.group(4))
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        fail(f"{field} has an invalid IP address (got {host!r})")
    if not 1 <= port <= 65535:
        fail(f"{field} port must be in 1..=65535 (got {port})")
    return ip, port


def loopback_host_port(value: object, field: str, example: str) -> int:
    if not isinstance(value, str):
        fail(f"{field} must be a string such as \"{example}\"")
    if "://" in value:
        fail(f"{field} must be a bare host:port such as \"{example}\", without a scheme (got {value!r})")
    # The node binds its ingress and topology ports on 0.0.0.0 (IPv4), so the
    # upstreams use the IPv4 loopback address itself, not a name.
    match = re.fullmatch(r"127\.0\.0\.1:([0-9]{1,5})", value)
    if match is None or not 1 <= int(match.group(1)) <= 65535:
        fail(
            f"{field} must be 127.0.0.1:<port> (the node listens on IPv4 and trusts the client IP header on "
            f"loopback only, and metrics stay on the host; got {value!r})"
        )
    return int(match.group(1))


def node_port(node: dict[str, object], field: str) -> int:
    value = node.get(field, 0)
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 65535:
        fail(f"config.toml {field} must be a port number")
    return value


def udp_port_owners(port: int) -> set[int]:
    """UIDs holding a UDP socket on <port> in this network namespace (host networking)."""
    owners: set[int] = set()
    for path in PROC_NET:
        try:
            lines = path.read_text(encoding="ascii").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 8:
                continue
            local_port = int(fields[1].rsplit(":", 1)[1], 16)
            if local_port == port:
                owners.add(int(fields[7]))
    return owners


def main(argv: list[str]) -> None:
    bridge = argv[5:] == ["--bridge"]
    if len(argv) != 5 and not bridge:
        fail("usage: preflight_kps.py <nox-kps.toml> <config.toml> <deployment.json> <nox-image> [--bridge]")
    label = "nox-kps-bridge.toml" if bridge else "nox-kps.toml"
    kps_version = release_kps_version(Path(argv[3]), argv[4])
    kps_config = load(Path(argv[1]), label)
    node = load(Path(argv[2]), "config.toml")

    unknown = set(kps_config) - KEYS
    if unknown:
        fail(
            f"nox-kps.toml has keys nox-kps does not know: {sorted(unknown)} "
            "(start from configs/nox-kps.toml; the schema is flat: listen, advertise, key_file, ...)"
        )
    for name in TABLES & set(kps_config):
        table(kps_config, name)
    for name in set(kps_config) - TABLES:
        if isinstance(kps_config[name], dict):
            fail(f"nox-kps.toml {name} must be a value, not a [{name}] table")

    listen_ip, kps_port = socket_address(kps_config.get("listen", "[::]:15005"), "listen")
    if bridge:
        if listen_ip.is_loopback or listen_ip.is_multicast:
            fail("bridge listen must be 0.0.0.0, [::] or this host's address for the bridge IP (such as \"10.0.1.23:15007\")")
        if kps_port == ENTRY_UDP_PORT:
            fail(f"bridge listen uses UDP {ENTRY_UDP_PORT}, the published entry's port: give the bridge its own, such as 15007")
    elif not listen_ip.is_unspecified:
        fail("listen must bind 0.0.0.0 or [::]: cloud public IPs are NATed and not on any interface")
    if kps_config.get("allow_private_advertise", False) is not False:
        fail("allow_private_advertise is for local test beds; remove it and list your public IP in advertise")
    advertise = kps_config.get("advertise")
    if not isinstance(advertise, list) or not advertise:
        fail("advertise must list this host's public IP: clients dial <ip>:<port>:<certhash> directly")
    for raw in advertise:
        try:
            ip = ipaddress.ip_address(raw) if isinstance(raw, str) else None
        except ValueError:
            ip = None
        if ip is None or not ip.is_global:
            fail(f"advertise entry {raw!r} is not a public IP address")
    key_file = kps_config.get("key_file", IDENTITY_DIR + "kps.key")
    if (
        not isinstance(key_file, str)
        or not key_file.startswith(IDENTITY_DIR)
        or key_file.startswith(BUNDLE_DIR)
        or ".." in key_file
    ):
        fail(f"key_file must be inside {IDENTITY_DIR} (the nox-kps-identity volume)")
    expected = kps_config.get("expected_certhash", "")
    if not isinstance(expected, str) or expected.strip() == "":
        fail(f"expected_certhash is empty, so nox-kps would refuse to run: {BRIDGE_INIT_HINT if bridge else INIT_HINT}")
    if CERTHASH.fullmatch(expected) is None:
        fail(f"expected_certhash must be the certhash `nox-kps init` printed (\"uEi\" + 44 characters; got {expected!r})")
    node_address = kps_config.get("node_address", "")
    if bridge and node_address != "":
        fail("bridge node_address must stay empty: a bridge is unpublished and its /metadata.json does not name the node")
    if not isinstance(node_address, str) or (node_address and NODE_ADDRESS.fullmatch(node_address) is None):
        fail(f"node_address must be empty or your 0x-prefixed registered node address (got {node_address!r})")
    keccak_dir = kps_config.get("keccak_dir", BUNDLE_DIR)
    if keccak_dir not in ("", BUNDLE_DIR):
        fail(f"keccak_dir must be {BUNDLE_DIR!r} (the nox-kps-bundles volume) or \"\" (got {keccak_dir!r})")

    ingress_port = node_port(node, "ingress_port")
    topology_port = node_port(node, "topology_api_port")
    metrics_port = node_port(node, "metrics_port")
    if ingress_port == 0:
        fail("config.toml ingress_port is 0: set ingress_port = 15002 (keep it closed to the internet, see README)")
    if loopback_host_port(kps_config.get("upstream_ingress", "127.0.0.1:15002"), "upstream_ingress", "127.0.0.1:15002") != ingress_port:
        fail(f"upstream_ingress must use the node's ingress_port {ingress_port}")
    topology_upstream = loopback_host_port(
        kps_config.get("upstream_topology", "127.0.0.1:15003"), "upstream_topology", "127.0.0.1:15003"
    )
    if topology_upstream not in {p for p in (topology_port, metrics_port) if p}:
        fail(
            f"upstream_topology port {topology_upstream} serves no topology: use topology_api_port "
            f"({topology_port or 'disabled'}) or metrics_port ({metrics_port})"
        )

    header = kps_config.get("client_ip_header", "x-real-ip")
    if not isinstance(header, str) or HEADER.fullmatch(header) is None:
        fail(f"client_ip_header must be a lowercase header name such as \"x-real-ip\" (got {header!r})")
    node_header = table(node, "ingress").get("client_ip_header", "")
    if node_header != header:
        fail(
            f"config.toml [ingress] client_ip_header is {node_header!r} but nox-kps sends {header!r}: "
            "set both to the same name so the node rate-limits each client, not the sidecar"
        )

    admin_port = loopback_host_port(kps_config.get("admin_listen", "127.0.0.1:15006"), "admin_listen", "127.0.0.1:15006")
    if admin_port in {ingress_port, topology_port, metrics_port}:
        fail(f"admin_listen port {admin_port} collides with a node port")
    if bridge and admin_port == ENTRY_ADMIN_PORT:
        fail(f"bridge admin_listen uses {ENTRY_ADMIN_PORT}, the published entry's admin port: use 127.0.0.1:15008")
    if kps_port in {node_port(node, "p2p_port"), ingress_port, topology_port, metrics_port}:
        fail(f"listen port {kps_port} collides with a node port")

    owners = udp_port_owners(kps_port)
    foreign = owners - {NOX_KPS_UID}
    if foreign:
        fail(f"UDP {kps_port} is already in use by UID(s) {sorted(foreign)}; nox-kps needs it")

    print(
        f"nox-kps {kps_version} {'bridge ' if bridge else ''}preflight passed: UDP {kps_port} on {', '.join(map(str, advertise))}, certhash {expected.strip()}, "
        f"upstreams 127.0.0.1:{ingress_port}/{topology_upstream}, client IP header {header}"
    )


if __name__ == "__main__":
    main(sys.argv)
