"""Immutable public configuration, identities, and measurement references."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import overload

from vsm.clock import ClockCalibration, TimeOrigin


@dataclass(frozen=True)
class DeviceId:
    value: str

    def __str__(self) -> str:
        return self.value

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not self.value or "\0" in self.value:
            raise ValueError("identity must be a nonempty string without NUL")


@dataclass(frozen=True)
class ActionId:
    value: str

    def __str__(self) -> str:
        return self.value

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not self.value or "\0" in self.value:
            raise ValueError("identity must be a nonempty string without NUL")


@dataclass(frozen=True, kw_only=True)
class TargetInfo:
    os: str
    architecture: str
    kernel: str
    abi: str | None = None
    abi_version: str | None = None
    boot_id: str | None = None
    clock_ticks_per_second: int | None = None
    page_size_bytes: int | None = None
    kernel_family: str | None = None
    cpu_features: tuple[str, ...] | None = None
    api_level: int | None = None
    available_libraries: tuple[str, ...] | None = None
    interpreter: str | None = None


@dataclass(frozen=True)
class TargetPath:
    device_id: DeviceId
    value: PurePosixPath

    def __str__(self) -> str:
        return str(self.value)

    @property
    def name(self) -> str:
        return self.value.name

    @property
    def parent(self) -> TargetPath:
        return TargetPath(self.device_id, self.value.parent)

    def __truediv__(self, operand: str | PurePosixPath) -> TargetPath:
        if not isinstance(operand, str | PurePosixPath):
            raise TypeError("target path operand must be str or PurePosixPath")
        return TargetPath(self.device_id, self.value / operand)


@dataclass(frozen=True)
class EnvVar:
    name: str
    value: str

    def __post_init__(self) -> None:
        if not self.name or "=" in self.name or "\0" in self.name + self.value:
            raise ValueError("invalid environment variable")


@dataclass(frozen=True, kw_only=True)
class Exec:
    argv: tuple[str, ...]
    cwd: TargetPath | None = None
    env: tuple[EnvVar, ...] = ()

    def __post_init__(self) -> None:
        if not self.argv or not self.argv[0] or any("\0" in a for a in self.argv):
            raise ValueError("argv must have an executable and contain no NUL")


@dataclass(frozen=True, kw_only=True)
class Shell:
    script: str
    cwd: TargetPath | None = None
    env: tuple[EnvVar, ...] = ()

    def __post_init__(self) -> None:
        if "\0" in self.script:
            raise ValueError("shell script contains NUL")


type Command = Exec | Shell


@dataclass(frozen=True, kw_only=True)
class CaptureOutput:
    limit_bytes: int = 1_048_576

    def __post_init__(self) -> None:
        if self.limit_bytes <= 0:
            raise ValueError("capture limit must be positive")


@dataclass(frozen=True)
class FileOutput:
    path: Path


@dataclass(frozen=True)
class DiscardOutput:
    pass


type Output = CaptureOutput | FileOutput | DiscardOutput


@dataclass(frozen=True, kw_only=True)
class Completed:
    exit_code: int
    stdout: bytes = b""
    stderr: bytes = b""
    stdout_path: Path | None = None
    stderr_path: Path | None = None


@dataclass(frozen=True, kw_only=True)
class Schema[Record]:
    name: str


@dataclass(frozen=True, kw_only=True)
class LogMetadata:
    target: TargetInfo
    provider: str
    interval_seconds: float | None = None
    warnings: tuple[str, ...] = ()
    clock: ClockCalibration | None = None
    time_origin: TimeOrigin | None = None


@dataclass(frozen=True, kw_only=True)
class LogRef[Record]:
    path: Path
    schema: Schema[Record]
    device_id: DeviceId
    action_id: ActionId
    complete: bool
    metadata: LogMetadata


@dataclass(frozen=True, kw_only=True)
class ProcessIdentity:
    device_id: DeviceId
    pid: int
    start_ticks: int
    boot_id: str | None
    name: str


@dataclass(frozen=True, init=False)
class ProcessSet(Sequence[ProcessIdentity]):
    """An immutable, ordered selection of processes; duplicates are preserved."""

    _items: tuple[ProcessIdentity, ...]

    def __init__(self, items: Iterable[ProcessIdentity] = (), /) -> None:
        object.__setattr__(self, "_items", tuple(items))

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[ProcessIdentity]:
        return iter(self._items)

    @overload
    def __getitem__(self, index: int) -> ProcessIdentity: ...

    @overload
    def __getitem__(self, index: slice) -> ProcessSet: ...

    def __getitem__(self, index: int | slice) -> ProcessIdentity | ProcessSet:
        if isinstance(index, slice):
            return ProcessSet(self._items[index])
        return self._items[index]


@dataclass(frozen=True, kw_only=True)
class InterfaceAddress:
    interface: str
    address: str
    family: str


class MonitorError(Exception):
    pass


class CapabilityUnavailable(MonitorError):
    pass


class OwnershipError(MonitorError):
    pass


class OutputLimitExceeded(MonitorError):
    pass


class CleanupUnknown(MonitorError):
    pass


class CommandFailed(MonitorError):
    def __init__(self, result: Completed):
        self.result = result
        super().__init__(
            f"command exited {result.exit_code}: {result.stderr.decode(errors='replace')}"
        )


class TargetctlProtocolError(MonitorError):
    """The target operation helper returned an incompatible or malformed stream."""
