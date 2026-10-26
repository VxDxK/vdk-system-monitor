from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass

from vsm._cleanup import cleanup
from vsm.action._collector import (
    await_ready,
    cancel_task,
    last_record,
    log_ref,
    native_ready,
    watch_exit,
)
from vsm.action._duration import duration_arguments, validate_duration
from vsm.action._launch_mode import LaunchScope, foreground_log
from vsm.action.action import ActionContext
from vsm.action.capture import Capture
from vsm.action.launch_mode import LaunchMode
from vsm.models import Exec, FileOutput, LogRef, Schema
from vsm.util.network import Endpoint, TrafficProtocol, TrafficRole
from vsm.util.records import TrafficRecord


@dataclass(frozen=True, kw_only=True)
class TrafficGenerator:
    """Generate or consume TCP/UDP traffic using native rgen."""

    address: Endpoint
    role: TrafficRole = TrafficRole.PRODUCER
    protocol: TrafficProtocol = TrafficProtocol.TCP
    #: Optional duration in seconds; None runs until scope exit.
    duration: float | None = None
    workers: int = 1
    #: Traffic payload size in bytes.
    payload_bytes: int = 1024
    #: Aggregate producer rate in bits/s across all workers; None is unlimited.
    rate_bps: int | None = None

    #: FOREGROUND uses do(); background modes use a resource scope (default stop on exit).
    mode: LaunchMode = LaunchMode.BACKGROUND_STOP

    def __post_init__(self) -> None:
        validate_duration(self.duration)

    def open(self, ctx: ActionContext) -> AbstractAsyncContextManager[Capture[TrafficRecord]]:
        return LaunchScope(self.mode, TrafficGenerator._open(self, ctx))

    async def run(self, ctx: ActionContext) -> LogRef[TrafficRecord]:
        return await foreground_log(self.mode, TrafficGenerator._open(self, ctx))

    @asynccontextmanager
    async def _open(self, ctx: ActionContext) -> AsyncIterator[Capture[TrafficRecord]]:
        if self.role not in ("producer", "consumer") or self.protocol not in ("tcp", "udp"):
            raise ValueError("invalid traffic role/protocol")
        tool = await ctx.binary("rgen")
        args = (
            self.role.value,
            "--address",
            str(self.address),
            "--protocol",
            self.protocol.value,
            *duration_arguments(self.duration),
            "--workers",
            str(self.workers),
            "--payload-bytes",
            str(self.payload_bytes),
        )
        if self.rate_bps is not None:
            args += ("--rate-bps", str(self.rate_bps))
        output = ctx.output_path("data.log")
        errors = ctx.output_path("stderr.log")
        command = Exec(argv=(str(tool), *args))
        c: Capture[TrafficRecord] = Capture()
        stopping = asyncio.Event()
        async with ctx.connection.launch(
            command, stdout=FileOutput(output), stderr=FileOutput(errors)
        ) as proc:
            c.attach(proc)
            wait = asyncio.create_task(proc.wait(check=False))
            complete = False
            try:
                await await_ready(native_ready(output), wait, allow_exit=True)
                ctx.supervise(watch_exit(wait, stopping, allow_exit=True))
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
                        c.finalize(
                            log_ref(
                                ctx,
                                output,
                                Schema[TrafficRecord](name="traffic_generator"),
                                complete=complete,
                                interval_ms=None,
                            )
                        )
