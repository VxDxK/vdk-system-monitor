from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import ClassVar

from vsm._cleanup import cleanup
from vsm.action._collector import (
    await_ready,
    cancel_task,
    finalize_capture,
    output_ready,
    watch_exit,
)
from vsm.action._lifecycle import OwnedScope, completed_log
from vsm.action.action import ActionContext
from vsm.action.capture import Capture
from vsm.models import Exec, FileOutput, LogRef, Schema
from vsm.util.records import TextRecord


@dataclass(frozen=True, kw_only=True)
class CommandCollector:
    """Capture an installed target executable's stdout and stderr as a scoped log.

    The executable must be available through the connection and its permissions
    must permit the requested operation. Raw profiles and traces may have no
    processor. capture.log is available only after resource finalization.
    do() waits for completion and returns a finalized log. use() yields a capture
    and stops/finalizes on scope exit; explicitly await capture.wait() inside the
    scope for natural completion. Open-ended commands wait until cancelled in do().
    """

    #: Arguments passed to the target executable; see that tool's --help.
    args: tuple[str, ...] = ()
    executable_name: ClassVar[str] = ""
    schema_name: ClassVar[str] = "text"

    def open(self, ctx: ActionContext) -> AbstractAsyncContextManager[Capture[TextRecord]]:
        return OwnedScope(CommandCollector._open(self, ctx))

    async def run(self, ctx: ActionContext) -> LogRef[TextRecord]:
        return await completed_log(CommandCollector._open(self, ctx))

    @asynccontextmanager
    async def _open(self, ctx: ActionContext) -> AsyncIterator[Capture[TextRecord]]:
        binary = await ctx.connection.executable(self.executable_name)
        output = ctx.output_path("data.log")
        errors = ctx.output_path("stderr.log")
        command = Exec(argv=(str(binary), *self.args))
        capture: Capture[TextRecord] = Capture()
        stopping = asyncio.Event()
        async with ctx.connection.launch(
            command, stdout=FileOutput(output), stderr=FileOutput(errors)
        ) as proc:
            capture.attach(proc)
            wait = asyncio.create_task(proc.wait(check=False))
            complete = False
            try:
                await await_ready(
                    output_ready(output),
                    wait,
                    allow_exit=True,
                )
                ctx.supervise(watch_exit(wait, stopping, allow_exit=True))
                yield capture
            finally:
                stopping.set()
                try:
                    result = await cleanup(proc.stop())
                    await cleanup(wait)
                    complete = result.exit_code in (0, -15, 143)
                finally:
                    try:
                        await cancel_task(wait)
                    finally:
                        await finalize_capture(
                            capture,
                            ctx,
                            output,
                            Schema[TextRecord](name=self.schema_name),
                            complete=complete,
                            interval_ms=None,
                        )
