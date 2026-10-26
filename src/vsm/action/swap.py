from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from vsm.action.poll_files import PollFiles
from vsm.util.records import FileRecord


@dataclass(frozen=True, kw_only=True)
class Swap(PollFiles[FileRecord]):
    """Sample swap capacity and usage from /proc/meminfo."""

    paths: tuple[str, ...] = ("/proc/meminfo",)
    schema_name: ClassVar[str] = "swap"
