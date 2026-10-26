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
from vsm.action.command_collector import CommandCollector
from vsm.models import Exec, FileOutput, LogRef, Schema
from vsm.util.records import TextRecord


@dataclass(frozen=True, kw_only=True)
class Fio(CommandCollector):
    """Run the target fio benchmark and retain JSON output for FioProcessor."""

    args: tuple[str, ...] = ()
    executable_name: ClassVar[str] = "fio"
    schema_name: ClassVar[str] = "fio"

    def open(self, ctx: ActionContext) -> AbstractAsyncContextManager[Capture[TextRecord]]:
        return OwnedScope(Fio._open(self, ctx))

    async def run(self, ctx: ActionContext) -> LogRef[TextRecord]:
        return await completed_log(Fio._open(self, ctx))

    @asynccontextmanager
    async def _open(self, ctx: ActionContext) -> AsyncIterator[Capture[TextRecord]]:
        if not self.args:
            raise ValueError("fio workload arguments required")
        binary = await ctx.connection.executable("fio")
        output = ctx.output_path("data.log")
        errors = ctx.output_path("stderr.log")
        command = Exec(argv=(str(binary), *self.args, "--output-format=json"))
        c: Capture[TextRecord] = Capture()
        stopping = asyncio.Event()
        async with ctx.connection.launch(
            command, stdout=FileOutput(output), stderr=FileOutput(errors)
        ) as proc:
            c.attach(proc)
            wait = asyncio.create_task(proc.wait(check=False))
            complete = False
            try:
                await await_ready(output_ready(output), wait, allow_exit=True)
                ctx.supervise(watch_exit(wait, stopping, allow_exit=True))
                yield c
            finally:
                stopping.set()
                try:
                    result = await cleanup(proc.stop())
                    await cleanup(wait)
                    complete = result.exit_code == 0
                finally:
                    try:
                        await cancel_task(wait)
                    finally:
                        await finalize_capture(
                            c,
                            ctx,
                            output,
                            Schema[TextRecord](name="fio"),
                            complete=complete,
                            interval_ms=None,
                        )
