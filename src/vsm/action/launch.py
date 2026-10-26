from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from vsm.action._lifecycle import OwnedScope, wait_and_finalize
from vsm.action.action import ActionContext
from vsm.connection.process import Process
from vsm.models import Completed, Exec


@dataclass(frozen=True, kw_only=True)
class Launch:
    """Launch an owned target Process, optionally deploying a local executable.

    do() waits for completion and cleanup. use() yields a process and stops/reaps
    it on scope exit; explicitly await process.wait() for natural completion.
    """

    argv: tuple[str, ...]
    executable: Path | None = None

    def open(self, ctx: ActionContext) -> AbstractAsyncContextManager[Process]:
        return OwnedScope(Launch._open(self, ctx))

    async def run(self, ctx: ActionContext) -> Completed:
        _, result = await wait_and_finalize(Launch._open(self, ctx))
        return result

    @asynccontextmanager
    async def _open(self, ctx: ActionContext) -> AsyncIterator[Process]:
        argv = self.argv
        if self.executable is not None:
            executable = await ctx.deploy(self.executable)
            argv = (str(executable), *self.argv)
        async with ctx.connection.launch(Exec(argv=argv)) as process:
            yield process
