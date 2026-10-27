from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
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
from vsm.action.set_file import SetFile
from vsm.models import Exec, FileOutput, LogRef, Schema, TargetPath
from vsm.util.records import TextRecord


@dataclass(frozen=True, kw_only=True)
class Ftrace:
    """Configure scoped kernel tracing and retain a raw trace artifact.

    use() stops/finalizes on exit; do() waits for natural completion or caller
    cancellation. The trace reader normally runs until its scope exits.
    """

    events: tuple[str, ...] = ()
    directory: str = "/sys/kernel/tracing"
    claim: ClassVar[str] = "ftrace"

    async def claims(self, ctx: ActionContext) -> tuple[TargetPath, ...]:
        return (
            ctx.connection.path(f"{self.directory}/tracing_on"),
            ctx.connection.path(f"{self.directory}/trace_pipe"),
            *(
                ctx.connection.path(f"{self.directory}/events/{event}/enable")
                for event in self.events
            ),
        )

    def open(self, ctx: ActionContext) -> AbstractAsyncContextManager[Capture[TextRecord]]:
        return OwnedScope(Ftrace._open(self, ctx))

    async def run(self, ctx: ActionContext) -> LogRef[TextRecord]:
        return await completed_log(Ftrace._open(self, ctx))

    @asynccontextmanager
    async def _open(self, ctx: ActionContext) -> AsyncIterator[Capture[TextRecord]]:
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(
                SetFile(path=ctx.connection.path(f"{self.directory}/tracing_on"), value=b"0").open(
                    ctx
                )
            )
            for event in self.events:
                if not re.fullmatch(r"[a-zA-Z0-9_]+/[a-zA-Z0-9_]+", event):
                    raise ValueError("invalid trace event")
                await stack.enter_async_context(
                    SetFile(
                        path=ctx.connection.path(f"{self.directory}/events/{event}/enable"),
                        value=b"1",
                    ).open(ctx)
                )
            await ctx.connection.write_bytes(
                ctx.connection.path(f"{self.directory}/tracing_on"), b"1"
            )
            cat = await ctx.connection.executable("cat")
            output = ctx.output_path("data.log")
            errors = ctx.output_path("stderr.log")
            command = Exec(argv=(str(cat), f"{self.directory}/trace_pipe"))
            c: Capture[TextRecord] = Capture()
            stopping = asyncio.Event()
            async with ctx.connection.launch(
                command, stdout=FileOutput(output), stderr=FileOutput(errors)
            ) as proc:
                c.attach(proc)
                wait = asyncio.create_task(proc.wait(check=False))
                complete = False
                try:
                    await await_ready(
                        output_ready(output),
                        wait,
                        allow_exit=True,
                    )
                    ctx.supervise(watch_exit(wait, stopping, allow_exit=True))
                    yield c
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
                                c,
                                ctx,
                                output,
                                Schema[TextRecord](name="ftrace"),
                                complete=complete,
                                interval_ms=None,
                            )
