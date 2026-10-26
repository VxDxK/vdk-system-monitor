from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from vsm.action.poll_files import PollFiles
from vsm.util.records import CpuRecord


@dataclass(frozen=True, kw_only=True)
class ProcStat(PollFiles[CpuRecord]):
    """Sample Linux CPU accounting ticks from /proc/stat."""

    paths: tuple[str, ...] = ("/proc/stat",)
    schema_name: ClassVar[str] = "proc_stat"
