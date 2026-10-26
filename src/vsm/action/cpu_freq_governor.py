from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import ClassVar

from vsm.action.action import ActionContext
from vsm.action.set_file import SetFile
from vsm.models import CapabilityUnavailable, TargetPath


@dataclass(frozen=True, kw_only=True)
class CpuFreqGovernor:
    """Temporarily set CPU governors and restore previous values on scope exit.

    Requires readable/writable scaling_governor files. Claims coordinate within
    one Run; external writers, aliases and independent runs are not covered.
    """

    governor: str
    paths: tuple[str, ...] = ("/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor",)
    claim: ClassVar[str] = "cpu-governors"

    async def claims(self, ctx: ActionContext) -> tuple[TargetPath, ...]:
        return tuple([p for pattern in self.paths for p in await ctx.connection.glob(pattern)])

    def open(self, ctx: ActionContext) -> AbstractAsyncContextManager[None]:
        return CpuFreqGovernor._open(self, ctx)

    @asynccontextmanager
    async def _open(self, ctx: ActionContext) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            found = tuple([p for pattern in self.paths for p in await ctx.connection.glob(pattern)])
            if not found:
                raise CapabilityUnavailable("CPU governor files unavailable")
            for p in found:
                await stack.enter_async_context(
                    SetFile(path=p, value=self.governor.encode()).open(ctx)
                )
            yield None
