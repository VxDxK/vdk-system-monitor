from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from vsm.action.poll_files import PollFiles
from vsm.util.records import FileRecord


@dataclass(frozen=True, kw_only=True)
class Meminfo(PollFiles[FileRecord]):
    """Sample Linux memory categories from /proc/meminfo."""

    paths: tuple[str, ...] = ("/proc/meminfo",)
    schema_name: ClassVar[str] = "meminfo"
