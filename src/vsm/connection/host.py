from __future__ import annotations

import asyncio
import glob as glob_module
import os
import platform
import shutil
import signal
import stat
import tempfile
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from vsm._cleanup import cleanup
from vsm.binary import BinaryProvider
from vsm.connection._streams import Sink
from vsm.connection._targetctl_transport import HostChannel, host_channel
from vsm.connection.connection import BaseConnection, Connection
from vsm.connection.process import Process
from vsm.connection.targetctl import TargetctlClient, TargetctlOptions
from vsm.models import (
    CapabilityUnavailable,
    Command,
    CommandFailed,
    Completed,
    DeviceId,
    Exec,
    Output,
    OutputLimitExceeded,
    OwnershipError,
    ProcessSet,
    TargetInfo,
    TargetPath,
)


class _BootstrapProcess:
    def __init__(self, proc: asyncio.subprocess.Process, stdout: Sink, stderr: Sink):
        self.proc = proc
        self.stdout = stdout
        self.stderr = stderr
        self._completion = asyncio.create_task(self._collect())
        self._stop_called = False

    @property
    def target_pid(self) -> int:
        return self.proc.pid

    async def _drain(self, stream: asyncio.StreamReader | None, sink: Sink) -> None:
        if stream is None:
            return
        while data := await stream.read(65536):
            if sink.file is not None:
                await cleanup(asyncio.to_thread(sink.write, data))
            else:
                sink.write(data)

    async def _collect(self) -> Completed:
        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(self._drain(self.proc.stdout, self.stdout))
                group.create_task(self._drain(self.proc.stderr, self.stderr))
                group.create_task(self.proc.wait())
        except BaseException:
            self._signal(signal.SIGKILL)
            await self.proc.wait()
            raise
        finally:
            self.stdout.close()
            self.stderr.close()
        return Completed(
            exit_code=self.proc.returncode or 0,
            stdout=bytes(self.stdout.data),
            stderr=bytes(self.stderr.data),
            stdout_path=self.stdout.path,
            stderr_path=self.stderr.path,
        )

    def _signal(self, sig: signal.Signals) -> None:
        try:
            os.killpg(self.proc.pid, sig)
        except ProcessLookupError:
            pass

    async def wait(self, *, check: bool = True) -> Completed:
        result = await asyncio.shield(self._completion)
        if check and result.exit_code != 0:
            raise CommandFailed(result)
        return result

    async def stop(self, *, grace: float = 5.0) -> Completed:
        # Signal the owned group even if the leader has exited and descendants hold pipes.
        if not self._stop_called:
            self._stop_called = True
            self._signal(signal.SIGTERM)
        if not self._completion.done():
            try:
                async with asyncio.timeout(grace):
                    return await self.wait(check=False)
            except TimeoutError:
                self._signal(signal.SIGKILL)
        return await self.wait(check=False)


class HostConnection(BaseConnection):
    def __init__(
        self,
        identity: DeviceId,
        target: TargetInfo,
        tmp_dir: str,
        *,
        binaries: BinaryProvider | None = None,
        targetctl: TargetctlOptions | None = None,
    ) -> None:
        super().__init__(identity, target, tmp_dir)
        self._targetctl = TargetctlClient(
            self, binaries or BinaryProvider(), targetctl or TargetctlOptions()
        )

    async def _targetctl_channel(self, path: TargetPath, mode: str) -> HostChannel:
        return await host_channel(path, mode)

    async def _bootstrap_run(
        self,
        command: Command,
        *,
        timeout: float | None = None,
        check: bool = True,
        stdout: Output | None = None,
        stderr: Output | None = None,
    ) -> Completed:
        async with self._bootstrap_launch(command, stdout=stdout, stderr=stderr) as process:
            async with asyncio.timeout(timeout):
                return await process.wait(check=check)

    async def discover_processes(self, pattern: str | None = None) -> ProcessSet:
        return await self._targetctl.discover(pattern)

    async def validate_processes(self, processes: ProcessSet) -> None:
        await self._targetctl.validate(processes)

    def launch(
        self, command: Command, *, stdout: Output | None = None, stderr: Output | None = None
    ) -> AbstractAsyncContextManager[Process]:
        return self._targetctl.launch(command, stdout=stdout, stderr=stderr)

    @asynccontextmanager
    async def _bootstrap_launch(
        self, command: Command, *, stdout: Output | None = None, stderr: Output | None = None
    ) -> AsyncIterator[_BootstrapProcess]:
        if self.closed:
            raise OwnershipError("connection is closed")
        if command.cwd is not None:
            self.check_path(command.cwd)
        args = command.argv if isinstance(command, Exec) else ("/bin/sh", "-c", command.script)
        env = os.environ.copy()
        env.update((item.name, item.value) for item in command.env)
        out = Sink(stdout)
        try:
            err = Sink(stderr)
        except BaseException:
            out.close()
            raise
        proc: _BootstrapProcess | None = None
        try:
            launch = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    *args,
                    cwd=str(command.cwd) if command.cwd else None,
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    stdin=asyncio.subprocess.DEVNULL,
                    start_new_session=True,
                )
            )
            try:
                native = await asyncio.shield(launch)
            except asyncio.CancelledError:
                native = await cleanup(launch)
                proc = _BootstrapProcess(native, out, err)
                await cleanup(proc.stop())
                raise
            proc = _BootstrapProcess(native, out, err)
            self._processes.append(proc)
            yield proc
        finally:
            if proc is not None:
                await cleanup(self._stop_process(proc))
            else:
                out.close()
                err.close()

    async def read_bytes(self, path: TargetPath) -> bytes:
        self.check_path(path)

        def read() -> bytes:
            fd = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise CapabilityUnavailable("read_bytes requires a regular/procfs/sysfs file")
                data = source.read(16 * 1024 * 1024 + 1)
                if len(data) > 16 * 1024 * 1024:
                    raise OutputLimitExceeded("file read limit exceeded")
                return data

        return await cleanup(asyncio.to_thread(read))

    async def write_bytes(self, path: TargetPath, data: bytes) -> None:
        self.check_path(path)

        def write() -> None:
            fd = os.open(str(path), os.O_WRONLY | os.O_NONBLOCK | os.O_CREAT | os.O_TRUNC, 0o666)
            with os.fdopen(fd, "wb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise CapabilityUnavailable("write_bytes requires a regular/procfs/sysfs file")
                stream.write(data)

        await cleanup(asyncio.to_thread(write))

    async def push(self, source: Path, destination: TargetPath) -> None:
        self.check_path(destination)
        await self._copy(source, Path(destination.value))

    async def pull(self, source: TargetPath, destination: Path) -> None:
        self.check_path(source)
        await self._copy(Path(source.value), destination)

    @staticmethod
    async def _copy(source: Path, destination: Path) -> None:
        def copy() -> None:
            if not source.is_file() or source.is_symlink():
                raise ValueError("transfer requires regular file")
            with source.open("rb") as src, destination.open("xb") as dst:
                shutil.copyfileobj(src, dst)

        await cleanup(asyncio.to_thread(copy))

    async def glob(self, pattern: str) -> tuple[TargetPath, ...]:
        if self.closed:
            raise OwnershipError("connection is closed")
        return tuple(
            self.path(p) for p in sorted(await asyncio.to_thread(glob_module.glob, pattern))
        )

    async def executable(self, name: str) -> TargetPath:
        if self.closed:
            raise OwnershipError("connection is closed")
        value = shutil.which(name)
        if value is None:
            raise CapabilityUnavailable(f"executable not found: {name}")
        return self.path(value)


@dataclass(frozen=True, kw_only=True)
class Host:
    """Connect to this Linux host using the regular connection abstraction.

    The backend owns a private temporary workspace and launched processes until
    scope exit. Each action still requires suitable files, tools and permissions.
    """

    binaries: BinaryProvider | None = None
    targetctl: TargetctlOptions = field(default_factory=TargetctlOptions)

    def with_binaries(self, binaries: BinaryProvider) -> Host:
        return replace(self, binaries=self.binaries if self.binaries is not None else binaries)

    def connect(self) -> AbstractAsyncContextManager[Connection]:
        return self._connect()

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[Connection]:
        if platform.system() != "Linux":
            raise CapabilityUnavailable("Host currently supports Linux")
        boot = Path("/proc/sys/kernel/random/boot_id")
        conn = HostConnection(
            DeviceId("host:" + platform.node()),
            TargetInfo(
                os="linux",
                kernel_family="linux",
                architecture=platform.machine(),
                kernel=platform.release(),
                abi=platform.libc_ver()[0] or None,
                abi_version=platform.libc_ver()[1] or None,
                boot_id=boot.read_text().strip() if boot.exists() else None,
                clock_ticks_per_second=os.sysconf("SC_CLK_TCK"),
                page_size_bytes=os.sysconf("SC_PAGESIZE"),
            ),
            tempfile.mkdtemp(prefix=f"vsm-tmp-data-{datetime.now(UTC):%Y%m%dT%H%M%S}-", dir="/tmp"),
            binaries=self.binaries,
            targetctl=self.targetctl,
        )
        try:
            yield conn
        finally:
            try:
                await cleanup(conn._close_processes())
                if not conn._retain_workspace:
                    await cleanup(asyncio.to_thread(shutil.rmtree, str(conn.tmp_dir)))
            finally:
                conn.closed = True
