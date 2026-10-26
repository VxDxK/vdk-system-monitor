"""Cleanup joins owned work through cancellation without spurious loop errors."""

import asyncio
import unittest

from vsm._cleanup import cleanup
from vsm.models import CleanupUnknown


class CleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_failure_after_repeated_cancellation_is_reported_once(self):
        started, release = asyncio.Event(), asyncio.Event()
        failure = CleanupUnknown("owned cleanup failed")
        contexts = []
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _, context: contexts.append(context))

        async def resource():
            started.set()
            await release.wait()
            raise failure

        task = asyncio.create_task(cleanup(resource()))
        try:
            await started.wait()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(CleanupUnknown) as raised:
                await task
            self.assertIs(raised.exception, failure)
            await asyncio.sleep(0)
            self.assertEqual(contexts, [])
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            loop.set_exception_handler(previous_handler)

    async def test_cancelled_owned_task_is_joined(self):
        owned = asyncio.create_task(asyncio.sleep(10))
        owned.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cleanup(owned)
        self.assertTrue(owned.done())
