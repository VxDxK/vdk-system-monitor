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
    last_record,
    native_ready,
    watch_exit,
)
from vsm.action._duration import duration_arguments, validate_duration
from vsm.action._interval import validate_interval
from vsm.action._processes import process_arguments, validate_processes
from vsm.action.action import ActionContext
from vsm.action.capture import Capture
from vsm.models import Exec, FileOutput, ProcessSet, Schema
from vsm.util.records import ProcessRecord


@dataclass(frozen=True, kw_only=True)
class PidStat:
    """Sample CPU, memory and I/O for selected processes and their threads.

    Requires BinaryProvider for pspider. Identities are pinned during sampling;
    terminated processes/threads end their series without following reused IDs. capture.log is finalized at
    resource exit. PidStatProcessor consumes the resulting pid_stat logs.
    """

    schema_name: ClassVar[str] = "pid_stat"
    #: ProcessSet from ScanProcesses. Startup validates device, boot and start ticks.
    processes: ProcessSet
    #: Sampling interval in positive integer milliseconds.
    interval: int = 250
    #: Optional duration in seconds; None collects until scope exit.
    duration: float | None = None
    #: Collect process leaders only, excluding their threads.
    self_only: bool = False

    def __post_init__(self) -> None:
        validate_interval(self.interval)
        validate_duration(self.duration)

    def open(self, ctx: ActionContext) -> AbstractAsyncContextManager[Capture[ProcessRecord]]:
        return PidStat._open(self, ctx)

    @asynccontextmanager
    async def _open(self, ctx: ActionContext) -> AsyncIterator[Capture[ProcessRecord]]:
        await validate_processes(ctx, self.processes)
        binary = await ctx.binary("pspider")
        args = process_arguments(ctx, self.processes)
        output = ctx.output_path("data.log")
        errors = ctx.output_path("stderr.log")
        command = Exec(
            argv=(
                str(binary),
                "--interval-ms",
                str(self.interval),
                *duration_arguments(self.duration),
                *args,
                *(("--self-only",) if self.self_only else ()),
            )
        )
        capture: Capture[ProcessRecord] = Capture()
        stopping = asyncio.Event()
        async with ctx.connection.launch(
            command, stdout=FileOutput(output), stderr=FileOutput(errors)
        ) as proc:
            capture.attach(proc)
            wait = asyncio.create_task(proc.wait(check=False))
            complete = False
            try:
                await await_ready(native_ready(output), wait, allow_exit=self.duration is not None)
                ctx.supervise(watch_exit(wait, stopping, allow_exit=self.duration is not None))
                yield capture
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
                            capture,
                            ctx,
                            output,
                            Schema[ProcessRecord](name=self.schema_name),
                            complete=complete,
                            interval_ms=self.interval,
                        )
