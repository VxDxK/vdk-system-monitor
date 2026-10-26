from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import ClassVar

from vsm.action.action import ActionContext
from vsm.action.set_file import SetFile
from vsm.models import TargetPath


@dataclass(frozen=True, kw_only=True)
class SocketBufferLimit:
    """Temporarily change Linux socket buffer limits and restore them on exit.

    Requires writable Linux sysctls. Claims coordinate within one Run; permanent
    transport loss can leave cleanup unknown.
    """

    bytes: int
    claim: ClassVar[str] = "socket-buffers"

    async def claims(self, ctx: ActionContext) -> tuple[TargetPath, ...]:
        return tuple(
            ctx.connection.path(f"/proc/sys/net/core/{name}") for name in ("rmem_max", "wmem_max")
        )

    def open(self, ctx: ActionContext) -> AbstractAsyncContextManager[None]:
        return SocketBufferLimit._open(self, ctx)

    @asynccontextmanager
    async def _open(self, ctx: ActionContext) -> AsyncIterator[None]:
        if self.bytes <= 0:
            raise ValueError("buffer limit must be positive")
        async with AsyncExitStack() as stack:
            for name in ("rmem_max", "wmem_max"):
                await stack.enter_async_context(
                    SetFile(
                        path=ctx.connection.path(f"/proc/sys/net/core/{name}"),
                        value=str(self.bytes).encode(),
                    ).open(ctx)
                )
            yield None
