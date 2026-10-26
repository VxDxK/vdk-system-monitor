"""Chart composition, identity preservation and offline report regressions."""

import json
import re
import unittest

import pandas as pd
import test_processors as fixtures
from matplotlib.backends.backend_agg import FigureCanvasAgg

from vsm.chart.line import Line
from vsm.chart.table import Table
from vsm.output.charts import Backend, ExportOptions, Layout, export_charts
from vsm.processing import Dataset, Params, dataset
from vsm.processor.battery_level import BatteryLevelProcessor
from vsm.processor.cpu_freq import CpuFreqProcessor
from vsm.processor.net_dev import NetProcessor
from vsm.processor.net_snmp import NetSnmpProcessor
from vsm.processor.net_udp import NetUdpProcessor
from vsm.processor.pid_stat import PidStatProcessor
from vsm.processor.proc_stat import CpuProcessor
from vsm.processor.temperature import TemperatureProcessor
from vsm.processor.traffic_generator import TrafficGeneratorProcessor
from vsm.processor.wifi_conn_info import WifiConnInfoProcessor
from vsm.util.labels import label_frame


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ProcessorTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root

    def process_files(self, processor, records):
        return processor.process(self.fixture.log(processor.name, records), params=Params())[0]

    def test_cpu_labels_csv_and_original_identity(self):
        paths = [f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_cur_freq" for i in (0, 1)]
        data = self.process_files(
            CpuFreqProcessor(),
            [self.fixture.file(path, "2000000", t) for t in (0, 1000000000) for path in paths],
        )
        self.assertEqual(data.frame.entity_label.tolist(), ["cpu0", "cpu1"] * 2)
        self.assertEqual(
            [trace.name for trace in data.charts[0].as_plotly(data.frame).data], ["cpu0", "cpu1"]
        )
        data.to_csv(self.root / "cpu.csv")
        csv = pd.read_csv(self.root / "cpu.csv")
        self.assertEqual(csv.path.tolist(), data.frame.path.tolist())
        self.assertEqual(csv.frequency_hz.tolist(), [2e9] * 4)

    def test_duplicate_labels_never_merge_and_input_is_unchanged(self):
        original = pd.DataFrame(
            {"time": [0, 0, 1, 1], "path": ["/a/temp", "/b/temp"] * 2, "value": [1, 2, 3, 4]}
        )
        snapshot = original.copy(deep=True)
        frame = label_frame(original)
        self.assertEqual(frame.entity_label.nunique(), 2)
        chart = Line(label="Sensors", y=("value",), group_by=("path",))
        self.assertEqual(len(chart.as_plotly(frame).data), 2)
        export_charts(
            (Dataset(frame=original, group_label="Sensors", data_label="temp", charts=(chart,)),),
            self.root / "report.html",
        )
        pd.testing.assert_frame_equal(original, snapshot)

    def test_html_escapes_script_data_and_sources_are_secondary(self):
        value = "</script><img src=x onerror=alert(1)>"
        data = dataset(
            "CPU",
            [{"time": 0, "cpu": value, "load": 1}],
            charts=(Line(label=value, y=("load",), group_by=("cpu",)),),
        ).with_source(self.fixture.log("proc_stat"))
        path = self.root / "report.html"
        export_charts((data,), path, title=value)
        html = path.read_text()
        self.assertNotIn("<img src=x", html)
        payloads = re.findall(r"data-figure='[^']+'>(.*?)</script>", html, re.DOTALL)
        self.assertEqual(len(payloads), 1)
        self.assertIn(value, json.loads(payloads[0])["data"][0]["name"])
        self.assertIn("Source details", html)
        self.assertNotIn(str(data.provenance.path), re.search(r"<summary>(.*?)</summary>", html)[1])
        self.assertNotIn("overflow:auto", html)
        self.assertIn("ResizeObserver", html)
        self.assertIn("aria-pressed", html)
        self.assertNotIn("<iframe", html)

    def test_many_series_static_legend_keeps_all_labels(self):
        frame = pd.DataFrame(
            {"time": [0] * 25, "cpu": [f"cpu{i}" for i in range(25)], "value": range(25)}
        )
        chart = Line(label="CPU", y=("value",), group_by=("cpu",))
        figure = chart.as_matplotlib(frame)
        self.assertEqual(len(figure.axes[0].get_legend().get_texts()), 25)
        FigureCanvasAgg(figure).draw()
        figure.clear()

    def test_cpu_state_composition_and_correct_combined_user(self):
        data = self.process_files(
            CpuProcessor(),
            [
                self.fixture.file(
                    "/proc/stat",
                    f"cpu0 {10 + t} {2 + t} {3 + t} {40 + t} {4 + t} {5 + t} {6 + t} {7 + t} 100 100",
                    t * 1000000000,
                )
                for t in (0, 1)
            ],
        )
        labels = [chart.label for chart in data.charts if not chart.additional]
        self.assertEqual(len(labels), 9)
        row = data.frame.iloc[1]
        self.assertAlmostEqual(row.user_percent, row.user_only_percent + row.nice_percent)
        self.assertAlmostEqual(row.user_only_percent, 12.5)
        short = self.process_files(
            CpuProcessor(),
            [
                self.fixture.file("/proc/stat", "cpu0 1 2 3 4", 0),
                self.fixture.file("/proc/stat", "cpu0 2 3 4 5", 1000000000),
            ],
        )
        self.assertNotIn("steal_percent", short.frame)

    def test_process_and_thread_composition_binding_and_pid_reuse(self):
        records = []
        for t, ticks in ((0, 100), (1, 350)):
            raw = fixtures.stat(ticks=ticks).strip().split(") ", 1)
            fields = raw[1].split() + ["0"] * 15
            fields[36] = "3"
            text = raw[0] + ") " + " ".join(fields)
            for scope, path in (
                ("process", "/proc/123/stat"),
                ("thread", "/proc/123/task/123/stat"),
            ):
                records.append(
                    self.fixture.file(path, text, t * 1000000000, {"pid": 123, "scope": scope})
                )
        result = PidStatProcessor().process(self.fixture.log("pid_stat", records), params=Params())
        self.assertEqual(result[0].group_label, result[1].group_label)
        self.assertEqual(
            [len([c for c in data.charts if not c.additional]) for data in result], [2, 4]
        )
        self.assertEqual(result[1].frame.cpu_binding.tolist(), [3, 3])
        self.assertEqual(result[1].frame.busy_percent.iloc[1], 100)
        for chart in result[1].charts[:3]:
            self.assertEqual(chart.y_range, (0, 100))
            self.assertEqual(tuple(chart.as_plotly(result[1].frame).layout.yaxis.range), (0, 100))
        binding = result[1].charts[3]
        self.assertEqual(binding.label, "Thread last CPU")
        self.assertTrue(binding.integer_y)
        self.assertTrue(binding.step)
        process_cpu = next(chart for chart in result[0].charts if chart.label == "Process CPU")
        self.assertIsNone(process_cpu.y_range)
        frame = result[0].frame.copy()
        frame.loc[frame.index[-1], "user_percent"] = 250
        plot = process_cpu.as_plotly(frame)
        self.assertEqual(plot.data[0].y[-1], 250)
        self.assertIsNone(plot.layout.yaxis.range)

    def test_frequency_and_interface_display_units(self):
        frequency = self.process_files(
            CpuFreqProcessor(),
            [self.fixture.file("/sys/cpu/cpufreq/scaling_cur_freq", "2000000", 0)],
        )
        self.assertEqual(frequency.frame.frequency_hz.iloc[0], 2_000_000_000)
        chart = frequency.charts[0]
        self.assertEqual(chart.ylabel, "GHz")
        self.assertEqual(chart.as_plotly(frequency.frame).data[0].y[0], 2)
        network = self.process_files(
            NetProcessor(),
            [
                self.fixture.file(
                    "/proc/net/dev",
                    f"eth0: {value} 2 0 0 0 0 0 0 {value} 3 0 0 0 0 0 0\n",
                    t,
                )
                for t, value in ((0, 0), (1_000_000_000, 1_048_576))
            ],
        )
        self.assertFalse(any(isinstance(chart, Table) for chart in network.charts))
        self.assertIn("rx_bytes", network.frame)
        throughput = network.charts[0]
        self.assertEqual(throughput.ylabel, "MiB/s")
        self.assertEqual(throughput.as_plotly(network.frame).data[0].y[1], 1)

    def test_rgen_bits_display_as_mebibytes_per_second(self):
        source = self.fixture.log(
            "traffic_generator",
            [
                {
                    "type": "traffic",
                    "time_ns": 0,
                    "bits_per_second": 8_388_608,
                    "operations_per_second": 10,
                }
            ],
        )
        data = TrafficGeneratorProcessor().process(source, params=Params())[0]
        self.assertEqual(data.frame.bits_per_second.iloc[0], 8_388_608)
        chart = data.charts[0]
        self.assertEqual(chart.ylabel, "MiB/s")
        self.assertEqual(chart.as_plotly(data.frame).data[0].y[0], 1)
        static = chart.as_matplotlib(data.frame)
        self.assertEqual(static.axes[0].lines[0].get_ydata()[0], 1)
        static.clear()

    def test_battery_units_sparse_sources_and_thermal_names(self):
        records = [
            self.fixture.file("/sys/class/power_supply/BAT0/" + name, value, i * 1000)
            for i, (name, value) in enumerate(
                (
                    ("capacity", "55"),
                    ("charge_now", "1230000"),
                    ("charge_full", "2000000"),
                    ("current_now", "-100000"),
                    ("voltage_now", "12000000"),
                )
            )
        ]
        data = self.process_files(BatteryLevelProcessor(), records)
        self.assertEqual(len(data.charts), 4)
        self.assertEqual(data.frame.current_ma.dropna().iloc[0], -100)
        self.assertEqual(len(data.charts[1].as_plotly(data.frame).data), 2)
        self.assertEqual(set(data.frame.entity_label), {"BAT0"})
        thermal = self.process_files(
            TemperatureProcessor(),
            [
                self.fixture.file("/sys/class/thermal/thermal_zone0/type", "CPU package"),
                self.fixture.file("/sys/class/thermal/thermal_zone0/temp", "42000"),
            ],
        )
        self.assertEqual(thermal.frame.entity_label.iloc[0], "CPU package (thermal_zone0)")
        self.assertEqual(thermal.frame.temperature_celsius.iloc[0], 42)

    def test_network_composition_rates_and_snmp_gauges(self):
        records = [
            self.fixture.file(
                "/proc/net/dev",
                f"eth0: {100 + t} 2 {t} {t} {t} 0 0 0 {200 + t} 3 {t} {t} {t} 0 0 0",
                t * 1000000000,
            )
            for t in (0, 1)
        ]
        data = self.process_files(NetProcessor(), records)
        self.assertEqual(len(data.charts), 3)
        self.assertFalse(any(isinstance(chart, Table) for chart in data.charts))
        self.assertEqual(data.frame.rx_drops_per_second.iloc[1], 1)
        snmp = self.process_files(
            NetSnmpProcessor(),
            [
                self.fixture.file(
                    "/proc/net/snmp",
                    f"Tcp: InSegs OutSegs CurrEstab\nTcp: {10 + t} {20 + t} 2",
                    t * 1000000000,
                )
                for t in (0, 1)
            ],
        )
        self.assertEqual(len(snmp.charts), 2)
        self.assertEqual(len(snmp.charts[0].as_plotly(snmp.frame).data), 2)
        self.assertEqual(snmp.charts[1].ylabel, "connections")
        udp = self.process_files(
            NetUdpProcessor(),
            [
                self.fixture.file(
                    "/proc/net/udp",
                    f"header\n0: 0100007F:1234 00000000:0000 07 00000000:00000000 00:00000000 00000000 1000 0 99 2 0 {t}",
                    t * 1000000000,
                )
                for t in (0, 1)
            ],
        )
        self.assertEqual(udp.frame.drops_per_second.iloc[1], 1)

    def test_wifi_structured_bitrates_and_typed_parameter_charts(self):
        content = "Station aa:bb:cc:dd:ee:ff (on wlan0)\n signal: -50 [-51, -52] dBm\n signal avg: -55 dBm\n connected time: 20 seconds\n tx retries: 3\n tx failed: 1\n tx bytes: 1000\n rx bytes: 2000\n tx packets: 3\n rx packets: 4\n tx bitrate: 866.7 MBit/s 80MHz VHT-MCS 9 VHT-NSS 2 short GI\n rx bitrate: 100 MBit/s 40MHz HE-MCS 5 HE-NSS 1 HE-GI 0 HE-DCM 1\n"
        source = self.fixture.log(
            "wifi_conn_info", text=json.dumps({"time_ns": 0, "stdout": content}) + "\n"
        )
        results = WifiConnInfoProcessor().process(source, params=Params())
        labels = [chart.label for chart in results[1].charts]
        self.assertEqual(len(labels), 15)  # table + 7 numeric families + 7 typed families
        self.assertTrue(all(chart.additional for chart in results[0].charts))
        self.assertEqual(results[1].frame.tx_bandwidth.iloc[0], 80)
        for chart in results[1].charts:
            self.assertTrue(chart.as_plotly(results[1].frame).data)
            figure = chart.as_matplotlib(results[1].frame)
            FigureCanvasAgg(figure).draw()
            figure.clear()

    def test_empty_reports_tables_and_file_warnings(self):
        data = Dataset(
            frame=pd.DataFrame(), group_label="Empty", data_label="none", warnings=("Unavailable",)
        )
        for backend in Backend:
            for layout in Layout:
                path = self.root / f"{backend}-{layout}"
                if layout is not Layout.FILES:
                    path = path.with_suffix(".html")
                paths = export_charts(
                    (data,), path, options=ExportOptions(backend=backend, layout=layout)
                )
                self.assertIn("No measurement data available", paths[0].read_text())
                self.assertIn("Unavailable", paths[0].read_text())
        export_charts((), self.root / "empty.html")
        self.assertIn("No measurement data", (self.root / "empty.html").read_text())

    def test_socket_charts_keep_full_identity_and_partial_kernel_fields(self):
        from vsm.processor.socket_stat import SocketStatProcessor

        records = [
            {
                "type": "socket",
                "time_ns": t * 1000000000,
                "source": "::1",
                "destination": "::1",
                "source_port": 1234,
                "destination_port": 80,
                "cookie": [1, 2],
                "inode": 99,
                "family": 10,
                "protocol": "tcp",
                "bytes_acked": 100 + t * 10,
                "bytes_received": 200 + t * 20,
                "bytes_sent": 150 + t * 30,
                "bytes_retransmitted": t,
                "rtt_us": 100,
                "receive_rtt_us": 200,
                "congestion_window_segments": 10,
                "receive_queue": 2,
                "send_queue": 3,
                "receive_buffer": 4096,
            }
            for t in (0, 1)
        ]
        result = SocketStatProcessor().process(
            self.fixture.log("socket_stat", records), params=Params()
        )[0]
        self.assertEqual(len(result.charts), 4)
        self.assertEqual(result.frame.bytes_sent_per_second.iloc[1], 30)
        self.assertEqual(result.frame.entity_label.iloc[0], "tcp [::1]:1234 → [::1]:80")
        for chart in result.charts:
            self.assertNotIn("[1, 2]", chart.label)
            self.assertTrue(chart.as_plotly(result.frame).data)
        self.assertIn("socket_id", result.charts[0].as_plotly(result.frame).data[0].text[0])
        for row in records:
            row.pop("receive_rtt_us")
            row.pop("receive_buffer")
        older = SocketStatProcessor().process(
            self.fixture.log("socket_stat", records), params=Params()
        )[0]
        self.assertNotIn("receive_rtt_us", older.frame)
        self.assertEqual(len(older.charts), 4)

    def test_column_count_is_an_integer_and_tables_remain_explicit(self):
        for value in (True, 2.5, "2"):
            with self.assertRaises(TypeError):
                ExportOptions(columns=value)
        from vsm.processor.fio import FioProcessor
        from vsm.processor.time import TimeProcessor

        for processor, text in (
            (
                FioProcessor(),
                '{"jobs":[{"jobname":"read", "read":{"bw_bytes":10,"iops":2,"io_bytes":100}}]}',
            ),
            (TimeProcessor(), "real 1.2\nuser 0.5\nsys 0.1\n"),
        ):
            data = processor.process(self.fixture.log(processor.name, text=text), params=Params())[
                0
            ]
            self.assertIsInstance(data.charts[0], Table)
            self.assertTrue(all(chart.additional for chart in data.charts[1:]))

    def test_typed_wifi_parameter_keeps_missing_values_as_gaps(self):
        frame = pd.DataFrame({"time": [0, 1, 2], "typed": ["HE-MCS 5", None, "HE-MCS 7"]})
        chart = Line(label="RX MCS", y=("typed",), categorical=True)
        self.assertTrue(pd.isna(chart.as_plotly(frame).data[0].y[1]))
        self.assertIsNone(json.loads(chart.as_plotly(frame).to_json())["data"][0]["y"][1])
        figure = chart.as_matplotlib(frame)
        self.assertTrue(pd.isna(figure.axes[0].lines[0].get_ydata()[1]))
        self.assertEqual(
            [label.get_text() for label in figure.axes[0].get_yticklabels()],
            ["HE-MCS 5", "HE-MCS 7"],
        )
        figure.clear()
