"""Compare the built artifact to the pinned upstream and exercise real network lifecycles."""

import asyncio
import json
import os
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from native_artifacts import native_artifact

from vsm.action.rup import Rup
from vsm.binary import BinaryProvider
from vsm.connection.host import Host
from vsm.models import Schema
from vsm.processing import Params, load_native
from vsm.processor.rup import RupProcessor
from vsm.runtime import Run
from vsm.util.network import Endpoint, ProbeProtocol, ProbeRole

ROOT = Path(__file__).resolve().parents[1]

TOOL = native_artifact("rup")
REFERENCE = Path(os.environ.get("VSM_RUP_REFERENCE", "/nonexistent/vsm-rup-reference"))


def require_network(case, family=socket.AF_INET, kind=socket.SOCK_STREAM):
    try:
        sock = socket.socket(family, kind)
        sock.bind(("127.0.0.1" if family == socket.AF_INET else "::1", 0))
        return sock
    except OSError as error:
        if error.errno in (1, 13, 97, 99):
            case.skipTest(f"network unavailable: {error}")
        raise


def require_reference(case):
    if not REFERENCE.is_file():
        case.skipTest("set VSM_RUP_REFERENCE to an external upstream rup executable")


def wait_marker(process, marker):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if marker.exists() and marker.read_bytes() == b"ready\n":
            return
        if process.poll() is not None:
            raise AssertionError(f"rup exited before readiness: {process.communicate()}")
        time.sleep(0.01)
    raise AssertionError("rup readiness timeout")


class RupCliCompatibilityTests(unittest.TestCase):
    def test_our_cli_accepts_compatible_options_and_rejects_invalid_combinations(self):
        for args in [("--help",), ("--version",), ("server", "--help")]:
            result = subprocess.run([str(TOOL), *args], capture_output=True, timeout=5, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(result.stdout)
        for args in [
            ("--wait-time", "0", "127.0.0.1"),
            ("--request-size", "11", "127.0.0.1"),
            ("--tos", "256", "127.0.0.1"),
            ("--output", "jsonl", "--format", "{seq}", "127.0.0.1"),
            ("-i", "1000", "server", "127.0.0.1:5000"),
            ("--protocol", "udp", "localhost"),
        ]:
            with self.subTest(args=args):
                result = subprocess.run(
                    [str(TOOL), *args], capture_output=True, timeout=5, check=False
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(result.stdout)

    def test_cli_success_and_error_codes_agree_with_external_upstream(self):
        require_reference(self)
        for args in [
            ("--help",),
            ("--version",),
            ("server", "--help"),
            ("--output", "jsonl", "--format", "{seq}", "127.0.0.1"),
            ("--protocol", "udp", "localhost"),
            ("--wait-time", "0", "127.0.0.1"),
            ("-i", "1000", "server", "127.0.0.1:5000"),
        ]:
            with self.subTest(args=args):
                actual = subprocess.run(
                    [str(TOOL), *args], capture_output=True, timeout=5, check=False
                )
                expected = subprocess.run(
                    [str(REFERENCE), *args], capture_output=True, timeout=5, check=False
                )
                self.assertEqual(actual.returncode, expected.returncode)
                if actual.returncode == 0:
                    self.assertTrue(actual.stdout)
                else:
                    self.assertFalse(actual.stdout)


class RupNetworkCompatibilityTests(unittest.TestCase):
    def interoperate(self, family):
        with require_network(self, family) as probe:
            port = probe.getsockname()[1]
        require_reference(self)
        address = f"127.0.0.1:{port}" if family == socket.AF_INET else f"[::1]:{port}"
        for protocol in ("tcp", "udp"):
            for server_tool, client_tool in ((TOOL, REFERENCE), (REFERENCE, TOOL)):
                with self.subTest(protocol=protocol, server=server_tool):
                    server = subprocess.Popen(
                        [str(server_tool), "-p", protocol, "server", address],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    try:
                        deadline = time.monotonic() + 3
                        while True:
                            result = subprocess.run(
                                [
                                    str(client_tool),
                                    "-p",
                                    protocol,
                                    "--output",
                                    "jsonl",
                                    "-n",
                                    "2",
                                    "-i",
                                    "10",
                                    "-W",
                                    "100",
                                    "--request-size",
                                    "64",
                                    "--response-size",
                                    "128",
                                    address,
                                ],
                                capture_output=True,
                                timeout=3,
                                check=False,
                            )
                            events = [json.loads(line) for line in result.stdout.splitlines()]
                            replies = [e for e in events if e.get("record") == "reply"]
                            if result.returncode == 0 and len(replies) == 2:
                                break
                            if server.poll() is not None or time.monotonic() >= deadline:
                                self.fail(
                                    f"interoperability failed: {result.stdout!r} {result.stderr!r}"
                                )
                            time.sleep(0.02)
                        self.assertEqual([e["size_bytes"] for e in replies], [128, 128])
                        self.assertEqual(events[0]["schema"], "rup.ping")
                        self.assertEqual(events[-1]["received"], 2)
                    finally:
                        server.terminate()
                        server.communicate(timeout=3)

    def test_bidirectional_tcp_udp_ipv4(self):
        self.interoperate(socket.AF_INET)

    def test_bidirectional_tcp_udp_ipv6(self):
        self.interoperate(socket.AF_INET6)

    def icmp(self, family):
        protocol = socket.IPPROTO_ICMP if family == socket.AF_INET else socket.IPPROTO_ICMPV6
        try:
            with socket.socket(family, socket.SOCK_DGRAM, protocol):
                pass
        except OSError as error:
            if error.errno in (1, 13, 97):
                self.skipTest(f"ICMP ping socket unavailable: {error}")
            raise
        target = "127.0.0.1" if family == socket.AF_INET else "::1"
        result = subprocess.run(
            [str(TOOL), "--output", "jsonl", "-n", "2", "-i", "10", "-W", "100", target],
            capture_output=True,
            timeout=3,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        records = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(records[0]["protocol"], "icmp")
        self.assertEqual(records[-1]["received"], 2)
        self.assertTrue(all(e["record"] == "reply" for e in records[1:-1]))

    def test_unprivileged_icmp_ipv4(self):
        self.icmp(socket.AF_INET)

    def test_unprivileged_icmp_ipv6(self):
        self.icmp(socket.AF_INET6)

    def test_sigterm_drains_pending_timeouts_and_summary(self):
        with (
            require_network(self, kind=socket.SOCK_DGRAM) as silent,
            tempfile.TemporaryDirectory() as folder,
        ):
            address = f"127.0.0.1:{silent.getsockname()[1]}"
            marker = Path(folder) / "ready"
            proc = subprocess.Popen(
                [str(TOOL), "-p", "udp", "--output", "jsonl", "-i", "10", "-W", "100", address],
                env={**os.environ, "VSM_RUP_READY_FILE": str(marker)},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                wait_marker(proc, marker)
                time.sleep(0.04)
                proc.terminate()
                output, errors = proc.communicate(timeout=3)
                self.assertEqual(proc.returncode, 0, errors)
                records = [json.loads(line) for line in output.splitlines()]
                self.assertEqual(records[-1]["record"], "summary")
                self.assertGreater(records[-1]["sent"], 0)
                self.assertEqual(records[-1]["received"], 0)
                self.assertEqual(
                    sum(e["record"] == "timeout" for e in records), records[-1]["sent"]
                )
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate(timeout=3)

    def test_bind_failure_has_no_readiness_and_forced_stop_is_incomplete(self):
        with (
            require_network(self, kind=socket.SOCK_DGRAM) as occupied,
            tempfile.TemporaryDirectory() as folder,
        ):
            address = f"127.0.0.1:{occupied.getsockname()[1]}"
            marker = Path(folder) / "ready"
            result = subprocess.run(
                [str(TOOL), "server", address],
                env={**os.environ, "VSM_RUP_READY_FILE": str(marker)},
                capture_output=True,
                timeout=3,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(marker.exists())
            output_path = Path(folder) / "client.jsonl"
            with output_path.open("wb") as output:
                proc = subprocess.Popen(
                    [str(TOOL), "-p", "udp", "--output", "jsonl", "-W", "100", address],
                    env={**os.environ, "VSM_RUP_READY_FILE": str(marker)},
                    stdout=output,
                    stderr=subprocess.PIPE,
                )
                try:
                    wait_marker(proc, marker)
                    proc.kill()
                    proc.communicate(timeout=3)
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.communicate(timeout=3)
            self.assertFalse(load_native(output_path, schema=Schema(name="rup")).complete)


class RupHostIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_client_action_processing_and_fractional_stop(self):
        with require_network(self) as probe:
            port = probe.getsockname()[1]
        with tempfile.TemporaryDirectory() as folder:
            async with Run(
                Path(folder) / "run", binaries=BinaryProvider(ROOT / "artifacts")
            ) as run:
                device = await run.connect(Host())
                address = Endpoint.parse(f"127.0.0.1:{port}")
                async with (
                    device.use(
                        Rup(
                            address=address,
                            role=ProbeRole.SERVER,
                            protocol=ProbeProtocol.TCP,
                            duration=None,
                        )
                    ) as server,
                    device.use(
                        Rup(
                            address=address,
                            protocol=ProbeProtocol.TCP,
                            duration=0.05,
                            interval=10,
                        )
                    ) as client,
                ):
                    await asyncio.wait_for(client.wait(), 3)
                self.assertTrue(client.log.complete)
                self.assertTrue(server.log.complete)
                results = RupProcessor().process(client.log, params=Params())
                self.assertGreater(results[-1].frame.iloc[0]["received"], 0)
                self.assertTrue(all(results[0].frame["record"] == "reply"))
