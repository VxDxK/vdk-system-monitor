from __future__ import annotations

from collections.abc import Coroutine
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Protocol, runtime_checkable

from vsm.connection.connection import Connection
from vsm.models import ActionId, TargetPath


class ActionContext(Protocol):
    @property
    def connection(self) -> Connection: ...
    @property
    def action_id(self) -> ActionId: ...
    def output_path(self, name: str) -> Path: ...
    async def binary(self, name: str) -> TargetPath: ...
    async def deploy(self, source: Path) -> TargetPath: ...
    def service[T](self, interface: type[T]) -> T: ...
    def supervise(self, coroutine: Coroutine[object, object, None]) -> None: ...


class Action[T](Protocol):
    async def run(self, ctx: ActionContext) -> T: ...


class ResourceAction[T](Protocol):
    def open(self, ctx: ActionContext) -> AbstractAsyncContextManager[T]: ...


@runtime_checkable
class ClaimedResource(Protocol):
    async def claims(self, ctx: ActionContext) -> tuple[TargetPath, ...]: ...
