#!/usr/bin/env python3
"""Move this node to a new public IP: publish the new location in NoxRegistry.

usage: change_ip.py --ip <new IPv4> (--key-file <path> | --address <0x...>)
                    [--deployment deployment.json] [--rpc-url <url>] [--config config.toml]
                    [--kps-config nox-kps.toml | --no-kps] [--kps-exec "docker compose exec -T nox-kps"]
                    [--compose "docker compose"] [--metrics-url http://127.0.0.1:15001/metrics]
                    [--allow-certhash-change] [--send]

A node's identity (address, Sphinx key, role) lives in NoxRegistry and stays the same. Its
location is two self-service registry fields that only the node key can write, at any time:

  url          /ip4/<ip>/tcp/<port>/p2p/<peer id>       updateUrl(string)
  metadataUrl  kps:<ip>:<port>:<certhash>/metadata.json  updateMetadataUrl(string)  (KPS entries)

Default: a DRY RUN that signs nothing. It reads the registry, builds both values from the
registered ones (only the IP changes: peer ID, ports and certhash stay), prints the calldata,
the gas estimate and what clients will see, and runs the KPS checks:

  - nox-kps.toml advertises the new IP and holds the certhash
  - the running sidecar serves <new ip>:<port>:<certhash>   (nox-kps address)
  - nox-kps healthcheck --kps                               (QUIC dial of the listener, GET /health)

--send signs updateUrl and updateMetadataUrl with the node key and, for each transaction,
checks the receipt, the event and the read-back, and finally that topologyFingerprint() and
relayerCount() are unchanged (both cover membership only). Values already on chain are
skipped, so a second run continues where the first stopped. --send refuses when a check
fails. An exit (role 2 or 3) sends paid transactions from the same key, so with --send the
tool waits for nox_eth_tx_pending 0, stops the node, sends, and starts the node again so it
reads the new nonce.

--key-file is a file only you can read (chmod 600) holding the node's hex private key, or
your run-nox .env (NOX__ETH_WALLET_PRIVATE_KEY). The key is handed to Foundry's `cast`
through a private terminal, so it stays off the command line, the environment and the output.
--address runs the dry run without a key. Requires Python 3.11+ and Foundry `cast` (for
--key-file and --send).
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import pty
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import termios
import time
import tomllib
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, NoReturn

USER_AGENT = "run-nox-change-ip/1"
DEFAULT_KPS_EXEC = "docker compose exec -T nox-kps"
DEFAULT_COMPOSE = "docker compose"
DEFAULT_METRICS_URL = "http://127.0.0.1:15001/metrics"

# cast sig "<signature>" (checked against cast in scripts/test_change_ip.py)
SELECTOR = {
    "updateUrl": "0xf928edc2",
    "updateMetadataUrl": "0x7132048c",
    "relayers": "0x5300f841",
    "topologyFingerprint": "0x3cce4d3d",
    "relayerCount": "0xcf1a7a21",
    "getNodeRole": "0x55b211e3",
}
# cast sig-event "<event>"
EVENT_TOPIC = {
    "updateUrl": "0xf07dbf3d77012f0b44fefb89300e81a4682a0955db1e819c834d2023583489cb",  # RelayerUpdated(address,string)
    "updateMetadataUrl": "0x52193edd3e8de052d16864b8b967909a47f29051d89cedc12662b326b8e8c298",  # MetadataUrlUpdated(address,string)
}
ROLE_NAME = {1: "relay", 2: "exit", 3: "full (relay + exit)"}
SENDS_PAID_TRANSACTIONS = {2, 3}

IPV4 = re.compile(r"^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$")
ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
PEER_ID = r"(?:12D3KooW[1-9A-HJ-NP-Za-km-z]{44}|Qm[1-9A-HJ-NP-Za-km-z]{44})"
IP4_MULTIADDR = re.compile(rf"^/ip4/([0-9.]+)/tcp/(\d{{1,5}})/p2p/({PEER_ID})$")
DNS_MULTIADDR = re.compile(rf"^/dns[46]?/([A-Za-z0-9.-]+)/tcp/(\d{{1,5}})/p2p/({PEER_ID})$")
# ARCHITECTURE §6 ABNF: certhash = "uEi" ("A" / "B" / "C" / "D") 43base64url
CERTHASH = re.compile(r"^uEi[A-D][A-Za-z0-9_-]{43}$")
KPS_METADATA = re.compile(r"^kps:([^/]+)/metadata\.json$")
PRIVATE_KEY = re.compile(r"^(?:0x)?([0-9a-fA-F]{64})$")
ENV_KEY = re.compile(
    r"""^\s*(?:export\s+)?NOX__ETH_WALLET_PRIVATE_KEY\s*=\s*["']?(?:0x)?([0-9a-fA-F]{64})["']?\s*$"""
)
GAS_HEADROOM_NUM = 3  # gas limit and balance check use 1.5x the estimate
GAS_HEADROOM_DEN = 2


class ChangeIpError(Exception):
    """An actionable failure; main() prints it and exits 1."""


def fail(message: str) -> NoReturn:
    raise ChangeIpError(message)


# --------------------------------------------------------------------------- values


def check_ipv4(value: str, field: str) -> str:
    if IPV4.fullmatch(value) is None:
        fail(f"{field} {value!r} is not an IPv4 address such as 198.51.100.7")
    if not ipaddress.IPv4Address(value).is_global:
        fail(f"{field} {value} is not publicly routable; clients and peers dial it from the internet")
    return value


def kps_address(ip: str, port: int, certhash: str) -> str:
    """<ip>:<port>:<certhash>, the address wallets dial (gateways, bridges, anchors)."""
    check_ipv4(ip, "KPS IP")
    if not 1 <= port <= 65535:
        fail(f"KPS port {port} is not a UDP port")
    if CERTHASH.fullmatch(certhash) is None:
        fail(f"certhash {certhash!r} is not \"uEi\" + A-D + 43 base64url characters (nox-kps init prints it)")
    return f"{ip}:{port}:{certhash}"


def kps_metadata_url(ip: str, port: int, certhash: str) -> str:
    """kps:<ip>:<port>:<certhash>/metadata.json (ARCHITECTURE §6, unchanged by S1)."""
    return f"kps:{kps_address(ip, port, certhash)}/metadata.json"


@dataclass(frozen=True)
class KpsParts:
    ip: str
    port: int
    certhash: str


def parse_kps_metadata_url(value: str) -> KpsParts | None:
    """Inverse of kps_metadata_url; None when the value is not a KPS address."""
    match = KPS_METADATA.fullmatch(value)
    if match is None:
        return None
    parts = match.group(1).split(":")
    if len(parts) != 3:
        return None
    ip, port_text, certhash = parts
    if IPV4.fullmatch(ip) is None or re.fullmatch(r"\d{1,5}", port_text) is None:
        return None
    port = int(port_text)
    if not 1 <= port <= 65535 or CERTHASH.fullmatch(certhash) is None:
        return None
    return KpsParts(ip, port, certhash)


@dataclass(frozen=True)
class UrlPlan:
    target: str
    current_ip: str | None
    note: str


def plan_url(registered: str, new_ip: str) -> UrlPlan:
    """The new url: the registered multiaddr with only the IP replaced."""
    match = IP4_MULTIADDR.fullmatch(registered)
    if match is not None:
        port = int(match.group(2))
        if not 1 <= port <= 65535:
            fail(f"registered url {registered} has an invalid TCP port")
        return UrlPlan(f"/ip4/{new_ip}/tcp/{port}/p2p/{match.group(3)}", match.group(1), "")
    dns = DNS_MULTIADDR.fullmatch(registered)
    if dns is not None:
        return UrlPlan(
            registered,
            None,
            f"url uses the DNS name {dns.group(1)}: point its A record at {new_ip}; the url stays as it is",
        )
    fail(
        f"registered url {registered!r} is not /ip4/<ip>/tcp/<port>/p2p/<peer id> or /dns4/<name>/tcp/<port>/p2p/<peer id>; "
        "fix the registration first (REGISTRATION.md)"
    )


# --------------------------------------------------------------------------- ABI


def encode_string_call(function: str, value: str) -> str:
    """Calldata for updateUrl(string) / updateMetadataUrl(string)."""
    data = value.encode("utf-8")
    padded = data + b"\x00" * ((32 - len(data) % 32) % 32)
    return (
        SELECTOR[function]
        + (32).to_bytes(32, "big").hex()
        + len(data).to_bytes(32, "big").hex()
        + padded.hex()
    )


def encode_address_call(function: str, address: str) -> str:
    return SELECTOR[function] + "0" * 24 + address[2:].lower()


def _word(data: bytes, index: int) -> int:
    start = index * 32
    if start + 32 > len(data):
        fail("registry returned a truncated ABI value")
    return int.from_bytes(data[start : start + 32], "big")


def _string_at(data: bytes, offset: int) -> str:
    if offset + 32 > len(data):
        fail("registry returned a truncated ABI string")
    length = int.from_bytes(data[offset : offset + 32], "big")
    end = offset + 32 + length
    if end > len(data):
        fail("registry returned a truncated ABI string")
    try:
        return data[offset + 32 : end].decode("utf-8")
    except UnicodeDecodeError:
        fail("registry returned a string that is not UTF-8")


@dataclass(frozen=True)
class Profile:
    sphinx_key: str
    url: str
    ingress_url: str
    metadata_url: str
    registered: bool
    frozen: bool


def decode_profile(hex_data: str) -> Profile:
    """relayers(address) -> (bytes32, string, string, string, uint256, uint256, bool, uint8, bool)."""
    data = bytes.fromhex(hex_data[2:] if hex_data.startswith("0x") else hex_data)
    if len(data) < 9 * 32:
        fail("registry returned no profile (is --deployment the right network?)")
    return Profile(
        sphinx_key="0x" + data[0:32].hex(),
        url=_string_at(data, _word(data, 1)),
        ingress_url=_string_at(data, _word(data, 2)),
        metadata_url=_string_at(data, _word(data, 3)),
        registered=_word(data, 6) == 1,
        frozen=_word(data, 8) == 1,
    )


def decode_event_string(hex_data: str) -> str:
    data = bytes.fromhex(hex_data[2:])
    return _string_at(data, _word(data, 0))


# --------------------------------------------------------------------------- inputs


def load_toml(path: Path, label: str) -> dict[str, object]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except OSError as error:
        fail(f"{label} {path} cannot be read: {error.strerror}")
    except tomllib.TOMLDecodeError as error:
        fail(f"{label} {path} is not valid TOML: {error}")


@dataclass(frozen=True)
class Deployment:
    registry: str
    chain_id: int


def load_deployment(path: Path) -> Deployment:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        fail(f"deployment manifest {path} cannot be read ({error.strerror}); cp configs/arbitrum-sepolia.deployment.json deployment.json")
    except json.JSONDecodeError as error:
        fail(f"deployment manifest {path} is not JSON: {error}")
    registry = doc.get("contracts", {}).get("noxRegistry") if isinstance(doc, dict) else None
    chain_id = doc.get("meta", {}).get("chainId") if isinstance(doc, dict) else None
    if not isinstance(registry, str) or ADDRESS.fullmatch(registry) is None:
        fail(f"{path} has no contracts.noxRegistry address")
    if not isinstance(chain_id, int) or isinstance(chain_id, bool) or chain_id <= 0:
        fail(f"{path} has no meta.chainId")
    return Deployment(registry, chain_id)


def read_private_key(path: Path) -> str:
    """The node's 0x-prefixed key from a hex file or a run-nox .env; never printed."""
    try:
        info = path.stat()
    except OSError as error:
        fail(f"key file {path} cannot be read: {error.strerror}")
    if info.st_mode & 0o077:
        fail(f"key file {path} is readable by group or others (mode {info.st_mode & 0o777:o}); chmod 600 it")
    text = path.read_text(encoding="utf-8").strip()
    single = PRIVATE_KEY.fullmatch(text)
    if single is not None:
        return "0x" + single.group(1).lower()
    keys = [m.group(1) for m in map(ENV_KEY.fullmatch, text.splitlines()) if m is not None]
    if len(keys) == 1:
        return "0x" + keys[0].lower()
    if len(keys) > 1:
        fail(f"key file {path} sets NOX__ETH_WALLET_PRIVATE_KEY more than once")
    fail(
        f"key file {path} holds neither a 32-byte hex key nor a NOX__ETH_WALLET_PRIVATE_KEY line "
        "(the registered node key; `grep 'Address (for registration)' .env` shows its address)"
    )


@dataclass(frozen=True)
class KpsConfig:
    port: int
    certhash: str
    advertise: list[str]


def load_kps_config(path: Path) -> KpsConfig:
    config = load_toml(path, "nox-kps config")
    listen = config.get("listen", "[::]:15005")
    port_text = listen.rsplit(":", 1)[-1] if isinstance(listen, str) else ""
    if re.fullmatch(r"\d{1,5}", port_text) is None or not 1 <= int(port_text) <= 65535:
        fail(f"{path} listen {listen!r} has no fixed UDP port")
    certhash = config.get("expected_certhash", "")
    if not isinstance(certhash, str) or CERTHASH.fullmatch(certhash.strip()) is None:
        fail(f"{path} expected_certhash {certhash!r} is not the certhash `nox-kps init` printed")
    advertise = config.get("advertise", [])
    if not isinstance(advertise, list) or not all(isinstance(ip, str) for ip in advertise):
        fail(f"{path} advertise must be a list of IP strings")
    return KpsConfig(int(port_text), certhash.strip(), list(advertise))


# --------------------------------------------------------------------------- chain


class Rpc:
    def __init__(self, url: str) -> None:
        if not url.startswith(("https://", "http://")):
            fail(f"--rpc-url {url!r} must be an http(s) URL")
        self.url = url
        self.next_id = 1

    def request(self, method: str, params: list[object]) -> object:
        body = json.dumps({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params}).encode()
        self.next_id += 1
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            fail(f"RPC {method} to {self.url} failed: {type(error).__name__}: {error}")
        if not isinstance(payload, dict):
            fail(f"RPC {method} returned no JSON-RPC object")
        if "error" in payload:
            rpc_error = payload["error"]
            message = rpc_error.get("message", rpc_error) if isinstance(rpc_error, dict) else rpc_error
            raise RpcError(f"RPC {method} returned an error: {message}")
        if "result" not in payload:
            fail(f"RPC {method} returned no result")
        return payload["result"]

    def hex(self, method: str, params: list[object]) -> str:
        value = self.request(method, params)
        if not isinstance(value, str) or re.fullmatch(r"0x[0-9a-fA-F]*", value) is None:
            fail(f"RPC {method} did not return hexadecimal data")
        return value.lower()

    def call(self, to: str, data: str) -> str:
        return self.hex("eth_call", [{"to": to, "data": data}, "latest"])

    def int(self, method: str, params: list[object]) -> int:
        return int(self.hex(method, params), 16)


class RpcError(ChangeIpError):
    pass


@dataclass(frozen=True)
class ChainView:
    profile: Profile
    role: int
    fingerprint: str
    relayer_count: int
    balance: int


def read_chain(rpc: Rpc, registry: str, address: str) -> ChainView:
    profile = decode_profile(rpc.call(registry, encode_address_call("relayers", address)))
    role = int(rpc.call(registry, encode_address_call("getNodeRole", address)), 16)
    fingerprint = rpc.call(registry, SELECTOR["topologyFingerprint"])
    count = int(rpc.call(registry, SELECTOR["relayerCount"]), 16)
    balance = rpc.int("eth_getBalance", [address, "latest"])
    return ChainView(profile, role, fingerprint, count, balance)


# --------------------------------------------------------------------------- cast


def cast_binary() -> str:
    cast = os.environ.get("CAST", "cast")
    path = shutil.which(cast)
    if path is None:
        fail(f"Foundry `cast` ({cast}) is not on PATH; install it with `curl -L https://foundry.paradigm.xyz | bash && foundryup`")
    return path


def run_cast_with_key(arguments: list[str], key: str, timeout: float) -> str:
    """Run `cast <arguments> --interactive` in a private terminal and type the key at its
    prompt, so it never appears in argv, the environment or a file. Returns the output with
    the prompt removed; raises on a non-zero exit."""
    cast = cast_binary()
    pid, fd = pty.fork()
    if pid == 0:  # child
        try:
            os.execv(cast, [cast, *arguments, "--interactive"])
        finally:
            os._exit(127)
    try:
        attributes = termios.tcgetattr(fd)
        attributes[3] &= ~termios.ECHO
        termios.tcsetattr(fd, termios.TCSANOW, attributes)
    except termios.error:
        pass
    output = b""
    sent = False
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            os.close(fd)
            fail(f"cast {arguments[0]} did not finish within {int(timeout)} s")
        ready, _, _ = select.select([fd], [], [], min(remaining, 1.0))
        if not ready:
            continue
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        output += chunk
        if not sent and b"private key" in output.lower():
            os.write(fd, key.encode() + b"\n")
            sent = True
    _, status = os.waitpid(pid, 0)
    os.close(fd)
    text = output.decode("utf-8", "replace").replace("\r\n", "\n")
    text = text.replace(key, "<key>").replace(key[2:], "<key>")
    lines = [line for line in text.splitlines() if "private key" not in line.lower()]
    text = "\n".join(lines).strip()
    code = os.waitstatus_to_exitcode(status)
    if code != 0:
        fail(f"cast {arguments[0]} exited {code}: {text[-400:]}")
    return text


def address_of_key(key: str) -> str:
    out = run_cast_with_key(["wallet", "address"], key, timeout=30)
    match = re.search(r"0x[0-9a-fA-F]{40}", out)
    if match is None:
        fail("cast wallet address printed no address")
    return match.group(0)


@dataclass(frozen=True)
class Receipt:
    tx_hash: str
    block: int
    logs: list[dict[str, object]]


def send_with_key(key: str, rpc_url: str, chain_id: int, to: str, data: str, gas_limit: int) -> Receipt:
    out = run_cast_with_key(
        ["send", to, data, "--rpc-url", rpc_url, "--chain", str(chain_id), "--gas-limit", str(gas_limit), "--json"],
        key,
        timeout=300,
    )
    start = out.find("{")
    try:
        receipt = json.loads(out[start:]) if start >= 0 else None
    except json.JSONDecodeError:
        receipt = None
    if not isinstance(receipt, dict):
        fail(f"cast send printed no JSON receipt: {out[-400:]}")
    tx_hash = str(receipt.get("transactionHash", ""))
    if receipt.get("status") not in ("0x1", 1, "1"):
        fail(f"transaction {tx_hash} failed (status {receipt.get('status')})")
    logs = receipt.get("logs")
    block = receipt.get("blockNumber", "0x0")
    return Receipt(
        tx_hash,
        int(block, 16) if isinstance(block, str) else int(block),
        [log for log in logs if isinstance(log, dict)] if isinstance(logs, list) else [],
    )


def event_value(receipt: Receipt, registry: str, function: str, address: str) -> str | None:
    want_topic1 = "0x" + "0" * 24 + address[2:].lower()
    for log in receipt.logs:
        raw_topics = log.get("topics")
        topics = [t.lower() for t in raw_topics if isinstance(t, str)] if isinstance(raw_topics, list) else []
        if (
            str(log.get("address", "")).lower() == registry.lower()
            and len(topics) == 2
            and topics[0] == EVENT_TOPIC[function]
            and topics[1] == want_topic1
            and isinstance(log.get("data"), str)
        ):
            return decode_event_string(str(log["data"]))
    return None


# --------------------------------------------------------------------------- node-side checks


def run_command(command: list[str], timeout: float) -> tuple[int, str]:
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return 127, f"{command[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, f"no answer within {int(timeout)} s"
    return done.returncode, (done.stdout + done.stderr).strip()


def kps_checks(kps_exec: list[str], config: KpsConfig, config_path: Path, new_ip: str) -> list[tuple[bool, str]]:
    """Ties the running sidecar to the new address before anything is signed."""
    address = kps_address(new_ip, config.port, config.certhash)
    results: list[tuple[bool, str]] = []
    if new_ip in config.advertise:
        extra = [ip for ip in config.advertise if ip != new_ip]
        note = f" (also advertises {', '.join(extra)}; drop addresses this host no longer holds)" if extra else ""
        results.append((True, f"{config_path} advertises {new_ip}{note}"))
    else:
        results.append(
            (
                False,
                f"{config_path} advertises {config.advertise}, not {new_ip}: set advertise = [\"{new_ip}\"] and run "
                "`docker compose up -d --force-recreate nox-kps`",
            )
        )
    code, out = run_command([*kps_exec, "nox-kps", "address"], timeout=60)
    served = re.findall(r"^address: (\S+)$", out, flags=re.MULTILINE)
    if code == 0 and address in served:
        results.append((True, f"the running nox-kps serves {address} (nox-kps address)"))
    else:
        detail = f"it serves {served}" if served else f"exit {code}: {out[-300:]}"
        results.append(
            (
                False,
                f"the running nox-kps does not serve {address} ({detail}); after editing {config_path} run "
                "`docker compose up -d --force-recreate nox-kps`",
            )
        )
    code, out = run_command([*kps_exec, "nox-kps", "healthcheck", "--kps"], timeout=60)
    if code == 0:
        results.append((True, "nox-kps healthcheck --kps: QUIC dial of the listener with this certhash, GET /health 200"))
    else:
        results.append((False, f"nox-kps healthcheck --kps failed (exit {code}): {out[-300:]}"))
    return results


def pending_transactions(metrics_url: str) -> int:
    try:
        with urllib.request.urlopen(urllib.request.Request(metrics_url, headers={"User-Agent": USER_AGENT}), timeout=10) as r:
            text = r.read().decode()
    except (OSError, UnicodeDecodeError) as error:
        fail(f"cannot read {metrics_url} ({error}); the exit must be running so its pending transactions can be checked")
    match = re.search(r"^nox_eth_tx_pending(?:\{[^}]*\})?\s+([0-9.eE+-]+)\s*$", text, flags=re.MULTILINE)
    if match is None:
        fail(f"{metrics_url} has no nox_eth_tx_pending metric")
    return int(float(match.group(1)))


# --------------------------------------------------------------------------- main


@dataclass
class Change:
    function: str
    current: str
    target: str

    @property
    def needed(self) -> bool:
        return self.current != self.target


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="change_ip.py",
        description="Publish this node's new public IP in NoxRegistry (dry run unless --send).",
    )
    parser.add_argument("--ip", required=True, help="the new public IPv4 address")
    who = parser.add_mutually_exclusive_group(required=True)
    who.add_argument("--key-file", type=Path, help="hex key file or run-nox .env (chmod 600)")
    who.add_argument("--address", help="registered node address, for a dry run without the key")
    parser.add_argument("--deployment", type=Path, default=Path("deployment.json"))
    parser.add_argument("--config", type=Path, default=Path("config.toml"), help="node config (eth_rpc_url)")
    parser.add_argument("--rpc-url", help="RPC endpoint (default: eth_rpc_url from --config)")
    kps = parser.add_mutually_exclusive_group()
    kps.add_argument("--kps-config", type=Path, help="nox-kps.toml (default: ./nox-kps.toml when present)")
    kps.add_argument("--no-kps", action="store_true", help="this node runs no KPS entry; update the url only")
    parser.add_argument("--kps-exec", default=DEFAULT_KPS_EXEC, help=f"prefix that runs nox-kps in the sidecar (default: {DEFAULT_KPS_EXEC})")
    parser.add_argument("--compose", default=DEFAULT_COMPOSE, help="Compose command for stopping and starting an exit")
    parser.add_argument("--metrics-url", default=DEFAULT_METRICS_URL, help="node metrics (exits: nox_eth_tx_pending)")
    parser.add_argument("--allow-certhash-change", action="store_true", help="replace a published certhash (new nox-kps identity)")
    parser.add_argument("--send", action="store_true", help="sign and send the transactions")
    args = parser.parse_args(argv)
    if args.send and args.key_file is None:
        parser.error("--send needs --key-file (the registered node key)")
    return args


def rpc_url_from(args: argparse.Namespace) -> str:
    if args.rpc_url:
        return str(args.rpc_url)
    if args.config.exists():
        url = load_toml(args.config, "node config").get("eth_rpc_url")
        if isinstance(url, str) and url:
            return url
    fail(f"pass --rpc-url, or run from the directory holding {args.config} with eth_rpc_url set")


def resolve_kps(args: argparse.Namespace) -> Path | None:
    if args.no_kps:
        return None
    if args.kps_config is not None:
        if not args.kps_config.exists():
            fail(f"--kps-config {args.kps_config} does not exist")
        return Path(args.kps_config)
    default = Path("nox-kps.toml")
    return default if default.exists() else None


def main(argv: list[str], out: Callable[[str], None] = print) -> int:
    args = parse_args(argv)
    new_ip = check_ipv4(args.ip, "--ip")
    deployment = load_deployment(args.deployment)
    rpc_url = rpc_url_from(args)
    kps_path = resolve_kps(args)
    kps_config = load_kps_config(kps_path) if kps_path is not None else None
    key = read_private_key(args.key_file) if args.key_file is not None else None
    if key is not None:
        address = address_of_key(key)
    else:
        if ADDRESS.fullmatch(args.address or "") is None:
            fail(f"--address {args.address!r} is not a 0x-prefixed 20-byte address")
        address = str(args.address)

    rpc = Rpc(rpc_url)
    rpc_chain = rpc.int("eth_chainId", [])
    if rpc_chain != deployment.chain_id:
        fail(f"{rpc_url} serves chain {rpc_chain}, but {args.deployment} is for chain {deployment.chain_id}")
    before = read_chain(rpc, deployment.registry, address)
    profile = before.profile
    mode = "SENDING transactions" if args.send else "DRY RUN: nothing is signed"
    out(f"registry {deployment.registry} on chain {deployment.chain_id}; {mode}")
    out(f"node     {address}, role {ROLE_NAME.get(before.role, before.role)}, sphinx key {profile.sphinx_key[:18]}...")
    if not profile.registered:
        fail(f"{address} is not registered in {deployment.registry}; register first (REGISTRATION.md)")
    if profile.frozen:
        out("warning: this node is frozen by governance; the new location is published, routing follows governance")

    url_plan = plan_url(profile.url, new_ip)
    changes = [Change("updateUrl", profile.url, url_plan.target)]
    if url_plan.note:
        out(f"note     {url_plan.note}")

    current_kps = parse_kps_metadata_url(profile.metadata_url)
    target_kps: str | None = None
    if kps_config is not None:
        if profile.metadata_url and current_kps is None:
            fail(
                f"metadataUrl holds {profile.metadata_url!r}, which is not a KPS address; this tool replaces only "
                "kps:<ip>:<port>:<certhash>/metadata.json values (clear it with updateMetadataUrl(\"\") first if you mean to)"
            )
        if current_kps is not None and current_kps.certhash != kps_config.certhash and not args.allow_certhash_change:
            fail(
                f"the published certhash {current_kps.certhash} differs from {kps_path} expected_certhash "
                f"{kps_config.certhash}; restore the backed-up nox-kps key, or pass --allow-certhash-change to publish "
                "the new identity"
            )
        target_kps = kps_address(new_ip, kps_config.port, kps_config.certhash)
        changes.append(Change("updateMetadataUrl", profile.metadata_url, f"kps:{target_kps}/metadata.json"))
    elif current_kps is not None and current_kps.ip != new_ip:
        fail(
            f"this node publishes the KPS address {current_kps.ip}:{current_kps.port}:{current_kps.certhash}; pass "
            "--kps-config nox-kps.toml so it moves too (or clear it first: README \"Stop Serving KPS\")"
        )

    for change in changes:
        label = "url" if change.function == "updateUrl" else "metadataUrl"
        out(f"{label:<12} {change.current or '(empty)'}")
        if change.needed:
            out(f"{'  ->':<12} {change.target}")
            out(f"{'  calldata':<12} {encode_string_call(change.function, change.target)}")
        else:
            out(f"{'':<12} already set; nothing to send")
    out(f"ingressUrl   {profile.ingress_url or '(empty)'} (unchanged by this tool)")
    if url_plan.current_ip and url_plan.current_ip in profile.ingress_url:
        out(f"warning: ingressUrl names the old IP {url_plan.current_ip}; register an https name for it instead")
    out(f"membership   topologyFingerprint {before.fingerprint}, relayerCount {before.relayer_count} (url changes leave both unchanged)")

    checks: list[tuple[bool, str]] = []
    if kps_config is not None and kps_path is not None:
        checks = kps_checks(shlex.split(args.kps_exec), kps_config, kps_path, new_ip)
        out("checks")
        for ok, message in checks:
            out(f"  {'ok  ' if ok else 'FAIL'} {message}")

    pending = [change for change in changes if change.needed]
    gas: dict[str, int] = {}
    for change in pending:
        try:
            gas[change.function] = rpc.int(
                "eth_estimateGas",
                [{"from": address, "to": deployment.registry, "data": encode_string_call(change.function, change.target)}],
            )
        except RpcError as error:
            fail(f"{change.function} would revert: {error}")
    gas_price = rpc.int("eth_gasPrice", [])
    needed_wei = sum(gas.values()) * gas_price * GAS_HEADROOM_NUM // GAS_HEADROOM_DEN
    if pending:
        estimates = ", ".join(f"{name} {value}" for name, value in gas.items())
        out(f"gas          {estimates}; balance {before.balance / 1e18:.6f} ETH, needs {needed_wei / 1e18:.6f} ETH (1.5x)")

    print_client_view(out, url_plan.target, target_kps, before.role)

    failed = [message for ok, message in checks if not ok]
    if not args.send:
        if failed:
            out(f"--send would refuse: {len(failed)} check(s) failed (see FAIL above)")
            return 1
        if pending:
            out("next         rerun with --send --key-file <key file> to sign")
        return 0

    if failed:
        fail(f"{len(failed)} check(s) failed; nothing was signed")
    if not pending:
        out("done         every value is already on chain")
        return 0
    if before.balance < needed_wei:
        fail(f"{address} holds {before.balance / 1e18:.6f} ETH and needs {needed_wei / 1e18:.6f} ETH; fund it first")
    assert key is not None
    exit_node = before.role in SENDS_PAID_TRANSACTIONS
    compose = shlex.split(args.compose)
    if exit_node:
        count = pending_transactions(args.metrics_url)
        if count != 0:
            fail(f"nox_eth_tx_pending is {count}; wait until the exit has no pending transaction, then rerun")
        out("exit         stopping the node so it reads the new nonce afterwards")
        code, text = run_command([*compose, "stop", "nox"], timeout=120)
        if code != 0:
            fail(f"`{args.compose} stop nox` failed (exit {code}): {text[-300:]}")
    try:
        for change in pending:
            limit = gas[change.function] * GAS_HEADROOM_NUM // GAS_HEADROOM_DEN
            receipt = send_with_key(
                key, rpc_url, deployment.chain_id, deployment.registry, encode_string_call(change.function, change.target), limit
            )
            value = event_value(receipt, deployment.registry, change.function, address)
            if value != change.target:
                fail(f"{receipt.tx_hash} mined without the expected {change.function} event")
            out(f"sent         {change.function} {receipt.tx_hash} (block {receipt.block}), event OK")
    finally:
        if exit_node:
            code, text = run_command([*compose, "up", "-d"], timeout=600)
            if code != 0:
                out(f"warning: `{args.compose} up -d` failed (exit {code}): {text[-300:]}; start the node by hand")
            else:
                out("exit         node started again")

    after = read_chain(rpc, deployment.registry, address)
    if after.profile.url != url_plan.target:
        fail(f"read-back url {after.profile.url!r} differs from {url_plan.target!r}")
    if target_kps is not None and after.profile.metadata_url != f"kps:{target_kps}/metadata.json":
        fail(f"read-back metadataUrl {after.profile.metadata_url!r} differs")
    if (after.fingerprint, after.relayer_count) != (before.fingerprint, before.relayer_count):
        fail(
            f"membership changed during the update (fingerprint {before.fingerprint} -> {after.fingerprint}, "
            f"count {before.relayer_count} -> {after.relayer_count}): a registration or removal landed at the same "
            "time; check the registry events"
        )
    out("verified     read-back matches; topologyFingerprint and relayerCount unchanged")
    if url_plan.current_ip and url_plan.current_ip != new_ip:
        out(f"rollback     python3 scripts/change_ip.py --ip {url_plan.current_ip} --key-file {args.key_file} --send")
    return 0


def print_client_view(out: Callable[[str], None], url: str, kps: str | None, role: int) -> None:
    out("what clients will see")
    out(f"  peers and route hops   {url}")
    if kps is not None:
        out(f"  KPS entry address      {kps}")
        out(f"  wallet config syntax   {{\"gateways\": [\"{kps}\"]}}")
    out("  identity               unchanged: address, Sphinx key and role, so routes and probation status stay")
    out("  S1 discovery clients   adopt the new location at their next registry check through the mixnet")
    out("                         (every 10 minutes by default), then cache it as a learned entry")
    out("  pinned-snapshot (S0)   bundles use the new location from the next worker bundle release")
    out("  nodes                  apply the RelayerUpdated event through their chain observer and dial the new url")
    if role in SENDS_PAID_TRANSACTIONS:
        out("  exits                  keep serving paid transactions; the node restarts once around --send")


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except ChangeIpError as error:
        print(f"change_ip: {error}", file=sys.stderr)
        sys.exit(1)
