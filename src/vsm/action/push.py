from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vsm.action.action import ActionContext
from vsm.models import TargetPath


@dataclass(frozen=True, kw_only=True)
class Push:
    """Copy a controller-local file to a target destination."""

    source: Path
    destination: TargetPath

    async def run(self, ctx: ActionContext) -> None:
        await ctx.connection.push(self.source, self.destination)
