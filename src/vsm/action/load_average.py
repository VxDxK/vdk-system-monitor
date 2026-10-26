from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from vsm.action.poll_files import PollFiles
from vsm.util.records import FileRecord


@dataclass(frozen=True, kw_only=True)
class LoadAverage(PollFiles[FileRecord]):
    """Sample Linux load averages and task counts from /proc/loadavg."""

    paths: tuple[str, ...] = ("/proc/loadavg",)
    schema_name: ClassVar[str] = "load_average"
