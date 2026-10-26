"""Startup identity checks shared by process collectors."""

from vsm.action.action import ActionContext
from vsm.models import CapabilityUnavailable, ProcessSet


async def validate_processes(ctx: ActionContext, processes: ProcessSet) -> None:
    if not processes:
        raise CapabilityUnavailable("no selected processes")
    await ctx.connection.validate_processes(processes)


def process_arguments(ctx: ActionContext, processes: ProcessSet) -> tuple[str, ...]:
    args = tuple(a for p in processes for a in ("--process", f"{p.pid}:{p.start_ticks}"))
    boot = ctx.connection.target.boot_id or next((p.boot_id for p in processes if p.boot_id), None)
    return args + (("--boot-id", boot) if boot else ())
