"""Clock mapping uses controller UTC; delays and tool precision remain explicit."""

import asyncio
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pandas as pd
from matplotlib.backends.backend_agg import FigureCanvasAgg

from vsm.action._collector import log_ref
from vsm.action.proc_stat import ProcStat
from vsm.chart.line import Line
from vsm.clock import ClockCalibration, ClockSample, ClockTracker, TargetClock, TimeOrigin
from vsm.connection.host import Host
from vsm.connection.targetctl import TargetctlClient, TargetctlOptions
from vsm.models import (
    ActionId,
    DeviceId,
    LogMetadata,
    LogRef,
    Schema,
    TargetctlProtocolError,
    TargetInfo,
)
from vsm.processing import Dataset, Params, dataset, load_native
from vsm.processor.iperf import IperfProcessor
from vsm.processor.proc_stat import CpuProcessor
from vsm.processor.rup import RupProcessor
from vsm.runtime import Run

UTC_NS = 1_800_000_000_000_000_000
TARGET = TargetInfo(os="linux", architecture="x86_64", kernel="test", boot_id="boot")


def sample(target=1_000_000_000, controller=100_000_000, delay=1000, realtime=50_000_000_000):
    return ClockSample(
        target=TargetClock(
            boot_id="boot", monotonic_ns=target, realtime_ns=realtime, uncertainty_ns=10
        ),
        controller_before_ns=controller - delay // 2,
        controller_after_ns=controller + delay // 2,
    )


def calibration(*samples):
    return ClockCalibration(
        controller_utc_ns=UTC_NS,
        controller_monotonic_ns=100_000_000,
        anchor_uncertainty_ns=5,
        samples=samples or (sample(),),
    )


def source(path, *, clock=None, origin=None, schema="test"):
    return LogRef(
        path=path,
        schema=Schema(name=schema),
        device_id=DeviceId("test"),
        action_id=ActionId("test"),
        complete=True,
        metadata=LogMetadata(target=TARGET, provider="test", clock=clock, time_origin=origin),
    )


class MappingTests(unittest.TestCase):
    def test_different_targets_align_without_matching_target_wall_clocks(self):
        first = calibration(sample())
        remote = sample(target=900_000_000_000, realtime=9_000_000_000_000)
        remote = replace(remote, target=replace(remote.target, boot_id="remote-boot"))
        second = calibration(remote)
        stamp1, _ = first.timestamp(
            TimeOrigin(domain="target_monotonic", value_ns=1_000_000_000), 500_000_000
        )
        stamp2, _ = second.timestamp(
            TimeOrigin(domain="target_monotonic", value_ns=900_000_000_000), 500_000_000
        )
        self.assertEqual(stamp1, stamp2)

    def test_interpolation_extrapolation_and_asymmetric_delay_estimate(self):
        clock = calibration(
            sample(),
            sample(target=3_000_000_000, controller=2_100_200_000, realtime=52_000_000_000),
        )
        origin = TimeOrigin(domain="target_monotonic", value_ns=1_000_000_000)
        timestamp, error = clock.timestamp(origin, 1_000_000_000)
        self.assertEqual(timestamp, UTC_NS + 1_000_100_000)
        self.assertEqual(error, 515)
        stamp, error = calibration().timestamp(origin, 1_000_000_000)
        self.assertEqual(stamp, UTC_NS + 1_000_000_000)
        self.assertEqual(error, 100_515)
        asymmetric = replace(
            sample(), controller_before_ns=100_000_000, controller_after_ns=100_010_000
        )
        stamp, error = calibration(asymmetric).timestamp(origin, 0)
        self.assertEqual(stamp, UTC_NS + 5000)
        self.assertEqual(error, 5015)

    def test_controller_origin_needs_no_target_sample(self):
        clock = replace(calibration(), samples=())
        stamp, error = clock.timestamp(
            TimeOrigin(domain="controller_monotonic", value_ns=100_000_000), 123
        )
        self.assertEqual((stamp, error), (UTC_NS + 123, 5))

    def test_realtime_conversion_resolution_jump_and_boot_mismatch(self):
        origin = TimeOrigin(
            domain="target_realtime", value_ns=50_000_000_000, uncertainty_ns=1_000_000
        )
        self.assertEqual(calibration().timestamp(origin, 0), (UTC_NS, 1_000_515))
        jumping = calibration(
            sample(),
            sample(target=3_000_000_000, controller=2_100_000_000, realtime=60_000_000_000),
        )
        with self.assertRaisesRegex(ValueError, "discontinuity"):
            jumping.timestamp(origin, 0)
        foreign = replace(sample(), target=replace(sample().target, boot_id="other"))
        with self.assertRaisesRegex(ValueError, "boots"):
            calibration(sample(), foreign).timestamp(origin, 0)
        with self.assertRaisesRegex(ValueError, "no target"):
            replace(calibration(), samples=()).timestamp(origin, 0)

    def test_frame_rates_csv_and_both_renderers(self):
        with tempfile.TemporaryDirectory() as directory:
            origin = TimeOrigin(domain="target_monotonic", value_ns=1_000_000_000)
            log = source(Path(directory) / "data.log", clock=calibration(), origin=origin)
            data = dataset(
                "test",
                [
                    {"time": 0.0, "value": 10},
                    {"time": 1.0, "value": None},
                    {"time": 2.0, "value": 20},
                ],
                units={"time": "s"},
                source=log,
                charts=(Line(label="test", y=("value",)),),
            )
            self.assertEqual(data.frame.time.tolist(), [0, 1, 2])
            self.assertEqual(data.frame.timestamp_ns.iloc[0], UTC_NS)
            plot = data.charts[0].as_plotly(data.frame)
            self.assertEqual(plot.layout.xaxis.type, "date")
            self.assertEqual(plot.layout.xaxis.title.text, "Date/time (UTC)")
            self.assertTrue(plot.data[0].x[0].endswith("Z"))
            self.assertIn("Estimated UTC uncertainty", plot.data[0].text[0])
            self.assertTrue(pd.isna(plot.data[0].y[1]))
            figure = data.charts[0].as_matplotlib(data.frame)
            FigureCanvasAgg(figure).draw()
            self.assertEqual(figure.axes[0].get_xlabel(), "Date/time (UTC)")
            figure.clear()
            path = Path(directory) / "test.csv"
            data.to_csv(path)
            restored = Dataset.from_csv(path)
            pd.testing.assert_frame_equal(restored.frame, data.frame)
            self.assertEqual(restored.provenance, data.provenance)
            self.assertEqual(restored.charts, data.charts)
            elapsed = replace(data.charts[0], datetime_x=False).as_plotly(data.frame)
            self.assertEqual(elapsed.layout.xaxis.title.text, "Time (s)")
            snapshots = replace(data.charts[0], x="value").as_plotly(data.frame)
            self.assertNotEqual(snapshots.layout.xaxis.type, "date")

    def test_missing_mapping_and_missing_samples_are_honest(self):
        for clock, origin in (
            (None, None),
            (replace(calibration(), samples=()), TimeOrigin(domain="target_monotonic", value_ns=0)),
        ):
            data = dataset(
                "test",
                [{"time": 0, "value": 1}],
                units={"time": "s"},
                source=source(Path("unused"), clock=clock, origin=origin),
            )
            self.assertNotIn("timestamp_ns", data.frame)
            self.assertIn("UTC timeline unavailable", data.warnings[-1])
        untimed = dataset("test", [{"snapshot": 1, "value": 1}], source=source(Path("unused")))
        self.assertFalse(untimed.warnings)

    def test_native_origin_and_external_tool_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.log"
            path.write_text(
                json.dumps(
                    {
                        "type": "metadata",
                        "monotonic_origin_ns": 1_000_000_000,
                        "origin_uncertainty_ns": 20,
                        "boot_id": "boot",
                    }
                )
                + "\n"
            )
            ctx = SimpleNamespace(
                connection=SimpleNamespace(target=TARGET, device_id=DeviceId("test")),
                action_id=ActionId("test"),
            )
            log = log_ref(ctx, path, Schema(name="cpu_freq"), complete=True, clock=calibration())
            self.assertEqual(log.metadata.time_origin.value_ns, 1_000_000_000)
            path.write_text(
                json.dumps(
                    {
                        "start": {"timestamp": {"timesecs": 50}},
                        "intervals": [
                            {"streams": [{"start": 0, "end": 1, "bits_per_second": 8, "bytes": 1}]}
                        ],
                    }
                )
            )
            data = IperfProcessor().process(
                source(path, schema="iperf", clock=calibration()), params=Params()
            )[0]
            self.assertEqual(data.frame.timestamp_ns.iloc[0], UTC_NS + 1_000_000_000)
            self.assertGreaterEqual(data.frame.timestamp_uncertainty_ns.iloc[0], 1_000_000_000)
            rup_path = Path(__file__).parent / "fixtures/rup/mixed.jsonl"
            header = json.loads(rup_path.read_text().splitlines()[0])
            start = header["started_at_ms"] * 1_000_000
            clock = calibration(sample(realtime=start))
            rup = RupProcessor().process(
                source(rup_path, schema="rup", clock=clock), params=Params()
            )[0]
            self.assertIn("timestamp_ns", rup.frame)
            expected = UTC_NS + (rup.frame.timestamp_ms.iloc[0] * 1_000_000 - start)
            self.assertEqual(rup.frame.timestamp_ns.iloc[0], expected)


class TrackerTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_cancellation_joins_final_calibration(self):
        tracker = ClockTracker(SimpleNamespace())
        entered, final_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = 0

        async def refresh():
            nonlocal calls
            calls += 1
            if calls == 2:
                final_started.set()
                await release.wait()
            return tracker.calibration

        tracker.refresh = refresh

        async def owner():
            async with tracker.session():
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(owner())
        await entered.wait()
        task.cancel()
        await final_started.wait()
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(calls, 2)

    async def test_selects_lowest_latency_and_preserves_failure(self):
        connection = SimpleNamespace(
            target=TARGET, sample_clock=AsyncMock(return_value=sample().target)
        )
        tracker = ClockTracker(connection)
        ticks = [0, 100, 200, 210, 300, 340, 400, 450, 500, 600]
        with patch("vsm.clock.time.monotonic_ns", side_effect=ticks):
            result = await tracker.refresh()
        self.assertEqual(result.samples[0].roundtrip_ns, 10)
        self.assertEqual(connection.sample_clock.await_count, 5)
        connection.sample_clock.side_effect = OSError("offline")
        failed = await tracker.refresh()
        self.assertEqual(failed.samples, result.samples)
        self.assertIn("offline", failed.warnings[-1])
        connection.sample_clock.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await tracker.refresh()

    async def test_boot_mismatch_is_a_warning_without_samples(self):
        connection = SimpleNamespace(
            target=TARGET,
            sample_clock=AsyncMock(return_value=replace(sample().target, boot_id="other")),
        )
        result = await ClockTracker(connection).refresh()
        self.assertFalse(result.samples)
        self.assertIn("another boot", result.warnings[-1])

    async def test_clock_wire_validation(self):
        client = TargetctlClient(SimpleNamespace(), None, TargetctlOptions())
        good = {"boot_id": "boot", "monotonic_ns": 1, "realtime_ns": 2, "uncertainty_ns": 3}
        client.request = AsyncMock(return_value=[good])
        self.assertEqual((await client.sample_clock()).monotonic_ns, 1)
        for invalid in ({**good, "monotonic_ns": True}, {**good, "uncertainty_ns": -1}, {}):
            client.request.return_value = [invalid]
            with self.assertRaises(TargetctlProtocolError):
                await client.sample_clock()


class HostClockTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_capture_reimports_controller_timestamps_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            before = time.time_ns()
            async with asyncio.timeout(20), Run(Path(directory) / "run") as run:
                host = await run.connect(Host())
                async with host.use(ProcStat(interval=20)) as capture:
                    await asyncio.sleep(0.08)
                log = capture.log
                self.assertGreaterEqual(len(log.metadata.clock.samples), 2)
                self.assertEqual(log.metadata.time_origin.domain, "target_monotonic")
                self.assertTrue(log.path.with_name(log.path.name + ".metadata.json").is_file())
            after = time.time_ns()
            restored = load_native(log.path, schema=Schema(name="proc_stat"))
            data = CpuProcessor().process(restored, params=Params())[0]
            self.assertIn("timestamp_ns", data.frame)
            for stamp, uncertainty in zip(
                data.frame.timestamp_ns, data.frame.timestamp_uncertainty_ns, strict=True
            ):
                self.assertGreaterEqual(stamp + uncertainty, before)
                self.assertLessEqual(stamp - uncertainty, after)
            self.assertFalse(data.warnings)
            self.assertEqual(data.charts[0].as_plotly(data.frame).layout.xaxis.type, "date")
