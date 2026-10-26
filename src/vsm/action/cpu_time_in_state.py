from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass

from vsm._cleanup import cleanup
from vsm.action.action import ActionContext
from vsm.action.capture import Capture
from vsm.models import CapabilityUnavailable, LogMetadata, LogRef, Schema
from vsm.util.records import FileRecord


@dataclass(frozen=True, kw_only=True)
class CpuTimeInState:
    """Capture before/after CPU frequency residency snapshots without a native sampler.

    Requires readable cpufreq stats/time_in_state files. CpuTimeInStateProcessor
    consumes the finalized cpu_time_in_state snapshot log after scope exit.
    """

    paths: tuple[str, ...] = ("/sys/devices/system/cpu/cpu*/cpufreq/stats/time_in_state",)

    def open(self, ctx: ActionContext) -> AbstractAsyncContextManager[Capture[FileRecord]]:
        return CpuTimeInState._open(self, ctx)

    @asynccontextmanager
    async def _open(self, ctx: ActionContext) -> AsyncIterator[Capture[FileRecord]]:
        found = tuple([p for pattern in self.paths for p in await ctx.connection.glob(pattern)])
        if not found:
            raise CapabilityUnavailable("time-in-state unavailable")

        async def snapshot() -> dict[str, str]:
            return {str(p): (await ctx.connection.read_bytes(p)).decode() for p in found}

        before = await snapshot()
        capture: Capture[FileRecord] = Capture()
        after: dict[str, str] = {}
        errors: list[dict[str, object]] = []
        complete = False

        async def final_snapshot() -> None:
            nonlocal complete
            for path in found:
                try:
                    after[str(path)] = (await ctx.connection.read_bytes(path)).decode()
                except Exception as error:
                    errors.append(
                        {
                            "path": str(path),
                            "error": str(error),
                            "errno": getattr(error, "errno", None),
                        }
                    )
                    raise
            complete = True

        try:
            yield capture
        finally:
            try:
                await cleanup(final_snapshot())
            finally:
                output = ctx.output_path("data.log")
                output.write_text(
                    json.dumps({"before": before, "after": after, "read_errors": errors})
                )
                capture.finalize(
                    LogRef(
                        path=output,
                        schema=Schema(name="cpu_time_in_state"),
                        device_id=ctx.connection.device_id,
                        action_id=ctx.action_id,
                        complete=complete,
                        metadata=LogMetadata(target=ctx.connection.target, provider="snapshot"),
                    )
                )
