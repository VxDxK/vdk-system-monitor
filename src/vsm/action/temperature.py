from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from vsm.action.action import ActionContext
from vsm.action.poll_files import PollFiles
from vsm.models import CapabilityUnavailable
from vsm.util.records import FileRecord


@dataclass(frozen=True, kw_only=True)
class Temperature(PollFiles[FileRecord]):
    """Sample thermal-zone temperatures and optional sensor identity fields."""

    paths: tuple[str, ...] = ("/sys/class/thermal/thermal_zone*/temp",)
    schema_name: ClassVar[str] = "temperature"

    async def arguments(self, ctx: ActionContext) -> tuple[str, ...]:
        paths = dict.fromkeys(
            [path for pattern in self.paths for path in await ctx.connection.glob(pattern)]
        )
        if not paths:
            raise CapabilityUnavailable("no matching thermal sources")
        for path in tuple(paths):
            for extra in await ctx.connection.glob(str(path.parent / "type")):
                paths[extra] = None
        return tuple(arg for path in paths for arg in ("--path", str(path)))
