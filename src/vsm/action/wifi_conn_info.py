from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import ClassVar

from vsm.action.action import ActionContext
from vsm.action.capture import Capture
from vsm.action.periodic_launcher import PeriodicLauncher
from vsm.models import LogRef, Schema
from vsm.util.records import TextRecord


@dataclass(frozen=True, kw_only=True)
class WifiConnInfo(PeriodicLauncher):
    """Periodically query a target Wi-Fi interface using iw."""

    #: Delay in positive integer milliseconds between Wi-Fi queries.
    interval: int = 500
    interface: str
    argv: tuple[str, ...] = ()
    schema_name: ClassVar[str] = "wifi_conn_info"

    def open(self, ctx: ActionContext) -> AbstractAsyncContextManager[Capture[TextRecord]]:
        return WifiConnInfo._open(self, ctx)

    @asynccontextmanager
    async def _open(self, ctx: ActionContext) -> AsyncIterator[Capture[TextRecord]]:
        tool = await ctx.connection.executable("iw")
        action = PeriodicLauncher(
            argv=(str(tool), "dev", self.interface, "station", "dump"), interval=self.interval
        )
        capture = None
        try:
            async with action.open(ctx) as c:
                capture = c
                yield c
        finally:
            if capture is not None:
                capture.finalize(
                    LogRef(
                        path=capture.log.path,
                        schema=Schema(name="wifi_conn_info"),
                        device_id=capture.log.device_id,
                        action_id=capture.log.action_id,
                        complete=capture.log.complete,
                        metadata=capture.log.metadata,
                    )
                )
