"""Scope-owned native lifetimes and explicitly requested deadlines."""

import inspect
import tempfile
import unittest

from support import FakeConnection
from support import TestContext as CollectorContext
from test_intervals import arguments

from vsm.action.pid_stat import PidStat
from vsm.action.poll_files import PollFiles
from vsm.action.registry import ACTION_TYPES
from vsm.action.rup import Rup
from vsm.action.scan_processes import ScanProcesses
from vsm.action.socket_stat import SocketStat
from vsm.action.traffic_generator import TrafficGenerator
from vsm.models import OwnershipError

FAMILIES = (PollFiles, PidStat, SocketStat, TrafficGenerator, Rup)


class DurationTests(unittest.IsolatedAsyncioTestCase):
    def test_defaults_including_inherited_measurements_and_validation(self):
        for name, implementation in ACTION_TYPES.items():
            if not issubclass(implementation, FAMILIES):
                continue
            with self.subTest(action=name):
                kwargs = arguments(implementation)
                self.assertIsNone(inspect.signature(implementation).parameters["duration"].default)
                self.assertIsNone(implementation(**kwargs).duration)
                self.assertEqual(implementation(**kwargs, duration=0.05).duration, 0.05)
                for invalid in (0, -1, float("nan"), float("inf"), 86401):
                    with self.subTest(duration=invalid), self.assertRaises(ValueError):
                        implementation(**kwargs, duration=invalid)

    async def test_native_argv_scope_exit_and_complete_logs(self):
        connection = FakeConnection()
        with tempfile.TemporaryDirectory() as directory:
            ctx = CollectorContext(connection, directory)
            selection = await ScanProcesses().run(ctx)
            await ctx.join()
            for implementation in FAMILIES:
                for duration in (None, 0.05):
                    with self.subTest(action=implementation.__name__, duration=duration):
                        kwargs = arguments(implementation)
                        if "processes" in kwargs:
                            kwargs["processes"] = selection
                        action = implementation(**kwargs, duration=duration)
                        ctx = CollectorContext(connection, directory)
                        async with action.open(ctx) as capture:
                            with self.assertRaises(OwnershipError):
                                _ = capture.log
                        await ctx.join()
                        self.assertTrue(capture.log.complete)
                        command = (
                            connection.commands[-1]
                            if implementation is not Rup
                            else next(
                                command for command in reversed(connection.commands) if command.env
                            )
                        )
                        if implementation is not Rup:
                            if duration is None:
                                self.assertNotIn("--duration-ms", command.argv)
                            else:
                                self.assertEqual(
                                    command.argv[command.argv.index("--duration-ms") + 1], "50"
                                )
