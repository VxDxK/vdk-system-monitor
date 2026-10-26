"""Rup marker readiness and summary validation, independent of process ownership."""

from __future__ import annotations

import asyncio
from pathlib import Path

from vsm.action._collector import last_record
from vsm.action.action import ActionContext
from vsm.models import Completed, TargetPath
from vsm.util.rup_format import validate_summary


async def marker_ready(ctx: ActionContext, marker: TargetPath) -> None:
    async with asyncio.timeout(5):
        while True:
            try:
                if await ctx.connection.read_bytes(marker) == b"ready\n":
                    return
            except FileNotFoundError:
                pass
            await asyncio.sleep(0.01)


def summary_complete(path: Path, result: Completed, *, client: bool) -> bool:
    if result.exit_code != 0:
        return False
    if not client:
        return True
    try:
        validate_summary(last_record(path))
        return True
    except ValueError, AttributeError:
        return False
