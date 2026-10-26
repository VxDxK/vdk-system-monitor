from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import ClassVar

from vsm._cleanup import cleanup
from vsm.action._interval import validate_interval
from vsm.action.action import ActionContext
from vsm.action.capture import Capture
from vsm.models import Exec, LogMetadata, LogRef, MonitorError, Schema
from vsm.util.records import TextRecord


@dataclass(frozen=True, kw_only=True)
class PeriodicLauncher:
    """Capture timestamped output from repeated target command executions."""

    #: Literal argument tuple; no shell parsing.
    argv: tuple[str, ...]
    #: Delay in positive integer milliseconds between command executions.
    interval: int = 1000
    schema_name: ClassVar[str] = "periodic_launcher"

    def __post_init__(self) -> None:
        validate_interval(self.interval)

    def open(self, ctx: ActionContext) -> AbstractAsyncContextManager[Capture[TextRecord]]:
        return PeriodicLauncher._open(self, ctx)

    @asynccontextmanager
    async def _open(self, ctx: ActionContext) -> AsyncIterator[Capture[TextRecord]]:
        output = ctx.output_path("data.log")
        capture: Capture[TextRecord] = Capture()
        stop = asyncio.Event()
        ready = asyncio.Event()

        async def loop() -> None:
            start = asyncio.get_running_loop().time()
            with output.open("x") as stream:
                while not stop.is_set():
                    result = await ctx.connection.run(Exec(argv=self.argv))
                    stream.write(
                        json.dumps(
                            {
                                "time_ns": round((asyncio.get_running_loop().time() - start) * 1e9),
                                "stdout": result.stdout.decode(errors="replace"),
                                "stderr": result.stderr.decode(errors="replace"),
                            }
                        )
                        + "\n"
                    )
                    stream.flush()
                    ready.set()
                    try:
                        await asyncio.wait_for(stop.wait(), self.interval / 1000)
                    except TimeoutError:
                        pass

        task = asyncio.create_task(loop())

        async def supervise() -> None:
            await asyncio.shield(task)

        ctx.supervise(supervise())
        readiness = asyncio.create_task(ready.wait())
        try:
            done, _ = await asyncio.wait((readiness, task), return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                await task
                raise MonitorError("periodic collector exited before readiness")
            yield capture
        finally:
            readiness.cancel()
            try:
                await cleanup(readiness)
            except asyncio.CancelledError:
                pass
            stop.set()
            task.cancel()
            try:
                await cleanup(task)
            except asyncio.CancelledError:
                pass
            finally:
                capture.finalize(
                    LogRef(
                        path=output,
                        schema=Schema(name=self.schema_name),
                        device_id=ctx.connection.device_id,
                        action_id=ctx.action_id,
                        complete=ready.is_set() and (task.cancelled() or task.exception() is None),
                        metadata=LogMetadata(
                            target=ctx.connection.target,
                            provider="periodic",
                            interval_seconds=self.interval / 1000,
                        ),
                    )
                )
