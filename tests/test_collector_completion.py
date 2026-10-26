"""Completion policy through real Run supervision and controllable target processes."""

import asyncio
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

from support import FakeConnection

from vsm.action.pid_stat import PidStat
from vsm.action.poll_files import PollFiles
from vsm.action.proc_stat import ProcStat
from vsm.action.process_io import ProcessIo
from vsm.action.smaps import Smaps
from vsm.action.socket_stat import SocketStat
from vsm.models import CommandFailed, Completed, MonitorError, OwnershipError
from vsm.processing import Params
from vsm.processor.proc_stat import CpuProcessor
from vsm.runtime import Context, Run


class ControlledProcess:
    def __init__(self, output):
        self.output = output
        self.finished = asyncio.Event()
        self.result = Completed(exit_code=0)
        self.stops = 0

    def finish(self, code=0, terminal='{"type":"end"}\n'):
        with self.output.open("a") as stream:
            stream.write(terminal)
        self.result = Completed(exit_code=code)
        self.finished.set()

    async def wait(self, *, check=True):
        await self.finished.wait()
        if check and self.result.exit_code:
            raise CommandFailed(self.result)
        return self.result

    async def stop(self, *, grace=5):
        self.stops += 1
        if not self.finished.is_set():
            self.finish()
        return self.result


class ControlledBackend(FakeConnection):
    def __init__(self, *, early=False, ready=True):
        super().__init__()
        self.early, self.ready = early, ready
        self.process = None

    @asynccontextmanager
    async def connect(self):
        yield self

    @asynccontextmanager
    async def launch(self, command, *, stdout=None, stderr=None):
        schema = "pspider" if Path(command.argv[0]).name == "pspider" else "rfpoll"
        stdout.path.write_text(json.dumps({"type": "metadata", "schema": schema}) + "\n")
        if self.ready:
            with stdout.path.open("a") as stream:
                stream.write('{"type":"ready"}\n')
        self.process = ControlledProcess(stdout.path)
        if self.early:
            self.process.finish()
        try:
            yield self.process
        finally:
            await self.process.stop()


class FixtureBinaries:
    async def resolve(self, connection, name):
        return connection.path("/bin/" + name)


class CollectorCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.workers = []

        # Retain handles as observation barriers, without replacing collector supervision.
        def supervise(ctx, coroutine):
            self.workers.append(ctx.device.run.spawn(coroutine))

        patcher = patch.object(Context, "supervise", supervise)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def exercise(
        self,
        implementation,
        *,
        duration=0.1,
        early=False,
        ready=True,
        code=0,
        terminal='{"type":"end"}\n',
    ):
        backend = ControlledBackend(early=early, ready=ready)
        self.backend = backend
        self.output = self.root / str(len(list(self.root.iterdir())))
        async with asyncio.timeout(7), Run(self.output, binaries=FixtureBinaries()) as run:
            device = await run.connect(backend)
            kwargs = {"duration": duration}
            if implementation in (PidStat, ProcessIo, Smaps):
                kwargs["processes"] = await backend.discover_processes()
            if implementation is PollFiles:
                kwargs["paths"] = ("/proc/123/stat",)
            async with device.use(implementation(**kwargs)) as capture:
                self.capture = capture
                unrelated = run.spawn(asyncio.sleep(0, result=42))
                if not early:
                    backend.process.finish(code, terminal)
                await capture.wait()
                for worker in self.workers:
                    await worker
                self.assertEqual(await unrelated, 42)
                with self.assertRaises(OwnershipError):
                    _ = capture.log
            self.assertTrue(backend.process.finished.is_set())
        return capture

    async def test_finite_completion_keeps_run_and_scope_healthy(self):
        for implementation in (PollFiles, ProcStat, PidStat, ProcessIo, Smaps, SocketStat):
            with self.subTest(collector=implementation.__name__):
                self.workers.clear()
                capture = await self.exercise(implementation)
                self.assertTrue(capture.log.complete)
                self.assertEqual(
                    json.loads((self.output / "run.json").read_text())["status"], "finished"
                )

    async def test_completed_before_readiness_is_observed(self):
        for implementation in (PollFiles, PidStat, SocketStat):
            with self.subTest(collector=implementation.__name__):
                self.workers.clear()
                self.assertTrue((await self.exercise(implementation, early=True)).log.complete)

    async def test_successful_exit_without_readiness_is_failure(self):
        with self.assertRaises(ExceptionGroup) as caught:
            await self.exercise(ProcStat, early=True, ready=False)
        self.assertIsNotNone(caught.exception.subgroup((MonitorError, TimeoutError)))

    async def test_unexpected_open_ended_exit_is_failure(self):
        with self.assertRaises(ExceptionGroup) as caught:
            await self.exercise(ProcStat, duration=None)
        self.assertIn("collector completed", repr(caught.exception))

    async def test_nonzero_finite_exit_is_failure(self):
        with self.assertRaises(ExceptionGroup) as caught:
            await self.exercise(ProcStat, code=3)
        self.assertIn("CommandFailed", repr(caught.exception))
        self.assertFalse(self.capture.log.complete)

    async def test_missing_or_truncated_end_stays_incomplete(self):
        for terminal in ("", '{"type":"end"'):
            with self.subTest(terminal=terminal):
                self.workers.clear()
                capture = await self.exercise(ProcStat, terminal=terminal)
                self.assertFalse(capture.log.complete)
                with self.assertRaisesRegex(ValueError, "incomplete"):
                    CpuProcessor().process(capture.log, params=Params())

    async def test_read_errors_stay_strict_after_successful_finite_exit(self):
        terminal = (
            json.dumps(
                {
                    "type": "read_error",
                    "path": "/proc/stat",
                    "error": "injected read failure",
                    "time_ns": 0,
                }
            )
            + '\n{"type":"end"}\n'
        )
        capture = await self.exercise(ProcStat, terminal=terminal)
        self.assertTrue(capture.log.complete)
        with self.assertRaisesRegex(ValueError, "read errors"):
            CpuProcessor().process(capture.log, params=Params())
        (data,) = CpuProcessor().process(capture.log, params=Params(allow_read_errors=True))
        self.assertIn("injected read failure", data.warnings[0])

    async def test_waiter_cancellation_preserves_process_and_scope_cancellation_reaps(self):
        backend = ControlledBackend()
        capture = None
        entered = asyncio.Event()

        async def owner():
            nonlocal capture
            async with Run(self.root / "cancelled", binaries=FixtureBinaries()) as run:
                device = await run.connect(backend)
                async with device.use(ProcStat()) as capture:
                    waiter = asyncio.create_task(capture.wait())
                    await asyncio.sleep(0)
                    waiter.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await waiter
                    self.assertEqual(backend.process.stops, 0)
                    self.assertFalse(backend.process.finished.is_set())
                    entered.set()
                    await asyncio.Event().wait()

        async with asyncio.timeout(3):
            task = asyncio.create_task(owner())
            try:
                await entered.wait()
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertGreater(backend.process.stops, 0)
        self.assertTrue(capture.log.complete)
