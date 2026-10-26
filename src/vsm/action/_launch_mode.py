"""Keep natural completion cancellable and finalization cancellation resistant."""

from contextlib import AbstractAsyncContextManager
from types import TracebackType
from typing import Protocol

from vsm._cleanup import cleanup
from vsm.action.capture import Capture
from vsm.action.launch_mode import LaunchMode
from vsm.models import Completed, LogRef


class Waitable(Protocol):
    async def wait(self) -> Completed: ...


def validate_mode(mode: LaunchMode, *, foreground: bool) -> None:
    if not isinstance(mode, LaunchMode):
        raise TypeError("typed LaunchMode required")
    if (mode is LaunchMode.FOREGROUND) != foreground:
        raise ValueError("FOREGROUND requires device.do(); background modes require device.use()")


class LaunchScope[T: Waitable](AbstractAsyncContextManager[T]):
    """Runtime prepares normal exit before shielding the underlying cleanup.

    Direct open() callers receive the same behavior through __aexit__.
    """

    def __init__(self, mode: LaunchMode, manager: AbstractAsyncContextManager[T]):
        validate_mode(mode, foreground=False)
        self.mode, self.manager = mode, manager
        self.resource: T
        self.prepared = False

    async def __aenter__(self) -> T:
        self.resource = await self.manager.__aenter__()
        return self.resource

    async def wait_before_exit(self) -> None:
        if not self.prepared and self.mode is LaunchMode.BACKGROUND_WAIT:
            await self.resource.wait()
        self.prepared = True

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        if exc_type is None:
            try:
                await self.wait_before_exit()
            except BaseException as primary:
                try:
                    await cleanup(
                        self.manager.__aexit__(type(primary), primary, primary.__traceback__)
                    )
                except BaseException as secondary:  # noqa: BLE001 -- Preserve the primary failure through cleanup.
                    primary.add_note(f"cleanup failed: {secondary}")
                raise
        return await cleanup(self.manager.__aexit__(exc_type, exc, tb))


async def foreground_log[R](
    mode: LaunchMode, manager: AbstractAsyncContextManager[Capture[R]]
) -> LogRef[R]:
    validate_mode(mode, foreground=True)
    async with LaunchScope(LaunchMode.BACKGROUND_WAIT, manager) as capture:
        pass
    return capture.log
