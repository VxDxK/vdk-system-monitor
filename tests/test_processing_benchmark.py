"""Semantic checks for the synthetic workload used to measure processing costs."""

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from vsm.models import Schema
from vsm.processing import Params, load_native
from vsm.processor.pid_stat import PidStatProcessor
from vsm.processor.proc_stat import CpuProcessor

SPEC = importlib.util.spec_from_file_location(
    "benchmark_processing", Path(__file__).resolve().parents[1] / "scripts/benchmark_processing.py"
)
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


UTC_FALLBACK_WARNING = (
    "UTC timeline unavailable; using elapsed seconds: controller clock mapping or "
    "measurement time origin was not recorded."
)


class ProcessingBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "input.jsonl"

    def test_cpu_fixture_rows_units_baselines_and_resets(self):
        benchmark.write_log("cpu", self.path, 4)
        source = load_native(self.path, schema=Schema(name="proc_stat"))
        (data,) = CpuProcessor().process(source, params=Params())
        self.assertEqual(len(data.frame), 8)
        for _, frame in data.frame.groupby("cpu"):
            self.assertTrue(pd.isna(frame.iloc[0].busy_percent))
            self.assertEqual(frame.busy_percent.iloc[1:].round(6).tolist(), [15, 15, 15])
        self.assertEqual({c.name: c.unit for c in data.columns}["busy_percent"], "%")
        self.assertTrue(data.charts)
        self.assertTrue(all("cpu" in chart.group_by for chart in data.charts))

        records = [json.loads(line) for line in self.path.read_text().splitlines()]
        for event in records:
            if event["type"] == "file" and event["time_ns"] == 2_000_000_000:
                event["data"] = b"cpu 1 0 1 8 0 0 0 0\ncpu0 1 0 1 8 0 0 0 0\n".hex()
        self.path.write_text("".join(json.dumps(event) + "\n" for event in records))
        (reset,) = CpuProcessor().process(source, params=Params())
        rows = reset.frame[reset.frame.time == 2]
        self.assertTrue(rows.counter_reset.all())
        self.assertTrue(rows.busy_percent.isna().all())

    def test_process_fixture_terminates_without_extra_rows_and_preserves_identity(self):
        benchmark.write_log("process", self.path, 4)
        source = load_native(self.path, schema=Schema(name="pid_stat"))
        datasets = PidStatProcessor().process(source, params=Params())
        self.assertEqual([len(data.frame) for data in datasets], [4, 4])
        for data in datasets:
            self.assertEqual(data.frame.time.tolist(), [0, 1, 2, 3])
            self.assertEqual(data.frame.rss_bytes.tolist(), [8192] * 4)
            self.assertTrue(pd.isna(data.frame.busy_percent.iloc[0]))
            self.assertEqual(data.frame.busy_percent.iloc[1:].tolist(), [15, 15, 15])
            self.assertEqual(data.warnings, (UTC_FALLBACK_WARNING,))
            self.assertNotIn("timestamp_ns", data.frame)
            self.assertTrue(data.charts)
            self.assertTrue(all("process_start_ticks" in c.group_by for c in data.charts))
            units = {c.name: c.unit for c in data.columns}
            self.assertEqual(units["rss_bytes"], "B")
            self.assertEqual(units["busy_percent"], "%")

    def test_timeout_is_reported_as_failure(self):
        self.path.write_text("")
        with patch.object(
            benchmark.subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 1)
        ):
            result = benchmark.run_case("cpu", self.path, "rss", timeout=1)
        self.assertEqual(result["status"], "timeout")
        self.assertNotIn("wall_seconds", result)
