from __future__ import annotations

from dataclasses import dataclass

from vsm.action.action import ActionContext
from vsm.models import Completed, Shell


@dataclass(frozen=True, kw_only=True)
class ShellAction:
    """Execute a target shell script and return the command result."""

    script: str

    async def run(self, ctx: ActionContext) -> Completed:
        return await ctx.connection.run(Shell(script=self.script))
