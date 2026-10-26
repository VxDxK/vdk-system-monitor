"""Small readiness, supervision, and log helpers; collectors own their lifecycle."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Coroutine
from pathlib import Path

from vsm._cleanup import cleanup
from vsm.action.action import ActionContext
from vsm.models import CommandFailed, Completed, LogMetadata, LogRef, MonitorError, Schema


async def native_ready(path: Path) -> None:
    cursor = 0
    pending = b""
    async with asyncio.timeout(5):
        while True:
            if path.exists():
                with path.open("rb") as stream:
                    stream.seek(cursor)
                    data = stream.read(65536)
                cursor += len(data)
                pending += data
                lines = pending.split(b"\n")
                pending = lines.pop()
                for line in lines:
                    if json.loads(line).get("type") == "ready":
                        return
            await asyncio.sleep(0.01)


async def output_ready(path: Path) -> None:
    async with asyncio.timeout(5):
        while not path.exists():
            await asyncio.sleep(0.01)


async def tcp_ready(ctx: ActionContext, port: int) -> None:
    async with asyncio.timeout(5):
        while True:
            for table in ("/proc/net/tcp", "/proc/net/tcp6"):
                data = (await ctx.connection.read_bytes(ctx.connection.path(table))).decode()
                for row in data.splitlines()[1:]:
                    fields = row.split()
                    if (
                        len(fields) > 3
                        and fields[3] == "0A"
                        and int(fields[1].rsplit(":", 1)[1], 16) == port
                    ):
                        return
            await asyncio.sleep(0.02)


async def cancel_task[T](task: asyncio.Task[T]) -> None:
    if not task.done():
        task.cancel()
    try:
        await cleanup(task)
    except asyncio.CancelledError:
        pass


async def await_ready(
    readiness: Coroutine[object, object, None],
    wait: asyncio.Task[Completed],
    *,
    allow_exit: bool = False,
) -> None:
    ready = asyncio.create_task(readiness)
    try:
        done, _ = await asyncio.wait((ready, wait), return_when=asyncio.FIRST_COMPLETED)
        if wait in done:
            result = wait.result()
            if result.exit_code:
                raise CommandFailed(result)
            if not allow_exit:
                raise MonitorError("collector exited before readiness")
        await ready
    finally:
        await cancel_task(ready)


async def watch_exit(
    wait: asyncio.Task[Completed], stopping: asyncio.Event, *, allow_exit: bool = False
) -> None:
    result = await asyncio.shield(wait)
    if not stopping.is_set():
        if result.exit_code:
            raise CommandFailed(result)
        if not allow_exit:
            raise MonitorError("collector completed while resource scope was active")


def last_record(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    with path.open("rb") as stream:
        stream.seek(0, 2)
        stream.seek(max(0, stream.tell() - 65536))
        lines = stream.read().splitlines()
    try:
        value = json.loads(lines[-1]) if lines else {}
        return value if isinstance(value, dict) else {}
    except ValueError, UnicodeDecodeError:
        return {}


def log_ref[R](
    ctx: ActionContext,
    path: Path,
    schema: Schema[R],
    *,
    complete: bool,
    interval_ms: int | None = None,
    provider: str | None = None,
) -> LogRef[R]:
    return LogRef(
        path=path,
        schema=schema,
        device_id=ctx.connection.device_id,
        action_id=ctx.action_id,
        complete=complete,
        metadata=LogMetadata(
            target=ctx.connection.target,
            provider=provider or schema.name,
            interval_seconds=interval_ms / 1000 if interval_ms is not None else None,
        ),
    )
