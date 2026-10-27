"""Display offsets affect exported charts, while UTC datasets remain unchanged."""

import json
import re
import tempfile
import unittest
from datetime import UTC, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd
from matplotlib import dates as mdates
from matplotlib.backends.backend_agg import FigureCanvasAgg

from vsm.chart.line import Line
from vsm.output.charts import Backend, ExportOptions, Layout, export_charts
from vsm.processing import Column, Dataset


class ExportTimezoneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.frame = pd.DataFrame(
            {
                "time": [0.0, 82800.0],
                "timestamp_ns": [
                    pd.Timestamp(value).value
                    for value in ("2026-01-01T00:30:00Z", "2026-01-01T23:30:00Z")
                ],
                "timestamp_uncertainty_ns": [1000, 1000],
                "value": [1.0, 2.0],
            }
        )
        self.chart = Line(label="Timeline", y=("value",))
        self.data = Dataset(
            frame=self.frame,
            group_label="Timeline",
            data_label="test",
            columns=(
                Column(name="time", unit="s"),
                Column(name="timestamp_ns", unit="ns since Unix epoch (UTC)"),
            ),
            charts=(self.chart,),
        )

    def test_offsets_roll_dates_and_match_both_renderers(self):
        for hours, expected, label in (
            (3, ("2026-01-01T03:30:00", "2026-01-02T02:30:00"), "UTC+03:00"),
            (-3, ("2025-12-31T21:30:00", "2026-01-01T20:30:00"), "UTC-03:00"),
            (5.5, ("2026-01-01T06:00:00", "2026-01-02T05:00:00"), "UTC+05:30"),
        ):
            with self.subTest(hours=hours):
                zone = timezone(timedelta(hours=hours))
                plot = self.chart.as_plotly(self.frame, timezone=zone)
                self.assertEqual(tuple(plot.data[0].x), expected)
                self.assertEqual(plot.layout.xaxis.title.text, f"Date/time ({label})")
                self.assertIn(label, plot.data[0].hovertemplate)
                self.assertIn("Estimated UTC uncertainty", plot.data[0].text[0])
                figure = self.chart.as_matplotlib(self.frame, timezone=zone)
                try:
                    FigureCanvasAgg(figure).draw()
                    axes = figure.axes[0]
                    self.assertEqual(axes.get_xlabel(), f"Date/time ({label})")
                    for value, local in zip(axes.lines[0].get_xdata(), expected, strict=True):
                        self.assertEqual(
                            axes.xaxis.get_major_formatter()(value), local.replace("T", "\n")
                        )
                    # Static chart coordinates continue representing the original instant.
                    self.assertEqual(mdates.num2date(axes.lines[0].get_xdata()[0], tz=UTC).hour, 0)
                finally:
                    figure.clear()

    def test_default_utc_and_natural_axes(self):
        self.assertEqual(ExportOptions().timezone, UTC)
        plot = self.chart.as_plotly(self.frame)
        self.assertTrue(plot.data[0].x[0].endswith("Z"))
        self.assertEqual(plot.layout.xaxis.title.text, "Date/time (UTC)")
        frame = self.frame.drop(columns="timestamp_ns")
        plot = self.chart.as_plotly(frame, timezone=timezone(timedelta(hours=3)))
        self.assertEqual(tuple(plot.data[0].x), tuple(frame.time))
        self.assertEqual(plot.layout.xaxis.title.text, "Time (s)")

    def test_validation_rejects_named_zones_and_subminute_offsets(self):
        for invalid in (None, "UTC+03:00", ZoneInfo("Europe/Moscow")):
            with self.subTest(invalid=invalid), self.assertRaises(TypeError):
                ExportOptions(timezone=invalid)
        with self.assertRaises(ValueError):
            ExportOptions(timezone=timezone(timedelta(seconds=30)))

    def test_reports_files_and_csv_keep_utc_data(self):
        original = self.frame.copy(deep=True)
        self.data.to_csv(self.root / "before.csv")
        offset = timezone(timedelta(hours=3))
        for layout in (Layout.FOLDABLE, Layout.TABLE, Layout.FILES):
            with self.subTest(layout=layout):
                destination = self.root / (
                    layout.value + ("" if layout is Layout.FILES else ".html")
                )
                paths = export_charts(
                    (self.data,), destination, options=ExportOptions(timezone=offset, layout=layout)
                )
                html = paths[0].read_text()
                payload = re.search(
                    r"<script type='application/json' data-figure='[^']+'>(.*?)</script>", html
                )[1]
                figure = json.loads(payload)
                self.assertEqual(figure["data"][0]["x"][0], "2026-01-01T03:30:00")
                self.assertEqual(
                    figure["layout"]["xaxis"]["title"]["text"], "Date/time (UTC+03:00)"
                )
        # Observe the actual figure passed to image export for both static paths.
        seen = []
        original_render = Line.as_matplotlib

        def render(chart, frame, *, timezone=UTC):
            figure = original_render(chart, frame, timezone=timezone)
            seen.append(figure.axes[0].get_xlabel())
            return figure

        for layout in (Layout.FOLDABLE, Layout.FILES):
            seen.clear()
            with patch.object(Line, "as_matplotlib", render):
                export_charts(
                    (self.data,),
                    self.root / f"static-{layout.value}",
                    options=ExportOptions(
                        backend=Backend.MATPLOTLIB, layout=layout, timezone=offset
                    ),
                )
            self.assertEqual(seen, ["Date/time (UTC+03:00)"])
        pd.testing.assert_frame_equal(self.frame, original)
        self.assertEqual(self.data.charts, (self.chart,))
        self.data.to_csv(self.root / "after.csv")
        self.assertEqual(
            (self.root / "before.csv").read_bytes(), (self.root / "after.csv").read_bytes()
        )
        self.assertEqual(
            (self.root / "before.csv.json").read_bytes(),
            (self.root / "after.csv.json").read_bytes(),
        )
        restored = Dataset.from_csv(self.root / "after.csv")
        pd.testing.assert_frame_equal(restored.frame, original)
