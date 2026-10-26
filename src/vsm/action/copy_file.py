from __future__ import annotations

from dataclasses import dataclass

from vsm.action.action import ActionContext
from vsm.models import Exec, FileOutput, LogMetadata, LogRef, OwnershipError, Schema, TargetPath
from vsm.util.records import TextRecord


@dataclass(frozen=True, kw_only=True)
class CopyFile:
    """Copy target files using cp and return a POSIX time LogRef."""

    source: TargetPath
    destination: TargetPath

    async def run(self, ctx: ActionContext) -> LogRef[TextRecord]:
        if (
            self.source.device_id != ctx.connection.device_id
            or self.destination.device_id != ctx.connection.device_id
        ):
            raise OwnershipError("copy paths belong to another device")
        tool = await ctx.connection.executable("/usr/bin/time")
        output = ctx.output_path("data.log")
        await ctx.connection.run(
            Exec(
                argv=(
                    str(tool),
                    "-p",
                    "cp",
                    "--",
                    str(self.source),
                    str(self.destination),
                )
            ),
            stdout=FileOutput(ctx.output_path("stdout.log")),
            stderr=FileOutput(output),
        )
        return LogRef(
            path=output,
            schema=Schema(name="time"),
            device_id=ctx.connection.device_id,
            action_id=ctx.action_id,
            complete=True,
            metadata=LogMetadata(target=ctx.connection.target, provider="time"),
        )
