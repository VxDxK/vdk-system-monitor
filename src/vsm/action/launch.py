from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from vsm.action._launch_mode import LaunchScope, validate_mode
from vsm.action.action import ActionContext
from vsm.action.launch_mode import LaunchMode
from vsm.connection.process import Process
from vsm.models import Completed, Exec


@dataclass(frozen=True, kw_only=True)
class Launch:
    """Launch an owned target Process, optionally deploying a local executable."""

    argv: tuple[str, ...]
    executable: Path | None = None

    #: FOREGROUND uses do(); background modes use a resource scope (default stop on exit).
    mode: LaunchMode = LaunchMode.BACKGROUND_STOP

    def open(self, ctx: ActionContext) -> AbstractAsyncContextManager[Process]:
        return LaunchScope(self.mode, Launch._open(self, ctx))

    async def run(self, ctx: ActionContext) -> Completed:
        validate_mode(self.mode, foreground=True)
        async with LaunchScope(LaunchMode.BACKGROUND_WAIT, Launch._open(self, ctx)) as process:
            result = await process.wait()
        return result

    @asynccontextmanager
    async def _open(self, ctx: ActionContext) -> AsyncIterator[Process]:
        argv = self.argv
        if self.executable is not None:
            executable = await ctx.deploy(self.executable)
            argv = (str(executable), *self.argv)
        async with ctx.connection.launch(Exec(argv=argv)) as process:
            yield process
