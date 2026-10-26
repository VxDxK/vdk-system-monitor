from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from vsm.action._processes import process_arguments, validate_processes
from vsm.action.action import ActionContext
from vsm.action.poll_files import PollFiles
from vsm.models import ProcessSet
from vsm.util.records import FileRecord


@dataclass(frozen=True, kw_only=True)
class Smaps(PollFiles[FileRecord]):
    """Sample memory mappings for an identity-validated ProcessSet.

    Requires readable per-process smaps. Identity-pinned sampling
    ends terminated processes without following reused PIDs. SmapsProcessor consumes
    the resulting smaps logs.
    """

    processes: ProcessSet
    paths: tuple[str, ...] = ()
    schema_name: ClassVar[str] = "smaps"

    async def arguments(self, ctx: ActionContext) -> tuple[str, ...]:
        await validate_processes(ctx, self.processes)
        return process_arguments(ctx, self.processes) + tuple(
            a for p in self.processes for a in ("--path", f"/proc/{p.pid}/smaps_rollup")
        )
