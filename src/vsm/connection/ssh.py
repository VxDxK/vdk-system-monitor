"""AsyncSSH backend with SFTP and separately controlled remote process groups."""

from __future__ import annotations

import asyncio
import shlex
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import asyncssh
from asyncssh import SFTPNoSuchFile, SFTPNoSuchPath

from vsm._cleanup import cleanup
from vsm.binary import BinaryProvider
from vsm.connection._command import shell_command
from vsm.connection._targetctl_transport import SSHChannel, ssh_channel
from vsm.connection.connection import BaseConnection, Connection
from vsm.connection.process import Process
from vsm.connection.targetctl import TargetctlClient, TargetctlOptions
from vsm.models import (
    CapabilityUnavailable,
    Command,
    CommandFailed,
    Completed,
    DeviceId,
    Output,
    OutputLimitExceeded,
    OwnershipError,
    ProcessSet,
    TargetInfo,
    TargetPath,
)


class SSHConnection(BaseConnection):
    def __init__(
        self,
        identity: DeviceId,
        target: TargetInfo,
        client: asyncssh.SSHClientConnection,
        sftp: asyncssh.SFTPClient,
        tmp_dir: str,
        *,
        binaries: BinaryProvider | None = None,
        targetctl: TargetctlOptions | None = None,
    ) -> None:
        super().__init__(identity, target, tmp_dir)
        self.client, self.sftp = client, sftp
        self._targetctl = TargetctlClient(
            self, binaries or BinaryProvider(), targetctl or TargetctlOptions()
        )

    def launch(
        self, command: Command, *, stdout: Output | None = None, stderr: Output | None = None
    ) -> AbstractAsyncContextManager[Process]:
        return self._targetctl.launch(command, stdout=stdout, stderr=stderr)

    async def _targetctl_channel(self, path: TargetPath, mode: str) -> SSHChannel:
        return await ssh_channel(self.client, path, mode)

    async def _bootstrap_run(
        self,
        command: Command,
        *,
        timeout: float | None = None,
        check: bool = True,
        stdout: Output | None = None,
        stderr: Output | None = None,
    ) -> Completed:
        result = await self.client.run(
            shell_command(command), encoding=None, check=False, timeout=timeout
        )
        completed = Completed(
            exit_code=result.exit_status, stdout=result.stdout, stderr=result.stderr
        )
        if check and completed.exit_code:
            raise CommandFailed(completed)
        return completed

    async def discover_processes(self, pattern: str | None = None) -> ProcessSet:
        return await self._targetctl.discover(pattern)

    async def validate_processes(self, processes: ProcessSet) -> None:
        await self._targetctl.validate(processes)

    async def _read_text(self, path: str) -> str:
        if not await self.sftp.exists(path):
            raise FileNotFoundError(path)
        async with self.sftp.open(path, "rb") as stream:
            value = await stream.read(16 * 1024 * 1024 + 1)
            if len(value) > 16 * 1024 * 1024:
                raise OutputLimitExceeded("file read limit exceeded")
            return value.decode()

    async def read_bytes(self, path: TargetPath) -> bytes:
        self.check_path(path)
        if not await self.sftp.exists(str(path)):
            raise FileNotFoundError(path.value)
        async with self.sftp.open(str(path), "rb") as stream:
            data = await stream.read(16 * 1024 * 1024 + 1)
            if len(data) > 16 * 1024 * 1024:
                raise OutputLimitExceeded("file read limit exceeded")
            return data

    async def write_bytes(self, path: TargetPath, data: bytes) -> None:
        self.check_path(path)
        async with self.sftp.open(str(path), "wb") as stream:
            await stream.write(data)

    async def push(self, source: Path, destination: TargetPath) -> None:
        self.check_path(destination)
        if not source.is_file() or source.is_symlink():
            raise ValueError("transfer requires regular file")
        if await self.sftp.exists(str(destination)):
            raise FileExistsError(destination.value)
        await self.sftp.put(str(source), str(destination))

    async def pull(self, source: TargetPath, destination: Path) -> None:
        self.check_path(source)
        if destination.exists():
            raise FileExistsError(destination)
        await self.sftp.get(str(source), str(destination))

    async def glob(self, pattern: str) -> tuple[TargetPath, ...]:
        if self.closed:
            raise OwnershipError("connection is closed")
        try:
            matches = await self.sftp.glob(pattern)
        except SFTPNoSuchFile, SFTPNoSuchPath:
            return ()
        return tuple(self.path(str(p)) for p in sorted(matches))

    async def executable(self, name: str) -> TargetPath:
        result = await self.client.run("command -v " + shlex.quote(name), check=False)
        if result.exit_status != 0:
            raise CapabilityUnavailable(f"executable unavailable: {name}")
        return self.path(str(result.stdout).strip())


@dataclass(frozen=True, kw_only=True)
class SSH:
    """Connect to a Linux target using verified SSH and SFTP.

    Requires SFTP, a POSIX shell, uname, getconf and compatible targetctl artifacts.
    Each connection owns its workspace and processes. Transport loss can leave
    CleanupUnknown and recovery evidence; it does not prove remote cleanup.
    """

    #: Target hostname or IP address.
    destination: str
    #: Optional SSH login name.
    username: str | None = None
    #: Optional SSH login password; prefer supplying secrets at runtime.
    password: str | None = field(default=None, repr=False)
    #: SSH service port.
    port: int = 22
    #: Controller-local private key paths.
    client_keys: tuple[Path, ...] = ()
    #: Optional known-hosts file. None retains normal host-key verification.
    known_hosts: Path | None = None

    binaries: BinaryProvider | None = None
    targetctl: TargetctlOptions = field(default_factory=TargetctlOptions)

    def with_binaries(self, binaries: BinaryProvider) -> SSH:
        return replace(self, binaries=self.binaries if self.binaries is not None else binaries)

    def connect(self) -> AbstractAsyncContextManager[Connection]:
        return self._connect()

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[Connection]:
        options: dict[str, object] = {"port": self.port}
        if self.username:
            options["username"] = self.username
        if self.password is not None:
            options["password"] = self.password
        if self.client_keys:
            options["client_keys"] = [str(p) for p in self.client_keys]
        if self.known_hosts:
            options["known_hosts"] = str(self.known_hosts)
        async with asyncssh.connect(self.destination, **options) as client:
            result = await client.run(
                "uname -s; uname -m; uname -r; getconf CLK_TCK; getconf PAGESIZE; cat /proc/sys/kernel/random/boot_id",
                check=True,
            )
            lines = str(result.stdout).splitlines()
            if len(lines) < 6 or lines[0] != "Linux":
                raise CapabilityUnavailable(
                    "SSH requires Linux, SFTP, a POSIX shell, uname and getconf"
                )
            libc_result = await client.run("getconf GNU_LIBC_VERSION", check=False)
            libc_fields = libc_result.stdout.split()
            target = TargetInfo(
                os="linux",
                kernel_family="linux",
                architecture=lines[1],
                kernel=lines[2],
                abi=libc_fields[0] if len(libc_fields) == 2 else None,
                abi_version=libc_fields[1] if len(libc_fields) == 2 else None,
                clock_ticks_per_second=int(lines[3]),
                page_size_bytes=int(lines[4]),
                boot_id=lines[5],
            )
            host = self.destination
            host = f"[{host}]" if ":" in host and not host.startswith("[") else host
            identity = DeviceId(f"{client.get_extra_info('username')}@{host}:{self.port}")
            async with client.start_sftp_client() as sftp:
                creating = asyncio.create_task(
                    client.run(
                        f"mktemp -d /tmp/vsm-tmp-data-{datetime.now(UTC):%Y%m%dT%H%M%S}-XXXXXXXX",
                        check=True,
                    )
                )
                directory: str | None = None
                conn: SSHConnection | None = None
                try:
                    created = await cleanup(creating)
                    directory = _workspace_path(str(created.stdout).strip())
                    conn = SSHConnection(
                        identity,
                        target,
                        client,
                        sftp,
                        directory,
                        binaries=self.binaries,
                        targetctl=self.targetctl,
                    )
                    yield conn
                finally:
                    # Creation may have finished after cancellation was requested.
                    # Reconcile its result before closing the transport/SFTP scope.
                    if (
                        directory is None
                        and creating.done()
                        and not creating.cancelled()
                        and creating.exception() is None
                    ):
                        directory = _workspace_path(str(creating.result().stdout).strip())
                    try:
                        if conn is not None:
                            await cleanup(conn._close_processes())
                        if directory is not None and (conn is None or not conn._retain_workspace):
                            await cleanup(
                                client.run(
                                    "rm -rf -- " + shlex.quote(directory), check=True, timeout=5
                                )
                            )
                    finally:
                        if conn is not None:
                            conn.closed = True


def _workspace_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        path.parent != PurePosixPath("/tmp")
        or not path.name.startswith("vsm-tmp-data-")
        or any(c.isspace() for c in value)
    ):
        raise CapabilityUnavailable("unexpected SSH workspace path")
    return value
