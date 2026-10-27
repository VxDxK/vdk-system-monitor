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
    output_ready,
    tcp_ready,
    watch_exit,
)
from vsm.action._lifecycle import OwnedScope, completed_log
from vsm.action.action import ActionContext
from vsm.action.capture import Capture
from vsm.models import Exec, FileOutput, LogRef, Schema
from vsm.util.records import TextRecord


@dataclass(frozen=True, kw_only=True)
class Iperf:
    """Run an iperf3 client or server and retain JSON output for IperfProcessor.

    do() waits for completion and returns a finalized log. use() stops/finalizes
    on exit; explicitly await capture.wait() inside the scope for natural
    completion. Servers normally run until scope exit or caller cancellation.
    """

    host: str | None = None
    port: int = 5201
    duration: int = 10
    server: bool = False
    udp: bool = False

    def open(self, ctx: ActionContext) -> AbstractAsyncContextManager[Capture[TextRecord]]:
        return OwnedScope(Iperf._open(self, ctx))

    async def run(self, ctx: ActionContext) -> LogRef[TextRecord]:
        return await completed_log(Iperf._open(self, ctx))

    @asynccontextmanager
    async def _open(self, ctx: ActionContext) -> AsyncIterator[Capture[TextRecord]]:
        if not 0 < self.port < 65536 or self.duration <= 0:
            raise ValueError("invalid iperf port/duration")
        binary = await ctx.connection.executable("iperf3")
        args = (
            ("-s",)
            if self.server
            else ("-c", self.host or "", "-t", str(self.duration), *(("-u",) if self.udp else ()))
        )
        if not self.server and not self.host:
            raise ValueError("iperf client host required")
        output = ctx.output_path("data.log")
        errors = ctx.output_path("stderr.log")
        command = Exec(argv=(str(binary), *args, "-p", str(self.port), "--json", "--forceflush"))
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
                    tcp_ready(ctx, self.port) if self.server else output_ready(output),
                    wait,
                    allow_exit=True,
                )
                ctx.supervise(
                    watch_exit(
                        wait,
                        stopping,
                        allow_exit=True,
                    )
                )
                yield c
            finally:
                stopping.set()
                try:
                    result = await cleanup(proc.stop())
                    await cleanup(wait)
                    complete = (
                        result.exit_code in (0, -15, 143) if self.server else result.exit_code == 0
                    )
                finally:
                    try:
                        await cancel_task(wait)
                    finally:
                        await finalize_capture(
                            c,
                            ctx,
                            output,
                            Schema[TextRecord](name="iperf"),
                            complete=complete,
                            interval_ms=None,
                        )
