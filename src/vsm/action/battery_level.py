from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from vsm.action.action import ActionContext
from vsm.action.poll_files import PollFiles
from vsm.models import CapabilityUnavailable
from vsm.util.records import FileRecord


@dataclass(frozen=True, kw_only=True)
class BatteryLevel(PollFiles[FileRecord]):
    """Sample battery capacity and optional power-supply fields from sysfs."""

    paths: tuple[str, ...] = ("/sys/class/power_supply/*/capacity",)
    schema_name: ClassVar[str] = "battery_level"

    async def arguments(self, ctx: ActionContext) -> tuple[str, ...]:
        required = [path for pattern in self.paths for path in await ctx.connection.glob(pattern)]
        if not required:
            raise CapabilityUnavailable("no matching battery sources")
        paths = dict.fromkeys(required)
        for path in required:
            for name in (
                "capacity_fcc",
                "capacity_rm",
                "charge_full",
                "charge_now",
                "current_now",
                "voltage_now",
            ):
                for extra in await ctx.connection.glob(str(path.parent / name)):
                    paths[extra] = None
        return tuple(arg for path in paths for arg in ("--path", str(path)))
