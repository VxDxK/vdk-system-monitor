from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass

from vsm._cleanup import cleanup
from vsm.action._collector import (
    await_ready,
    cancel_task,
    finalize_capture,
    last_record,
    native_ready,
    watch_exit,
)
from vsm.action._duration import duration_arguments, validate_duration
from vsm.action._interval import validate_interval
from vsm.action.action import ActionContext
from vsm.action.capture import Capture
from vsm.models import Exec, FileOutput, Schema
from vsm.util.records import SocketRecord


@dataclass(frozen=True, kw_only=True)
class SocketStat:
    """Sample Linux TCP/UDP sockets through native inet_diag."""

    #: Sampling interval in positive integer milliseconds.
    interval: int = 250
    #: Optional duration in seconds; None collects until scope exit.
    duration: float | None = None

    def __post_init__(self) -> None:
        validate_interval(self.interval)
        validate_duration(self.duration)

    def open(self, ctx: ActionContext) -> AbstractAsyncContextManager[Capture[SocketRecord]]:
        return SocketStat._open(self, ctx)

    @asynccontextmanager
    async def _open(self, ctx: ActionContext) -> AsyncIterator[Capture[SocketRecord]]:
        tool = await ctx.binary("socket-stat")
        output = ctx.output_path("data.log")
        errors = ctx.output_path("stderr.log")
        command = Exec(
            argv=(
                str(tool),
                "--interval-ms",
                str(self.interval),
                *duration_arguments(self.duration),
            )
        )
        c: Capture[SocketRecord] = Capture()
        stopping = asyncio.Event()
        async with ctx.connection.launch(
            command, stdout=FileOutput(output), stderr=FileOutput(errors)
        ) as proc:
            c.attach(proc)
            wait = asyncio.create_task(proc.wait(check=False))
            complete = False
            try:
                await await_ready(native_ready(output), wait, allow_exit=self.duration is not None)
                ctx.supervise(watch_exit(wait, stopping, allow_exit=self.duration is not None))
                yield c
            finally:
                stopping.set()
                try:
                    result = await cleanup(proc.stop())
                    await cleanup(wait)
                    complete = result.exit_code == 0 and last_record(output).get("type") == "end"
                finally:
                    try:
                        await cancel_task(wait)
                    finally:
                        await finalize_capture(
                            c,
                            ctx,
                            output,
                            Schema[SocketRecord](name="socket_stat"),
                            complete=complete,
                            interval_ms=self.interval,
                        )
