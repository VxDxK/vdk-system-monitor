from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from pathlib import Path, PurePosixPath
from typing import Protocol

from vsm.clock import ClockCalibration, ClockTracker, TargetClock
from vsm.connection.process import Process
from vsm.models import (
    Command,
    Completed,
    DeviceId,
    Output,
    OwnershipError,
    ProcessSet,
    TargetInfo,
    TargetPath,
)


class Connection(Protocol):
    async def sample_clock(self) -> TargetClock: ...

    async def calibrate_clock(self) -> ClockCalibration: ...

    @property
    def device_id(self) -> DeviceId: ...

    @property
    def target(self) -> TargetInfo: ...

    @property
    def tmp_dir(self) -> TargetPath: ...

    def path(self, value: str) -> TargetPath: ...

    async def run(
        self,
        command: Command,
        *,
        check: bool = True,
        stdout: Output | None = None,
        stderr: Output | None = None,
    ) -> Completed: ...

    def launch(
        self, command: Command, *, stdout: Output | None = None, stderr: Output | None = None
    ) -> AbstractAsyncContextManager[Process]: ...

    async def discover_processes(self, pattern: str | None = None) -> ProcessSet: ...

    async def validate_processes(self, processes: ProcessSet) -> None: ...

    async def read_bytes(self, path: TargetPath) -> bytes: ...

    async def write_bytes(self, path: TargetPath, data: bytes) -> None: ...

    async def push(self, source: Path, destination: TargetPath) -> None: ...

    async def pull(self, source: TargetPath, destination: Path) -> None: ...

    async def glob(self, pattern: str) -> tuple[TargetPath, ...]: ...

    async def executable(self, name: str) -> TargetPath: ...


class ConnectionBackend(Protocol):
    def connect(self) -> AbstractAsyncContextManager[Connection]: ...


class BaseConnection:
    def __init__(self, identity: DeviceId, target: TargetInfo, tmp_dir: str):
        self._identity = identity
        self._target = target
        self.closed = False
        self._tmp_dir = self.path(tmp_dir)
        self._processes: list[Process] = []
        self._retain_workspace = False
        self._clock_tracker = ClockTracker(self)

    async def sample_clock(self) -> TargetClock:
        raise NotImplementedError

    async def calibrate_clock(self) -> ClockCalibration:
        return await self._clock_tracker.refresh()

    @property
    def device_id(self) -> DeviceId:
        return self._identity

    @property
    def target(self) -> TargetInfo:
        return self._target

    @property
    def tmp_dir(self) -> TargetPath:
        return self._tmp_dir

    async def _stop_process(self, process: Process) -> None:
        try:
            await process.stop()
        except BaseException as error:
            self._retain_workspace = True
            error.add_note(f"Recovery workspace retained: {self.tmp_dir}")
            raise
        finally:
            # The launch scope already surfaced this cleanup result. Do not replay
            # its failure when the backend closes; retain recovery files instead.
            if process in self._processes:
                self._processes.remove(process)

    async def _close_processes(self) -> None:
        errors: list[BaseException] = []
        for process in tuple(reversed(self._processes)):
            try:
                await self._stop_process(process)
            except BaseException as error:  # noqa: BLE001 -- Include cancellation and failures from user-defined code.
                errors.append(error)
        if errors:
            raise BaseExceptionGroup("connection process cleanup failed", errors)

    def path(self, value: str) -> TargetPath:
        if "\x00" in value:
            raise ValueError("path contains NUL")
        return TargetPath(self.device_id, PurePosixPath(value))

    def check_path(self, path: TargetPath) -> None:
        if self.closed:
            raise OwnershipError("connection is closed")
        if path.device_id != self.device_id:
            raise OwnershipError("path belongs to another device")

    async def run(
        self,
        command: Command,
        *,
        check: bool = True,
        stdout: Output | None = None,
        stderr: Output | None = None,
    ) -> Completed:
        async with self.launch(command, stdout=stdout, stderr=stderr) as proc:
            return await proc.wait(check=check)

    def launch(
        self, command: Command, *, stdout: Output | None = None, stderr: Output | None = None
    ) -> AbstractAsyncContextManager[Process]:
        raise NotImplementedError
