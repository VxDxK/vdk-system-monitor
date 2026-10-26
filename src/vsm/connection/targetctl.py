"""Typed configuration and JSONL client for the target-local operation helper."""

from __future__ import annotations

import asyncio
import base64
import binascii
import math
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import asdict, dataclass
from itertools import count

import asyncssh

from vsm._cleanup import cleanup
from vsm.binary import BinaryProvider
from vsm.connection._streams import Sink
from vsm.connection._targetctl_protocol import (
    MAX_ITEMS,
    MAX_LINE,
    _diagnostics,
    _encode,
    _error,
    _events,
    _identity,
)
from vsm.connection.process import Process
from vsm.models import (
    CleanupUnknown,
    Command,
    CommandFailed,
    Completed,
    Exec,
    Output,
    OutputLimitExceeded,
    OwnershipError,
    ProcessIdentity,
    ProcessSet,
    TargetctlProtocolError,
    TargetPath,
)


@dataclass(frozen=True, kw_only=True)
class TargetctlOptions:
    startup_timeout: float = 5.0
    renewal_interval: float = 5.0
    lease_timeout: float = 30.0
    disconnect_grace: float = 5.0

    def __post_init__(self) -> None:
        for value in asdict(self).values():
            if (
                isinstance(value, bool)
                or not isinstance(value, (float, int))
                or not math.isfinite(value)
                or not 0 < value <= 86400
            ):
                raise ValueError("targetctl times must be finite, positive seconds <= 86400")
        if self.renewal_interval >= self.lease_timeout:
            raise ValueError("renewal_interval must be shorter than lease_timeout")


class _Bootstrap:
    """Provider-facing adapter; its run path cannot recursively supervise deployment."""

    def __init__(self, connection):
        self.connection = connection

    def __getattr__(self, name):
        return getattr(self.connection, name)

    async def run(self, command, **kwargs):
        return await self.connection._bootstrap_run(command, **kwargs)


class TargetctlClient:
    def __init__(self, connection, binaries: BinaryProvider, options: TargetctlOptions):
        self.connection, self.binaries, self.options = connection, binaries, options
        self._path: TargetPath | None = None
        self._lock = asyncio.Lock()
        self._numbers = count(1)

    async def path(self) -> TargetPath:
        async with self._lock:
            if self._path is None:
                self._path = await self.binaries.resolve(_Bootstrap(self.connection), "targetctl")
            return self._path

    async def request(self, operation: str, params: dict) -> list[dict]:
        path = await self.path()
        encoded = _encode({"id": 1, "operation": operation, "params": params})
        async with asyncio.timeout(self.options.startup_timeout):
            channel = await self.connection._targetctl_channel(path, "request")
        diagnostics = asyncio.create_task(_diagnostics(channel))
        try:
            items = []
            terminal = False
            async with asyncio.timeout(self.options.startup_timeout):
                channel.stdin.write(encoded)
                await channel.stdin.drain()
                channel.stdin.write_eof()
                async for event in _events(channel):
                    if event["id"] != 1 or event.get("operation") != operation or terminal:
                        raise TargetctlProtocolError("unexpected targetctl response")
                    if event["event"] == "error":
                        raise _error(event)
                    if event["event"] == "item":
                        if len(items) >= MAX_ITEMS:
                            raise TargetctlProtocolError("too many targetctl items")
                        items.append(event["result"])
                    elif event["event"] == "result":
                        terminal = True
                        if operation == "capabilities":
                            items.append(event["result"])
                    else:
                        raise TargetctlProtocolError("unexpected request event")
                await channel.wait_closed()
                if not terminal or channel.exit_status != 0:
                    raise TargetctlProtocolError("targetctl request ended without success")
            return items
        finally:

            async def close_request():
                channel.close()
                try:
                    async with asyncio.timeout(2):
                        await channel.wait_closed()
                except TimeoutError:
                    channel.abort()
                    async with asyncio.timeout(2):
                        await channel.wait_closed()
                finally:
                    diagnostics.cancel()
                    await asyncio.gather(diagnostics, return_exceptions=True)

            await cleanup(close_request())

    async def discover(self, pattern: str | None = None) -> ProcessSet:
        return ProcessSet(
            _identity(self.connection, value)
            for value in await self.request("discover", {"pattern": pattern})
        )

    async def validate(self, processes: ProcessSet) -> None:
        for process in processes:
            if process.device_id != self.connection.device_id:
                raise OwnershipError("process belongs to another device")
            if (
                process.boot_id
                and self.connection.target.boot_id
                and process.boot_id != self.connection.target.boot_id
            ):
                raise OwnershipError("selected process belongs to another boot")
        if len(processes) > MAX_ITEMS:
            raise ValueError("too many selected processes")
        # Bound each frame independently while preserving order and duplicates.
        batch = []
        size = 128
        for process in processes:
            value = {k: v for k, v in asdict(process).items() if k != "device_id"}
            value_size = len(_encode(value))
            if size + value_size > MAX_LINE // 2 and batch:
                await self.request("validate", {"processes": batch})
                batch, size = [], 128
            batch.append(value)
            size += value_size
        if batch:
            await self.request("validate", {"processes": batch})

    def launch(
        self, command: Command, *, stdout: Output | None = None, stderr: Output | None = None
    ) -> AbstractAsyncContextManager[Process]:
        return self._launch(command, stdout=stdout, stderr=stderr)

    @asynccontextmanager
    async def _launch(
        self, command: Command, *, stdout: Output | None, stderr: Output | None
    ) -> AsyncIterator[Process]:
        if self.connection.closed:
            raise OwnershipError("connection is closed")
        if command.cwd:
            self.connection.check_path(command.cwd)
        path = await self.path()
        out = Sink(stdout)
        try:
            err = Sink(stderr)
        except BaseException:
            out.close()
            raise
        process = None
        channel = None

        async def prepare():
            nonlocal process, channel
            async with asyncio.timeout(self.options.startup_timeout):
                channel = await self.connection._targetctl_channel(path, "supervise")
                directory = self.connection.tmp_dir / f"targetctl-{next(self._numbers):08d}"
                process = TargetctlProcess(self, channel, directory, out, err)
                self.connection._processes.append(process)
                await process.start(
                    {
                        "argv": command.argv
                        if isinstance(command, Exec)
                        else ("/bin/sh", "-c", command.script),
                        "env": {entry.name: entry.value for entry in command.env},
                        "cwd": str(command.cwd) if command.cwd else None,
                        "directory": str(directory),
                        "lease_seconds": self.options.lease_timeout,
                        "grace_seconds": self.options.disconnect_grace,
                    }
                )

        starting = asyncio.create_task(prepare())
        try:
            try:
                await asyncio.shield(starting)
            except asyncio.CancelledError:
                await cleanup(starting)
                raise
            yield process
        finally:
            try:
                if process is not None:
                    await cleanup(self.connection._stop_process(process))
                elif channel is not None:
                    channel.close()
                    await cleanup(channel.wait_closed())
            finally:
                out.close()
                err.close()


class TargetctlProcess:
    def __init__(
        self, client: TargetctlClient, channel, directory: TargetPath, out: Sink, err: Sink
    ):
        self.client, self.channel, self.directory = client, channel, directory
        self.out, self.err = out, err
        self._pinned: ProcessIdentity | None = None
        loop = asyncio.get_running_loop()
        self._ready = loop.create_future()
        self._completion = loop.create_future()
        self._cleaned = loop.create_future()
        for future in (self._ready, self._completion, self._cleaned):
            future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        self._pending = {}
        self._ids = count(2)
        self._write_lock = asyncio.Lock()
        self._stop_lock = asyncio.Lock()
        self._output_error: Exception | None = None
        self._closed = False
        self._reader = asyncio.create_task(self._read())
        self._diagnostic = asyncio.create_task(_diagnostics(channel))
        self._renewal = asyncio.create_task(self._renew())

    @property
    def target_pid(self) -> int | None:
        return self._pinned.pid if self._pinned else None

    async def _send(self, operation: str, params: dict, request_id: int) -> None:
        encoded = _encode({"id": request_id, "operation": operation, "params": params})
        async with self._write_lock:
            self.channel.stdin.write(encoded)
            await self.channel.stdin.drain()

    async def start(self, params: dict) -> None:
        async with asyncio.timeout(self.client.options.startup_timeout):
            await self._send("launch", params, 1)
            await asyncio.shield(self._ready)

    async def _renew(self) -> None:
        try:
            while not self._cleaned.done():
                await asyncio.sleep(self.client.options.renewal_interval)
                await self._send("renew", {}, next(self._ids))
        except OSError, asyncssh.Error:
            # The event reader reports missing cleanup; the remote lease remains authoritative.
            return

    async def _read(self) -> None:
        try:
            async for event in _events(self.channel):
                kind, request_id = event["event"], event["id"]
                value = event.get("result", {})
                if request_id != 1:
                    pending = self._pending.pop(request_id, None)
                    if pending is None or kind not in {"result", "error"}:
                        raise TargetctlProtocolError("unexpected supervisor response ID")
                    if kind == "error":
                        pending.set_exception(_error(event))
                    else:
                        pending.set_result(value)
                    continue
                if event.get("operation") != "launch":
                    raise TargetctlProtocolError("unexpected supervisor operation")
                if kind == "launched" and not self._ready.done():
                    self._pinned = _identity(self.client.connection, value.get("workload"))
                    if (
                        type(value.get("supervisor_pid")) is not int
                        or value["supervisor_pid"] == self.target_pid
                    ):
                        raise TargetctlProtocolError("invalid supervisor/workload identity")
                    self._ready.set_result(None)
                elif kind == "output" and self._pinned is not None and not self._completion.done():
                    if value.get("stream") not in {"stdout", "stderr"} or not isinstance(
                        value.get("data"), str
                    ):
                        raise TargetctlProtocolError("invalid output event")
                    try:
                        data = base64.b64decode(value["data"], validate=True)
                    except (ValueError, binascii.Error) as error:
                        raise TargetctlProtocolError("invalid output encoding") from error
                    if len(data) > 32768:
                        raise TargetctlProtocolError("output chunk exceeds limit")
                    if self._output_error is None:
                        sink = self.out if value["stream"] == "stdout" else self.err
                        try:
                            if sink.file is not None:
                                await cleanup(asyncio.to_thread(sink.write, data))
                            else:
                                sink.write(data)
                        except (OSError, ValueError, RuntimeError, OutputLimitExceeded) as error:
                            self._output_error = error
                            await self._send("stop", {"grace_seconds": 0}, self._pending_id())
                elif (
                    kind == "completed" and self._pinned is not None and not self._completion.done()
                ):
                    if (
                        type(value.get("exit_code")) is not int
                        or type(value.get("output_complete")) is not bool
                    ):
                        raise TargetctlProtocolError("invalid completion")
                    if self._output_error:
                        self._completion.set_exception(
                            ExceptionGroup("workload output failed", [self._output_error])
                        )
                    elif not value["output_complete"]:
                        self._completion.set_exception(
                            CleanupUnknown("workload output is incomplete")
                        )
                    else:
                        self._completion.set_result(
                            Completed(
                                exit_code=value["exit_code"],
                                stdout=bytes(self.out.data),
                                stderr=bytes(self.err.data),
                                stdout_path=self.out.path,
                                stderr_path=self.err.path,
                            )
                        )
                elif kind == "cleanup" and not self._cleaned.done():
                    if value.get("cleanup") != "confirmed" or value.get("scope") != "process_group":
                        raise CleanupUnknown("process group cleanup unconfirmed")
                    self._cleaned.set_result(None)
                    if (
                        not self._completion.done()
                        and self._ready.done()
                        and self._ready.exception()
                    ):
                        self._completion.set_exception(self._ready.exception())
                elif kind == "error" and not self._ready.done():
                    self._ready.set_exception(_error(event))
                    if event.get("cleanup") == "confirmed":
                        self._cleaned.set_result(None)
                else:
                    raise TargetctlProtocolError("invalid supervisor lifecycle ordering")
            for pending in self._pending.values():
                if not pending.done():
                    pending.set_exception(ProcessLookupError("supervisor session has ended"))
            self._pending.clear()
            if not self._cleaned.done():
                diagnostic = (await self._diagnostic).decode(errors="replace")
                raise CleanupUnknown(
                    f"targetctl disconnected; recovery workspace {self.directory}; {diagnostic}"
                )
        except BaseException as error:  # noqa: BLE001 -- Propagate reader failure to every waiter.
            for future in (self._ready, self._completion, self._cleaned, *self._pending.values()):
                if not future.done():
                    future.set_exception(error)
            self._pending.clear()
            self._renewal.cancel()

    def _pending_id(self):
        request_id = next(self._ids)
        future = asyncio.get_running_loop().create_future()
        future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        self._pending[request_id] = future
        return request_id

    async def identity(self) -> ProcessIdentity:
        value = await self._request("identity", {})
        identity = _identity(self.client.connection, value)
        if (
            self._pinned is None
            or identity.pid != self._pinned.pid
            or identity.start_ticks != self._pinned.start_ticks
            or (
                self._pinned.boot_id
                and identity.boot_id
                and self._pinned.boot_id != identity.boot_id
            )
        ):
            raise OwnershipError("workload identity changed")
        return identity

    async def processes(self) -> ProcessSet:
        return ProcessSet((await self.identity(),))

    async def _request(self, operation: str, params: dict) -> dict:
        if self._closed or self._reader.done():
            raise ProcessLookupError("supervised workload is no longer available")
        request_id = next(self._ids)
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            async with asyncio.timeout(self.client.options.startup_timeout):
                await self._send(operation, params, request_id)
                return await asyncio.shield(future)
        finally:
            # Retain timed-out requests so late replies cannot be mistaken for new work.
            future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)

    async def wait(self, *, check: bool = True) -> Completed:
        result = await asyncio.shield(self._completion)
        if check and result.exit_code:
            raise CommandFailed(result)
        return result

    async def stop(self, *, grace: float = 5.0) -> Completed:
        return await cleanup(self._stop(grace=grace))

    async def _stop(self, *, grace: float) -> Completed:
        if isinstance(grace, bool) or not math.isfinite(grace) or not 0 <= grace <= 86400:
            raise ValueError("grace must be finite seconds between 0 and 86400")
        async with self._stop_lock:
            if not self._closed:
                try:
                    async with asyncio.timeout(grace + self.client.options.startup_timeout + 3):
                        if not self._cleaned.done():
                            await self._send("stop", {"grace_seconds": grace}, self._pending_id())
                        await asyncio.shield(self._cleaned)
                        await self.channel.wait_closed()
                except BaseException as error:
                    self.client.connection._retain_workspace = True
                    if isinstance(error, asyncio.CancelledError):
                        raise
                    raise CleanupUnknown(
                        f"cleanup unknown; recovery workspace {self.directory}: {error}"
                    ) from error
                finally:
                    self._closed = True
                    self._renewal.cancel()
                    await asyncio.gather(self._renewal, return_exceptions=True)
                    self.channel.close()
                    try:
                        async with asyncio.timeout(self.client.options.disconnect_grace + 3):
                            await self.channel.wait_closed()
                            await self._reader
                            await self._diagnostic
                    except TimeoutError:
                        self.channel.abort()
                        self._reader.cancel()
                        self._diagnostic.cancel()
                        await asyncio.gather(self._reader, self._diagnostic, return_exceptions=True)
                        raise CleanupUnknown(
                            f"helper shutdown unconfirmed: {self.directory}"
                        ) from None
                # Remove only this acknowledged session; unknown outcomes retain evidence.
                try:
                    await self.client.connection._bootstrap_run(
                        Exec(argv=("rm", "-rf", "--", str(self.directory))),
                        timeout=5,
                    )
                except OSError, CommandFailed:
                    self.client.connection._retain_workspace = True
                    raise
            if not self._completion.done():
                if self._ready.done() and self._ready.exception():
                    raise self._ready.exception()
                raise CleanupUnknown("supervisor did not return workload completion")
            return await self.wait(check=False)
