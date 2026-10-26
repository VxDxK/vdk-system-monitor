"""Public millisecond intervals, inherited defaults, and transport boundaries."""

import asyncio
import inspect
import tempfile
import unittest
from unittest.mock import patch

from support import FakeConnection
from support import TestContext as CollectorContext

from vsm.action.periodic_launcher import PeriodicLauncher
from vsm.action.pid_stat import PidStat
from vsm.action.poll_files import PollFiles
from vsm.action.process_io import ProcessIo
from vsm.action.registry import ACTION_TYPES
from vsm.action.rup import Rup
from vsm.action.scan_processes import ScanProcesses
from vsm.action.smaps import Smaps
from vsm.action.socket_stat import SocketStat
from vsm.action.wifi_conn_info import WifiConnInfo
from vsm.models import ProcessSet
from vsm.util.network import Endpoint


def arguments(implementation):
    supplied = {
        "argv": ("echo", "value"),
        "paths": ("/proc/123/stat",),
        "processes": ProcessSet(()),
        "interface": "wlan0",
        "address": Endpoint.parse("127.0.0.1:5000"),
    }
    return {
        name: supplied[name]
        for name, parameter in inspect.signature(implementation).parameters.items()
        if parameter.default is inspect.Parameter.empty
    }


class IntervalTests(unittest.IsolatedAsyncioTestCase):
    def test_all_registered_interval_defaults_and_validation(self):
        for name, implementation in ACTION_TYPES.items():
            if "interval" not in inspect.signature(implementation).parameters:
                continue
            kwargs = arguments(implementation)
            expected = {
                "periodic_launcher": 1000,
                "ss": 1000,
                "wifi_conn_info": 500,
                "rup": 100,
            }.get(name, 250)
            with self.subTest(action=name):
                action = implementation(**kwargs)
                self.assertIs(type(action.interval), int)
                self.assertEqual(action.interval, expected)
                self.assertEqual(implementation(**kwargs, interval=7).interval, 7)
                for invalid in (
                    True,
                    False,
                    1.0,
                    0.1,
                    0,
                    -1,
                    float("nan"),
                    float("inf"),
                    "100",
                    None,
                ):
                    with (
                        self.subTest(invalid=invalid),
                        self.assertRaisesRegex(
                            ValueError, "interval.*positive integer.*milliseconds"
                        ),
                    ):
                        implementation(**kwargs, interval=invalid)
                with self.assertRaises(TypeError):
                    implementation(**kwargs, interval_ms=100)

    async def test_native_argv_and_metadata(self):
        conn = FakeConnection()
        with tempfile.TemporaryDirectory() as directory:
            selection_ctx = CollectorContext(conn, directory)
            processes = await ScanProcesses().run(selection_ctx)
            await selection_ctx.join()
            for implementation in (PollFiles, Smaps, PidStat, ProcessIo, SocketStat, Rup):
                for interval in (None, 7):
                    with self.subTest(action=implementation.__name__, interval=interval):
                        kwargs = arguments(implementation)
                        if "processes" in kwargs:
                            kwargs["processes"] = processes
                        if interval is not None:
                            kwargs["interval"] = interval
                        action = implementation(**kwargs)
                        ctx = CollectorContext(conn, directory)
                        async with action.open(ctx) as capture:
                            pass
                        await ctx.join()
                        command = (
                            conn.commands[-1]
                            if implementation is not Rup
                            else next(c for c in reversed(conn.commands) if c.env)
                        )
                        flag = "--interval" if implementation is Rup else "--interval-ms"
                        self.assertEqual(
                            command.argv[command.argv.index(flag) + 1], str(action.interval)
                        )
                        self.assertEqual(
                            capture.log.metadata.interval_seconds, action.interval / 1000
                        )

    async def test_periodic_and_wifi_scheduling_and_metadata(self):
        conn = FakeConnection()
        with tempfile.TemporaryDirectory() as directory:
            for action in (
                PeriodicLauncher(argv=("echo", "value")),
                PeriodicLauncher(argv=("echo", "value"), interval=7),
                WifiConnInfo(interface="wlan0"),
                WifiConnInfo(interface="wlan0", interval=7),
            ):
                ctx = CollectorContext(conn, directory)
                with patch(
                    "vsm.action.periodic_launcher.asyncio.wait_for", wraps=asyncio.wait_for
                ) as wait:
                    async with action.open(ctx) as capture:
                        pass
                    await ctx.join()
                self.assertEqual(wait.call_args.args[1], action.interval / 1000)
                self.assertEqual(capture.log.metadata.interval_seconds, action.interval / 1000)
                if isinstance(action, WifiConnInfo):
                    self.assertEqual(
                        conn.commands[-1].argv, ("/bin/iw", "dev", "wlan0", "station", "dump")
                    )

    async def test_rup_adaptive_omits_interval_and_metadata(self):
        conn = FakeConnection()
        with tempfile.TemporaryDirectory() as directory:
            ctx = CollectorContext(conn, directory)
            action = Rup(address=Endpoint.parse("127.0.0.1:5000"), adaptive=True, interval=7)
            self.assertNotIn("--interval", action.arguments())
            self.assertIn("--adaptive-interval", action.arguments())
            async with action.open(ctx) as capture:
                pass
            await ctx.join()
            self.assertIsNone(capture.log.metadata.interval_seconds)
