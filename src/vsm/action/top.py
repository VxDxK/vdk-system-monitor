from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from vsm.action.command_collector import CommandCollector


@dataclass(frozen=True, kw_only=True)
class Top(CommandCollector):
    """Collect target batch top process statistics."""

    args: tuple[str, ...] = ("-b", "-d", "1")
    executable_name: ClassVar[str] = "top"
    schema_name: ClassVar[str] = "top"
