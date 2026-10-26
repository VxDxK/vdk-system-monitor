from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from vsm.action.poll_files import PollFiles
from vsm.util.records import FileRecord


@dataclass(frozen=True, kw_only=True)
class DiskIo(PollFiles[FileRecord]):
    """Sample Linux disk accounting counters from /proc/diskstats."""

    paths: tuple[str, ...] = ("/proc/diskstats",)
    schema_name: ClassVar[str] = "disk_io"
