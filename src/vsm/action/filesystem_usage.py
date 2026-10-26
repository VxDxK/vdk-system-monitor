from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from vsm.action.action import ActionContext
from vsm.action.poll_files import PollFiles
from vsm.util.records import FileRecord


@dataclass(frozen=True, kw_only=True)
class FilesystemUsage(PollFiles[FileRecord]):
    """Sample filesystem capacity and available space for target paths."""

    paths: tuple[str, ...] = ("/",)
    schema_name: ClassVar[str] = "filesystem_usage"

    async def arguments(self, ctx: ActionContext) -> tuple[str, ...]:
        return tuple(a for p in self.paths for a in ("--filesystem", p))
