"""Run-owned structured concurrency, scoped actions, and native artifact deployment."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Coroutine, Generator
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import asdict, dataclass
from importlib.metadata import version
from itertools import count
from pathlib import Path
from types import TracebackType
from typing import Literal, Self, cast

from vsm._cleanup import cleanup
from vsm.action.action import Action, ClaimedResource, ResourceAction
from vsm.binary import BinaryProvider, deploy
from vsm.connection.connection import Connection, ConnectionBackend
from vsm.models import ActionId, CapabilityUnavailable, DeviceId, OwnershipError, TargetPath


class TaskHandle[T]:
    def __init__(self, task: asyncio.Task[T]):
        self._task = task

    def __await__(self) -> Generator[object, None, T]:
        return self.join().__await__()

    async def join(self) -> T:
        return await self._task

    async def cancel_and_wait(self) -> None:
        self._task.cancel()
        try:
            await cleanup(self._task)
        except asyncio.CancelledError:
            pass

    @property
    def done(self) -> bool:
        return self._task.done()


class TaskScope:
    def __init__(self) -> None:
        self._group = asyncio.TaskGroup()
        self._active = False
        self._closing = False
        self._tasks: set[asyncio.Task[object]] = set()

    @property
    def active(self) -> bool:
        return self._active

    async def __aenter__(self) -> Self:
        await self._group.__aenter__()
        self._active = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        self._closing = True
        try:
            await self._group.__aexit__(exc_type, exc, tb)
        finally:
            self._active = False
        return False

    def spawn[T](
        self, coroutine: Coroutine[object, object, T], *, name: str | None = None
    ) -> TaskHandle[T]:
        if not self._active or (self._closing and asyncio.current_task() not in self._tasks):
            coroutine.close()
            raise OwnershipError("task scope is not active")
        task = self._group.create_task(coroutine, name=name)
        owned = cast(asyncio.Task[object], task)
        self._tasks.add(owned)
        task.add_done_callback(lambda _: self._tasks.discard(owned))
        return TaskHandle(task)


@dataclass
class Context:
    device: Device
    action_id: ActionId

    @property
    def connection(self) -> Connection:
        return self.device.connection

    def output_path(self, name: str) -> Path:
        if Path(name).name != name:
            raise ValueError("output name must be a filename")
        path = self.device.run.output / self.device.output_name / str(self.action_id)
        path.mkdir(parents=True, exist_ok=True)
        return path / name

    async def binary(self, name: str) -> TargetPath:
        provider = self.device.run.binaries
        return await provider.resolve(self.connection, name)

    def service[T](self, interface: type[T]) -> T:
        return self.device.run.services.get(interface)

    async def deploy(self, source: Path) -> TargetPath:
        return await deploy(self.connection, source)

    def supervise(self, coroutine: Coroutine[object, object, None]) -> None:
        self.device.run.spawn(coroutine)


class Device:
    def __init__(self, run: Run, connection: Connection, label: str | None, output_name: str):
        self.run, self.connection, self.label = run, connection, label
        self.output_name = output_name

    @property
    def id(self) -> DeviceId:
        return self.connection.device_id

    def _check(self) -> None:
        if not self.run.active:
            raise OwnershipError("device's run is closed")

    async def do[T](self, action: Action[T]) -> T:
        self._check()
        ctx = Context(self, ActionId(f"action-{next(self.run._action_numbers):06d}"))
        self.run.record(ctx, type(action).__name__, "started")
        try:
            async with self._claim(ctx, action):
                value = await action.run(ctx)
        except BaseException:
            self.run.record(ctx, type(action).__name__, "failed")
            raise
        self.run.record(ctx, type(action).__name__, "finished")
        return value

    def use[T](self, action: ResourceAction[T]) -> AbstractAsyncContextManager[T]:
        return self._use(action)

    @asynccontextmanager
    async def _use[T](self, action: ResourceAction[T]) -> AsyncIterator[T]:
        self._check()
        ctx = Context(self, ActionId(f"action-{next(self.run._action_numbers):06d}"))
        async with self._claim(ctx, action):
            self.run.record(ctx, type(action).__name__, "started")
            try:
                manager = action.open(ctx)
                value = await manager.__aenter__()
                try:
                    yield value
                except BaseException as primary:
                    try:
                        await cleanup(
                            manager.__aexit__(type(primary), primary, primary.__traceback__)
                        )
                    except BaseException as secondary:  # noqa: BLE001 -- Include cancellation and failures from user-defined code.
                        if not isinstance(secondary, asyncio.CancelledError):
                            primary.add_note(f"cleanup failed: {secondary}")
                            self.run.cleanup_errors.append(str(secondary))
                    raise
                else:
                    await cleanup(manager.__aexit__(None, None, None))
                self.run.record(ctx, type(action).__name__, "finished")
            except BaseException:
                self.run.record(ctx, type(action).__name__, "failed")
                raise

    @asynccontextmanager
    async def _claim(self, ctx: Context, action: object) -> AsyncIterator[None]:
        if isinstance(action, ClaimedResource):
            paths = await action.claims(ctx)
            if any(p.device_id != self.id for p in paths):
                raise OwnershipError("claimed path belongs to another device")
            keys = tuple((self.id, "file:" + str(p)) for p in paths)
        else:
            claim = getattr(action, "claim", None)
            keys = ((self.id, str(claim)),) if claim else ()
        if any(key in self.run.claims for key in keys):
            raise OwnershipError("conflicting resource claims")
        self.run.claims.update(keys)
        try:
            yield
        finally:
            self.run.claims.difference_update(keys)


class Run(TaskScope):
    def __init__(self, output: Path, *, binaries: BinaryProvider | None = None):
        super().__init__()
        self.output = output
        self.binaries = binaries if binaries is not None else BinaryProvider()
        self._stack = AsyncExitStack()
        self.services = Services()
        self._devices: list[Device] = []
        self._action_numbers = count(1)
        self.claims: set[tuple[DeviceId, str]] = set()
        self._actions: list[dict[str, str]] = []
        self.cleanup_errors: list[str] = []

    async def __aenter__(self) -> Self:
        self._vsm_version = version("vsm")
        self.output.mkdir(parents=True, exist_ok=False)
        await self._stack.__aenter__()
        await super().__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        primary: BaseException | None = None
        try:
            await super().__aexit__(exc_type, exc, tb)
        except BaseException as error:  # noqa: BLE001 -- Include cancellation and failures from user-defined code.
            primary = error
        try:
            await cleanup(self._stack.aclose())
        except BaseException as error:  # noqa: BLE001 -- Include cancellation and failures from user-defined code.
            self.cleanup_errors.append(str(error))
            if primary:
                primary.add_note(f"connection cleanup failed: {error}")
            else:
                primary = error
        finally:
            data = {
                "vsm_version": self._vsm_version,
                "devices": [
                    {
                        "id": d.id.value,
                        "connection": d.output_name,
                        "label": d.label,
                        "target": asdict(d.connection.target),
                    }
                    for d in self._devices
                ],
                "actions": self._actions,
                "cleanup_errors": self.cleanup_errors,
                "status": "failed" if primary or exc else "finished",
            }
            (self.output / "run.json").write_text(json.dumps(data, indent=2))
        if primary:
            raise primary
        return False

    async def connect(self, backend: ConnectionBackend, *, label: str | None = None) -> Device:
        if not self._active:
            raise OwnershipError("run not active")
        configure = getattr(backend, "with_binaries", None)
        if configure is not None:
            backend = configure(self.binaries)
        conn = await self._stack.enter_async_context(backend.connect())
        device = Device(self, conn, label, f"connection-{len(self._devices) + 1:04d}")
        self._devices.append(device)
        return device

    def record(self, ctx: Context, action: str, status: str) -> None:
        self._actions.append(
            {
                "device": ctx.device.id.value,
                "connection": ctx.device.output_name,
                "id": str(ctx.action_id),
                "action": action,
                "status": status,
            }
        )


class Registry:
    """Explicit plugin names are discovery metadata, not device-routing keys."""

    def __init__(self) -> None:
        self._items: dict[str, type[object]] = {}

    def register(self, name: str, implementation: type[object]) -> None:
        if name in self._items:
            raise ValueError(f"duplicate registration: {name}")
        self._items[name] = implementation

    def resolve(self, name: str) -> type[object]:
        return self._items[name]


class Services:
    def __init__(self) -> None:
        self._services: dict[type[object], object] = {}

    def register[T](self, interface: type[T], implementation: T) -> None:
        if interface in self._services:
            raise ValueError("service already registered")
        self._services[interface] = implementation

    def get[T](self, interface: type[T]) -> T:
        if interface not in self._services:
            raise CapabilityUnavailable(f"service unavailable: {interface.__name__}")
        return cast(T, self._services[interface])
