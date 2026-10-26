"""Owned lifecycle tests use controllable process/transport doubles."""

import asyncio
import tempfile
import unittest
from contextlib import AsyncExitStack, asynccontextmanager
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
from vsm.action.ping import Ping
from vsm.action.rup import Rup
from vsm.action.simpleperf import Simpleperf
from vsm.action.strace import Strace
from vsm.action.tcpdump import Tcpdump
from vsm.action.top import Top
from vsm.action.traffic_generator import TrafficGenerator
from vsm.models import CleanupUnknown, CommandFailed, Completed, OwnershipError
from vsm.runtime import Device, Run
from vsm.util.network import Endpoint, ProbeRole


class ControlledProcess(FakeProcess):
    def __init__(self, output, rup=False):
        super().__init__(output, rup)
        self.code = 0
        self.stop_requested = asyncio.Event()
        self.release_cleanup = asyncio.Event()
        self.release_cleanup.set()
        self.cleanup_error = None
        self.waiting = asyncio.Event()

    async def finish(self, code=0):
        self.code = code
        # Model a native terminal record, without issuing a stop request.
        await super().stop()

    async def wait(self, *, check=True):
        if check:
            self.waiting.set()
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
        self.launches = asyncio.Queue()

    @asynccontextmanager
    async def launch(self, command, **kwargs):
        async with AsyncExitStack() as stack:
            with patch("support.FakeProcess", ControlledProcess):
                process = await stack.enter_async_context(super().launch(command, **kwargs))
            self.process = process
            if self.instant:
                await process.finish()
            self.started.set()
            self.launches.put_nowait(process)
            yield process


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    def factories(self):
        address = Endpoint.parse("127.0.0.1:5201")
        return (
            Launch(argv=("workload",)),
            TrafficGenerator(address=address),
            Rup(address=address, duration=None),
            Rup(address=address, role=ProbeRole.SERVER),
            Iperf(host="127.0.0.1"),
            Iperf(server=True),
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

    async def test_every_wrapper_waits_explicitly_or_stops_on_exit(self):
        for action in self.factories():
            for invocation in ("run", "open_wait", "open_stop"):
                with (
                    self.subTest(action=action, invocation=invocation),
                    tempfile.TemporaryDirectory() as tmp,
                ):
                    conn = ControlledConnection()
                    ctx = ContextDouble(conn, tmp)
                    entered = asyncio.Event()

                    async def execute(
                        invocation=invocation, action=action, ctx=ctx, entered=entered
                    ):
                        if invocation == "run":
                            return await action.run(ctx)
                        async with action.open(ctx) as resource:
                            entered.set()
                            if invocation == "open_wait":
                                await resource.wait()
                            if not isinstance(action, Launch):
                                with self.assertRaises(OwnershipError):
                                    _ = resource.log
                        return resource

                    task = asyncio.create_task(execute())
                    await asyncio.wait_for(conn.started.wait(), 1)
                    if invocation != "run":
                        await asyncio.wait_for(entered.wait(), 1)
                    if invocation == "open_stop":
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
                        log = result if invocation == "run" else result.log
                        self.assertTrue(log.complete)
                        self.assertTrue(log.path.exists())
                    elif invocation == "run":
                        self.assertEqual(result.exit_code, 0)
                    self.assertEqual(conn.files["/sys/kernel/tracing/tracing_on"], b"0")

    async def test_short_commands_can_complete_during_startup(self):
        for action in self.factories():
            for invocation in ("run", "open"):
                with (
                    self.subTest(action=action, invocation=invocation),
                    tempfile.TemporaryDirectory() as tmp,
                ):
                    ctx = ContextDouble(ControlledConnection(instant=True), tmp)
                    if invocation == "run":
                        result = await asyncio.wait_for(action.run(ctx), 1)
                    else:
                        async with action.open(ctx) as result:
                            await result.wait()
                    await ctx.join()
                    self.assertIsNotNone(result)

    async def test_clean_completion_is_allowed_while_scope_remains_open(self):
        for action in self.factories():
            with self.subTest(action=action), tempfile.TemporaryDirectory() as tmp:
                conn = ControlledConnection()
                ctx = ContextDouble(conn, tmp)
                async with action.open(ctx) as resource:
                    await conn.process.finish()
                    await resource.wait()
                    await ctx.join()
                    self.assertFalse(conn.process.stop_requested.is_set())
                    if not isinstance(action, Launch):
                        with self.assertRaises(OwnershipError):
                            _ = resource.log
                if not isinstance(action, Launch):
                    self.assertTrue(resource.log.complete)

    async def test_nonzero_completion_finalizes_log_and_propagates(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = ControlledConnection()
            ctx = ContextDouble(conn, tmp)
            action = Fio(args=("--name=test",))
            with self.assertRaises(CommandFailed):
                async with action.open(ctx) as capture:
                    await conn.process.finish(7)
                    await capture.wait()
            self.assertFalse(capture.log.complete)
            self.assertTrue(conn.process.stop_requested.is_set())
            await asyncio.gather(*ctx.workers, return_exceptions=True)

    async def test_nonzero_exit_is_supervised_without_an_explicit_wait(self):
        for action in self.factories():
            if isinstance(action, Launch):
                continue
            with self.subTest(action=action), tempfile.TemporaryDirectory() as tmp:
                conn = ControlledConnection()
                ctx = ContextDouble(conn, tmp)
                async with action.open(ctx) as capture:
                    await conn.process.finish(7)
                    with self.assertRaises(CommandFailed):
                        await ctx.join()
                self.assertFalse(capture.log.complete)

    async def test_readiness_failure_stops_and_finalizes_each_collector(self):
        async def fail(readiness, wait, **kwargs):
            readiness.close()
            raise TimeoutError("not ready")

        for action in self.factories():
            if isinstance(action, Launch):
                continue
            with self.subTest(action=action), tempfile.TemporaryDirectory() as tmp:
                conn = ControlledConnection()
                ctx = ContextDouble(conn, tmp)
                with (
                    patch(f"{type(action).open.__module__}.await_ready", fail),
                    self.assertRaisesRegex(TimeoutError, "not ready"),
                ):
                    async with action.open(ctx):
                        self.fail("entered before readiness")
                self.assertTrue(conn.process.stop_requested.is_set())
                self.assertTrue(conn.process.stopped.is_set())
                self.assertTrue(ctx.output_path("data.log.metadata.json").exists())
                self.assertEqual(conn.files["/sys/kernel/tracing/tracing_on"], b"0")
                await ctx.join()

    async def test_do_preserves_wait_failure_when_cleanup_also_fails(self):
        for action in (Launch(argv=("workload",)), Fio(args=("--name=test",))):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as tmp:
                conn = ControlledConnection()
                ctx = ContextDouble(conn, tmp)
                task = asyncio.create_task(action.run(ctx))
                await asyncio.wait_for(conn.started.wait(), 1)
                await asyncio.wait_for(conn.process.waiting.wait(), 1)
                conn.process.cleanup_error = RuntimeError("cleanup failure")
                await conn.process.finish(7)
                with self.assertRaises(CommandFailed) as caught:
                    await asyncio.wait_for(task, 1)
                self.assertIn("cleanup failure", " ".join(caught.exception.__notes__))
                self.assertTrue(conn.process.stopped.is_set())
                await asyncio.gather(*ctx.workers, return_exceptions=True)

    async def test_runtime_explicit_wait_is_cancellable_through_repeated_cancellation(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = ControlledConnection()
            async with Run(Path(tmp) / "run") as run:
                device = Device(run, conn, None, "connection-0001")
                # No native deployment is needed for the generic launch action.
                action = Launch(argv=("workload",))

                async def execute():
                    async with device.use(action) as process:
                        conn.process.release_cleanup.clear()
                        await process.wait()

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
                    async with device.use(Launch(argv=("workload",))):
                        conn.process.cleanup_error = RuntimeError("cleanup failure")
                        raise ValueError("body failure")
                self.assertTrue(conn.process.stop_requested.is_set())
                self.assertIn(
                    "cleanup failure", " ".join(getattr(caught.exception, "__notes__", ()))
                )
                self.assertEqual(run.cleanup_errors, ["cleanup failure"])

    async def test_do_claims_conflict_with_scoped_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = ControlledConnection()
            async with Run(Path(tmp) / "run") as run:
                device = Device(run, conn, None, "connection-0001")
                from vsm.action.set_file import SetFile

                async with device.use(
                    SetFile(path=conn.path("/sys/kernel/tracing/tracing_on"), value=b"0")
                ):
                    with self.assertRaises(OwnershipError):
                        await device.do(Ftrace())
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
            action = Fio(args=("--name=test",))
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

    async def test_do_cancellation_stops_and_joins_owned_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = ControlledConnection()
            ctx = ContextDouble(conn, tmp)
            action = Fio(args=("--name=test",))
            task = asyncio.create_task(action.run(ctx))
            await asyncio.wait_for(conn.started.wait(), 1)
            await asyncio.sleep(0.03)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            self.assertTrue(conn.process.stop_requested.is_set())
            self.assertTrue(conn.process.stopped.is_set())
            await ctx.join()

    async def test_do_runtime_returns_completed_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = ControlledConnection(instant=True)
            async with Run(Path(tmp) / "run") as run:
                device = Device(run, conn, None, "connection-0001")
                result = await device.do(Launch(argv=("workload",)))
                self.assertEqual(result.exit_code, 0)
                self.assertEqual(run._actions[-1]["status"], "finished")

    async def test_run_exit_joins_concurrent_actions_until_natural_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = ControlledConnection()
            exit_started = asyncio.Event()
            async with Run(Path(tmp) / "run") as run:
                device = Device(run, conn, None, "connection-0001")
                command = run.spawn(device.do(Launch(argv=("workload",))))
                command_process = await asyncio.wait_for(conn.launches.get(), 1)
                benchmark = run.spawn(device.do(Fio(args=("--name=test",))))
                benchmark_process = await asyncio.wait_for(conn.launches.get(), 1)

                async def finish_work():
                    await exit_started.wait()
                    self.assertFalse(command_process.stop_requested.is_set())
                    self.assertFalse(benchmark_process.stop_requested.is_set())
                    self.assertFalse(command.done)
                    self.assertFalse(benchmark.done)
                    await command_process.finish()
                    await benchmark_process.finish()

                run.spawn(finish_work())
                exit_started.set()
            self.assertEqual((await command).exit_code, 0)
            self.assertTrue((await benchmark).complete)
            self.assertTrue(command_process.stopped.is_set())
            self.assertTrue(benchmark_process.stopped.is_set())

    async def test_run_failure_cancels_and_cleans_up_concurrent_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = ControlledConnection()
            with self.assertRaises(ExceptionGroup) as caught:
                async with Run(Path(tmp) / "run") as run:
                    device = Device(run, conn, None, "connection-0001")
                    sibling = run.spawn(device.do(Fio(args=("--name=test",))))
                    sibling_process = await asyncio.wait_for(conn.launches.get(), 1)
                    failing = run.spawn(device.do(Launch(argv=("workload",))))
                    failing_process = await asyncio.wait_for(conn.launches.get(), 1)
                    await failing_process.finish(7)
            self.assertIsNotNone(caught.exception.subgroup(CommandFailed))
            self.assertTrue(sibling.done)
            self.assertTrue(failing.done)
            self.assertTrue(sibling_process.stop_requested.is_set())
            self.assertTrue(sibling_process.stopped.is_set())
            self.assertTrue(failing_process.stopped.is_set())

    async def test_direct_scope_exit_joins_cleanup_through_repeated_cancellation(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = ControlledConnection()
            ctx = ContextDouble(conn, tmp)

            async def execute():
                async with Fio(args=("--name=test",)).open(ctx) as capture:
                    captures.append(capture)
                    conn.process.release_cleanup.clear()

            captures = []
            task = asyncio.create_task(execute())
            await asyncio.wait_for(conn.started.wait(), 1)
            await asyncio.wait_for(conn.process.stop_requested.wait(), 1)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            conn.process.release_cleanup.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            self.assertTrue(conn.process.stopped.is_set())
            self.assertTrue(captures[0].log.path.exists())
            await ctx.join()
