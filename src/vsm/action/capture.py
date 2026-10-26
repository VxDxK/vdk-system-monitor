from __future__ import annotations

from vsm.connection.process import Process
from vsm.models import CapabilityUnavailable, Completed, LogRef, OwnershipError, ProcessSet


class Capture[R]:
    def __init__(self) -> None:
        self._log: LogRef[R] | None = None
        self._process: Process | None = None

    def attach(self, process: Process) -> None:
        self._process = process

    async def processes(self) -> ProcessSet:
        if self._process is None:
            raise CapabilityUnavailable("capture has no attached process")
        return await self._process.processes()

    async def wait(self) -> Completed:
        if self._process is None:
            raise CapabilityUnavailable("capture has no finite process completion")
        return await self._process.wait()

    def finalize(self, log: LogRef[R]) -> None:
        self._log = log

    @property
    def log(self) -> LogRef[R]:
        if self._log is None:
            raise OwnershipError("capture is not finalized")
        return self._log
