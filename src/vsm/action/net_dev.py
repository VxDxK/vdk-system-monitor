from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from vsm.action.poll_files import PollFiles
from vsm.util.records import NetRecord


@dataclass(frozen=True, kw_only=True)
class NetDev(PollFiles[NetRecord]):
    """Sample per-interface network byte, packet and error counters."""

    paths: tuple[str, ...] = ("/proc/net/dev",)
    schema_name: ClassVar[str] = "net_dev"
