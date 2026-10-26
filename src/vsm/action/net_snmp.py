from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from vsm.action.poll_files import PollFiles
from vsm.util.records import FileRecord


@dataclass(frozen=True, kw_only=True)
class NetSnmp(PollFiles[FileRecord]):
    """Sample Linux SNMP protocol counters from /proc/net/snmp."""

    paths: tuple[str, ...] = ("/proc/net/snmp",)
    schema_name: ClassVar[str] = "net_snmp"
