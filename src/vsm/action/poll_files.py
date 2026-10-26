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
from vsm.action.action import ActionContext
from vsm.action.capture import Capture
from vsm.models import CapabilityUnavailable, Exec, FileOutput, Schema
from vsm.util.records import FileRecord


@dataclass(frozen=True, kw_only=True)
class PollFiles[T: FileRecord = FileRecord]:
    """Sample target files through rfpoll and retain explicit read-error records.

    Requires BinaryProvider for rfpoll and readable target sources. Missing sources
    raise CapabilityUnavailable. Hardware availability is device-dependent.
    capture.log is available after scope exit; processing read errors requires an
    explicit opt-in. Resource entry establishes collector readiness.
    """

    #: Target paths or globs. Matching sources must exist and be readable.
    paths: tuple[str, ...]
    #: Sampling interval in positive integer milliseconds.
    interval: int = 250
    #: Optional duration in seconds; None collects until scope exit.
    duration: float | None = None
    schema_name: ClassVar[str] = "poll_files"
    tool: ClassVar[str] = "rfpoll"

    def __post_init__(self) -> None:
        validate_interval(self.interval)
        validate_duration(self.duration)

    async def arguments(self, ctx: ActionContext) -> tuple[str, ...]:
        if not self.paths or not any([await ctx.connection.glob(path) for path in self.paths]):
            raise CapabilityUnavailable("no matching collection sources")
        return tuple(arg for path in self.paths for arg in ("--path", path))

    def open(self, ctx: ActionContext) -> AbstractAsyncContextManager[Capture[T]]:
        return PollFiles._open(self, ctx)

    @asynccontextmanager
    async def _open(self, ctx: ActionContext) -> AsyncIterator[Capture[T]]:
        args = await self.arguments(ctx)
        binary = await ctx.binary(self.tool)
        output = ctx.output_path("data.log")
        errors = ctx.output_path("stderr.log")
        command = Exec(
            argv=(
                str(binary),
                "--interval-ms",
                str(self.interval),
                *duration_arguments(self.duration),
                *args,
            )
        )
        result: Capture[T] = Capture()
        stopping = asyncio.Event()
        async with ctx.connection.launch(
            command, stdout=FileOutput(output), stderr=FileOutput(errors)
        ) as proc:
            result.attach(proc)
            wait = asyncio.create_task(proc.wait(check=False))
            complete = False
            try:
                await await_ready(native_ready(output), wait, allow_exit=self.duration is not None)
                ctx.supervise(watch_exit(wait, stopping, allow_exit=self.duration is not None))
                yield result
            finally:
                stopping.set()
                try:
                    stopped = await cleanup(proc.stop())
                    await cleanup(wait)
                    complete = stopped.exit_code == 0 and last_record(output).get("type") == "end"
                finally:
                    try:
                        await cancel_task(wait)
                    finally:
                        await finalize_capture(
                            result,
                            ctx,
                            output,
                            Schema[T](name=self.schema_name),
                            complete=complete,
                            interval_ms=self.interval,
                        )
