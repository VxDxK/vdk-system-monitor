"""Private duplex adapters for launching the native helper without recursion."""

import asyncio
import shlex

from vsm.models import TargetPath


class HostChannel:
    def __init__(self, process: asyncio.subprocess.Process):
        self.process = process
        self.stdin, self.stdout, self.stderr = process.stdin, process.stdout, process.stderr

    @property
    def exit_status(self) -> int | None:
        return self.process.returncode

    async def wait_closed(self) -> None:
        await self.process.wait()

    def close(self) -> None:
        self.stdin.close()

    def abort(self) -> None:
        if self.process.returncode is None:
            self.process.kill()


async def host_channel(path: TargetPath, mode: str) -> HostChannel:
    return HostChannel(
        await asyncio.create_subprocess_exec(
            str(path),
            mode,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    )


class SSHChannel:
    def __init__(self, channel):
        self.channel = channel
        self.stdin, self.stdout, self.stderr = channel.stdin, channel.stdout, channel.stderr

    @property
    def exit_status(self):
        return self.channel.exit_status

    async def wait_closed(self):
        await self.channel.wait_closed()

    def close(self):
        self.channel.stdin.write_eof()

    def abort(self):
        self.channel.close()


async def ssh_channel(client, path: TargetPath, mode: str) -> SSHChannel:
    return SSHChannel(
        await client.create_process(
            "exec " + shlex.join((str(path), mode)),
            encoding=None,
        )
    )
