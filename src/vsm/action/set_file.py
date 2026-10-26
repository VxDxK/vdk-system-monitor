from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass

from vsm._cleanup import cleanup
from vsm.action.action import ActionContext
from vsm.models import TargetPath


@dataclass(frozen=True, kw_only=True)
class SetFile:
    """Temporarily replace a target file and restore its original bytes.

    Requires read/write access. Restoration is attempted even after partial
    startup or cancellation. Claims coordinate equal-device connections within
    one Run; external writers and aliases are outside that protection.
    """

    path: TargetPath
    value: bytes

    async def claims(self, ctx: ActionContext) -> tuple[TargetPath, ...]:
        return (self.path,)

    def open(self, ctx: ActionContext) -> AbstractAsyncContextManager[bytes]:
        return SetFile._open(self, ctx)

    @asynccontextmanager
    async def _open(self, ctx: ActionContext) -> AsyncIterator[bytes]:
        old = await ctx.connection.read_bytes(self.path)
        import hashlib

        name = "previous-" + hashlib.sha256(str(self.path).encode()).hexdigest()[:16] + ".json"
        ctx.output_path(name).write_text(
            json.dumps({"path": str(self.path), "encoding": "hex", "value": old.hex()})
        )
        try:
            await ctx.connection.write_bytes(self.path, self.value)
            yield old
        finally:
            await cleanup(ctx.connection.write_bytes(self.path, old))
