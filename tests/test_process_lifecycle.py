"""Structured lifecycle contracts; expected exits do not relax read-error policy."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from support import stat

from vsm.models import ActionId, DeviceId, LogMetadata, LogRef, Schema, TargetInfo
from vsm.processing import Params, load_native
from vsm.processor.pid_stat import PidStatProcessor
from vsm.processor.process_io import ProcessIoProcessor
from vsm.processor.smaps import SmapsProcessor


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def tags(self, start=99, *, tid=123, scope="process", process_start=None):
        return {
            "pid": 123,
            "tid": tid,
            "scope": scope,
            "start_ticks": start,
            "process_start_ticks": start if process_start is None else process_start,
        }

    def file(self, filename="stat", *, start=99, time=0, text=None, tags=None):
        tags = tags or self.tags(start)
        prefix = f"/proc/123/task/{tags['tid']}/" if tags["scope"] == "thread" else "/proc/123/"
        if text is None:
            text = stat(pid=tags["tid"], start=start)
        return {
            "type": "file",
            "path": prefix + filename,
            "tags": tags,
            "time_ns": time,
            "encoding": "hex",
            "data": text.encode().hex(),
        }

    def end(self, start=99, *, reason="disappeared", tags=None):
        return {
            "type": "entity_end",
            "tags": tags or self.tags(start),
            "reason": reason,
            "time_ns": 2_000_000_000,
        }

    def log(self, name, records, *, native=None):
        metadata = {
            "type": "metadata",
            "schema": native or ("rfpoll" if name == "smaps" else "pspider"),
            "time_ns": 0,
            "clock_ticks_per_second": 100,
            "page_size_bytes": 4096,
        }
        events = [metadata, *records, {"type": "end", "time_ns": 3_000_000_000}]
        path = self.root / f"{name}.log"
        path.write_text(
            "".join(json.dumps(dict(e, sequence=i)) + "\n" for i, e in enumerate(events))
        )
        return LogRef(
            path=path,
            schema=Schema(name=name),
            device_id=DeviceId("fixture"),
            action_id=ActionId("capture"),
            complete=True,
            metadata=LogMetadata(
                target=TargetInfo(
                    os="linux",
                    architecture="x86_64",
                    kernel="fixture",
                    clock_ticks_per_second=100,
                    page_size_bytes=4096,
                ),
                provider="test",
            ),
        )

    def test_series_ends_at_last_valid_sample_without_synthetic_rows(self):
        for reason in ("exited", "disappeared", "identity_changed"):
            with self.subTest(reason=reason):
                records = [self.file(), self.file(time=1_000_000_000), self.end(reason=reason)]
                data = PidStatProcessor().process(self.log("pid_stat", records), params=Params())
                self.assertEqual(len(data[0].frame), 2)
                self.assertEqual(data[0].frame.time.tolist(), [0, 1])
                self.assertEqual(data[0].warnings, ())
                self.assertEqual(data[1].warnings, ())

    def test_io_rates_use_pinned_identity_even_without_preceding_stat(self):
        records = [
            self.file("io", text="read_bytes: 10\n"),
            self.file("io", text="read_bytes: 30\n", time=1_000_000_000),
            self.end(),
        ]
        (data,) = ProcessIoProcessor().process(self.log("process_io", records), params=Params())
        self.assertEqual(len(data.frame), 2)
        self.assertEqual(data.frame.start_ticks.tolist(), [99, 99])
        self.assertEqual(data.frame.iloc[1].read_bytes_per_second, 20)
        self.assertEqual(data.warnings, ())

    def test_smaps_series_and_chart_grouping_separate_reused_pid(self):
        records = [
            self.file("smaps_rollup", text="Rss: 10 kB\nPss: 5 kB\n"),
            self.end(),
            self.file(
                "smaps_rollup", text="Rss: 20 kB\nPss: 10 kB\n", start=200, time=2_500_000_000
            ),
        ]
        (data,) = SmapsProcessor().process(self.log("smaps", records), params=Params())
        self.assertEqual(len(data.frame), 2)
        self.assertEqual(data.frame.Rss.tolist(), [10240, 20480])
        self.assertEqual(data.frame.start_ticks.tolist(), [99, 200])
        self.assertTrue(all("start_ticks" in c.group_by for c in data.charts))
        self.assertEqual(data.warnings, ())

    def test_thread_rate_baselines_include_parent_and_thread_generations(self):
        records = [
            self.file(tags=self.tags(50, tid=456, scope="thread", process_start=99), start=50),
            self.end(tags=self.tags(50, tid=456, scope="thread", process_start=99)),
            self.file(
                tags=self.tags(50, tid=456, scope="thread", process_start=200),
                start=50,
                time=2_500_000_000,
            ),
        ]
        _, threads = PidStatProcessor().process(self.log("pid_stat", records), params=Params())
        self.assertEqual(len(threads.frame), 2)
        self.assertTrue(threads.frame.user_percent.isna().all())
        self.assertEqual(threads.frame.process_start_ticks.tolist(), [99, 200])

    def test_termination_without_measurements_returns_stable_empty_datasets(self):
        for processor, name in (
            (PidStatProcessor(), "pid_stat"),
            (ProcessIoProcessor(), "process_io"),
            (SmapsProcessor(), "smaps"),
        ):
            with self.subTest(name=name):
                for data in processor.process(self.log(name, [self.end()]), params=Params()):
                    self.assertTrue(data.frame.empty)
                    self.assertIn("time", data.frame)
                    self.assertIn("start_ticks", data.frame)
                    self.assertEqual(data.charts, ())
                    self.assertEqual(data.warnings, ())
                    self.assertIsNotNone(data.provenance)

    def test_live_metric_failure_and_permission_errors_stay_strict(self):
        for errno in (2, 3, 5, 13):
            for processor, name in (
                (PidStatProcessor(), "pid_stat"),
                (ProcessIoProcessor(), "process_io"),
                (SmapsProcessor(), "smaps"),
            ):
                with self.subTest(errno=errno, name=name):
                    error = {
                        "type": "read_error",
                        "path": "/proc/123/io",
                        "tags": self.tags(),
                        "operation": "read",
                        "errno": errno,
                        "error": "unavailable",
                        "time_ns": 1,
                    }
                    log = self.log(name, [error, self.end()])
                    with self.assertRaisesRegex(ValueError, "read errors"):
                        processor.process(log, params=Params())
                    data = processor.process(log, params=Params(allow_read_errors=True))
                    self.assertTrue(all(any("unavailable" in w for w in d.warnings) for d in data))

    def test_invalid_lifecycle_and_records_after_termination_fail(self):
        bad_records = [
            [dict(self.end(), reason="unknown")],
            [dict(self.end(), tags={})],
            [self.end(), self.end()],
            [self.end(), self.file()],
            [
                self.end(),
                self.file(tags=self.tags(50, tid=456, scope="thread", process_start=99), start=50),
            ],
            [self.file(start=200, tags=self.tags())],
            [dict(self.file(), path="/proc/999/stat")],
            [dict(self.file(), tags=self.tags(tid=999))],
            [dict(self.end(), tags=self.tags(start=True))],
        ]
        for records in bad_records:
            with self.subTest(records=records), self.assertRaises(ValueError):
                PidStatProcessor().process(self.log("pid_stat", records), params=Params())

    def test_schema_sequence_completeness_and_native_import(self):
        log = self.log("pid_stat", [self.file(), self.end()])
        imported = load_native(log.path, schema=Schema(name="pid_stat"))
        self.assertEqual(len(PidStatProcessor().process(imported, params=Params())[0].frame), 1)
        with self.assertRaisesRegex(ValueError, "schema mismatch"):
            load_native(log.path, schema=Schema(name="smaps"))
        for source in (
            replace(log, schema=Schema(name="other")),
            replace(log, complete=False),
        ):
            with self.assertRaises(ValueError):
                PidStatProcessor().process(source, params=Params())
        lines = log.path.read_text().splitlines()
        event = json.loads(lines[1])
        event["sequence"] = 99
        lines[1] = json.dumps(event)
        log.path.write_text("\n".join(lines) + "\n")
        with self.assertRaisesRegex(ValueError, "sequence"):
            PidStatProcessor().process(log, params=Params())
        with self.assertRaises(ValueError):
            PidStatProcessor().process(
                self.log("pid_stat", [self.file()], native="rfpoll"), params=Params()
            )

    def test_obsolete_native_schema_is_rejected(self):
        log = self.log("pid_stat", [self.file()], native="pspider/2")
        for params in (Params(), Params(allow_partial=True, allow_read_errors=True)):
            with self.assertRaisesRegex(ValueError, "schema mismatch"):
                PidStatProcessor().process(log, params=params)
        with self.assertRaisesRegex(ValueError, "schema mismatch"):
            load_native(log.path, schema=Schema(name="pid_stat"))
