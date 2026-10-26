"""Chart grouping, units, export layouts, and safe artifact names."""

import base64
import re
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from vsm.chart.line import Line
from vsm.models import ActionId, DeviceId, LogMetadata, LogRef, Schema, TargetInfo
from vsm.output.charts import Backend, ExportOptions, ImageFormat, Layout, export_charts
from vsm.processing import Dataset, Params
from vsm.processor.meminfo import MeminfoProcessor
from vsm.processor.proc_stat import CpuProcessor


class ChartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.frame = pd.DataFrame(
            {
                "time": [0, 1, 2, 0, 1, 2],
                "cpu": ["cpu0"] * 3 + ["cpu1"] * 3,
                "busy_percent": [10, None, 30, 90, 80, 70],
            }
        )
        self.chart = Line(
            label="CPU <load>", y=("busy_percent",), ylabel="%", group_by=("cpu",), y_range=(0, 100)
        )
        self.data = Dataset(
            frame=self.frame,
            group_label="<unsafe/group>",
            data_label="../cpu",
            charts=(self.chart,),
        )

    def test_entity_groups_stay_separate_and_missing_values_remain_gaps(self):
        plotly = self.chart.as_plotly(self.frame)
        self.assertEqual(len(plotly.data), 2)
        self.assertFalse(plotly.data[0].connectgaps)
        self.assertEqual(len(plotly.data[0].x), 3)
        self.assertTrue(pd.isna(plotly.data[0].y[1]))
        self.assertEqual(plotly.layout.yaxis.title.text, "%")
        static = self.chart.as_matplotlib(self.frame)
        self.assertEqual(len(static.axes[0].lines), 2)
        self.assertTrue(pd.isna(static.axes[0].lines[0].get_ydata()[1]))
        FigureCanvasAgg(static).draw()
        static.clear()

    def test_static_and_plotly_reports_in_both_layouts(self):
        for backend in Backend:
            for layout in (Layout.FOLDABLE, Layout.TABLE):
                with self.subTest(backend=backend, layout=layout):
                    path = self.root / f"{backend}-{layout}.html"
                    export_charts(
                        (self.data, self.data),
                        path,
                        options=ExportOptions(backend=backend, layout=layout),
                        title="<report>",
                    )
                    html = path.read_text()
                    self.assertIn("&lt;report&gt;", html)
                    self.assertIn("&lt;unsafe/group&gt;", html)
                    self.assertEqual(
                        "<details open class='section'" in html, layout is Layout.FOLDABLE
                    )
                    if backend is Backend.PLOTLY:
                        self.assertEqual(html.count("plotly.js v"), 1)
                        self.assertEqual(html.count("data-figure='chart-"), 2)
                        self.assertNotIn('<script src="https://cdn.plot.ly', html)
                    else:
                        encoded = re.search(r"data:image/png;base64,([^']+)", html)[1]
                        self.assertTrue(base64.b64decode(encoded).startswith(b"\x89PNG\r\n\x1a\n"))

    def test_separate_files_all_static_formats_and_duplicate_labels(self):
        for image_format in ImageFormat:
            paths = export_charts(
                (self.data, self.data),
                self.root / image_format,
                options=ExportOptions(
                    backend=Backend.MATPLOTLIB, layout=Layout.FILES, image_format=image_format
                ),
            )
            self.assertEqual(len(set(paths)), 2)
            for path in paths:
                self.assertTrue(path.is_relative_to(self.root / image_format))
                self.assertGreater(path.stat().st_size, 100)
                if image_format is ImageFormat.PDF:
                    self.assertTrue(path.read_bytes().startswith(b"%PDF"))
        paths = export_charts(
            (self.data, self.data),
            self.root / "interactive",
            options=ExportOptions(backend=Backend.PLOTLY, layout=Layout.FILES),
        )
        self.assertEqual(len(set(paths)), 2)
        self.assertTrue(all("plotly.js v" in path.read_text() for path in paths))

    def test_table_only_datasets_and_invalid_chart_columns(self):
        data = Dataset(
            frame=pd.DataFrame({"note": ["<hello>"]}), group_label="Summary", data_label="summary"
        )
        path = self.root / "summary.html"
        export_charts((data,), path)
        self.assertIn("&lt;hello&gt;", path.read_text())
        with self.assertRaises(ValueError):
            self.chart.as_plotly(pd.DataFrame({"time": [0]}))
        for invalid in (ExportOptions,):
            with self.assertRaises(ValueError):
                invalid(columns=0)
        with self.assertRaises(TypeError):
            ExportOptions(backend="plotly")

    def test_plotly_is_default_backend(self):
        self.assertIs(ExportOptions().backend, Backend.PLOTLY)
        path = self.root / "default.html"
        export_charts((self.data,), path)
        html = path.read_text()
        self.assertIn("Plotly.newPlot(", html)
        self.assertNotIn("<img src=", html)

    def test_csv_has_sidecar_and_direct_figures_have_expected_types(self):
        self.data.to_csv(self.root / "cpu.csv")
        self.assertTrue((self.root / "cpu.csv.json").exists())
        restored = Dataset.from_csv(self.root / "cpu.csv")
        self.assertEqual(restored.charts, self.data.charts)
        self.assertIsInstance(self.chart.as_matplotlib(self.frame), Figure)

    def test_memory_and_cpu_units_and_chart_columns(self):
        from test_processors import ProcessorTests

        fixtures = ProcessorTests()
        fixtures.setUp()
        self.addCleanup(fixtures.tearDown)
        log = fixtures.log(
            "meminfo",
            [
                fixtures.file(
                    "/proc/meminfo",
                    "MemTotal: 100 kB\nMemAvailable: 50 kB\nHugePages_Total: 2\nDirectMap4k: 3 kB\n",
                    0,
                )
            ],
        )
        data = MeminfoProcessor().process(log, params=Params())[0]
        units = {column.name: column.unit for column in data.columns}
        self.assertEqual(units["MemTotal"], "B")
        self.assertEqual(units["DirectMap4k"], "B")
        self.assertEqual(data.frame.iloc[0]["DirectMap4k"], 3072)
        self.assertEqual(units["HugePages_Total"], "count")
        self.assertEqual(units["used_percent"], "%")
        self.assertEqual(data.frame.iloc[0]["MemTotal"], 102400)
        self.assertEqual({chart.ylabel for chart in data.charts}, {"B", "count", "%"})
        log = fixtures.log(
            "proc_stat",
            [
                fixtures.file("/proc/stat", f"cpu0 {value} 0 20 80 0 0 0 0\n", time)
                for value, time in ((10, 0), (20, 1000000000))
            ],
        )
        data = CpuProcessor().process(log, params=Params())[0]
        self.assertEqual({column.name: column.unit for column in data.columns}["busy_percent"], "%")
        self.assertEqual(data.charts[0].group_by, ("cpu", "observation_epoch"))
        self.assertEqual(data.charts[0].y_range, (0, 100))

    def test_provenance_keeps_distinct_sources_in_separate_groups(self):
        target = TargetInfo(os="linux", architecture="x86_64", kernel="fixture")
        datasets = []
        for connection in ("connection-1", "connection-2"):
            source = LogRef(
                path=Path(connection) / "action-1/data.log",
                schema=Schema(name="proc_stat"),
                device_id=DeviceId("serial"),
                action_id=ActionId("action-1"),
                complete=False,
                metadata=LogMetadata(target=target, provider="fixture"),
            )
            datasets.append(self.data.with_source(source))
        path = self.root / "sources.html"
        export_charts(datasets, path)
        html = path.read_text()
        self.assertEqual(html.count("<details open class='section'"), 2)
        self.assertIn("Incomplete source log", html)
