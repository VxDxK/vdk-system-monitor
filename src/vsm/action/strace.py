from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from vsm.action.command_collector import CommandCollector


@dataclass(frozen=True, kw_only=True)
class Strace(CommandCollector):
    """Collect a raw system-call trace using target strace."""

    #: Supply a target PID or a command; ptrace permissions are required.
    args: tuple[str, ...] = ()  # Target PID or command required at execution.
    executable_name: ClassVar[str] = "strace"
    schema_name: ClassVar[str] = "strace"
