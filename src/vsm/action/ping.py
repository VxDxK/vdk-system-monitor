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
    log_ref,
    output_ready,
    watch_exit,
)
from vsm.action._launch_mode import LaunchScope, foreground_log
from vsm.action.action import ActionContext
from vsm.action.capture import Capture
from vsm.action.command_collector import CommandCollector
from vsm.action.launch_mode import LaunchMode
from vsm.models import Exec, FileOutput, LogRef, Schema
from vsm.util.records import TextRecord


@dataclass(frozen=True, kw_only=True)
class Ping(CommandCollector):
    """Collect latency measurements using the target ping executable."""

    destination: str
    executable_name: ClassVar[str] = "ping"
    schema_name: ClassVar[str] = "ping"

    def open(self, ctx: ActionContext) -> AbstractAsyncContextManager[Capture[TextRecord]]:
        return LaunchScope(self.mode, Ping._open(self, ctx))

    async def run(self, ctx: ActionContext) -> LogRef[TextRecord]:
        return await foreground_log(self.mode, Ping._open(self, ctx))

    @asynccontextmanager
    async def _open(self, ctx: ActionContext) -> AsyncIterator[Capture[TextRecord]]:
        binary = await ctx.connection.executable("ping")
        output = ctx.output_path("data.log")
        errors = ctx.output_path("stderr.log")
        command = Exec(argv=(str(binary), "-n", self.destination, *self.args))
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
                    allow_exit=self.mode is not LaunchMode.BACKGROUND_STOP,
                )
                ctx.supervise(
                    watch_exit(
                        wait, stopping, allow_exit=self.mode is not LaunchMode.BACKGROUND_STOP
                    )
                )
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
                        c.finalize(
                            log_ref(
                                ctx,
                                output,
                                Schema[TextRecord](name="ping"),
                                complete=complete,
                                interval_ms=None,
                            )
                        )
