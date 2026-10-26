from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vsm.action.action import ActionContext
from vsm.models import TargetPath


@dataclass(frozen=True, kw_only=True)
class Pull:
    """Copy a target file to a controller-local destination."""

    source: TargetPath
    destination: Path

    async def run(self, ctx: ActionContext) -> None:
        await ctx.connection.pull(self.source, self.destination)
