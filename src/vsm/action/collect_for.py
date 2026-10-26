from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass

from vsm.action.action import ActionContext, ResourceAction
from vsm.action.capture import Capture
from vsm.models import LogRef


@dataclass(frozen=True, kw_only=True)
class CollectFor[R]:
    collector: ResourceAction[Capture[R]]
    duration: float

    async def run(self, ctx: ActionContext) -> LogRef[R]:
        if not math.isfinite(self.duration) or self.duration <= 0:
            raise ValueError("duration must be positive")
        async with self.collector.open(ctx) as capture:
            await asyncio.sleep(self.duration)
        return capture.log
