#!/usr/bin/env python3
"""Preflight for the optional nox-kps sidecar (Compose profile "kps").

usage: preflight_kps.py <nox-kps.toml> <config.toml> <nox-kps-image>

Checks, without network access and without reading any secret:
  - the image is an immutable ghcr.io/hisoka-io/nox-kps@sha256 digest
  - kps.listen is a valid UDP socket address and kps.public_ips lists only public IPs
    (clients dial these addresses directly; there is no DNS)
  - the identity key lives in the nox-kps-identity volume
  - upstreams point at 127.0.0.1 on the node's enabled ingress_port and
    topology_api_port (or metrics_port, which also serves /topology)
  - proxy.client_ip_header is a valid header name and equals the node's
    [ingress] client_ip_header, so per-IP rate limits see the real client address
  - the metrics endpoint is loopback-only
  - the UDP port is free, or held by the nox-kps UID itself (a restart)

Set ingress_port, topology_api_port and [ingress] client_ip_header in config.toml:
this check reads the file, not NOX__ environment overrides.
"""
from __future__ import annotations

import ipaddress
import re
import sys
import tomllib
from pathlib import Path
from typing import NoReturn
from urllib.parse import urlsplit

KPS_IMAGE = re.compile(r"^ghcr\.io/hisoka-io/nox-kps@sha256:[0-9a-f]{64}$")
HEADER = re.compile(r"^[a-z0-9!#$%&'*+.^_`|~-]+$")
SECTIONS = {"kps", "upstreams", "routes", "proxy", "limits", "bundles", "metrics", "log", "shutdown"}
IDENTITY_DIR = "/var/lib/nox-kps/"
NOX_KPS_UID = 10002
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


def loopback_upstream(value: object, field: str) -> int:
    if not isinstance(value, str):
        fail(f"{field} must be a URL such as \"http://127.0.0.1:15002\"")
    parts = urlsplit(value)
    if parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "::1"} or parts.port is None:
        fail(f"{field} must be http://127.0.0.1:<port> (the node trusts the client IP header on loopback only; got {value!r})")
    if parts.path not in {"", "/"} or parts.query or parts.fragment:
        fail(f"{field} must not carry a path (got {value!r})")
    return parts.port


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
    if len(argv) != 4:
        fail("usage: preflight_kps.py <nox-kps.toml> <config.toml> <nox-kps-image>")
    kps_config = load(Path(argv[1]), "nox-kps.toml")
    node = load(Path(argv[2]), "config.toml")
    image = argv[3]
    if KPS_IMAGE.fullmatch(image) is None:
        fail(f"NOX_KPS_IMAGE must be an immutable ghcr.io/hisoka-io/nox-kps@sha256:<digest> reference (got {image!r})")

    unknown = set(kps_config) - SECTIONS
    if unknown:
        fail(f"nox-kps.toml has unknown sections: {sorted(unknown)}")

    kps = table(kps_config, "kps")
    listen_ip, kps_port = socket_address(kps.get("listen", "[::]:15005"), "kps.listen")
    if not listen_ip.is_unspecified:
        fail("kps.listen must bind 0.0.0.0 or [::]: cloud public IPs are NATed and not on any interface")
    public_ips = kps.get("public_ips")
    if not isinstance(public_ips, list) or not public_ips:
        fail("kps.public_ips must list this host's public IP: clients dial <ip>:<port>:<certhash> directly")
    for raw in public_ips:
        try:
            ip = ipaddress.ip_address(raw) if isinstance(raw, str) else None
        except ValueError:
            ip = None
        if ip is None or not ip.is_global:
            fail(f"kps.public_ips entry {raw!r} is not a public IP address")
    identity = kps.get("identity_key_file", IDENTITY_DIR + "kps.key")
    if not isinstance(identity, str) or not identity.startswith(IDENTITY_DIR) or ".." in identity:
        fail(f"kps.identity_key_file must be inside {IDENTITY_DIR} (the nox-kps-identity volume)")

    ingress_port = node_port(node, "ingress_port")
    topology_port = node_port(node, "topology_api_port")
    metrics_port = node_port(node, "metrics_port")
    if ingress_port == 0:
        fail("config.toml ingress_port is 0: set ingress_port = 15002 (keep it closed to the internet, see README)")
    upstreams = table(kps_config, "upstreams")
    if loopback_upstream(upstreams.get("ingress", "http://127.0.0.1:15002"), "upstreams.ingress") != ingress_port:
        fail(f"upstreams.ingress must use the node's ingress_port {ingress_port}")
    topology_upstream = loopback_upstream(upstreams.get("topology", "http://127.0.0.1:15003"), "upstreams.topology")
    if topology_upstream not in {p for p in (topology_port, metrics_port) if p}:
        fail(
            f"upstreams.topology port {topology_upstream} serves no topology: use topology_api_port "
            f"({topology_port or 'disabled'}) or metrics_port ({metrics_port})"
        )

    header = table(kps_config, "proxy").get("client_ip_header", "x-forwarded-for")
    if not isinstance(header, str) or HEADER.fullmatch(header) is None:
        fail(f"proxy.client_ip_header must be a lowercase header name such as \"x-real-ip\" (got {header!r})")
    node_header = table(node, "ingress").get("client_ip_header", "")
    if node_header != header:
        fail(
            f"config.toml [ingress] client_ip_header is {node_header!r} but nox-kps sends {header!r}: "
            "set both to the same name so the node rate-limits each client, not the sidecar"
        )

    metrics_listen = table(kps_config, "metrics").get("listen", "127.0.0.1:15006")
    if metrics_listen != "":
        metrics_ip, metrics_port_kps = socket_address(metrics_listen, "metrics.listen")
        if not metrics_ip.is_loopback:
            fail("metrics.listen must be a loopback address (127.0.0.1)")
        if metrics_port_kps in {ingress_port, topology_port, metrics_port}:
            fail(f"metrics.listen port {metrics_port_kps} collides with a node port")
    if kps_port in {node_port(node, "p2p_port"), ingress_port, topology_port, metrics_port}:
        fail(f"kps.listen port {kps_port} collides with a node port")

    owners = udp_port_owners(kps_port)
    foreign = owners - {NOX_KPS_UID}
    if foreign:
        fail(f"UDP {kps_port} is already in use by UID(s) {sorted(foreign)}; nox-kps needs it")

    print(
        f"nox-kps preflight passed: UDP {kps_port} on {', '.join(map(str, public_ips))}, "
        f"upstreams 127.0.0.1:{ingress_port}/{topology_upstream}, client IP header {header}"
    )


if __name__ == "__main__":
    main(sys.argv)
