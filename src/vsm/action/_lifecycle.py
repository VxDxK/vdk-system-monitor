"""Keep natural completion cancellable and owned resource exit cancellation resistant."""

from contextlib import AbstractAsyncContextManager
from types import TracebackType
from typing import Protocol

from vsm._cleanup import cleanup
from vsm.action.capture import Capture
from vsm.models import Completed, LogRef


class Waitable(Protocol):
    async def wait(self) -> Completed: ...


class OwnedScope[T](AbstractAsyncContextManager[T]):
    """Join resource cleanup, including for callers using action.open() directly."""

    def __init__(self, manager: AbstractAsyncContextManager[T]):
        self.manager = manager

    async def __aenter__(self) -> T:
        return await self.manager.__aenter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        return await cleanup(self.manager.__aexit__(exc_type, exc, tb))


async def wait_and_finalize[T: Waitable](
    manager: AbstractAsyncContextManager[T],
) -> tuple[T, Completed]:
    scope = OwnedScope(manager)
    resource = await scope.__aenter__()
    try:
        result = await resource.wait()
    except BaseException as primary:
        try:
            await scope.__aexit__(type(primary), primary, primary.__traceback__)
        except BaseException as secondary:  # noqa: BLE001 -- Preserve the wait failure through cleanup.
            primary.add_note(f"cleanup failed: {secondary}")
        raise
    else:
        await scope.__aexit__(None, None, None)
    return resource, result


async def completed_log[R](manager: AbstractAsyncContextManager[Capture[R]]) -> LogRef[R]:
    capture, _ = await wait_and_finalize(manager)
    return capture.log
