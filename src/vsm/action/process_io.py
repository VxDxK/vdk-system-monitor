from dataclasses import dataclass
from typing import ClassVar

from vsm.action.pid_stat import PidStat


@dataclass(frozen=True, kw_only=True)
class ProcessIo(PidStat):
    """Sample I/O counters for selected processes and their threads."""

    schema_name: ClassVar[str] = "process_io"
