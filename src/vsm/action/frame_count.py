from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from vsm.action.poll_files import PollFiles
from vsm.util.records import FileRecord


@dataclass(frozen=True, kw_only=True)
class FrameCount(PollFiles[FileRecord]):
    """Sample cumulative display frame counters for frame-rate processing."""

    paths: tuple[str, ...] = ("/sys/class/graphics/fb*/frame_count",)
    schema_name: ClassVar[str] = "frame_count"
