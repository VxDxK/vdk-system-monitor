"""Behavioral regressions for identities, workspace ownership, and collector lifecycles."""

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from support import FakeConnection, FakeProcess, TestContext

from vsm.action.fio import Fio
from vsm.action.pid_stat import PidStat
from vsm.action.poll_files import PollFiles
from vsm.action.scan_processes import ScanProcesses
from vsm.action.set_file import SetFile
from vsm.binary import BinaryProvider, deploy
from vsm.connection.host import Host
from vsm.models import (
    ActionId,
    CapabilityUnavailable,
    Completed,
    DeviceId,
    Exec,
    MonitorError,
    OwnershipError,
    ProcessSet,
)
from vsm.runtime import Context, Run

ROOT = Path(__file__).resolve().parents[1]


class IdentityTests(unittest.TestCase):
    def test_identifiers_are_strings_and_imports_are_lazy(self):
        for cls in (DeviceId, ActionId):
            self.assertEqual(cls("phone serial").value, "phone serial")
            for invalid in ("", "\0", 123):
                with self.assertRaises(ValueError):
                    cls(invalid)
        code = "import sys; import vsm, vsm.runtime; assert 'vsm.action.registry' not in sys.modules; assert 'vsm.processor.registry' not in sys.modules; assert 'pandas' not in sys.modules; assert not hasattr(vsm, 'Run')"
        subprocess.run(
            [sys.executable, "-c", code],
            check=True,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
            timeout=10,
        )


class WorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_device_connections_have_independent_workspaces_and_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            async with Run(Path(directory) / "run") as run:
                first, second = await asyncio.gather(run.connect(Host()), run.connect(Host()))
                self.assertEqual(first.id, second.id)
                self.assertNotEqual(first.output_name, second.output_name)
                self.assertNotEqual(first.connection.tmp_dir, second.connection.tmp_dir)
                first_path, second_path = (
                    Path(first.connection.tmp_dir.value),
                    Path(second.connection.tmp_dir.value),
                )
                self.assertEqual(first_path.stat().st_mode & 0o777, 0o700)
                self.assertTrue(first_path.name.startswith("vsm-tmp-data-"))
                target = first.connection.path(str(first_path / "shared"))
                await first.connection.write_bytes(target, b"same device")
                self.assertEqual(await second.connection.read_bytes(target), b"same device")
                a = Context(first, ActionId("action-1")).output_path("data.log")
                b = Context(second, ActionId("action-1")).output_path("data.log")
                self.assertNotEqual(a, b)
                # Claims describe the device state and therefore span its connections.
                async with first.use(SetFile(path=target, value=b"changed")):
                    with self.assertRaises(OwnershipError):
                        async with second.use(SetFile(path=target, value=b"conflict")):
                            pass
                self.assertEqual(await second.connection.read_bytes(target), b"same device")
            self.assertFalse(first_path.exists())
            self.assertFalse(second_path.exists())
            manifest = json.loads((Path(directory) / "run/run.json").read_text())
            self.assertEqual(len(manifest["devices"]), 2)
            self.assertEqual(manifest["devices"][0]["id"], manifest["devices"][1]["id"])

    async def test_closing_one_connection_does_not_stop_the_other(self):
        async with Host().connect() as second:
            second_path = Path(second.tmp_dir.value)
            async with second.launch(Exec(argv=("sleep", "20"))) as process:
                async with Host().connect() as first:
                    first_path = Path(first.tmp_dir.value)
                self.assertFalse(first_path.exists())
                self.assertTrue(second_path.exists())
                os.kill(process.target_pid, 0)
        self.assertFalse(second_path.exists())

    async def test_backend_stops_owned_processes_even_if_launch_scope_outlives_it(self):
        backend = Host().connect()
        connection = await backend.__aenter__()
        launching = connection.launch(Exec(argv=("sleep", "20")))
        process = await launching.__aenter__()
        await backend.__aexit__(None, None, None)
        self.assertIsInstance(await process.wait(check=False), Completed)
        await launching.__aexit__(None, None, None)
        self.assertFalse(Path(connection.tmp_dir.value).exists())

    async def test_concurrent_deployments_are_private_and_manifest_driven(self):
        with tempfile.TemporaryDirectory() as directory:
            async with Host().connect() as first, Host().connect() as second:
                provider = BinaryProvider(ROOT / "artifacts")
                paths = await asyncio.gather(*(provider.resolve(first, "rfpoll") for _ in range(4)))
                self.assertEqual(len(set(paths)), 1)
                other = await provider.resolve(second, "rfpoll")
                self.assertNotEqual(paths[0], other)
                self.assertEqual(
                    (await first.run(Exec(argv=(str(paths[0]), "--version")))).exit_code, 0
                )
                manifest = json.loads((ROOT / "artifacts/manifest.json").read_text())
                variant = next(
                    item
                    for item in manifest["tools"]["rfpoll"]
                    if item["sha256"] + ".bin" == paths[0].name
                )
                source = ROOT / "artifacts" / variant["file"]
                custom = Path(directory) / "custom-tool.bin"
                custom.write_bytes(source.read_bytes())
                manifest["tools"] = {
                    "custom-tool": [
                        {
                            **variant,
                            "file": custom.name,
                            "sha256": hashlib.sha256(custom.read_bytes()).hexdigest(),
                        }
                    ]
                }
                (Path(directory) / "manifest.json").write_text(json.dumps(manifest))
                custom_path = await BinaryProvider(Path(directory)).resolve(first, "custom-tool")
                self.assertEqual(custom_path, paths[0])
                await first.write_bytes(custom_path, b"corrupted")
                with self.assertRaises(CapabilityUnavailable):
                    await deploy(first, custom)

    async def test_discovery_can_be_consumed_by_another_connection_to_same_device(self):
        first, second = FakeConnection(), FakeConnection()
        with tempfile.TemporaryDirectory() as directory:
            discover_ctx = TestContext(first, directory)
            selected = await ScanProcesses().run(discover_ctx)
            collector_ctx = TestContext(second, directory)
            async with PidStat(processes=selected).open(collector_ctx) as capture:
                pass
            await collector_ctx.join()
            self.assertTrue(capture.log.complete)
            self.assertIsInstance(selected, ProcessSet)

    async def test_raw_device_strings_never_become_output_paths(self):
        from contextlib import asynccontextmanager

        connection = FakeConnection()
        connection._identity = DeviceId("../../serial/with:characters")

        class Backend:
            @asynccontextmanager
            async def connect(self):
                yield connection

        with tempfile.TemporaryDirectory() as directory:
            async with Run(Path(directory) / "run") as run:
                device = await run.connect(Backend())
                path = Context(device, ActionId("action-1")).output_path("data.log")
                self.assertEqual(path, run.output / "connection-0001/action-1/data.log")

    async def test_synchronous_open_failure_releases_claims(self):
        class BrokenSetting:
            async def claims(self, ctx):
                return (ctx.connection.path("/setting"),)

            def open(self, ctx):
                raise ValueError("invalid configuration")

        from contextlib import asynccontextmanager

        connection = FakeConnection()

        class Backend:
            @asynccontextmanager
            async def connect(self):
                yield connection

        with tempfile.TemporaryDirectory() as directory:
            async with Run(Path(directory) / "run") as run:
                device = await run.connect(Backend())
                with self.assertRaises(ValueError):
                    async with device.use(BrokenSetting()):
                        pass
                self.assertFalse(run.claims)
                async with device.use(SetFile(path=connection.path("/setting"), value=b"new")):
                    pass


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ctx = TestContext(FakeConnection(), self.temp.name)

    async def test_readiness_failure_stops_and_reaps(self):
        async def fail(path):
            raise TimeoutError("not ready")

        with patch("vsm.action.poll_files.native_ready", fail), self.assertRaises(TimeoutError):
            async with PollFiles(paths=("/proc/123/stat",)).open(self.ctx):
                self.fail("entered before readiness")
        await self.ctx.join()
        self.assertTrue(self.ctx.output_path("data.log").read_bytes().rstrip().endswith(b"}"))

    async def test_unexpected_exit_is_supervised(self):
        finished = asyncio.Event()

        async def wait(process, *, check=True):
            await finished.wait()
            return Completed(exit_code=0)

        with patch.object(FakeProcess, "wait", wait):
            async with PollFiles(paths=("/proc/123/stat",)).open(self.ctx):
                finished.set()
                with self.assertRaises(MonitorError):
                    await self.ctx.join()

    async def test_finite_tool_can_finish_before_readiness(self):
        async def wait(process, *, check=True):
            return Completed(exit_code=0)

        with patch.object(FakeProcess, "wait", wait):
            async with Fio(args=("--name=test",)).open(self.ctx) as capture:
                await capture.wait()
            await self.ctx.join()
        self.assertTrue(capture.log.complete)

    async def test_forced_stop_and_missing_end_are_incomplete(self):
        async def stop(process, *, grace=5):
            process.stopped.set()
            return Completed(exit_code=-9)

        with patch.object(FakeProcess, "stop", stop):
            async with PollFiles(paths=("/proc/123/stat",)).open(self.ctx) as capture:
                pass
            await self.ctx.join()
        self.assertFalse(capture.log.complete)
