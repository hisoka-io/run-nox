#!/usr/bin/env python3
"""Tests for scripts/change_ip.py.

  python3 scripts/test_change_ip.py

Unit tests run anywhere. The calldata, selector and ABI tests compare with Foundry `cast`,
and the end-to-end tests run the tool against a local anvil chain that carries the live
NoxRegistry implementation bytecode (Arbitrum Sepolia 0x7285125c...e2a2, read with
`cast code`, in fixtures/noxregistry-impl-0x7285125c.hex). Both are skipped with a message
when Foundry is not installed; CI installs it.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import change_ip as tool  # noqa: E402

HAVE_FOUNDRY = shutil.which("cast") is not None and shutil.which("anvil") is not None
NEED_FOUNDRY = "Foundry (cast, anvil) is not installed"

# anvil's well-known development accounts (public test mnemonic, never funded on a real chain)
ADMIN_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
RELAY_KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
EXIT_KEY = "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a"
STRANGER_KEY = "0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6"
REGISTRY = "0x00000000000000000000000000000000000F0B05"
PEER = "12D3KooW" + "A" * 44
CERT = "uEiBVDwIs40bsslDkM-BYb2AOHw3PHe70_bj5U_09r7vdIQ"
OTHER_CERT = "uEiDGVPDwsQ96ri9T5WLR6jZov_9LW-gRAgs-DN9FyKuHuw"
OLD_IP = "3.232.137.146"
NEW_IP = "18.215.18.61"


def cast(*args: str) -> str:
    return subprocess.run(["cast", *args], capture_output=True, text=True, check=True).stdout.strip()


def write(path: Path, text: str, mode: int = 0o600) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


class Values(unittest.TestCase):
    def test_metadata_url_format_and_round_trip(self) -> None:
        value = tool.kps_metadata_url(NEW_IP, 15005, CERT)
        self.assertEqual(value, f"kps:{NEW_IP}:15005:{CERT}/metadata.json")
        self.assertEqual(tool.parse_kps_metadata_url(value), tool.KpsParts(NEW_IP, 15005, CERT))
        self.assertEqual(tool.kps_address(NEW_IP, 15005, CERT), f"{NEW_IP}:15005:{CERT}")

    def test_metadata_url_rejects_bad_parts(self) -> None:
        for ip, port, cert in [
            ("3.232.137", 15005, CERT),
            ("10.0.0.7", 15005, CERT),
            ("203.0.113.9", 15005, CERT),
            (NEW_IP, 0, CERT),
            (NEW_IP, 70000, CERT),
            (NEW_IP, 15005, "uEiE" + "x" * 43),
            (NEW_IP, 15005, CERT[:-1]),
        ]:
            with self.subTest(ip=ip, port=port, cert=cert), self.assertRaises(tool.ChangeIpError):
                tool.kps_metadata_url(ip, port, cert)

    def test_parse_returns_none_for_other_values(self) -> None:
        for value in [
            "",
            "https://example.org/metadata.json",
            f"kps:{NEW_IP}:15005:{CERT}/other",
            f"kps:{NEW_IP}:15005:{CERT}",
            f"kps:{NEW_IP}:99999:{CERT}/metadata.json",
            f"kps:[::1]:15005:{CERT}/metadata.json",
        ]:
            with self.subTest(value=value):
                self.assertIsNone(tool.parse_kps_metadata_url(value))

    def test_url_plan_replaces_only_the_ip(self) -> None:
        plan = tool.plan_url(f"/ip4/{OLD_IP}/tcp/15000/p2p/{PEER}", NEW_IP)
        self.assertEqual(plan.target, f"/ip4/{NEW_IP}/tcp/15000/p2p/{PEER}")
        self.assertEqual(plan.current_ip, OLD_IP)
        dns = tool.plan_url(f"/dns4/nox-9.example.org/tcp/15000/p2p/{PEER}", NEW_IP)
        self.assertEqual(dns.target, f"/dns4/nox-9.example.org/tcp/15000/p2p/{PEER}")
        self.assertIn("A record", dns.note)
        for bad in ["", f"/ip4/{OLD_IP}/udp/15000/p2p/{PEER}", f"/ip4/{OLD_IP}/tcp/15000/p2p/x", "https://nox.example.org"]:
            with self.subTest(url=bad), self.assertRaises(tool.ChangeIpError):
                tool.plan_url(bad, NEW_IP)


class Keys(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="change-ip-keys-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.dir)

    def test_hex_and_env_files(self) -> None:
        self.assertEqual(tool.read_private_key(write(self.dir / "a", RELAY_KEY + "\n")), RELAY_KEY)
        self.assertEqual(tool.read_private_key(write(self.dir / "b", RELAY_KEY[2:].upper())), RELAY_KEY)
        env = "# Address (for registration): 0x70997970C51812dc3A010C7d01b50e0d17dc79C8\n" \
              f"NOX__ROUTING_PRIVATE_KEY={'1' * 64}\nNOX__ETH_WALLET_PRIVATE_KEY={RELAY_KEY[2:]}\nCOMPOSE_PROFILES=kps\n"
        self.assertEqual(tool.read_private_key(write(self.dir / ".env", env)), RELAY_KEY)

    def test_refusals_never_show_the_key(self) -> None:
        cases = {
            "readable": (write(self.dir / "c", RELAY_KEY, 0o644), "chmod 600"),
            "two keys": (write(self.dir / "d", f"NOX__ETH_WALLET_PRIVATE_KEY={RELAY_KEY[2:]}\nNOX__ETH_WALLET_PRIVATE_KEY={EXIT_KEY[2:]}\n"), "more than once"),
            "other env": (write(self.dir / "e", f"NOX__ROUTING_PRIVATE_KEY={RELAY_KEY[2:]}\n"), "neither"),
            "short": (write(self.dir / "f", RELAY_KEY[:-2]), "neither"),
            "missing": (self.dir / "absent", "cannot be read"),
        }
        for name, (path, message) in cases.items():
            with self.subTest(name), self.assertRaises(tool.ChangeIpError) as raised:
                tool.read_private_key(path)
            self.assertIn(message, str(raised.exception))
            self.assertNotIn(RELAY_KEY[2:], str(raised.exception))


class Arguments(unittest.TestCase):
    def parse_error(self, argv: list[str]) -> str:
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            tool.parse_args(argv)
        self.assertEqual(raised.exception.code, 2)
        return stderr.getvalue()

    def test_usage_errors(self) -> None:
        self.assertIn("--ip", self.parse_error(["--address", "0x" + "1" * 40]))
        self.assertIn("--key-file", self.parse_error(["--ip", NEW_IP]))
        self.assertIn("not allowed with", self.parse_error(["--ip", NEW_IP, "--address", "0x" + "1" * 40, "--key-file", "k"]))
        self.assertIn("--send needs --key-file", self.parse_error(["--ip", NEW_IP, "--address", "0x" + "1" * 40, "--send"]))
        self.assertIn("not allowed with", self.parse_error(["--ip", NEW_IP, "--address", "0x" + "1" * 40, "--no-kps", "--kps-config", "x"]))

    def test_dry_run_is_the_default(self) -> None:
        args = tool.parse_args(["--ip", NEW_IP, "--address", "0x" + "1" * 40])
        self.assertFalse(args.send)
        self.assertEqual(args.kps_exec, "docker compose exec -T nox-kps")

    def test_input_validation_before_any_network_call(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            d = Path(raw)
            manifest = d / "deployment.json"
            manifest.write_text(json.dumps({"meta": {"chainId": 421614}, "contracts": {"noxRegistry": REGISTRY}}))
            base = ["--address", "0x" + "1" * 40, "--deployment", str(manifest), "--rpc-url", "http://127.0.0.1:9", "--no-kps"]
            for argv, message in [
                (["--ip", "1.2.3"] + base, "not an IPv4 address"),
                (["--ip", "192.168.1.4"] + base, "not publicly routable"),
                (["--ip", NEW_IP, "--deployment", str(d / "none.json"), "--address", "0x" + "1" * 40, "--rpc-url", "http://x"], "cp configs/"),
                (["--ip", NEW_IP, "--rpc-url", "ftp://x"] + base[:4] + ["--no-kps"], "http(s)"),
                (["--ip", NEW_IP, "--address", "0x12"] + base[2:], "20-byte address"),
                (["--ip", NEW_IP, "--address", "0x" + "1" * 40, "--deployment", str(manifest), "--config", str(d / "none.toml")], "--rpc-url"),
            ]:
                with self.subTest(argv=argv), self.assertRaises(tool.ChangeIpError) as raised:
                    tool.main(argv, out=lambda _line: None)
                self.assertIn(message, str(raised.exception))

    def test_kps_config_validation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            d = Path(raw)
            good = write(d / "good.toml", f'listen = "0.0.0.0:15005"\nadvertise = ["{NEW_IP}"]\nexpected_certhash = "{CERT}"\n', 0o644)
            self.assertEqual(tool.load_kps_config(good), tool.KpsConfig(15005, CERT, [NEW_IP]))
            named = write(d / "named.toml", f'listen = "0.0.0.0:15005"\nadvertise = ["{NEW_IP}"]\nexpected_certhash = "{CERT}"\nnode_address = "0x{"Ab" * 20}"\n', 0o644)
            loaded = tool.load_kps_config(named)
            self.assertEqual(loaded.node_address, "0x" + "Ab" * 20)
            self.assertTrue(tool.names_node(loaded, "0x" + "ab" * 20))
            self.assertFalse(tool.names_node(loaded, "0x" + "cd" * 20))
            for name, body, message in [
                ("nocert", f'listen = "0.0.0.0:15005"\nadvertise = ["{NEW_IP}"]\nexpected_certhash = ""\n', "expected_certhash"),
                ("port0", f'listen = "0.0.0.0:0"\nadvertise = ["{NEW_IP}"]\nexpected_certhash = "{CERT}"\n', "fixed UDP port"),
                ("adv", f'listen = "0.0.0.0:15005"\nadvertise = "{NEW_IP}"\nexpected_certhash = "{CERT}"\n', "list of IP"),
                ("toml", "listen = ", "not valid TOML"),
                ("node", f'listen = "0.0.0.0:15005"\nadvertise = ["{NEW_IP}"]\nexpected_certhash = "{CERT}"\nnode_address = "0x12"\n', "node_address"),
            ]:
                with self.subTest(name), self.assertRaises(tool.ChangeIpError) as raised:
                    tool.load_kps_config(write(d / f"{name}.toml", body, 0o644))
                self.assertIn(message, str(raised.exception))


@unittest.skipUnless(HAVE_FOUNDRY, NEED_FOUNDRY)
class AgainstCast(unittest.TestCase):
    def test_selectors_and_event_topics(self) -> None:
        signatures = {
            "updateUrl": "updateUrl(string)",
            "updateMetadataUrl": "updateMetadataUrl(string)",
            "relayers": "relayers(address)",
            "topologyFingerprint": "topologyFingerprint()",
            "relayerCount": "relayerCount()",
            "getNodeRole": "getNodeRole(address)",
        }
        for name, signature in signatures.items():
            self.assertEqual(tool.SELECTOR[name], cast("sig", signature), name)
        self.assertEqual(tool.EVENT_TOPIC["updateUrl"], cast("sig-event", "RelayerUpdated(address,string)"))
        self.assertEqual(tool.EVENT_TOPIC["updateMetadataUrl"], cast("sig-event", "MetadataUrlUpdated(address,string)"))

    def test_calldata_matches_cast(self) -> None:
        values = [
            f"/ip4/{NEW_IP}/tcp/15000/p2p/{PEER}",
            tool.kps_metadata_url(NEW_IP, 15005, CERT),
            tool.kps_metadata_url("100.56.0.72", 15005, CERT),
            "",
            "x" * 32,
        ]
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(tool.encode_string_call("updateUrl", value), cast("calldata", "updateUrl(string)", value))
                self.assertEqual(
                    tool.encode_string_call("updateMetadataUrl", value), cast("calldata", "updateMetadataUrl(string)", value)
                )
        address = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
        self.assertEqual(tool.encode_address_call("relayers", address), cast("calldata", "relayers(address)", address))

    def test_profile_decoding_matches_cast_encoding(self) -> None:
        sphinx = "0x" + "ab" * 32
        url = f"/ip4/{OLD_IP}/tcp/15000/p2p/{PEER}"
        meta = tool.kps_metadata_url(OLD_IP, 15005, CERT)
        encoded = cast(
            "abi-encode", "f(bytes32,string,string,string,uint256,uint256,bool,uint8,bool)",
            sphinx, url, "https://nox-2.hisoka.io", meta, "1000", "0", "true", "0", "false",
        )
        profile = tool.decode_profile(encoded)
        self.assertEqual(profile, tool.Profile(sphinx, url, "https://nox-2.hisoka.io", meta, True, False))
        with self.assertRaises(tool.ChangeIpError):
            tool.decode_profile("0x" + "00" * 64)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@unittest.skipUnless(HAVE_FOUNDRY, NEED_FOUNDRY)
class EndToEnd(unittest.TestCase):
    """The tool against anvil + the live NoxRegistry implementation."""

    anvil: subprocess.Popen[bytes]
    rpc: str
    dir: Path
    relay: str
    exit: str
    manifest: Path
    wrong_chain: Path
    relay_key: Path
    exit_env: Path
    stranger_key: Path
    kps_toml: Path
    kps_exec: Path
    compose_log: Path
    compose: Path
    snapshot: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.dir = Path(tempfile.mkdtemp(prefix="change-ip-e2e-"))
        port = free_port()
        cls.rpc = f"http://127.0.0.1:{port}"
        cls.anvil = subprocess.Popen(
            ["anvil", "--port", str(port), "--chain-id", "31337", "--silent"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        for _ in range(100):
            try:
                cast("block-number", "--rpc-url", cls.rpc)
                break
            except subprocess.CalledProcessError:
                time.sleep(0.1)
        code = (HERE / "fixtures" / "noxregistry-impl-0x7285125c.hex").read_text().strip()
        cast("rpc", "anvil_setCode", REGISTRY, code, "--rpc-url", cls.rpc)
        admin = cast("wallet", "address", "--private-key", ADMIN_KEY)
        cls.relay = cast("wallet", "address", "--private-key", RELAY_KEY)
        cls.exit = cast("wallet", "address", "--private-key", EXIT_KEY)
        send = ["send", "--private-key", ADMIN_KEY, "--rpc-url", cls.rpc, REGISTRY]
        cast(*send, "initialize((uint48,address,address,uint256,uint256,uint256,address,address,address))",
             f"(0,{admin},{admin},1,86400,1,{admin},{admin},{admin})")
        sig = "registerPrivileged(address,bytes32,string,string,string,uint8)"
        cast(*send, sig, cls.relay, "0x" + "11" * 32, f"/ip4/{OLD_IP}/tcp/15000/p2p/{PEER}", "",
             tool.kps_metadata_url(OLD_IP, 15005, CERT), "1")
        cast(*send, sig, cls.exit, "0x" + "22" * 32, f"/ip4/{OLD_IP}/tcp/15100/p2p/{PEER}", "", "", "2")

        cls.manifest = cls.dir / "deployment.json"
        cls.manifest.write_text(json.dumps({"meta": {"chainId": 31337}, "contracts": {"noxRegistry": REGISTRY}}))
        cls.wrong_chain = cls.dir / "deployment-421614.json"
        cls.wrong_chain.write_text(json.dumps({"meta": {"chainId": 421614}, "contracts": {"noxRegistry": REGISTRY}}))
        cls.relay_key = write(cls.dir / "relay.key", RELAY_KEY + "\n")
        cls.exit_env = write(cls.dir / "exit.env", f"NOX__ETH_WALLET_PRIVATE_KEY={EXIT_KEY[2:]}\n")
        cls.stranger_key = write(cls.dir / "stranger.key", STRANGER_KEY)
        cls.kps_toml = write(
            cls.dir / "nox-kps.toml", f'listen = "0.0.0.0:15005"\nadvertise = ["{NEW_IP}"]\nexpected_certhash = "{CERT}"\n', 0o644
        )
        # Stand-in for `docker compose exec -T nox-kps`: answers `nox-kps address` and
        # `nox-kps healthcheck --kps` from environment variables.
        cls.kps_exec = write(
            cls.dir / "fake-kps-exec",
            "#!/bin/sh\n"
            'case "$2" in\n'
            '  address) echo "address: ${FAKE_KPS_ADDRESS}"; echo "metadataUrl: kps:${FAKE_KPS_ADDRESS}/metadata.json" ;;\n'
            '  healthcheck) [ "$3" = --kps ] || exit 3; [ "${FAKE_KPS_HEALTH:-0}" = 0 ] || { echo "nox-kps: unhealthy: dial failed" >&2; exit 1; } ;;\n'
            "  *) exit 4 ;;\n"
            "esac\n",
            0o755,
        )
        cls.compose_log = cls.dir / "compose.log"
        cls.compose = write(cls.dir / "fake-compose", f'#!/bin/sh\necho "$*" >> "{cls.compose_log}"\n', 0o755)
        os.environ["FAKE_KPS_ADDRESS"] = f"{NEW_IP}:15005:{CERT}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.anvil.terminate()
        cls.anvil.wait()
        shutil.rmtree(cls.dir)

    def setUp(self) -> None:
        os.environ["FAKE_KPS_ADDRESS"] = f"{NEW_IP}:15005:{CERT}"
        os.environ["FAKE_KPS_HEALTH"] = "0"
        self.snapshot = cast("rpc", "evm_snapshot", "--rpc-url", self.rpc).strip('"')

    def tearDown(self) -> None:
        cast("rpc", "evm_revert", self.snapshot, "--rpc-url", self.rpc)

    def run_tool(self, *argv: str, kps: bool = True, kps_toml: Path | None = None) -> tuple[int | None, str]:
        lines: list[str] = []
        base = ["--deployment", str(self.manifest), "--rpc-url", self.rpc, "--kps-exec", str(self.kps_exec),
                "--compose", str(self.compose)]
        base += ["--kps-config", str(kps_toml or self.kps_toml)] if kps else ["--no-kps"]
        try:
            code: int | None = tool.main([*argv, *base], out=lines.append)
        except tool.ChangeIpError as error:
            lines.append(f"ERROR {error}")
            code = None
        text = "\n".join(lines)
        for key in (RELAY_KEY, EXIT_KEY, STRANGER_KEY):
            self.assertNotIn(key[2:], text.lower(), "a private key must never be printed")
        return code, text

    def profile(self, address: str) -> tool.Profile:
        return tool.decode_profile(cast("call", REGISTRY, tool.encode_address_call("relayers", address), "--rpc-url", self.rpc))

    def nonce(self, address: str) -> int:
        return int(cast("nonce", address, "--rpc-url", self.rpc))

    def kps_toml_naming(self, node: str) -> Path:
        return write(
            self.dir / f"nox-kps-{node[2:10]}.toml",
            f'listen = "0.0.0.0:15005"\nadvertise = ["{NEW_IP}"]\nexpected_certhash = "{CERT}"\nnode_address = "{node}"\n',
            0o644,
        )

    def test_dry_run_prints_values_calldata_checks_and_client_view(self) -> None:
        before = self.nonce(self.relay)
        code, out = self.run_tool("--ip", NEW_IP, "--key-file", str(self.relay_key), kps_toml=self.kps_toml_naming(self.relay))
        self.assertEqual(code, 0, out)
        new_url = f"/ip4/{NEW_IP}/tcp/15000/p2p/{PEER}"
        new_meta = f"kps:{NEW_IP}:15005:{CERT}/metadata.json"
        self.assertIn("DRY RUN: nothing is signed", out)
        self.assertIn(f"node     {self.relay}, role relay", out)
        self.assertIn(cast("calldata", "updateUrl(string)", new_url), out)
        self.assertIn(cast("calldata", "updateMetadataUrl(string)", new_meta), out)
        self.assertIn(f"advertises {NEW_IP}", out)
        self.assertIn(f"the running nox-kps serves {NEW_IP}:15005:{CERT}", out)
        self.assertIn("ok   nox-kps healthcheck --kps", out)
        self.assertIn(f"KPS entry address      {NEW_IP}:15005:{CERT}", out)
        self.assertIn(f'{{"gateways": ["{NEW_IP}:15005:{CERT}"]}}', out)
        self.assertRegex(out, r"gas          updateUrl \d+, updateMetadataUrl \d+")
        self.assertIn("rerun with --send", out)
        self.assertEqual(self.nonce(self.relay), before)
        self.assertEqual(self.profile(self.relay).url, f"/ip4/{OLD_IP}/tcp/15000/p2p/{PEER}")

    def test_gateway_line_needs_node_address(self) -> None:
        # Empty node_address: the move is published, but wallets cannot use the address as a gateway yet.
        code, out = self.run_tool("--ip", NEW_IP, "--key-file", str(self.relay_key))
        self.assertEqual(code, 0, out)
        self.assertNotIn('{"gateways"', out)
        self.assertIn("set node_address in nox-kps.toml", out)
        # node_address naming another node: /metadata.json would name the wrong member, so the check fails.
        code, out = self.run_tool("--ip", NEW_IP, "--key-file", str(self.relay_key), kps_toml=self.kps_toml_naming(self.exit))
        self.assertEqual(code, 1, out)
        self.assertIn(f"node_address {self.exit} is another node than {self.relay}", out)
        before = self.nonce(self.relay)
        code, out = self.run_tool("--ip", NEW_IP, "--key-file", str(self.relay_key), "--send", kps_toml=self.kps_toml_naming(self.exit))
        self.assertNotEqual(code, 0, out)
        self.assertEqual(self.nonce(self.relay), before)

    def test_dry_run_with_address_only(self) -> None:
        code, out = self.run_tool("--ip", NEW_IP, "--address", self.relay)
        self.assertEqual(code, 0, out)
        self.assertIn("updateMetadataUrl", out)

    def test_failed_checks_fail_the_dry_run_and_block_send(self) -> None:
        os.environ["FAKE_KPS_HEALTH"] = "1"
        code, out = self.run_tool("--ip", NEW_IP, "--key-file", str(self.relay_key))
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL nox-kps healthcheck --kps failed", out)
        self.assertIn("--send would refuse", out)
        before = self.nonce(self.relay)
        code, out = self.run_tool("--ip", NEW_IP, "--key-file", str(self.relay_key), "--send")
        self.assertIsNone(code)
        self.assertIn("nothing was signed", out)
        self.assertEqual(self.nonce(self.relay), before)

    def test_sidecar_still_on_the_old_address_is_reported(self) -> None:
        os.environ["FAKE_KPS_ADDRESS"] = f"{OLD_IP}:15005:{CERT}"
        code, out = self.run_tool("--ip", NEW_IP, "--key-file", str(self.relay_key))
        self.assertEqual(code, 1)
        self.assertIn("--force-recreate nox-kps", out)
        stale = write(self.dir / "stale.toml", f'listen = "0.0.0.0:15005"\nadvertise = ["{OLD_IP}"]\nexpected_certhash = "{CERT}"\n', 0o644)
        code, out = tool.main(["--ip", NEW_IP, "--address", self.relay, "--deployment", str(self.manifest), "--rpc-url", self.rpc,
                               "--kps-exec", str(self.kps_exec), "--kps-config", str(stale)], out=lambda _l: None), ""
        self.assertEqual(code, 1)

    def test_send_moves_a_relay_and_keeps_membership(self) -> None:
        fingerprint = cast("call", REGISTRY, "topologyFingerprint()(bytes32)", "--rpc-url", self.rpc)
        code, out = self.run_tool("--ip", NEW_IP, "--key-file", str(self.relay_key), "--send")
        self.assertEqual(code, 0, out)
        self.assertIn("SENDING transactions", out)
        self.assertRegex(out, r"sent         updateUrl 0x[0-9a-f]{64} \(block \d+\), event OK")
        self.assertRegex(out, r"sent         updateMetadataUrl 0x[0-9a-f]{64} \(block \d+\), event OK")
        self.assertIn("topologyFingerprint and relayerCount unchanged", out)
        self.assertIn(f"rollback     python3 scripts/change_ip.py --ip {OLD_IP}", out)
        profile = self.profile(self.relay)
        self.assertEqual(profile.url, f"/ip4/{NEW_IP}/tcp/15000/p2p/{PEER}")
        self.assertEqual(profile.metadata_url, f"kps:{NEW_IP}:15005:{CERT}/metadata.json")
        self.assertEqual(cast("call", REGISTRY, "topologyFingerprint()(bytes32)", "--rpc-url", self.rpc), fingerprint)
        self.assertFalse(self.compose_log.exists(), "a relay is never stopped")
        nonce = self.nonce(self.relay)
        code, out = self.run_tool("--ip", NEW_IP, "--key-file", str(self.relay_key), "--send")
        self.assertEqual(code, 0, out)
        self.assertIn("every value is already on chain", out)
        self.assertEqual(self.nonce(self.relay), nonce)

    def test_certhash_change_needs_the_flag(self) -> None:
        other = write(self.dir / "other.toml", f'listen = "0.0.0.0:15005"\nadvertise = ["{NEW_IP}"]\nexpected_certhash = "{OTHER_CERT}"\n', 0o644)
        os.environ["FAKE_KPS_ADDRESS"] = f"{NEW_IP}:15005:{OTHER_CERT}"
        argv = ["--ip", NEW_IP, "--address", self.relay, "--deployment", str(self.manifest), "--rpc-url", self.rpc,
                "--kps-exec", str(self.kps_exec), "--kps-config", str(other)]
        with self.assertRaises(tool.ChangeIpError) as raised:
            tool.main(argv, out=lambda _l: None)
        self.assertIn("--allow-certhash-change", str(raised.exception))
        self.assertEqual(tool.main([*argv, "--allow-certhash-change"], out=lambda _l: None), 0)

    def test_url_only_refused_while_a_kps_address_is_published(self) -> None:
        code, out = self.run_tool("--ip", NEW_IP, "--key-file", str(self.relay_key), kps=False)
        self.assertIsNone(code)
        self.assertIn("--kps-config nox-kps.toml so it moves too", out)

    def test_wrong_chain_and_unregistered_key_are_refused(self) -> None:
        with self.assertRaises(tool.ChangeIpError) as raised:
            tool.main(["--ip", NEW_IP, "--key-file", str(self.relay_key), "--deployment", str(self.wrong_chain),
                       "--rpc-url", self.rpc, "--no-kps"], out=lambda _l: None)
        self.assertIn("serves chain 31337", str(raised.exception))
        code, out = self.run_tool("--ip", NEW_IP, "--key-file", str(self.stranger_key), "--send", kps=False)
        self.assertIsNone(code)
        self.assertIn("is not registered", out)

    def test_exit_waits_for_pending_transactions_then_stops_and_starts_the_node(self) -> None:
        metrics = self.dir / "metrics.txt"
        metrics.write_text("nox_eth_tx_pending 1\n")
        url = metrics.as_uri()
        before = self.nonce(self.exit)
        code, out = self.run_tool("--ip", NEW_IP, "--key-file", str(self.exit_env), "--send", "--metrics-url", url, kps=False)
        self.assertIsNone(code)
        self.assertIn("nox_eth_tx_pending is 1", out)
        self.assertEqual(self.nonce(self.exit), before)
        metrics.write_text("# HELP x\nnox_eth_tx_pending 0\n")
        self.compose_log.unlink(missing_ok=True)
        code, out = self.run_tool("--ip", NEW_IP, "--key-file", str(self.exit_env), "--send", "--metrics-url", url, kps=False)
        self.assertEqual(code, 0, out)
        self.assertIn("role exit", out)
        self.assertEqual(self.compose_log.read_text().splitlines(), ["stop nox", "up -d"])
        self.assertEqual(self.profile(self.exit).url, f"/ip4/{NEW_IP}/tcp/15100/p2p/{PEER}")
        self.assertEqual(self.profile(self.exit).metadata_url, "")
        self.compose_log.unlink()

    def test_cli_exit_codes(self) -> None:
        script = str(HERE / "change_ip.py")
        common = ["--deployment", str(self.manifest), "--rpc-url", self.rpc, "--kps-exec", str(self.kps_exec)]
        ok = subprocess.run([sys.executable, script, "--ip", NEW_IP, "--address", self.relay, "--kps-config", str(self.kps_toml), *common],
                            capture_output=True, text=True, check=False)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertIn("what clients will see", ok.stdout)
        bad = subprocess.run([sys.executable, script, "--ip", NEW_IP, "--address", self.relay, "--no-kps", *common],
                             capture_output=True, text=True, check=False)
        self.assertEqual(bad.returncode, 1)
        self.assertTrue(bad.stderr.startswith("change_ip: "), bad.stderr)


if __name__ == "__main__":
    if not HAVE_FOUNDRY:
        print(f"note: {NEED_FOUNDRY}; the cast and anvil tests are skipped", file=sys.stderr)
    unittest.main(verbosity=2)
