"""A cancellable pause for Enter on the controller's stdin."""

from __future__ import annotations

import asyncio
import os
import select
import sys
from dataclasses import dataclass

from vsm.action.action import ActionContext


@dataclass(frozen=True, kw_only=True)
class Manual:
    """Wait for Enter; the caller must exclusively own controller stdin.

    Uses a readable file descriptor (terminal, pipe, or file) on Linux. EOF
    raises EOFError; cancellation leaves no input thread or reader behind.
    """

    #: Displayed on controller stdout before waiting for Enter.
    prompt: str = "Press Enter to continue..."

    async def run(self, ctx: ActionContext) -> None:
        descriptor = sys.stdin.fileno()
        print(self.prompt, end=" ", flush=True)
        while True:
            # Poll rather than use a blocking input thread, which cannot be
            # cancelled. Unlike epoll registration, select also accepts files.
            await asyncio.sleep(0.05)
            if not select.select((descriptor,), (), (), 0)[0]:
                continue
            value = os.read(descriptor, 1)
            if not value:
                raise EOFError("stdin closed before Manual received Enter")
            if value == b"\n":
                return
