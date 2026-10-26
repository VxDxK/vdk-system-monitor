"""Dataset disk round trips and rendering without live collection state."""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from matplotlib.backends.backend_agg import FigureCanvasAgg

from vsm.chart.bar import Bar
from vsm.chart.box import Box
from vsm.chart.line import Line
from vsm.chart.table import Table
from vsm.models import ActionId, DeviceId, LogMetadata, LogRef, Schema, TargetInfo
from vsm.processing import Column, Dataset


class DatasetCsvTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "cpu.csv"
        self.sidecar = self.root / "cpu.csv.json"
        self.frame = pd.DataFrame(
            {
                "time": [0.0, 1.0, 2.0, 0.0, 1.0, 2.0],
                "cpu": ["001"] * 3 + ["NA"] * 3,
                "busy_percent": [10.0, float("nan"), 30.0, 60.0, 70.0, 80.0],
                "entity_label": ["First"] * 3 + ["Second"] * 3,
                "epoch": [0] * 6,
            }
        )
        self.line = Line(
            label="Load",
            y=("busy_percent",),
            group_by=("cpu", "epoch"),
            sparse=False,
            additional=True,
            title="CPU",
            xlabel="Elapsed",
            ylabel="%",
            exclude=(("cpu", "excluded"),),
            where=(("epoch", "0"),),
            label_column="entity_label",
            metric_labels=(("busy_percent", "Busy"),),
            y_range=(0.0, 100.0),
            markers=True,
            categorical=False,
        )
        self.data = Dataset(
            frame=self.frame,
            group_label="CPU",
            data_label="CPU utilization",
            columns=(Column(name="time", unit="s"), Column(name="busy_percent", unit="%")),
            charts=(
                self.line,
                Box(label="Distribution", y=("busy_percent",), points=False),
                Bar(label="Bars", y=("busy_percent",)),
                Table(
                    label="Values", columns=("cpu", "busy_percent"), title="Values", additional=True
                ),
            ),
            warnings=("A warning",),
        ).with_source(
            LogRef(
                path=self.root / "raw.log",
                schema=Schema(name="proc_stat"),
                device_id=DeviceId("host:test"),
                action_id=ActionId("action-1"),
                complete=False,
                metadata=LogMetadata(
                    target=TargetInfo(
                        os="linux",
                        architecture="x86_64",
                        kernel="test",
                        cpu_features=("sse2",),
                        available_libraries=("libc.so.6",),
                    ),
                    provider="rfpoll",
                    interval_seconds=0.25,
                    warnings=("source warning",),
                ),
            )
        )

    def test_round_trip_all_charts_and_metadata(self):
        self.data.to_csv(self.path)
        restored = Dataset.from_csv(self.path)
        pd.testing.assert_frame_equal(self.data.frame, restored.frame)
        for field in ("group_label", "data_label", "columns", "charts", "warnings", "provenance"):
            self.assertEqual(getattr(self.data, field), getattr(restored, field))
        for before, after in zip(self.data.charts, restored.charts, strict=True):
            self.assertEqual(
                before.as_plotly(self.frame).to_json(), after.as_plotly(restored.frame).to_json()
            )
            figure = after.as_matplotlib(restored.frame)
            FigureCanvasAgg(figure).draw()
            figure.clear()
        original_series = list(self.line.series(self.frame))
        loaded_series = list(restored.charts[0].series(restored.frame))
        for (label, frame, col), (other_label, other_frame, other_col) in zip(
            original_series, loaded_series, strict=True
        ):
            self.assertEqual((label, col), (other_label, other_col))
            pd.testing.assert_frame_equal(frame, other_frame)

    def test_scalars_structures_missing_values_and_dtypes(self):
        frame = pd.DataFrame(
            {
                "strings": pd.Series(["", "NA", "null", "001", 'comma,quote"\nline'], dtype=object),
                "identity": pd.Series([2**64 - 1, 2**63 + 1, 0, 1, 2], dtype="uint64"),
                "nullable": pd.Series([1, None, 3, 4, 5], dtype="Int64"),
                "boolean": pd.Series([True, False, None, True, False], dtype="boolean"),
                "string": pd.Series(["", "NA", None, "001", "x"], dtype="string"),
                "float": [float("inf"), -float("inf"), float("nan"), 1.2345678901234567, -0.0],
                "mixed": pd.Series([None, pd.NA, float("nan"), "", 17], dtype=object),
                "nested": pd.Series([[1, 2], {"a": [True, None, "x"]}, [], {}, None], dtype=object),
            }
        )
        data = Dataset(frame=frame, group_label="Values", data_label="Values")
        data.to_csv(self.path)
        restored = Dataset.from_csv(self.path)
        pd.testing.assert_frame_equal(frame, restored.frame)
        self.assertIs(restored.frame["mixed"].iloc[0], None)
        self.assertIs(restored.frame["mixed"].iloc[1], pd.NA)
        # Ordinary CSV readers still see the familiar columns and scalar values.
        self.assertEqual(list(pd.read_csv(self.path).columns), list(frame.columns))

    def test_empty_datasets_and_discarded_index(self):
        frames = [
            pd.DataFrame(),
            pd.DataFrame(index=range(3)),
            self.frame.iloc[:0],
            self.frame.set_axis(range(10, 16)),
        ]
        for frame in frames:
            with self.subTest(shape=frame.shape):
                data = Dataset(frame=frame, group_label="Empty", data_label="Empty")
                data.to_csv(self.path)
                pd.testing.assert_frame_equal(
                    frame.reset_index(drop=True), Dataset.from_csv(self.path).frame
                )

    def test_pair_can_move_and_raw_log_is_not_required(self):
        self.data.to_csv(self.path)
        moved = self.root / "moved"
        moved.mkdir()
        shutil.move(self.path, moved)
        shutil.move(self.sidecar, moved)
        self.assertFalse(self.data.provenance.path.exists())
        self.assertEqual(Dataset.from_csv(moved / "cpu.csv").provenance, self.data.provenance)

    def test_fresh_producer_and_consumer_processes(self):
        # Producer has no shared in-memory objects with the rendering process.
        producer = """
from pathlib import Path
import pandas as pd
from vsm.chart.line import Line
from vsm.processing import Dataset
Dataset(frame=pd.DataFrame({"time": [0.,1.,2.], "load": [10.,None,30.]}),
        group_label="CPU", data_label="CPU",
        charts=(Line(label="Load", y=("load",), ylabel="%"),)).to_csv(Path("cpu.csv"))
"""
        consumer = """
from pathlib import Path
from vsm.processing import Dataset
from vsm.output.charts import Backend, ExportOptions, export_charts
loaded = Dataset.from_csv(Path("cpu.csv"))
for backend in Backend:
    export_charts((loaded,), Path(backend.value + ".html"), options=ExportOptions(backend=backend))
"""
        for script in (producer, consumer):
            subprocess.run(
                [sys.executable, "-c", script],
                cwd=self.root,
                check=True,
                capture_output=True,
                timeout=45,
            )
        self.assertIn("plotly", (self.root / "plotly.html").read_text())
        self.assertIn("data:image/png;base64,", (self.root / "matplotlib.html").read_text())

    def test_missing_and_malformed_sidecars(self):
        with self.assertRaisesRegex(FileNotFoundError, "sidecar required"):
            Dataset.from_csv(self.path)
        for contents in ("{", "[]", '{"dataset": {}, "frame": {}}'):
            self.sidecar.write_text(contents)
            with self.assertRaisesRegex(ValueError, "invalid .*dataset"):
                Dataset.from_csv(self.path)

    def test_corrupt_metadata_and_chart_fields(self):
        self.data.to_csv(self.path)
        original = json.loads(self.sidecar.read_text())
        for mutate in (
            lambda p: p["dataset"]["charts"]["items"][0].update(kind="Custom"),
            lambda p: p["dataset"]["charts"]["items"][0]["fields"].update(markers="yes"),
            lambda p: p["dataset"].update(group_label=17),
            lambda p: p["frame"]["columns"][0].update(dtype="datetime64[ns]"),
            lambda p: p["frame"]["columns"][0].update(overrides={"999": "none"}),
            lambda p: p["dataset"]["charts"]["items"][0]["fields"].update(x="missing"),
        ):
            payload = json.loads(json.dumps(original))
            mutate(payload)
            self.sidecar.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, "invalid CSV dataset"):
                Dataset.from_csv(self.path)

    def test_corrupt_csv_headers_dimensions_and_cells(self):
        self.data.to_csv(self.path)
        original = self.path.read_text()
        for contents in (
            original.replace("time,", "wrong,", 1),
            original.splitlines()[0] + "\n",
            original.replace("0.0,001", "bad,001", 1),
            original + "extra\n",
        ):
            self.path.write_text(contents)
            with self.assertRaisesRegex(ValueError, "invalid CSV dataset"):
                Dataset.from_csv(self.path)

    def test_unsupported_values_leave_existing_pair_untouched(self):
        self.data.to_csv(self.path)
        original = (self.path.read_bytes(), self.sidecar.read_bytes())
        for data in (
            replace(self.data, charts=(object(),)),
            Dataset(frame=pd.DataFrame({"x": [object()]}), group_label="X", data_label="X"),
            Dataset(
                frame=pd.DataFrame({"x": pd.to_datetime(["2020-01-01"])}),
                group_label="X",
                data_label="X",
            ),
            Dataset(
                frame=pd.DataFrame([[1, 2]], columns=["x", "x"]), group_label="X", data_label="X"
            ),
        ):
            with self.assertRaises(ValueError):
                data.to_csv(self.path)
            self.assertEqual(original, (self.path.read_bytes(), self.sidecar.read_bytes()))

    def test_staging_failure_leaves_existing_pair_and_no_temporary_files(self):
        self.data.to_csv(self.path)
        original = (self.path.read_bytes(), self.sidecar.read_bytes())
        from vsm import _dataset_csv

        real_temporary = tempfile.NamedTemporaryFile
        with (
            patch.object(
                _dataset_csv.tempfile,
                "NamedTemporaryFile",
                side_effect=[
                    real_temporary(mode="w", dir=self.root, delete=False),
                    OSError("disk full"),
                ],
            ),
            self.assertRaisesRegex(OSError, "disk full"),
        ):
            self.data.to_csv(self.path)
        self.assertEqual(original, (self.path.read_bytes(), self.sidecar.read_bytes()))
        self.assertEqual(set(self.root.iterdir()), {self.path, self.sidecar})
