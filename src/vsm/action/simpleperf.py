from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from vsm.action.command_collector import CommandCollector


@dataclass(frozen=True, kw_only=True)
class Simpleperf(CommandCollector):
    """Collect raw profiler output using target simpleperf."""

    args: tuple[str, ...] = ("stat", "-a")
    executable_name: ClassVar[str] = "simpleperf"
    schema_name: ClassVar[str] = "simpleperf"
