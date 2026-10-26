from __future__ import annotations

import asyncio
from collections.abc import Awaitable


async def cleanup[T](awaitable: Awaitable[T]) -> T:
    """Retain and join asynchronous cleanup even if its caller is cancelled again."""
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while not task.done():
        try:
            # wait() does not cancel owned work when its waiter is cancelled.
            # Unlike shield(), it also does not install a Python 3.14 callback
            # which logs a later failure while we are still joining that task.
            await asyncio.wait((task,))
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result
