from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from vsm.action.poll_files import PollFiles
from vsm.util.records import FileRecord


@dataclass(frozen=True, kw_only=True)
class NpuFreq(PollFiles[FileRecord]):
    """Sample NPU devfreq frequency readings from target sysfs."""

    paths: tuple[str, ...] = ("/sys/class/devfreq/*npu*/cur_freq",)
    schema_name: ClassVar[str] = "npu_freq"
