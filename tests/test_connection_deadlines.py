"""Caller deadlines cover launch and wait, and join owned cleanup before returning."""

import asyncio
import unittest
from contextlib import asynccontextmanager

from support import FakeConnection

from vsm._cleanup import cleanup
from vsm.connection.connection import BaseConnection
from vsm.models import Completed, Exec


class DeadlineConnection(BaseConnection):
    def __init__(self, phase="success"):
        fixture = FakeConnection()
        super().__init__(fixture.device_id, fixture.target, "/tmp/deadline-fixture")
        self.phase = phase
        self.started = asyncio.Event()
        self.cleanup_started = asyncio.Event()
        self.release_cleanup = asyncio.Event()
        self.alive = False

    @asynccontextmanager
    async def launch(self, command, *, stdout=None, stderr=None):
        connection = self

        class Process:
            async def wait(self, *, check=True):
                if command.argv == ("true",):
                    return Completed(exit_code=0)
                connection.started.set()
                await asyncio.Event().wait()

        async def stop():
            self.cleanup_started.set()
            await self.release_cleanup.wait()
            self.alive = False

        self.alive = True
        try:
            if self.phase == "startup":
                self.started.set()
                await asyncio.Event().wait()
            yield Process()
        finally:
            await cleanup(stop())


class ConnectionDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_removed_timeout_keyword_is_rejected(self):
        connection = DeadlineConnection()
        connection.release_cleanup.set()
        with self.assertRaises(TypeError):
            await connection.run(Exec(argv=("true",)), timeout=5)

    async def test_deadline_covers_startup_and_wait_and_joins_cleanup(self):
        for phase in ("startup", "execution"):
            with self.subTest(phase=phase):
                connection = DeadlineConnection(phase)
                deadline = None

                async def operation(connection=connection):
                    nonlocal deadline
                    async with asyncio.timeout(None) as deadline:
                        await connection.run(Exec(argv=("sleep", "30")))

                async with asyncio.timeout(3):
                    task = asyncio.create_task(operation())
                    try:
                        await connection.started.wait()
                        deadline.reschedule(asyncio.get_running_loop().time())
                        await connection.cleanup_started.wait()
                        self.assertFalse(task.done())
                        self.assertTrue(connection.alive)
                    finally:
                        connection.release_cleanup.set()
                        with self.assertRaises(TimeoutError):
                            await task
                self.assertFalse(connection.alive)
                connection.phase = "success"
                self.assertEqual((await connection.run(Exec(argv=("true",)))).exit_code, 0)
