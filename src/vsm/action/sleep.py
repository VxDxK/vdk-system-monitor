from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass

from vsm.action.action import ActionContext


@dataclass(frozen=True, kw_only=True)
class Sleep:
    """Pause the experiment coroutine for a duration in seconds."""

    #: Pause duration in seconds.
    duration: float

    async def run(self, ctx: ActionContext) -> None:
        if not math.isfinite(self.duration) or self.duration < 0:
            raise ValueError("invalid sleep duration")
        await asyncio.sleep(self.duration)
