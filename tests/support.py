"""Deterministic in-memory transport for action lifecycle/command contract tests."""

import asyncio
import fnmatch
from contextlib import asynccontextmanager
from itertools import count
from pathlib import Path

from vsm.connection.connection import BaseConnection
from vsm.models import (
    Completed,
    DeviceId,
    Exec,
    FileOutput,
    OwnershipError,
    ProcessIdentity,
    ProcessSet,
    TargetInfo,
)

_numbers = count(1)


def stat(pid=123, ticks=100, start=99, rss=10):
    fields = ["0"] * 22
    fields[0] = "S"
    fields[11] = str(ticks)
    fields[12] = "0"
    fields[17] = "2"
    fields[19] = str(start)
    fields[21] = str(rss)
    return f"{pid} (service (worker)) " + " ".join(fields) + "\n"


class FakeProcess:
    def __init__(self, output, rup=False):
        self.output = output
        self.rup = rup
        self.stopped = asyncio.Event()

    @property
    def target_pid(self):
        return 123

    async def identity(self):
        return ProcessIdentity(
            device_id=DeviceId("fixture-device"),
            pid=123,
            start_ticks=99,
            boot_id="fixture-boot",
            name="fixture",
        )

    async def processes(self):
        return ProcessSet((await self.identity(),))

    async def wait(self, *, check=True):
        await self.stopped.wait()
        return Completed(exit_code=0)

    async def stop(self, *, grace=5):
        if not self.stopped.is_set() and self.output:
            with self.output.open("ab") as stream:
                stream.write(
                    b'{"record":"summary","timestamp_ms":1000,"sent":0,"received":0,"loss_percent":0,"rtt_min_ms":null,"rtt_mean_ms":null,"rtt_median_ms":null,"rtt_max_ms":null,"rtt_std_dev_ms":null}\n'
                    if self.rup
                    else b'{"type":"end","time_ns":1000000000}\n'
                )
        self.stopped.set()
        return Completed(exit_code=0)


class FakeConnection(BaseConnection):
    def __init__(self):
        super().__init__(
            DeviceId("fixture-device"),
            TargetInfo(
                os="linux",
                architecture="x86_64",
                kernel="fixture",
                clock_ticks_per_second=250,
                page_size_bytes=8192,
                boot_id="fixture-boot",
            ),
            "/tmp/vsm-fixture",
        )
        self.files = {
            "/proc/123/stat": stat().encode(),
            "/proc/123/cmdline": b"service\0",
            "/setting": b"old",
            "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor": b"old",
            "/sys/devices/system/cpu/cpu0/cpufreq/stats/time_in_state": b"1000 10\n",
            "/proc/sys/net/core/rmem_max": b"100",
            "/proc/sys/net/core/wmem_max": b"100",
            "/sys/kernel/tracing/tracing_on": b"0",
            "/sys/kernel/tracing/events/sched/sched_switch/enable": b"0",
            "/proc/net/tcp": b"header\n0: 0100007F:1451 00000000:0000 0A\n",
            "/proc/net/tcp6": b"header\n",
        }
        from dataclasses import MISSING, fields

        from vsm.action.poll_files import PollFiles
        from vsm.action.registry import ACTION_TYPES

        for implementation in ACTION_TYPES.values():
            if issubclass(implementation, PollFiles):
                for descriptor in fields(implementation):
                    if descriptor.name == "paths" and descriptor.default is not MISSING:
                        for pattern in descriptor.default:
                            concrete = pattern.replace("*", "0").replace("?", "0")
                            self.files.setdefault(concrete, b"1\n")
        self.commands = []
        self.fail_write = None

    async def discover_processes(self, pattern=None):
        result = []
        for path in await self.glob("/proc/[0-9]*/stat"):
            try:
                text = (await self.read_bytes(path)).decode()
                pid = int(path.parent.name)
                end = text.rfind(")")
                if end < 0:
                    raise ValueError("malformed stat")
                if pattern is not None and pattern not in (
                    await self.read_bytes(self.path(f"/proc/{pid}/cmdline"))
                ).decode(errors="replace"):
                    continue
                result.append(
                    ProcessIdentity(
                        device_id=self.device_id,
                        pid=pid,
                        start_ticks=int(text[end + 2 :].split()[19]),
                        boot_id=self.target.boot_id,
                        name=text[text.find("(") + 1 : end],
                    )
                )
            except FileNotFoundError, ProcessLookupError:
                continue
        return ProcessSet(result)

    async def validate_processes(self, processes):
        for process in processes:
            if process.device_id != self.device_id:
                raise OwnershipError("process belongs to another device")
            if process.boot_id and self.target.boot_id and process.boot_id != self.target.boot_id:
                raise OwnershipError("selected process belongs to another boot")
            text = (await self.read_bytes(self.path(f"/proc/{process.pid}/stat"))).decode()
            if int(text[text.rfind(")") + 2 :].split()[19]) != process.start_ticks:
                raise OwnershipError("selected PID has been reused")

    async def run(self, command, *, check=True, stdout=None, stderr=None):
        self.commands.append(command)
        if isinstance(command, Exec) and command.argv[:2] == ("mktemp", "-d"):
            return Completed(
                exit_code=0,
                stdout=(command.argv[2].replace("XXXXXXXX", str(next(_numbers)))).encode(),
            )
        if isinstance(command, Exec) and command.argv[:3] == ("rm", "-rf", "--"):
            prefix = command.argv[3] + "/"
            self.files = {
                key: value for key, value in self.files.items() if not key.startswith(prefix)
            }
        data = (
            b'[{"ifname":"eth0","addr_info":[{"local":"127.0.0.1","family":"inet"}]}]'
            if isinstance(command, Exec) and "-j" in command.argv
            else b"ok"
        )
        if isinstance(stdout, FileOutput):
            stdout.path.write_bytes(data)
        if isinstance(stderr, FileOutput):
            stderr.path.write_bytes(b"real 0.1\nuser 0.05\nsys 0.01\n")
        return Completed(exit_code=0, stdout=data)

    @asynccontextmanager
    async def launch(self, command, *, stdout=None, stderr=None):
        self.commands.append(command)
        output = stdout.path if isinstance(stdout, FileOutput) else None
        rup = isinstance(command, Exec) and any(
            item.name == "VSM_RUP_READY_FILE" for item in command.env
        )
        if rup:
            marker = next(item.value for item in command.env if item.name == "VSM_RUP_READY_FILE")
            self.files[marker] = b"ready\n"
        if rup and output:
            output.write_text(
                '{"record":"metadata","schema":"rup.ping","version":3,"started_at_ms":0,"protocol":"udp"}\n'
            )
        elif output:
            schema = "pspider" if Path(command.argv[0]).name == "pspider" else "rfpoll"
            output.write_text(
                f'{{"type":"metadata","schema":"{schema}","time_ns":0}}\n{{"type":"ready","time_ns":0}}\n'
            )
        if isinstance(stderr, FileOutput):
            stderr.path.write_bytes(b"")
        process = FakeProcess(output, rup=rup)
        try:
            yield process
        finally:
            await process.stop()

    async def read_bytes(self, path):
        self.check_path(path)
        if str(path) not in self.files:
            raise FileNotFoundError(path.value)
        return self.files[str(path)]

    async def write_bytes(self, path, data):
        self.check_path(path)
        if str(path) == self.fail_write:
            raise PermissionError("injected write failure")
        self.files[str(path)] = data

    async def glob(self, pattern):
        return tuple(self.path(p) for p in sorted(self.files) if fnmatch.fnmatch(p, pattern))

    async def executable(self, name):
        return self.path("/bin/" + name.rsplit("/", 1)[-1])

    async def push(self, source, destination):
        self.files[str(destination)] = source.read_bytes()

    async def pull(self, source, destination):
        destination.write_bytes(await self.read_bytes(source))


class TestContext:
    def __init__(self, connection, directory):
        from vsm.models import ActionId

        self.connection = connection
        self.action_id = ActionId(f"action-{next(_numbers):06d}")
        self.directory = Path(directory) / str(self.action_id)
        self.directory.mkdir()
        self.workers = []

    def output_path(self, name):
        return self.directory / name

    async def binary(self, name):
        return self.connection.path("/bin/" + name)

    async def deploy(self, source):
        return self.connection.path(str(source.resolve()))

    def service(self, interface):
        raise LookupError(interface)

    def supervise(self, coroutine):
        self.workers.append(asyncio.create_task(coroutine))

    async def join(self):
        for task in self.workers:
            try:
                await task
            except asyncio.CancelledError:
                pass
