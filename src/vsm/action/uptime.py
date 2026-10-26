from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from vsm.action.poll_files import PollFiles
from vsm.util.records import FileRecord


@dataclass(frozen=True, kw_only=True)
class Uptime(PollFiles[FileRecord]):
    """Sample uptime, accumulated CPU idle time and boot epoch."""

    paths: tuple[str, ...] = ("/proc/uptime", "/proc/stat")
    schema_name: ClassVar[str] = "uptime"
