from __future__ import annotations

from dataclasses import dataclass

from vsm.action.action import ActionContext
from vsm.models import ProcessSet


@dataclass(frozen=True, kw_only=True)
class ScanProcesses:
    """Discover immutable process identities as a ProcessSet."""

    #: Optional substring matched against target process command lines.
    pattern: str | None = None

    async def run(self, ctx: ActionContext) -> ProcessSet:
        return await ctx.connection.discover_processes(self.pattern)
