"""Upstream-format regression tests and deterministic resource lifecycle checks."""

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from support import FakeConnection, FakeProcess, TestContext

from vsm.action.rup import Rup
from vsm.action.rup_capture import summary_complete
from vsm.models import ActionId, CleanupUnknown, Completed, DeviceId, LogMetadata, LogRef, Schema
from vsm.processing import Params, load_native
from vsm.processor.rup import RupProcessor
from vsm.util.network import BindEndpoint, Endpoint, ProbeProtocol, ProbeRole
from vsm.util.records import LatencyRecord

FIXTURES = Path(__file__).parent / "fixtures/rup"


class RupProcessorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "rup.jsonl"
        self.records = [
            json.loads(line) for line in (FIXTURES / "mixed.jsonl").read_text().splitlines()
        ]

    def source(self, records=None, complete=True):
        self.path.write_text(
            "".join(json.dumps(r) + "\n" for r in (self.records if records is None else records))
        )
        return LogRef(
            path=self.path,
            schema=Schema[LatencyRecord](name="rup"),
            device_id=DeviceId("fixture-device"),
            action_id=ActionId("fixture-action"),
            complete=complete,
            metadata=LogMetadata(target=FakeConnection().target, provider="rup.ping/3"),
        )

    def test_all_outcomes_summary_and_provenance(self):
        source = self.source()
        measurements, summary = RupProcessor().process(source, params=Params())
        self.assertEqual(
            list(measurements.frame["record"]),
            ["reply", "terminal_reply", "timeout", "reorder_or_loss"],
        )
        self.assertEqual(list(measurements.frame["time"]), [0.1, 0.2, 0.3, 0.4])
        self.assertEqual(summary.frame.iloc[0]["received"], 2)
        self.assertEqual(summary.frame.iloc[0]["lost"], 2)
        self.assertEqual(summary.frame.iloc[0]["rtt_std_dev_ms"], 1)
        self.assertFalse(hasattr(measurements.provenance, "version"))
        self.assertEqual(next(c.unit for c in measurements.columns if c.name == "rtt_ms"), "ms")
        measurements.to_csv(Path(self.temp.name) / "measurements.csv")
        self.assertTrue((Path(self.temp.name) / "measurements.csv.json").exists())

    def test_loss_only_and_empty_measurements(self):
        records = [json.loads(line) for line in (FIXTURES / "loss.jsonl").read_text().splitlines()]
        results = RupProcessor().process(self.source(records), params=Params())
        self.assertEqual(results[0].frame.iloc[0]["record"], "timeout")
        self.assertIsNone(results[1].frame.iloc[0]["rtt_mean_ms"])
        results = RupProcessor().process(self.source([records[0], records[-1]]), params=Params())
        self.assertTrue(results[0].frame.empty)
        self.assertIn("rtt_ms", results[0].frame.columns)

    def test_partial_and_truncated_summary(self):
        source = self.source(self.records[:-1], complete=False)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            RupProcessor().process(source, params=Params())
        self.path.write_text(self.path.read_text() + '{"record":')
        result = RupProcessor().process(source, params=Params(allow_partial=True))
        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0].frame), 4)
        with self.assertRaises(ValueError):
            RupProcessor().process(replace(source, complete=True), params=Params())

    def test_rejects_bad_records_and_summary(self):
        changes = [
            (0, "version", 4),
            (0, "started_at_ms", -1),
            (1, "rtt_ms", float("nan")),
            (1, "rtt_ms", True),
            (1, "size_bytes", 0),
            (1, "ttl", 256),
            (2, "seq", 0),
            (2, "timestamp_ms", 900),
            (3, "record", "surprise"),
            (5, "received", 3),
            (5, "loss_percent", 0),
            (5, "rtt_mean_ms", None),
        ]
        for index, key, value in changes:
            with self.subTest(index=index, key=key):
                records = [r.copy() for r in self.records]
                records[index][key] = value
                with self.assertRaises(ValueError):
                    RupProcessor().process(self.source(records), params=Params())
        with self.assertRaises(ValueError):
            RupProcessor().process(self.source(self.records + [self.records[1]]), params=Params())
        with self.assertRaises(ValueError):
            RupProcessor().process(
                replace(self.source(), schema=Schema(name="other")), params=Params()
            )

    def test_server_logs_rejected(self):
        source = self.source()
        self.path.write_text("Running UDP server listening 127.0.0.1:5000\n")
        with self.assertRaisesRegex(ValueError, "server logs"):
            RupProcessor().process(source, params=Params())

    def test_wrong_measurement_is_rejected_even_with_partial_opt_in(self):
        source = replace(self.source(), schema=Schema(name="other"))
        for params in (Params(), Params(allow_partial=True, allow_read_errors=True)):
            with self.assertRaisesRegex(ValueError, "schema mismatch"):
                RupProcessor().process(source, params=params)

    def test_import_standalone_upstream_and_detects_missing_summary(self):
        source = self.source()
        imported = load_native(source.path, schema=source.schema)
        self.assertTrue(imported.complete)
        self.assertEqual(imported.metadata.provider, "rup.ping/3")
        self.assertEqual(imported.metadata.target.os, "unknown")
        self.assertEqual(len(RupProcessor().process(imported, params=Params())[0].frame), 4)
        with self.assertRaises(ValueError):
            load_native(source.path, schema=Schema(name="other"))
        self.source(self.records[:-1])
        self.assertFalse(load_native(source.path, schema=source.schema).complete)

    def test_completion_needs_zero_exit_and_valid_summary(self):
        source = self.source()
        self.assertTrue(summary_complete(source.path, Completed(exit_code=0), client=True))
        self.assertFalse(summary_complete(source.path, Completed(exit_code=-9), client=True))
        self.source(self.records[:-1])
        self.assertFalse(summary_complete(source.path, Completed(exit_code=0), client=True))
        self.path.write_text("invalid\n")
        self.assertFalse(summary_complete(source.path, Completed(exit_code=0), client=True))


class RupActionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.conn = FakeConnection()
        self.ctx = TestContext(self.conn, self.temp.name)
        self.address = Endpoint.parse("127.0.0.1:5000")

    async def test_upstream_client_options_and_fractional_duration(self):
        action = Rup(
            address=self.address,
            duration=0.01,
            count=3,
            adaptive=True,
            request_size=64,
            response_size=128,
            traffic_class=184,
            local=BindEndpoint.parse("127.0.0.1:0"),
        )
        async with action.open(self.ctx) as capture:
            await capture.wait()
        await self.ctx.join()
        command = next(c for c in self.conn.commands if c.env)
        self.assertNotIn("client", command.argv)
        self.assertNotIn("--interval", command.argv)
        self.assertNotIn("--run-time", command.argv)
        self.assertIn("--adaptive-interval", command.argv)
        self.assertIn("--tos", command.argv)
        self.assertFalse(hasattr(capture.log.schema, "version"))
        self.assertTrue(capture.log.complete)
        self.assertFalse(
            any(key.startswith(str(self.conn.tmp_dir / "rup.")) for key in self.conn.files)
        )

    async def test_server_lifecycle_and_fixed_interval(self):
        client = Rup(address=self.address)
        self.assertEqual(client.arguments()[:2], ("--protocol", "udp"))
        self.assertIn("100", client.arguments())
        action = Rup(
            address=self.address, role=ProbeRole.SERVER, protocol=ProbeProtocol.TCP, duration=0.01
        )
        self.assertEqual(action.arguments(), ("--protocol", "tcp", "server", str(self.address)))
        async with action.open(self.ctx) as capture:
            await capture.wait()
        await self.ctx.join()
        self.assertTrue(capture.log.complete)

    async def test_cancel_stops_and_finalizes(self):
        holder = []

        async def run():
            async with Rup(address=self.address, duration=None).open(self.ctx) as capture:
                holder.append(capture)
                await asyncio.sleep(100)

        task = asyncio.create_task(run())
        while not holder:
            await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.ctx.join()
        self.assertTrue(holder[0].log.complete)
        self.assertFalse(
            any(key.startswith(str(self.conn.tmp_dir / "rup.")) for key in self.conn.files)
        )

    async def test_readiness_failure_cleans_up_owned_directory(self):
        async def fail(*args):
            raise TimeoutError("no readiness")

        with patch("vsm.action.rup.marker_ready", fail), self.assertRaises(TimeoutError):
            async with Rup(address=self.address).open(self.ctx):
                self.fail("must not enter")
        self.assertFalse(
            any(key.startswith(str(self.conn.tmp_dir / "rup.")) for key in self.conn.files)
        )
        self.assertTrue(any(c.argv[:3] == ("rm", "-rf", "--") for c in self.conn.commands))

    async def test_unknown_cleanup_retains_control_and_incomplete_capture(self):
        async def unknown(process, *, grace=5):
            process.stopped.set()
            raise CleanupUnknown("unconfirmed stop")

        captures = []
        with patch.object(FakeProcess, "stop", unknown), self.assertRaises(CleanupUnknown):
            async with Rup(address=self.address, duration=None).open(self.ctx) as capture:
                captures.append(capture)
        await self.ctx.join()
        self.assertFalse(captures[0].log.complete)
        self.assertTrue(
            any(key.startswith(str(self.conn.tmp_dir / "rup.")) for key in self.conn.files)
        )
        self.assertFalse(any(c.argv[:3] == ("rm", "-rf", "--") for c in self.conn.commands))
        self.assertTrue(self.ctx.output_path("rup-control.json").is_file())

    def test_bind_endpoint_allows_ephemeral_port_without_changing_remote_invariant(self):
        self.assertEqual(str(BindEndpoint.parse("[::1]:0")), "[::1]:0")
        with self.assertRaises(ValueError):
            Endpoint.parse("127.0.0.1:0")
        for value in ("127.0.0.1:-1", "127.0.0.1:65536"):
            with self.assertRaises(ValueError):
                BindEndpoint.parse(value)

    def test_validation(self):
        for options in [
            {"count": 0},
            {"count": True},
            {"duration": float("inf")},
            {"interval": 0},
            {"timeout": 0},
            {"request_size": 11},
            {"response_size": 65536},
            {"traffic_class": 256},
            {"local": BindEndpoint.parse("[::1]:0")},
            {"protocol": ProbeProtocol.ICMP, "role": ProbeRole.SERVER},
            {"protocol": ProbeProtocol.ICMP, "response_size": 12},
            {"role": ProbeRole.SERVER, "count": 1},
            {"protocol": "udp"},
        ]:
            with self.subTest(options=options), self.assertRaises(ValueError):
                Rup(address=self.address, **options)
