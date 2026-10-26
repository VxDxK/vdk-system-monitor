"""Owned lifecycle tests use controllable process/transport doubles."""

import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from support import FakeConnection, FakeProcess
from support import TestContext as ContextDouble

from vsm.action.command_collector import CommandCollector
from vsm.action.dmesg import Dmesg
from vsm.action.fio import Fio
from vsm.action.ftrace import Ftrace
from vsm.action.iperf import Iperf
from vsm.action.launch import Launch
from vsm.action.launch_mode import LaunchMode
from vsm.action.ping import Ping
from vsm.action.rup import Rup
from vsm.action.simpleperf import Simpleperf
from vsm.action.strace import Strace
from vsm.action.tcpdump import Tcpdump
from vsm.action.top import Top
from vsm.action.traffic_generator import TrafficGenerator
from vsm.models import CleanupUnknown, CommandFailed, Completed, OwnershipError
from vsm.runtime import Device, Run
from vsm.util.network import Endpoint


class ControlledProcess(FakeProcess):
    def __init__(self, output, rup=False):
        super().__init__(output, rup)
        self.code = 0
        self.stop_requested = asyncio.Event()
        self.release_cleanup = asyncio.Event()
        self.release_cleanup.set()
        self.cleanup_error = None

    async def finish(self, code=0):
        self.code = code
        # Model a native terminal record, without issuing a stop request.
        await super().stop()

    async def wait(self, *, check=True):
        await self.stopped.wait()
        result = Completed(exit_code=self.code)
        if check and self.code:
            raise CommandFailed(result)
        return result

    async def stop(self, *, grace=5):
        self.stop_requested.set()
        await self.release_cleanup.wait()
        await self.finish(self.code)
        if self.cleanup_error:
            raise self.cleanup_error
        return Completed(exit_code=self.code)


class ControlledConnection(FakeConnection):
    def __init__(self, *, instant=False):
        super().__init__()
        self.started = asyncio.Event()
        self.instant = instant
        self.process = None

    @asynccontextmanager
    async def launch(self, command, **kwargs):
        with patch("support.FakeProcess", ControlledProcess):
            async with super().launch(command, **kwargs) as process:
                self.process = process
                if self.instant:
                    await process.finish()
                self.started.set()
                yield process


class ModeTests(unittest.IsolatedAsyncioTestCase):
    def factories(self):
        address = Endpoint.parse("127.0.0.1:5201")
        return (
            Launch(argv=("workload",)),
            TrafficGenerator(address=address),
            Rup(address=address, duration=None),
            Iperf(host="127.0.0.1"),
            Fio(args=("--name=test",)),
            Ping(destination="127.0.0.1", args=("-c", "1")),
            CommandCollector(),
            Simpleperf(),
            Strace(),
            Tcpdump(),
            Dmesg(),
            Top(),
            Ftrace(events=("sched/sched_switch",)),
        )

    async def test_every_wrapper_foreground_background_wait_and_stop(self):
        for configured in self.factories():
            for mode in LaunchMode:
                with (
                    self.subTest(action=type(configured).__name__, mode=mode),
                    tempfile.TemporaryDirectory() as tmp,
                ):
                    conn = ControlledConnection()
                    ctx = ContextDouble(conn, tmp)
                    action = replace(configured, mode=mode)
                    entered = asyncio.Event()

                    async def execute(mode=mode, action=action, ctx=ctx, entered=entered):
                        if mode is LaunchMode.FOREGROUND:
                            return await action.run(ctx)
                        async with action.open(ctx) as resource:
                            entered.set()
                        return resource

                    task = asyncio.create_task(execute())
                    await asyncio.wait_for(conn.started.wait(), 1)
                    if mode is not LaunchMode.FOREGROUND:
                        await asyncio.wait_for(entered.wait(), 1)
                    if mode is LaunchMode.BACKGROUND_STOP:
                        await asyncio.wait_for(task, 1)
                        self.assertTrue(conn.process.stop_requested.is_set())
                    else:
                        await asyncio.sleep(0.03)
                        self.assertFalse(task.done())
                        self.assertFalse(conn.process.stop_requested.is_set())
                        await conn.process.finish()
                    result = await asyncio.wait_for(task, 1)
                    await ctx.join()
                    if not isinstance(action, Launch):
                        log = result if mode is LaunchMode.FOREGROUND else result.log
                        self.assertTrue(log.complete)
                        self.assertTrue(log.path.exists())
                    elif mode is LaunchMode.FOREGROUND:
                        self.assertEqual(result.exit_code, 0)
                    self.assertEqual(conn.files["/sys/kernel/tracing/tracing_on"], b"0")

    async def test_wrong_entrypoint_and_untyped_mode_do_not_launch(self):
        for configured in self.factories():
            with (
                self.subTest(action=type(configured).__name__),
                tempfile.TemporaryDirectory() as tmp,
            ):
                ctx = ContextDouble(ControlledConnection(), tmp)
                with self.assertRaises(ValueError):
                    await configured.run(ctx)
                with self.assertRaises(ValueError):
                    async with replace(configured, mode=LaunchMode.FOREGROUND).open(ctx):
                        pass
                with self.assertRaises(TypeError):
                    async with replace(configured, mode="background_wait").open(ctx):
                        pass
                self.assertFalse(ctx.connection.commands)

    async def test_short_commands_can_complete_during_startup(self):
        for configured in self.factories():
            with (
                self.subTest(action=type(configured).__name__),
                tempfile.TemporaryDirectory() as tmp,
            ):
                ctx = ContextDouble(ControlledConnection(instant=True), tmp)
                result = await asyncio.wait_for(
                    replace(configured, mode=LaunchMode.FOREGROUND).run(ctx), 1
                )
                await ctx.join()
                self.assertIsNotNone(result)

    async def test_nonzero_completion_finalizes_log_and_propagates(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = ControlledConnection()
            ctx = ContextDouble(conn, tmp)
            action = Fio(args=("--name=test",), mode=LaunchMode.BACKGROUND_WAIT)
            manager = action.open(ctx)
            capture = await manager.__aenter__()
            await conn.process.finish(7)
            with self.assertRaises(CommandFailed):
                await manager.__aexit__(None, None, None)
            self.assertFalse(capture.log.complete)
            self.assertTrue(conn.process.stop_requested.is_set())
            await asyncio.gather(*ctx.workers, return_exceptions=True)

    async def test_runtime_wait_exit_is_cancellable_through_repeated_cancellation(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = ControlledConnection()
            async with Run(Path(tmp) / "run") as run:
                device = Device(run, conn, None, "connection-0001")
                # No native deployment is needed for the generic launch action.
                action = Launch(argv=("workload",), mode=LaunchMode.BACKGROUND_WAIT)

                async def execute():
                    async with device.use(action):
                        conn.process.release_cleanup.clear()

                task = asyncio.create_task(execute())
                await asyncio.wait_for(conn.started.wait(), 1)
                await asyncio.sleep(0.03)
                self.assertFalse(task.done())
                task.cancel()
                await asyncio.wait_for(conn.process.stop_requested.wait(), 1)
                task.cancel()
                await asyncio.sleep(0.03)
                self.assertFalse(task.done())
                conn.process.release_cleanup.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 1)
                self.assertTrue(conn.process.stopped.is_set())
                self.assertEqual(run._actions[-1]["status"], "failed")

    async def test_body_failure_stops_instead_of_waiting_and_retains_cleanup_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = ControlledConnection()
            async with Run(Path(tmp) / "run") as run:
                device = Device(run, conn, None, "connection-0001")
                with self.assertRaisesRegex(ValueError, "body failure") as caught:
                    async with device.use(
                        Launch(argv=("workload",), mode=LaunchMode.BACKGROUND_WAIT)
                    ):
                        conn.process.cleanup_error = RuntimeError("cleanup failure")
                        raise ValueError("body failure")
                self.assertTrue(conn.process.stop_requested.is_set())
                self.assertIn(
                    "cleanup failure", " ".join(getattr(caught.exception, "__notes__", ()))
                )
                self.assertEqual(run.cleanup_errors, ["cleanup failure"])

    async def test_foreground_claims_conflict_with_scoped_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = ControlledConnection()
            async with Run(Path(tmp) / "run") as run:
                device = Device(run, conn, None, "connection-0001")
                from vsm.action.set_file import SetFile

                async with device.use(
                    SetFile(path=conn.path("/sys/kernel/tracing/tracing_on"), value=b"0")
                ):
                    with self.assertRaises(OwnershipError):
                        await device.do(Ftrace(mode=LaunchMode.FOREGROUND))
                    self.assertFalse(conn.commands)

    async def test_unknown_cleanup_propagates_and_marks_log_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = ControlledConnection()
            ctx = ContextDouble(conn, tmp)
            action = TrafficGenerator(address=Endpoint.parse("127.0.0.1:5201"))
            with self.assertRaises(CleanupUnknown):
                async with action.open(ctx) as capture:
                    conn.process.cleanup_error = CleanupUnknown("transport lost")
            self.assertFalse(capture.log.complete)
            self.assertTrue(capture.log.path.exists())
            await asyncio.gather(*ctx.workers, return_exceptions=True)

    async def test_cancelling_a_waiter_keeps_capture_owned_and_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = ControlledConnection()
            ctx = ContextDouble(conn, tmp)
            action = Fio(args=("--name=test",), mode=LaunchMode.BACKGROUND_WAIT)
            async with action.open(ctx) as capture:
                waiter = asyncio.create_task(capture.wait())
                await asyncio.sleep(0)
                waiter.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await waiter
                self.assertFalse(conn.process.stop_requested.is_set())
                self.assertFalse(conn.process.stopped.is_set())
                await conn.process.finish()
            self.assertTrue(capture.log.complete)
            await ctx.join()

    async def test_foreground_cancellation_stops_and_joins_owned_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = ControlledConnection()
            ctx = ContextDouble(conn, tmp)
            action = Fio(args=("--name=test",), mode=LaunchMode.FOREGROUND)
            task = asyncio.create_task(action.run(ctx))
            await asyncio.wait_for(conn.started.wait(), 1)
            await asyncio.sleep(0.03)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            self.assertTrue(conn.process.stop_requested.is_set())
            self.assertTrue(conn.process.stopped.is_set())
            await ctx.join()

    async def test_foreground_runtime_returns_completed_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = ControlledConnection(instant=True)
            async with Run(Path(tmp) / "run") as run:
                device = Device(run, conn, None, "connection-0001")
                result = await device.do(Launch(argv=("workload",), mode=LaunchMode.FOREGROUND))
                self.assertEqual(result.exit_code, 0)
                self.assertEqual(run._actions[-1]["status"], "finished")
