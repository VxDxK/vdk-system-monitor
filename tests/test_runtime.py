import asyncio
import json
import os
import sys
import tempfile
import tomllib
import unittest
from importlib.metadata import version
from pathlib import Path

from vsm._cleanup import cleanup
from vsm.action.set_file import SetFile
from vsm.action.sleep import Sleep
from vsm.connection._command import shell_command
from vsm.connection.host import Host
from vsm.models import (
    CaptureOutput,
    CommandFailed,
    DeviceId,
    EnvVar,
    Exec,
    OutputLimitExceeded,
    OwnershipError,
    Shell,
    TargetPath,
)
from vsm.runtime import Run, TaskScope


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_records_package_version_on_all_exit_paths(self):
        expected = tomllib.loads(
            (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
        )["project"]["version"]
        self.assertEqual(version("vsm"), expected)
        for error in (None, RuntimeError("work failed"), asyncio.CancelledError()):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "run"
                if error is None:
                    async with Run(output):
                        pass
                else:
                    with self.assertRaises(
                        asyncio.CancelledError
                        if isinstance(error, asyncio.CancelledError)
                        else ExceptionGroup
                    ) as raised:
                        async with Run(output):
                            raise error
                    if isinstance(raised.exception, ExceptionGroup):
                        self.assertEqual(raised.exception.exceptions, (error,))
                summary = json.loads((output / "run.json").read_text())
                self.assertEqual(summary["vsm_version"], expected)
                self.assertNotIn("version", summary)
                self.assertEqual(summary["status"], "failed" if error else "finished")

    async def test_concurrent_tasks_and_result_types(self):
        first, second, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def task(ready, value):
            ready.set()
            await release.wait()
            return value

        async with TaskScope() as scope:
            a = scope.spawn(task(first, 1))
            b = scope.spawn(task(second, "two"))
            await first.wait()
            await second.wait()
            self.assertFalse(a.done)
            self.assertFalse(b.done)
            release.set()
            self.assertEqual(await a, 1)
            self.assertEqual(await b, "two")

    async def test_failure_cancels_sibling_and_waits_cleanup(self):
        started, finished = asyncio.Event(), asyncio.Event()

        async def sibling():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                finished.set()

        async def failure():
            await started.wait()
            raise ValueError("broken")

        with self.assertRaises(ExceptionGroup):
            async with TaskScope() as scope:
                scope.spawn(sibling())
                scope.spawn(failure())
        self.assertTrue(finished.is_set())

    async def test_cancel_and_wait(self):
        ended = asyncio.Event()
        started = asyncio.Event()

        async def work():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                ended.set()

        async with TaskScope() as scope:
            h = scope.spawn(work())
            await started.wait()
            await h.cancel_and_wait()
            self.assertTrue(ended.is_set())

    async def test_closed_scope_rejects_work(self):
        scope = TaskScope()
        with self.assertRaises(OwnershipError):
            scope.spawn(asyncio.sleep(0))

    async def test_host_argv_env_and_exit(self):
        async with Host().connect() as connection:
            result = await connection.run(
                Exec(
                    argv=(
                        sys.executable,
                        "-c",
                        "import os,sys;print(repr(sys.argv[1:]));print(os.environ['VDK_TEST'])",
                        "",
                        "a b",
                        "$(echo injected)",
                    ),
                    env=(EnvVar("VDK_TEST", "value"),),
                )
            )
            self.assertIn(b"['', 'a b', '$(echo injected)']", result.stdout)
            self.assertIn(b"value", result.stdout)
            with self.assertRaises(CommandFailed):
                await connection.run(Exec(argv=("/bin/sh", "-c", "exit 7")))
            self.assertEqual(
                (
                    await connection.run(Exec(argv=("/bin/sh", "-c", "exit 7")), check=False)
                ).exit_code,
                7,
            )

    async def test_large_stderr_drain(self):
        async with Host().connect() as conn:
            result = await conn.run(
                Exec(argv=(sys.executable, "-c", "import sys;sys.stderr.write('x'*200000)"))
            )
            self.assertEqual(len(result.stderr), 200000)

    async def test_capture_limit_kills_process(self):
        async with Host().connect() as conn:
            with self.assertRaises(ExceptionGroup) as caught:
                await conn.run(
                    Exec(argv=(sys.executable, "-c", "print('x'*10000)")),
                    stdout=CaptureOutput(limit_bytes=100),
                )
            self.assertIsInstance(caught.exception.exceptions[0], OutputLimitExceeded)

    async def test_timeout_reaps_process(self):
        async with Host().connect() as conn:
            async with conn.launch(Exec(argv=("/bin/sleep", "20"))) as process:
                pid = process.target_pid
                with self.assertRaises(TimeoutError):
                    async with asyncio.timeout(0.02):
                        await process.wait()
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    async def test_wait_cancel_does_not_kill_process(self):
        async with (
            Host().connect() as conn,
            conn.launch(Exec(argv=("/bin/sleep", "20"))) as process,
        ):
            waiter = asyncio.create_task(process.wait())
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            os.kill(process.target_pid, 0)

    async def test_file_copy_and_path_ownership(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "a"
            source.write_bytes(b"bytes")
            async with Host().connect() as conn:
                await conn.push(source, conn.path(str(root / "b")))
                self.assertEqual(await conn.read_bytes(conn.path(str(root / "b"))), b"bytes")
                with self.assertRaises(FileExistsError):
                    await conn.push(source, conn.path(str(root / "b")))
                await conn.pull(conn.path(str(root / "b")), root / "c")
                with self.assertRaises(OwnershipError):
                    await conn.read_bytes(
                        TargetPath(
                            DeviceId("fixture-device"),
                            __import__("pathlib").PurePosixPath("/a"),
                        )
                    )
            with self.assertRaises(OwnershipError):
                await conn.read_bytes(conn.path(str(root / "b")))

    async def test_run_manifest_and_conflicting_claim(self):
        with tempfile.TemporaryDirectory() as d:
            setting = Path(d) / "setting"
            setting.write_bytes(b"old")
            async with Run(Path(d) / "run") as run:
                device = await run.connect(Host())
                async with device.use(
                    SetFile(path=device.connection.path(str(setting)), value=b"new")
                ):
                    self.assertEqual(setting.read_bytes(), b"new")
                    with self.assertRaises(OwnershipError):
                        async with device.use(
                            SetFile(path=device.connection.path(str(setting)), value=b"other")
                        ):
                            pass
                await device.do(Sleep(duration=0))
            self.assertEqual(setting.read_bytes(), b"old")
            manifest = json.loads((Path(d) / "run/run.json").read_text())
            self.assertEqual(manifest["status"], "finished")
            with self.assertRaises(OwnershipError):
                await device.do(Sleep(duration=0))

    async def test_cleanup_survives_repeat_cancel(self):
        ready, finished = asyncio.Event(), asyncio.Event()

        async def resource():
            ready.set()
            await asyncio.sleep(0.03)
            finished.set()

        task = asyncio.create_task(cleanup(resource()))
        await ready.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(finished.is_set())


class CommandTests(unittest.TestCase):
    def test_invalid_inputs(self):
        for args in ((), ("",), ("a\0",)):
            with self.assertRaises(ValueError):
                Exec(argv=args)
        with self.assertRaises(ValueError):
            EnvVar("bad=name", "a")
        with self.assertRaises(ValueError):
            CaptureOutput(limit_bytes=0)

    def test_shell_encoding(self):
        self.assertEqual(
            shell_command(Exec(argv=("printf", "%s", "", "a b"))), "exec printf %s '' 'a b'"
        )
        self.assertIn(
            "env VDK='a b'", shell_command(Shell(script='echo "$VDK"', env=(EnvVar("VDK", "a b"),)))
        )
        with self.assertRaises(ValueError):
            shell_command(Exec(argv=("true",), env=(EnvVar("x;false", "1"),)))


class JoiningTests(unittest.IsolatedAsyncioTestCase):
    async def test_spawned_pipeline_can_finish_and_spawn_workers_during_normal_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            async with Run(Path(directory) / "run") as run:
                host = await run.connect(Host())

                async def pipeline():
                    await asyncio.sleep(0.01)
                    await host.do(Sleep(duration=0))
                    nested = run.spawn(asyncio.sleep(0, result=42))
                    return await nested

                handle = run.spawn(pipeline())
                # Do not join explicitly: normal run exit must retain device ownership.
            self.assertEqual(await handle, 42)

    async def test_fifo_read_rejected_without_blocking(self):
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory) / "fifo"
            os.mkfifo(fifo)
            async with Host().connect() as conn:
                from vsm.models import CapabilityUnavailable

                with self.assertRaises(CapabilityUnavailable):
                    await conn.read_bytes(conn.path(str(fifo)))


class MixedClaimsTests(unittest.IsolatedAsyncioTestCase):
    async def test_governor_and_direct_setting_conflict(self):
        from vsm.action.cpu_freq_governor import CpuFreqGovernor

        with tempfile.TemporaryDirectory() as directory:
            setting = Path(directory) / "governor"
            setting.write_bytes(b"old")
            async with Run(Path(directory) / "run") as run:
                host = await run.connect(Host())
                async with host.use(CpuFreqGovernor(governor="new", paths=(str(setting),))):
                    with self.assertRaises(OwnershipError):
                        async with host.use(
                            SetFile(path=host.connection.path(str(setting)), value=b"other")
                        ):
                            pass
                self.assertEqual(setting.read_bytes(), b"old")
