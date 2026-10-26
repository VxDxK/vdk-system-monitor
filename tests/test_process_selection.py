"""Process collectors share identity checks before launching any target work."""

import tempfile
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from support import FakeConnection, TestContext, stat

from vsm.action.pid_stat import PidStat
from vsm.action.process_io import ProcessIo
from vsm.action.scan_processes import ScanProcesses
from vsm.action.smaps import Smaps
from vsm.models import CapabilityUnavailable, DeviceId, Exec, OwnershipError, ProcessSet

COLLECTORS = (PidStat, ProcessIo, Smaps)


class ProcessSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.connection = FakeConnection()
        self.selection = await ScanProcesses().run(self.context())

    def context(self, connection=None):
        return TestContext(connection or self.connection, self.temp.name)

    async def assert_rejected(self, selection, error):
        for collector in COLLECTORS:
            with self.subTest(collector=collector.__name__):
                self.connection.commands.clear()
                with self.assertRaises(error):
                    async with collector(processes=selection).open(self.context()):
                        self.fail("invalid selection entered resource scope")
                self.assertEqual(self.connection.commands, [])

    async def test_empty_and_foreign_selections(self):
        await self.assert_rejected(ProcessSet(), CapabilityUnavailable)
        process = self.selection[0]
        await self.assert_rejected(
            ProcessSet((replace(process, device_id=DeviceId("another-device")),)), OwnershipError
        )
        await self.assert_rejected(
            ProcessSet((replace(process, boot_id="another-boot"),)), OwnershipError
        )

    async def test_reused_and_exited_processes(self):
        self.connection.files["/proc/123/stat"] = stat(start=100).encode()
        await self.assert_rejected(self.selection, OwnershipError)
        del self.connection.files["/proc/123/stat"]
        await self.assert_rejected(self.selection, FileNotFoundError)

    async def test_read_failures_propagate(self):
        with patch.object(self.connection, "read_bytes", AsyncMock(side_effect=PermissionError)):
            await self.assert_rejected(self.selection, PermissionError)

    async def test_same_device_connection_and_sliced_selection(self):
        second = FakeConnection()
        for collector in COLLECTORS:
            with self.subTest(collector=collector.__name__):
                ctx = self.context(second)
                async with collector(processes=self.selection[:1]).open(ctx) as capture:
                    pass
                await ctx.join()
                self.assertTrue(capture.log.complete)
                self.assertFalse(hasattr(capture.log.schema, "version"))
        commands = [command.argv for command in second.commands if isinstance(command, Exec)]
        self.assertTrue(
            all(command[command.index("--process") + 1] == "123:99" for command in commands)
        )
        self.assertTrue(
            all(command[command.index("--boot-id") + 1] == "fixture-boot" for command in commands)
        )
        self.assertTrue(any("/proc/123/smaps_rollup" in command for command in commands))

    async def test_unknown_boot_ids_do_not_prevent_selection(self):
        for selection, connection in (
            (ProcessSet((replace(self.selection[0], boot_id=None),)), self.connection),
            (self.selection, FakeConnection()),
        ):
            if connection is not self.connection:
                connection._target = replace(connection.target, boot_id=None)
            for collector in COLLECTORS:
                with self.subTest(collector=collector.__name__):
                    ctx = self.context(connection)
                    async with collector(processes=selection).open(ctx):
                        pass
                    await ctx.join()

    async def test_smaps_removes_pid_only_configuration(self):
        with self.assertRaises(TypeError):
            Smaps(pids=(123,))
