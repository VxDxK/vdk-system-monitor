from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from vsm.action.poll_files import PollFiles
from vsm.util.records import FileRecord


@dataclass(frozen=True, kw_only=True)
class NetUdp(PollFiles[FileRecord]):
    """Sample IPv4/IPv6 UDP socket queues from target procfs."""

    paths: tuple[str, ...] = ("/proc/net/udp", "/proc/net/udp6")
    schema_name: ClassVar[str] = "net_udp"
