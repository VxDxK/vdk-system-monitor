from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from vsm.action.action import ActionContext
from vsm.action.poll_files import PollFiles
from vsm.models import CapabilityUnavailable
from vsm.util.records import FileRecord


@dataclass(frozen=True, kw_only=True)
class CpuFreq(PollFiles[FileRecord]):
    """Sample CPU scaling frequencies from existing cpufreq sources."""

    paths: tuple[str, ...] = ("/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq",)
    schema_name: ClassVar[str] = "cpu_freq"

    async def arguments(self, ctx: ActionContext) -> tuple[str, ...]:
        # Resolve complete paths: native component expansion otherwise invents
        # missing suffixes below directories such as cpu/cpufreq and cpu/cpuidle.
        paths = dict.fromkeys(
            [path for pattern in self.paths for path in await ctx.connection.glob(pattern)]
        )
        if not paths:
            raise CapabilityUnavailable("no matching CPU frequency sources")
        return tuple(arg for path in paths for arg in ("--path", str(path)))
