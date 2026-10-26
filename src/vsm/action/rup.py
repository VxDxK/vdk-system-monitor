from __future__ import annotations

import asyncio
import json
import math
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import PurePosixPath

from vsm._cleanup import cleanup
from vsm.action._collector import await_ready, cancel_task, log_ref, watch_exit
from vsm.action._interval import validate_interval
from vsm.action._launch_mode import LaunchScope, foreground_log
from vsm.action.action import ActionContext
from vsm.action.capture import Capture
from vsm.action.launch_mode import LaunchMode
from vsm.action.rup_capture import marker_ready, summary_complete
from vsm.models import CleanupUnknown, EnvVar, Exec, FileOutput, LogRef, MonitorError, Schema
from vsm.util.network import BindEndpoint, Endpoint, ProbeProtocol, ProbeRole
from vsm.util.records import LatencyRecord


@dataclass(frozen=True, kw_only=True)
class Rup:
    """Collect client latency or host an echo service with the compatible rup tool.

    Requires BinaryProvider for rup. RupProcessor accepts client framework rup
    logs wrapping rup.ping/3; server service logs are not measurements. Terminal
    responses count as received. ICMP requires ping-socket permissions. Server
    role rejects client-only options; capture.log is available after scope exit.
    """

    address: Endpoint
    role: ProbeRole = ProbeRole.CLIENT
    protocol: ProbeProtocol = ProbeProtocol.UDP
    #: Optional framework duration in seconds; None runs until count completion or scope exit.
    duration: float | None = None
    #: Fixed probe interval in positive integer milliseconds; adaptive mode omits it from native arguments.
    interval: int = 100
    #: Reply timeout in seconds.
    timeout: float = 1
    count: int | None = None
    adaptive: bool = False
    #: Optional BindEndpoint; local port zero requests an ephemeral port.
    local: BindEndpoint | None = None
    request_size: int | None = None
    response_size: int | None = None
    traffic_class: int | None = None
    #: FOREGROUND uses do(); background modes use a resource scope (default stop on exit).
    mode: LaunchMode = LaunchMode.BACKGROUND_STOP

    def __post_init__(self) -> None:
        if not isinstance(self.role, ProbeRole) or not isinstance(self.protocol, ProbeProtocol):
            raise ValueError("typed ProbeRole and ProbeProtocol required")  # noqa: TRY004 -- Validation uses ValueError consistently.
        if self.duration is not None and (
            not math.isfinite(self.duration) or not 0 < self.duration <= 86400
        ):
            raise ValueError("duration must be in (0, 24h], or None")
        validate_interval(self.interval)
        if not math.isfinite(self.timeout) or not 0.001 <= self.timeout <= 60:
            raise ValueError("timeout must be in [1ms, 60s]")
        if self.count is not None and (type(self.count) is not int or not 0 < self.count < 2**64):
            raise ValueError("count must be a positive u64")
        for size in (self.request_size, self.response_size):
            if size is not None and (type(size) is not int or not 12 <= size <= 65535):
                raise ValueError("packet sizes must be integers in [12, 65535]")
        if self.traffic_class is not None and (
            type(self.traffic_class) is not int or not 0 <= self.traffic_class <= 255
        ):
            raise ValueError("traffic_class must be an integer byte")
        if self.local is not None and self.local.address.version != self.address.address.version:
            raise ValueError("local and remote address families must match")
        if self.protocol is ProbeProtocol.ICMP and (
            self.role is ProbeRole.SERVER or self.response_size is not None
        ):
            raise ValueError("ICMP has no server or configurable response size")
        if self.role is ProbeRole.SERVER and (
            self.count is not None
            or self.adaptive
            or self.local is not None
            or self.request_size is not None
            or self.response_size is not None
            or self.traffic_class is not None
        ):
            raise ValueError("client options cannot be used with a server")
        if self.protocol is not ProbeProtocol.ICMP and self.address.port == 0:
            raise ValueError("TCP/UDP require a nonzero port for reproducible endpoint discovery")

    def arguments(self) -> tuple[str, ...]:
        prefix = ("--protocol", self.protocol.value)
        if self.role is ProbeRole.SERVER:
            return (*prefix, "server", str(self.address))
        args = (*prefix, "--output", "jsonl", "--wait-time", str(round(self.timeout * 1000)))
        args += ("--adaptive-interval",) if self.adaptive else ("--interval", str(self.interval))
        for flag, value in (
            ("--ping-number", self.count),
            ("--local-address", self.local),
            ("--request-size", self.request_size),
            ("--response-size", self.response_size),
            ("--tos", self.traffic_class),
        ):
            if value is not None:
                args += (flag, str(value))
        return (*args, str(self.address))

    def open(self, ctx: ActionContext) -> AbstractAsyncContextManager[Capture[LatencyRecord]]:
        return LaunchScope(self.mode, Rup._open(self, ctx))

    async def run(self, ctx: ActionContext) -> LogRef[LatencyRecord]:
        return await foreground_log(self.mode, Rup._open(self, ctx))

    @asynccontextmanager
    async def _open(self, ctx: ActionContext) -> AsyncIterator[Capture[LatencyRecord]]:
        tool = await ctx.binary("rup")
        created = await ctx.connection.run(
            Exec(argv=("mktemp", "-d", str(ctx.connection.tmp_dir / "rup.XXXXXXXX")))
        )
        directory = created.stdout.decode().strip()
        if PurePosixPath(directory).parent != ctx.connection.tmp_dir.value or not PurePosixPath(
            directory
        ).name.startswith("rup."):
            raise MonitorError("unexpected owned rup control directory")
        remove_control = True
        try:
            ctx.output_path("rup-control.json").write_text(json.dumps({"directory": directory}))
            marker = ctx.connection.path(directory + "/ready")
            output = ctx.output_path("data.log")
            errors = ctx.output_path("stderr.log")
            command = Exec(
                argv=(str(tool), *self.arguments()),
                env=(EnvVar(name="VSM_RUP_READY_FILE", value=str(marker)),),
            )
            capture: Capture[LatencyRecord] = Capture()
            stopping = asyncio.Event()
            grace = max(5, self.timeout + 1)
            allow_exit = (
                self.role is ProbeRole.CLIENT
                or self.duration is not None
                or self.mode is not LaunchMode.BACKGROUND_STOP
            )
            async with ctx.connection.launch(
                command, stdout=FileOutput(output), stderr=FileOutput(errors)
            ) as proc:
                capture.attach(proc)
                wait = asyncio.create_task(proc.wait(check=False))
                timer: asyncio.Task[None] | None = None
                complete = False
                try:
                    await await_ready(marker_ready(ctx, marker), wait, allow_exit=allow_exit)
                    ctx.supervise(watch_exit(wait, stopping, allow_exit=allow_exit))
                    duration = self.duration
                    if duration is not None:

                        async def limit() -> None:
                            try:
                                async with asyncio.timeout(duration):
                                    await asyncio.shield(wait)
                            except TimeoutError:
                                await cleanup(proc.stop(grace=grace))

                        timer = asyncio.create_task(limit())

                        async def watch_timer(task: asyncio.Task[None]) -> None:
                            await asyncio.shield(task)

                        ctx.supervise(watch_timer(timer))
                    yield capture
                finally:
                    stopping.set()
                    try:
                        if timer is not None:
                            await cancel_task(timer)
                        result = await cleanup(proc.stop(grace=grace))
                        await cleanup(wait)
                        complete = summary_complete(
                            output, result, client=self.role is ProbeRole.CLIENT
                        )
                    finally:
                        try:
                            await cancel_task(wait)
                        finally:
                            capture.finalize(
                                log_ref(
                                    ctx,
                                    output,
                                    Schema[LatencyRecord](name="rup"),
                                    complete=complete,
                                    interval_ms=self.interval if not self.adaptive else None,
                                    provider="binary-utils rup 0.1.0; rup.ping/3",
                                )
                            )
        except CleanupUnknown:
            remove_control = False
            raise
        finally:
            if remove_control:
                await cleanup(ctx.connection.run(Exec(argv=("rm", "-rf", "--", directory))))
