from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from vsm.action.command_collector import CommandCollector


@dataclass(frozen=True, kw_only=True)
class Tcpdump(CommandCollector):
    """Collect raw packet capture output using target tcpdump."""

    args: tuple[str, ...] = ("-U", "-w", "-", "-i", "any")
    executable_name: ClassVar[str] = "tcpdump"
    schema_name: ClassVar[str] = "pcap"
