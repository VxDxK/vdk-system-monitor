"""Snapshot absence ends observations without synthetic measurement rows."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from support import FakeConnection
from support import TestContext as ContextDouble

from vsm.action.capture import Capture
from vsm.action.cpu_time_in_state import CpuTimeInState
from vsm.action.ss import Ss
from vsm.action.wifi_conn_info import WifiConnInfo
from vsm.models import (
    ActionId,
    CommandFailed,
    Completed,
    DeviceId,
    LogMetadata,
    LogRef,
    Schema,
    TargetInfo,
)
from vsm.processing import Params
from vsm.processor._parsing import native_schema
from vsm.processor.cpu_freq import CpuFreqProcessor
from vsm.processor.cpu_time_in_state import CpuTimeInStateProcessor
from vsm.processor.disk_io import DiskIoProcessor
from vsm.processor.frame_count import FrameCountProcessor
from vsm.processor.net_dev import NetProcessor
from vsm.processor.net_snmp import NetSnmpProcessor
from vsm.processor.net_udp import NetUdpProcessor
from vsm.processor.proc_stat import CpuProcessor
from vsm.processor.socket_stat import SocketStatProcessor
from vsm.processor.ss import SsProcessor
from vsm.processor.top import TopProcessor
from vsm.processor.wifi_conn_info import WifiConnInfoProcessor

UDP_HEADER = "  sl local_address rem_address st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode ref pointer drops\n"
NET_HEADER = "Inter-| Receive | Transmit\n face |bytes packets errs drop fifo |bytes packets errs drop fifo\n"


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def log(self, name, records=None, text=None):
        path = self.root / (name + ".log")
        if text is None:
            records = [
                {"type": "metadata", "schema": native_schema(name)},
                *(records or []),
                {"type": "end"},
            ]
            text = "".join(json.dumps(dict(e, sequence=i)) + "\n" for i, e in enumerate(records))
        path.write_text(text)
        return LogRef(
            path=path,
            schema=Schema(name=name),
            device_id=DeviceId("fixture"),
            action_id=ActionId("capture"),
            complete=True,
            metadata=LogMetadata(
                target=TargetInfo(os="linux", architecture="x86_64", kernel="fixture"),
                provider="test",
            ),
        )

    def file(self, path, text, t=0):
        return {
            "type": "file",
            "path": path,
            "time_ns": t * 1_000_000_000,
            "encoding": "hex",
            "data": text.encode().hex(),
        }

    def commands(self, outputs):
        return "".join(
            json.dumps({"time_ns": t * 1_000_000_000, "stdout": value, "stderr": ""}) + "\n"
            for t, value in enumerate(outputs)
        )

    def interface(self, n):
        return f"eth0: {n} 2 0 0 0 0 0 0 {n} 3 0 0 0 0 0 0\n"

    def disk(self, n):
        return f"8 0 sda 1 0 {n} 0 3 0 {n} 0 0 0 0\n"

    def udp(self, drops):
        return (
            UDP_HEADER
            + f"0: 0100007F:1234 00000000:0000 07 00000000:00000000 00:00000000 00000000 1000 0 99 1 0 {drops}\n"
        )

    def test_valid_empty_snapshots_return_empty_datasets(self):
        cases = [
            (NetProcessor(), "net_dev", [self.file("/proc/net/dev", NET_HEADER)], None),
            (DiskIoProcessor(), "disk_io", [self.file("/proc/diskstats", "")], None),
            (NetUdpProcessor(), "net_udp", [self.file("/proc/net/udp", UDP_HEADER)], None),
            (SocketStatProcessor(), "socket_stat", [{"type": "sample", "time_ns": 0}], None),
            (SsProcessor(), "ss", None, self.commands([""])),
            (WifiConnInfoProcessor(), "wifi_conn_info", None, self.commands([""])),
            (TopProcessor(), "top", None, "top - 12:00\nPID USER %CPU COMMAND\n"),
        ]
        for processor, name, records, text in cases:
            with self.subTest(name=name):
                (data,) = processor.process(self.log(name, records, text), params=Params())
                self.assertTrue(data.frame.empty)
                self.assertTrue(len(data.columns))
                self.assertEqual(data.charts, ())
                self.assertEqual(data.warnings, ())

    def test_interface_and_disk_rates_restart_after_observed_absence(self):
        for processor, name, path, row, empty, rate in (
            (
                NetProcessor(),
                "net_dev",
                "/proc/net/dev",
                self.interface,
                NET_HEADER,
                "rx_bytes_per_second",
            ),
            (
                DiskIoProcessor(),
                "disk_io",
                "/proc/diskstats",
                self.disk,
                "",
                "read_bytes_per_second",
            ),
        ):
            with self.subTest(name=name):
                records = [
                    self.file(path, row(100), 0),
                    self.file(path, row(150), 1),
                    self.file(path, empty, 2),
                    self.file(path, row(300), 3),
                    self.file(path, row(350), 4),
                ]
                (data,) = processor.process(self.log(name, records), params=Params())
                self.assertEqual(data.frame.observation_epoch.tolist(), [0, 0, 1, 1])
                self.assertEqual(data.frame.time.tolist(), [0, 1, 3, 4])
                self.assertTrue(data.frame[rate].iloc[[0, 2]].isna().all())
                self.assertGreater(data.frame[rate].iloc[1], 0)
                self.assertTrue(
                    all(
                        "observation_epoch" in chart.group_by
                        for chart in data.charts
                        if hasattr(chart, "group_by")
                    )
                )

    def test_cpu_hotplug_restarts_only_the_missing_core_baseline(self):
        records = [
            self.file("/proc/stat", "cpu 10 0 5 100\ncpu0 10 0 5 100\n", 0),
            self.file("/proc/stat", "cpu 20 0 10 200\n", 1),
            self.file("/proc/stat", "cpu 30 0 15 300\ncpu0 30 0 15 300\n", 2),
        ]
        (data,) = CpuProcessor().process(self.log("proc_stat", records), params=Params())
        core = data.frame[data.frame.cpu == "cpu0"]
        self.assertEqual(core.observation_epoch.tolist(), [0, 1])
        self.assertTrue(core.busy_percent.isna().all())
        self.assertFalse(data.frame[data.frame.cpu == "cpu"].busy_percent.iloc[1:].isna().any())

    def test_udp_family_snapshots_are_independent_and_reuse_restarts_rates(self):
        records = []
        for t, content in enumerate((self.udp(10), self.udp(12), UDP_HEADER, self.udp(20))):
            records += [
                self.file("/proc/net/udp", content, t),
                self.file("/proc/net/udp6", UDP_HEADER, t),
            ]
        (data,) = NetUdpProcessor().process(self.log("net_udp", records), params=Params())
        self.assertEqual(data.frame.observation_epoch.tolist(), [0, 0, 1])
        self.assertEqual(data.frame.drops_per_second.iloc[1], 2)
        self.assertTrue(data.frame.drops_per_second.iloc[[0, 2]].isna().all())
        self.assertTrue(all("observation_epoch" in c.group_by for c in data.charts))

    def test_ss_wifi_and_top_do_not_join_reappearing_entities(self):
        ss = "ESTAB 0 0 127.0.0.1:1 127.0.0.1:2\n rtt:1.0 cwnd:10\n"
        station = "Station aa:bb:cc:dd:ee:ff (on wlan0)\n signal: -50 dBm\n"
        top_header = "top - 12:00\nTasks: 1 total\nPID USER %CPU COMMAND\n"
        for processor, name, content in (
            (SsProcessor(), "ss", self.commands([ss, "", ss])),
            (WifiConnInfoProcessor(), "wifi_conn_info", self.commands([station, "", station])),
            (
                TopProcessor(),
                "top",
                top_header
                + "123 user 10 worker\n"
                + top_header
                + top_header
                + "123 user 10 worker\n",
            ),
        ):
            with self.subTest(name=name):
                results = processor.process(self.log(name, text=content), params=Params())
                self.assertEqual(results[0].frame.observation_epoch.tolist(), [0, 1])
                self.assertTrue(all("observation_epoch" in c.group_by for c in results[0].charts))
                if name == "wifi_conn_info":
                    self.assertEqual(len(results), 3)
                if name == "top":
                    self.assertEqual(results[0].frame.snapshot.tolist(), [1, 3])

    def test_socket_cookie_identity_and_snapshot_absence(self):
        for cookie, reliable in (
            ([1, 2], True),
            ([], False),
            ([1], False),
            ([True, 2], False),
            ([4294967295, 4294967295], False),
        ):
            with self.subTest(cookie=cookie):
                records = []
                for t in range(4):
                    records.append({"type": "sample", "time_ns": t * 1_000_000_000})
                    if t == 2:
                        continue
                    records.append(
                        {
                            "type": "socket",
                            "time_ns": t * 1_000_000_000,
                            "cookie": cookie,
                            "inode": 10,
                            "source": "127.0.0.1",
                            "destination": "127.0.0.1",
                            "source_port": 1,
                            "destination_port": 2,
                            "receive_queue": 0,
                            "send_queue": 0,
                            **{
                                c: 100 + t * 10
                                for c in (
                                    "bytes_acked",
                                    "bytes_received",
                                    "bytes_sent",
                                    "bytes_retransmitted",
                                )
                            },
                        }
                    )
                (data,) = SocketStatProcessor().process(
                    self.log("socket_stat", records), params=Params()
                )
                self.assertEqual(data.frame.observation_epoch.tolist(), [0, 0, 1])
                if reliable:
                    self.assertEqual(data.frame.bytes_acked_per_second.iloc[1], 10)
                    self.assertTrue(data.frame.bytes_acked_per_second.iloc[[0, 2]].isna().all())
                else:
                    self.assertNotIn("bytes_acked_per_second", data.frame)

    def test_unexpected_read_errors_and_malformed_snapshots_remain_errors(self):
        for processor, name, records, text in (
            (NetUdpProcessor(), "net_udp", [self.file("/proc/net/udp", "garbage\n")], None),
            (NetProcessor(), "net_dev", [self.file("/proc/net/dev", "garbage\n")], None),
            (DiskIoProcessor(), "disk_io", [self.file("/proc/diskstats", "garbage\n")], None),
            (SsProcessor(), "ss", None, self.commands(["command failed"])),
            (WifiConnInfoProcessor(), "wifi_conn_info", None, self.commands(["command failed"])),
            (TopProcessor(), "top", None, "garbage\n"),
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                processor.process(self.log(name, records, text), params=Params())
        for processor, name in ((NetProcessor(), "net_dev"), (CpuFreqProcessor(), "cpu_freq")):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "read errors"):
                processor.process(
                    self.log(
                        name, [{"type": "read_error", "path": "/missing", "error": "not found"}]
                    ),
                    params=Params(),
                )

    def test_diagnostic_read_error_opt_in_invalidates_counter_baseline(self):
        records = [
            self.file("/proc/net/dev", self.interface(100), 0),
            {
                "type": "read_error",
                "path": "/proc/net/dev",
                "error": "denied",
                "time_ns": 1_000_000_000,
            },
            self.file("/proc/net/dev", self.interface(300), 2),
        ]
        (data,) = NetProcessor().process(
            self.log("net_dev", records), params=Params(allow_read_errors=True)
        )
        self.assertTrue(data.frame.rx_bytes_per_second.isna().all())
        self.assertEqual(data.frame.observation_epoch.tolist(), [0, 1])
        self.assertTrue(data.warnings)

    def test_station_header_without_numeric_metrics_does_not_mean_absence(self):
        header = "Station AA:BB:CC:DD:EE:FF (on wlan0)\n"
        content = self.commands(
            [header + " signal: -50 dBm\n", header, header + " signal: -40 dBm\n"]
        )
        results = WifiConnInfoProcessor().process(
            self.log("wifi_conn_info", text=content), params=Params()
        )
        self.assertEqual(results[0].frame.observation_epoch.tolist(), [0, 0])
        self.assertEqual(results[0].frame.station.tolist(), ["aa:bb:cc:dd:ee:ff"] * 2)
        self.assertEqual(len(results), 2)

    def test_snmp_counter_reappearance_starts_a_new_baseline(self):
        records = [
            self.file("/proc/net/snmp", "Udp: InDatagrams OutDatagrams\nUdp: 10 20\n", 0),
            self.file("/proc/net/snmp", "Udp: OutDatagrams\nUdp: 30\n", 1),
            self.file("/proc/net/snmp", "Udp: InDatagrams OutDatagrams\nUdp: 50 40\n", 2),
        ]
        (data,) = NetSnmpProcessor().process(self.log("net_snmp", records), params=Params())
        incoming = data.frame[data.frame.counter == "InDatagrams"]
        self.assertEqual(incoming.observation_epoch.tolist(), [0, 1])
        self.assertTrue(incoming.value_per_second.isna().all())
        outgoing = data.frame[data.frame.counter == "OutDatagrams"]
        self.assertEqual(outgoing.value_per_second.iloc[1:].tolist(), [10, 10])

    def test_frame_rate_does_not_bridge_a_recorded_read_failure(self):
        path = "/sys/fb/frame_count"
        records = [
            self.file(path, "10\n", 0),
            {"type": "read_error", "path": path, "error": "denied"},
            self.file(path, "30\n", 2),
        ]
        (data,) = FrameCountProcessor().process(
            self.log("frame_count", records), params=Params(allow_read_errors=True)
        )
        self.assertTrue(data.frame.frames_per_second.isna().all())
        self.assertEqual(data.frame.observation_epoch.tolist(), [0, 1])

    def test_new_frequency_has_no_fabricated_zero_residency_baseline(self):
        source = self.log(
            "cpu_time_in_state",
            text=json.dumps(
                {"before": {"cpu0": "1000 10\n"}, "after": {"cpu0": "1000 15\n2000 30\n"}}
            ),
        )
        (data,) = CpuTimeInStateProcessor().process(source, params=Params())
        self.assertEqual(data.frame.residency_ticks.iloc[0], 5)
        self.assertTrue(data.frame.residency_ticks.iloc[1:].isna().all())
        self.assertFalse(data.frame.counter_reset.any())


class PeriodicFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_final_residency_read_preserves_before_and_partial_after(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = FakeConnection()
            ctx = ContextDouble(conn, tmp)
            path = "/sys/devices/system/cpu/cpu0/cpufreq/stats/time_in_state"
            with self.assertRaises(FileNotFoundError):
                async with CpuTimeInState().open(ctx) as capture:
                    del conn.files[path]
            self.assertFalse(capture.log.complete)
            raw = json.loads(capture.log.path.read_text())
            self.assertIn(path, raw["before"])
            self.assertEqual(raw["read_errors"][0]["path"], path)
            with self.assertRaisesRegex(ValueError, "invalid snapshot"):
                CpuTimeInStateProcessor().process(capture.log, params=Params())

    async def test_failed_first_command_does_not_hang_waiting_for_readiness(self):
        for action in (Ss(), WifiConnInfo(interface="wlan0")):
            with self.subTest(action=type(action).__name__), tempfile.TemporaryDirectory() as tmp:
                conn = FakeConnection()
                ctx = ContextDouble(conn, tmp)
                error = CommandFailed(Completed(exit_code=1, stderr=b"failed query"))
                captured = []
                finalize = Capture.finalize

                def record(capture, log, captured=captured, finalize=finalize):
                    captured.append(log)
                    finalize(capture, log)

                with (
                    patch.object(conn, "run", AsyncMock(side_effect=error)),
                    patch.object(Capture, "finalize", record),
                    self.assertRaises(CommandFailed),
                ):
                    async with asyncio.timeout(1):
                        async with action.open(ctx):
                            self.fail("failed command became ready")
                await asyncio.gather(*ctx.workers, return_exceptions=True)
                self.assertTrue(captured)
                self.assertFalse(captured[-1].complete)

    async def test_later_failure_preserves_incomplete_wifi_log_and_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = FakeConnection()
            ctx = ContextDouble(conn, tmp)
            failed = asyncio.Event()
            calls = 0

            async def query(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls > 1:
                    failed.set()
                    raise CommandFailed(Completed(exit_code=1))
                return Completed(exit_code=0, stdout=b"")

            with patch.object(conn, "run", query):
                with self.assertRaises(CommandFailed):
                    async with WifiConnInfo(interface="wlan0", interval=1).open(ctx) as capture:
                        await asyncio.wait_for(failed.wait(), 1)
                self.assertEqual(capture.log.schema.name, "wifi_conn_info")
                self.assertFalse(capture.log.complete)
            await asyncio.gather(*ctx.workers, return_exceptions=True)
