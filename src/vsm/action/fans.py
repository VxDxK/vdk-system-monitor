from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from vsm.action.poll_files import PollFiles
from vsm.util.records import FileRecord


@dataclass(frozen=True, kw_only=True)
class Fans(PollFiles[FileRecord]):
    """Sample fan speeds from target hwmon sources."""

    paths: tuple[str, ...] = ("/sys/class/hwmon/hwmon*/fan*_input",)
    schema_name: ClassVar[str] = "fans"
