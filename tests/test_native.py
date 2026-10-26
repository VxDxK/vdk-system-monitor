import json
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path

from native_artifacts import native_artifact
from support import stat

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"


class NativeTests(unittest.TestCase):
    def command(self, name, *args, check=True):
        result = subprocess.run(
            [str(native_artifact(name)), *args], capture_output=True, timeout=5, check=False
        )
        if check:
            self.assertEqual(result.returncode, 0, result.stderr.decode())
        return result

    def test_all_tool_help_and_invalid_args(self):
        for name in ("rfpoll", "pspider", "socket-stat", "rgen", "rup", "targetctl"):
            with self.subTest(tool=name):
                self.assertIn(name.encode(), self.command(name, "--help").stdout)
                self.assertNotEqual(
                    self.command(name, "--not-an-option", check=False).returncode, 0
                )
        for name, args in (
            ("rfpoll", ("--path", "/proc/stat", "--interval-ms", "0")),
            ("pspider", ("--samples", "0")),
            ("socket-stat", ("--interval-ms", "0")),
            ("rgen", ("producer", "--address", "127.0.0.1:1", "--workers", "0")),
            ("rup", ("--protocol", "udp", "--wait-time", "0", "127.0.0.1:1")),
        ):
            self.assertNotEqual(self.command(name, *args, check=False).returncode, 0)

    def test_rfpoll_bytes_globs_errors_and_end(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "source").mkdir()
            (root / "source/a").write_bytes(b"\x00\xff\n")
            result = self.command(
                "rfpoll",
                "--root",
                d,
                "--path",
                "/source/*",
                "--path",
                "/missing",
                "--interval-ms",
                "1",
                "--samples",
                "2",
            )
            events = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual(events[0]["schema"], "rfpoll")
            self.assertEqual(events[-1]["type"], "end")
            self.assertTrue(any(e["type"] == "ready" for e in events))
            files = [e for e in events if e["type"] == "file"]
            self.assertEqual(len(files), 2)
            self.assertEqual(bytes.fromhex(files[0]["data"]), b"\x00\xff\n")
            self.assertEqual(sum(e["type"] == "read_error" for e in events), 2)
            self.assertEqual([e["sequence"] for e in events], list(range(len(events))))

    def test_pspider_fixture_identity_and_thread_discovery(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "proc/123"
            (p / "task/123").mkdir(parents=True)
            for path, value in (
                (p / "stat", stat()),
                (p / "task/123/stat", stat()),
                (p / "io", "read_bytes: 10\n"),
                (p / "status", "Name: service\n"),
                (p / "cmdline", "service\0"),
            ):
                path.write_text(value)
            result = self.command(
                "pspider",
                "--root",
                d,
                "--pattern",
                "service",
                "--interval-ms",
                "1",
                "--samples",
                "1",
            )
            es = [json.loads(line) for line in result.stdout.splitlines()]
            records = [e for e in es if e["type"] == "file"]
            self.assertTrue(any(e["tags"]["scope"] == "thread" for e in records))
            self.assertTrue(all(e["tags"]["pid"] == 123 for e in records))

    def test_default_file_and_process_sampling_growth(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proc = root / "proc/123"
            (proc / "task/123").mkdir(parents=True)
            for path, value in (
                (proc / "stat", stat()),
                (proc / "task/123/stat", stat()),
                (proc / "io", "read_bytes: 10\n"),
                (proc / "status", "Name: service\n"),
                (proc / "cmdline", "service\0"),
            ):
                path.write_text(value)
            for name, selection in (
                ("rfpoll", ("--path", "/proc/123/stat")),
                ("pspider", ("--pid", "123")),
            ):
                with self.subTest(tool=name):
                    common = ("--root", directory, "--duration-ms", "1250", *selection)
                    default = self.command(name, *common).stdout
                    slow = self.command(name, *common, "--interval-ms", "1000").stdout
                    streams = [
                        [json.loads(line) for line in data.splitlines()] for data in (default, slow)
                    ]
                    for events in streams:
                        self.assertEqual(events[0]["schema"], name)
                        self.assertEqual(events[-1]["type"], "end")
                        self.assertFalse(any(event["type"] == "read_error" for event in events))
                    counts = [
                        sum(event["type"] == "sample" for event in events) for events in streams
                    ]
                    self.assertGreaterEqual(counts[0], 2)
                    self.assertGreater(counts[0], counts[1])
                    self.assertGreater(len(default), len(slow))

    def network(self):
        try:
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                return s.getsockname()[1]
        except PermissionError:
            self.skipTest("socket creation denied by environment")

    def test_pinned_workload_exit_and_default_processing(self):
        from vsm.models import Schema
        from vsm.processing import Params, load_native
        from vsm.processor.pid_stat import PidStatProcessor
        from vsm.processor.smaps import SmapsProcessor

        for tool, name, processor in (
            ("pspider", "pid_stat", PidStatProcessor()),
            ("rfpoll", "smaps", SmapsProcessor()),
        ):
            with self.subTest(tool=tool), tempfile.TemporaryDirectory() as directory:
                workload = subprocess.Popen(["/bin/sleep", "30"])
                collector = None
                try:
                    raw = Path(f"/proc/{workload.pid}/stat").read_text()
                    start = int(raw[raw.rfind(")") + 2 :].split()[19])
                    args = [
                        str(native_artifact(tool)),
                        "--process",
                        f"{workload.pid}:{start}",
                        "--interval-ms",
                        "20",
                        "--duration-ms",
                        "800",
                    ]
                    args += (
                        ["--self-only"]
                        if tool == "pspider"
                        else ["--path", f"/proc/{workload.pid}/smaps_rollup"]
                    )
                    collector = subprocess.Popen(
                        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE
                    )
                    first = []
                    while True:
                        line = collector.stdout.readline()
                        self.assertTrue(line, "collector exited before readiness")
                        first.append(line)
                        if json.loads(line)["type"] == "ready":
                            break
                    workload.terminate()
                    workload.wait(timeout=3)
                    rest, errors = collector.communicate(timeout=3)
                    self.assertEqual(collector.returncode, 0, errors.decode())
                    data = b"".join(first) + rest
                    records = [json.loads(line) for line in data.splitlines()]
                    endings = [e for e in records if e["type"] == "entity_end"]
                    self.assertEqual(len(endings), 1)
                    self.assertFalse(any(e["type"] == "read_error" for e in records))
                    self.assertEqual(records[-1]["type"], "end")
                    path = Path(directory) / "capture.log"
                    path.write_bytes(data)
                    source = load_native(path, schema=Schema(name=name))
                    results = processor.process(source, params=Params())
                    self.assertFalse(results[0].frame.empty)
                    self.assertTrue(all(not d.warnings for d in results))
                    self.assertLess(results[0].frame.time.max(), endings[0]["time_ns"] / 1e9)
                finally:
                    if workload.poll() is None:
                        workload.terminate()
                    workload.wait(timeout=3)
                    if collector is not None and collector.poll() is None:
                        collector.terminate()
                        collector.communicate(timeout=3)

    def test_rgen_udp_traffic(self):
        port = self.network()
        address = f"127.0.0.1:{port}"
        server = subprocess.Popen(
            [
                str(native_artifact("rgen")),
                "consumer",
                "--address",
                address,
                "--protocol",
                "udp",
                "--duration-ms",
                "500",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert server.stdout is not None
            while json.loads(server.stdout.readline())["type"] != "ready":
                pass
            client = self.command(
                "rgen",
                "producer",
                "--address",
                address,
                "--protocol",
                "udp",
                "--duration-ms",
                "100",
                "--rate-bps",
                "1000000",
            )
            summary = next(
                json.loads(line)
                for line in client.stdout.splitlines()
                if json.loads(line)["type"] == "summary"
            )
            self.assertGreater(summary["bytes"], 0)
        finally:
            server.terminate()
            server.communicate(timeout=3)


class NativeAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_host_collection_processing_export(self):
        from vsm.action.collect_for import CollectFor
        from vsm.action.proc_stat import ProcStat
        from vsm.binary import BinaryProvider
        from vsm.connection.host import Host
        from vsm.processing import Params, process
        from vsm.processor.proc_stat import CpuProcessor
        from vsm.runtime import Run

        with tempfile.TemporaryDirectory() as d:
            async with Run(Path(d) / "run", binaries=BinaryProvider(ARTIFACTS)) as run:
                host = await run.connect(Host())
                log = await host.do(CollectFor(collector=ProcStat(interval=20), duration=0.08))
                results = await process(CpuProcessor(), log, params=Params())
                self.assertTrue(log.complete)
                self.assertGreater(len(results[0].frame), 0)
                results[0].to_csv(Path(d) / "cpu.csv")
