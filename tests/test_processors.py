import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from support import stat

from vsm.models import ActionId, DeviceId, LogMetadata, LogRef, Schema, TargetInfo
from vsm.processing import Dataset, Params, load_native
from vsm.processor._parsing import endpoint, events, native_schema, rate_rows
from vsm.processor.pid_stat import PidStatProcessor
from vsm.processor.registry import PROCESSORS, FileProcessor

FILES = {
    "battery_level": ("/sys/battery/capacity", "55\n"),
    "cpu_freq": ("/sys/cpu/cpufreq/scaling_cur_freq", "2000000\n"),
    "ddr_freq": ("/sys/ddr/cur_freq", "800000000\n"),
    "gpu_freq": ("/sys/gpu/cur_freq", "400000000\n"),
    "npu_freq": ("/sys/npu/cur_freq", "300000000\n"),
    "frame_count": ("/sys/fb/frame_count", "10\n"),
    "meminfo": ("/proc/meminfo", "MemTotal: 100 kB\nMemAvailable: 25 kB\nCached: 10 kB\n"),
    "swap": ("/proc/meminfo", "SwapTotal: 0 kB\nSwapFree: 0 kB\n"),
    "smaps": ("/proc/123/smaps_rollup", "Rss: 10 kB\nPss: 5 kB\n"),
    "net_dev": ("/proc/net/dev", "eth0: 100 2 0 0 0 0 0 0 200 3 0 0 0 0 0 0\n"),
    "net_snmp": ("/proc/net/snmp", "Udp: InDatagrams OutDatagrams\nUdp: 10 20\n"),
    "net_udp": (
        "/proc/net/udp",
        "header\n0: 0100007F:1234 00000000:0000 07 00000000:00000000 00:00000000 00000000 1000 0 99\n",
    ),
    "proc_stat": ("/proc/stat", "cpu 100 0 50 850 0 0 0 0 10 0\ncpu0 100 0 50 850 0 0 0 0\n"),
    "temperature": ("/sys/class/thermal/thermal_zone0/temp", "42000\n"),
    "fans": ("/sys/class/hwmon/hwmon0/fan1_input", "1200\n"),
    "disk_io": ("/proc/diskstats", "8 0 sda 1 0 2 0 3 0 4 0 0 0 0\n"),
    "load_average": ("/proc/loadavg", "1.2 0.8 0.5 2/100 123\n"),
    "uptime": ("/proc/uptime", "100.0 300.0\n"),
}


class ProcessorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.target = TargetInfo(
            os="linux",
            architecture="x86_64",
            kernel="test",
            clock_ticks_per_second=250,
            page_size_bytes=8192,
        )

    def tearDown(self):
        self.temp.cleanup()

    def log(self, name, records=None, text=None, complete=True):
        path = self.root / (name + ".log")
        if text is None:
            if name in ("pid_stat", "process_io", "smaps"):
                for record in records or []:
                    if record["type"] == "read_error":
                        record.update(
                            path=record.get("path", "/proc/123/stat"),
                            tags={
                                "pid": 123,
                                "tid": 123,
                                "scope": "process",
                                "start_ticks": 99,
                                "process_start_ticks": 99,
                            },
                            errno=2,
                            operation="read",
                        )
            data = [
                {"type": "metadata", "schema": native_schema(name), "time_ns": 0},
                *(records or []),
                {"type": "end", "time_ns": 2000000000},
            ]
            path.write_text("".join(json.dumps(e) + "\n" for e in data))
        else:
            path.write_text(text)
        return LogRef(
            path=path,
            schema=Schema(name=name),
            device_id=DeviceId("fixture-device"),
            action_id=ActionId("fixture-action"),
            complete=complete,
            metadata=LogMetadata(target=self.target, provider="test"),
        )

    def file(self, path, value, t=0, tags=None):
        if path.startswith("/proc/123/"):
            tags = {
                "pid": 123,
                "tid": 123,
                "scope": "process",
                "start_ticks": 99,
                "process_start_ticks": 99,
                **(tags or {}),
            }
        return {
            "type": "file",
            "path": path,
            "encoding": "hex",
            "data": value.encode().hex(),
            "time_ns": t,
            "tags": tags or {},
        }

    def test_all_32_processor_entrypoints(self):
        self.assertEqual(len(PROCESSORS), 32)
        for name, processor in PROCESSORS.items():
            with self.subTest(processor=name):
                if name in FILES:
                    path, value = FILES[name]
                    log = self.log(
                        name, [self.file(path, value, 0), self.file(path, value, 1000000000)]
                    )
                elif name == "filesystem_usage":
                    log = self.log(
                        name,
                        [
                            {
                                "type": "filesystem",
                                "path": "/",
                                "total_bytes": 1000,
                                "free_bytes": 400,
                                "available_bytes": 300,
                                "time_ns": 0,
                            }
                        ],
                    )
                elif name in ("pid_stat", "process_io"):
                    records = []
                    for t, ticks in ((0, 100), (1000000000, 350)):
                        records.extend(
                            [
                                self.file(
                                    "/proc/123/stat",
                                    stat(ticks=ticks),
                                    t,
                                    {"pid": 123, "tid": 123, "scope": "process"},
                                ),
                                self.file(
                                    "/proc/123/task/123/stat",
                                    stat(ticks=ticks),
                                    t,
                                    {"pid": 123, "tid": 123, "scope": "thread"},
                                ),
                                self.file(
                                    "/proc/123/io",
                                    f"read_bytes: {ticks}\nwrite_bytes: {ticks}\n",
                                    t,
                                    {"pid": 123},
                                ),
                            ]
                        )
                    log = self.log(name, records)
                elif name == "socket_stat":
                    log = self.log(
                        name,
                        [
                            {
                                "type": "socket",
                                "time_ns": 0,
                                "source": "127.0.0.1",
                                "destination": "127.0.0.1",
                                "source_port": 1,
                                "destination_port": 2,
                                "cookie": [1, 2],
                                "state": 1,
                                "receive_queue": 2,
                                "send_queue": 3,
                            }
                        ],
                    )
                elif name == "traffic_generator":
                    log = self.log(
                        name,
                        [
                            {
                                "type": "traffic",
                                "time_ns": 0,
                                "bits_per_second": 1000,
                                "operations_per_second": 10,
                                "operation_unit": "datagrams",
                            }
                        ],
                    )
                elif name == "rup":
                    log = load_native(
                        Path(__file__).parent / "fixtures/rup/mixed.jsonl",
                        schema=Schema(name="rup"),
                    )
                elif name == "cpu_time_in_state":
                    log = self.log(
                        name,
                        text=json.dumps(
                            {"before": {"cpu0": "1000 10\n"}, "after": {"cpu0": "1000 15\n"}}
                        ),
                    )
                elif name == "ping":
                    log = self.log(
                        name, text="64 bytes from 127.0.0.1: icmp_seq=1 ttl=64 time=0.15 ms\n"
                    )
                elif name == "time":
                    log = self.log(name, text="real 1.2\nuser 0.5\nsys 0.1\n")
                elif name == "fio":
                    log = self.log(
                        name,
                        text=json.dumps(
                            {
                                "jobs": [
                                    {
                                        "jobname": "test",
                                        "read": {"io_bytes": 4096, "bw": 4, "iops": 1},
                                    }
                                ]
                            }
                        ),
                    )
                elif name == "iperf":
                    log = self.log(
                        name,
                        text=json.dumps(
                            {
                                "intervals": [
                                    {
                                        "streams": [
                                            {
                                                "start": 0,
                                                "end": 1,
                                                "bits_per_second": 8000,
                                                "bytes": 1000,
                                            }
                                        ]
                                    }
                                ]
                            }
                        ),
                    )
                elif name == "top":
                    log = self.log(
                        name, text="top - 12:00\nPID USER %CPU COMMAND\n123 user 10.0 worker\n"
                    )
                elif name == "ss":
                    log = self.log(
                        name,
                        text=json.dumps(
                            {
                                "time_ns": 0,
                                "stdout": "ESTAB 0 1 127.0.0.1:1 127.0.0.1:2\n rtt:1.5 cwnd:10\n",
                            }
                        )
                        + "\n",
                    )
                elif name == "wifi_conn_info":
                    log = self.log(
                        name,
                        text=json.dumps(
                            {
                                "time_ns": 0,
                                "stdout": "Station aa:bb:cc:dd:ee:ff (on wlan0)\n signal: -50 dBm\n rx bytes: 100\n",
                            }
                        )
                        + "\n",
                    )
                else:
                    self.fail(name)
                results = processor.process(log, params=Params())
                self.assertTrue(results)
                self.assertTrue(any(result.charts for result in results))
                self.assertTrue(any(not d.frame.empty for d in results))
                for i, data in enumerate(results):
                    path = self.root / f"{name}-{i}.csv"
                    data.to_csv(path)
                    self.assertTrue(path.exists())
                    self.assertTrue(path.with_suffix(".csv.json").exists())
                    restored = Dataset.from_csv(path)
                    pd.testing.assert_frame_equal(data.frame, restored.frame)
                    self.assertEqual(data.charts, restored.charts)
                    self.assertEqual(data.columns, restored.columns)
                    self.assertEqual(data.provenance, restored.provenance)
                    from matplotlib.backends.backend_agg import FigureCanvasAgg

                    for chart in data.charts:
                        figure = chart.as_plotly(data.frame)
                        self.assertGreater(len(figure.data), 0)
                        static = chart.as_matplotlib(data.frame)
                        FigureCanvasAgg(static).draw()
                        static.clear()

    def test_process_read_errors_are_strict_by_default_and_retained_on_opt_in(self):
        from vsm.processor.process_io import ProcessIoProcessor

        for processor, name in (
            (PidStatProcessor(), "pid_stat"),
            (ProcessIoProcessor(), "process_io"),
        ):
            with self.subTest(name=name):
                log = self.log(
                    name,
                    [
                        self.file("/proc/123/stat", stat(), tags={"pid": 123, "scope": "process"}),
                        self.file("/proc/123/io", "read_bytes: 10\n", tags={"pid": 123}),
                        {"type": "read_error", "pid": 123, "error": "process exited", "time_ns": 1},
                        {
                            "type": "read_error",
                            "path": "/proc/123/stat",
                            "error": "not found",
                            "time_ns": 2,
                        },
                    ],
                )
                with self.assertRaisesRegex(ValueError, "read errors"):
                    processor.process(log, params=Params())
                results = processor.process(log, params=Params(allow_read_errors=True))
                for data in results:
                    self.assertIn("/proc/123/stat: process exited", data.warnings)
                    self.assertIn("/proc/123/stat: not found", data.warnings)
                from dataclasses import replace

                with self.assertRaisesRegex(ValueError, "incomplete"):
                    processor.process(
                        replace(log, complete=False), params=Params(allow_read_errors=True)
                    )
                log.path.write_text("not JSON\n")
                with self.assertRaisesRegex(ValueError, "JSONL"):
                    processor.process(log, params=Params(allow_read_errors=True))

    def test_correct_target_units(self):
        es = [
            self.file("/proc/123/stat", stat(ticks=100), 0, {"pid": 123, "scope": "process"}),
            self.file(
                "/proc/123/stat", stat(ticks=350), 1000000000, {"pid": 123, "scope": "process"}
            ),
        ]
        rows = PidStatProcessor().process(self.log("pid_stat", es), params=Params())[0].frame
        self.assertEqual(rows.iloc[1]["user_percent"], 100)
        self.assertEqual(rows.iloc[1]["rss_bytes"], 81920)
        path, data = FILES["meminfo"]
        frame = (
            FileProcessor("meminfo")
            .process(self.log("meminfo", [self.file(path, data)]), params=Params())[0]
            .frame
        )
        self.assertEqual(frame.iloc[0]["MemTotal"], 102400)
        self.assertEqual(frame.iloc[0]["used_percent"], 75)

    def test_cpu_guest_not_double_counted_and_reset(self):
        rows = rate_rows(
            [
                {"time": 0, "id": "a", "bytes": 10},
                {"time": 1, "id": "a", "bytes": 20},
                {"time": 2, "id": "a", "bytes": 1},
            ],
            ("id",),
            ("bytes",),
        )
        self.assertIsNone(rows[0]["bytes_per_second"])
        self.assertEqual(rows[1]["bytes_per_second"], 10)
        self.assertTrue(rows[2]["counter_reset"])
        self.assertIsNone(rows[2]["bytes_per_second"])
        p, v = FILES["proc_stat"]
        frame = (
            FileProcessor("proc_stat")
            .process(self.log("proc_stat", [self.file(p, v)]), params=Params())[0]
            .frame
        )
        self.assertEqual(frame.iloc[0]["total"], 1000)

    def test_zero_swap(self):
        p, v = FILES["swap"]
        frame = (
            FileProcessor("swap")
            .process(self.log("swap", [self.file(p, v)]), params=Params())[0]
            .frame
        )
        self.assertIsNone(frame.iloc[0]["used_percent"])
        self.assertFalse(frame.iloc[0]["swap_present"])

    def test_errors_not_empty_success(self):
        log = self.log(
            "proc_stat",
            [{"type": "read_error", "path": "/proc/stat", "error": "denied", "time_ns": 0}],
        )
        with self.assertRaises(ValueError):
            FileProcessor("proc_stat").process(log, params=Params())
        frame = FileProcessor("proc_stat").process(log, params=Params(allow_read_errors=True))[0]
        self.assertTrue(frame.warnings)
        with self.assertRaises(ValueError):
            FileProcessor("net_dev").process(log, params=Params())
        with self.assertRaises(ValueError):
            events(self.log("proc_stat", complete=False), Params())

    def test_truncation_and_end_detection(self):
        log = self.log("proc_stat", text='{"type":"metadata","schema":"rfpoll"}\n{"type":')
        with self.assertRaises(ValueError):
            events(log, Params())
        self.assertEqual(len(events(log, Params(allow_partial=True))), 1)

    def test_endpoint_decode(self):
        self.assertEqual(endpoint("0100007F:1234"), "127.0.0.1:4660")


class MetadataTests(unittest.TestCase):
    setUp = ProcessorTests.setUp
    tearDown = ProcessorTests.tearDown
    log = ProcessorTests.log
    file = ProcessorTests.file

    def test_provenance_and_sequence_validation(self):
        p, v = FILES["proc_stat"]
        log = self.log("proc_stat", [self.file(p, v)])
        result = FileProcessor("proc_stat").process(log, params=Params())[0]
        self.assertEqual(result.provenance.path, log.path)
        events_list = [json.loads(line) for line in log.path.read_text().splitlines()]
        for i, e in enumerate(events_list):
            e["sequence"] = i + 1
        log.path.write_text("".join(json.dumps(e) + "\n" for e in events_list))
        with self.assertRaises(ValueError):
            events(log, Params())

    def test_standalone_native_import(self):
        from vsm.processing import load_native

        log = self.log("proc_stat", [self.file(*FILES["proc_stat"])])
        imported = load_native(log.path, schema=Schema(name="proc_stat"))
        self.assertTrue(imported.complete)
        self.assertEqual(imported.path, log.path)

    def test_missing_memory_cannot_be_zero(self):
        log = self.log("meminfo", [self.file("/proc/meminfo", "")])
        with self.assertRaises(ValueError):
            FileProcessor("meminfo").process(log, params=Params())
