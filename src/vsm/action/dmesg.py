from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from vsm.action.command_collector import CommandCollector


@dataclass(frozen=True, kw_only=True)
class Dmesg(CommandCollector):
    """Collect raw kernel messages using the target dmesg executable."""

    args: tuple[str, ...] = ("--follow", "--human")
    executable_name: ClassVar[str] = "dmesg"
    schema_name: ClassVar[str] = "dmesg"
