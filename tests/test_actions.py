import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from support import FakeConnection, TestContext

from vsm.action.capture import Capture
from vsm.action.collect_for import CollectFor
from vsm.action.copy_file import CopyFile
from vsm.action.cpu_freq import CpuFreq
from vsm.action.cpu_freq_governor import CpuFreqGovernor
from vsm.action.fio import Fio
from vsm.action.ftrace import Ftrace
from vsm.action.iperf import Iperf
from vsm.action.launch import Launch
from vsm.action.periodic_launcher import PeriodicLauncher
from vsm.action.pid_stat import PidStat
from vsm.action.ping import Ping
from vsm.action.poll_files import PollFiles
from vsm.action.proc_stat import ProcStat
from vsm.action.process_io import ProcessIo
from vsm.action.pull import Pull
from vsm.action.push import Push
from vsm.action.registry import ACTION_TYPES
from vsm.action.rup import Rup
from vsm.action.scan_processes import ScanProcesses
from vsm.action.set_file import SetFile
from vsm.action.shell import ShellAction
from vsm.action.sleep import Sleep
from vsm.action.smaps import Smaps
from vsm.action.socket_buffer_limit import SocketBufferLimit
from vsm.action.strace import Strace
from vsm.action.traffic_generator import TrafficGenerator
from vsm.action.wifi_conn_info import WifiConnInfo
from vsm.models import (
    CapabilityUnavailable,
    DeviceId,
    Exec,
    OwnershipError,
    ProcessIdentity,
    ProcessSet,
)
from vsm.util.network import Endpoint
from vsm.util.records import FileRecord


class ActionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.conn = FakeConnection()

    async def asyncTearDown(self):
        self.temp.cleanup()

    def context(self):
        return TestContext(self.conn, self.temp.name)

    async def test_all_50_action_implementations(self):
        ctx = self.context()
        processes = await ScanProcesses().run(ctx)
        source = Path(self.temp.name) / "source"
        source.write_bytes(b"payload")
        factories = {
            "launch": Launch(argv=("sleep", "10")),
            "periodic_launcher": PeriodicLauncher(argv=("echo", "value"), interval=10),
            "poll_files": PollFiles(paths=("/proc/stat",)),
            "pull": Pull(
                source=self.conn.path("/setting"), destination=Path(self.temp.name) / "pulled"
            ),
            "push": Push(source=source, destination=self.conn.path("/uploaded")),
            "shell": ShellAction(script="true"),
            "sleep": Sleep(duration=0),
            "copy_file": CopyFile(
                source=self.conn.path("/setting"), destination=self.conn.path("/copy")
            ),
            "cpu_freq_governor": CpuFreqGovernor(governor="performance"),
            "fio": Fio(args=("--name=fixture", "--size=4096", "--rw=read")),
            "ftrace": Ftrace(events=("sched/sched_switch",)),
            "iperf": Iperf(host="127.0.0.1"),
            "pid_stat": PidStat(processes=processes),
            "process_io": ProcessIo(processes=processes),
            "ping": Ping(destination="127.0.0.1"),
            "rup": Rup(address=Endpoint.parse("127.0.0.1:12345")),
            "set_file": SetFile(path=self.conn.path("/setting"), value=b"new"),
            "smaps": Smaps(processes=processes),
            "socket_buffer_limit": SocketBufferLimit(bytes=12345),
            "strace": Strace(args=("-p", "123")),
            "traffic_generator": TrafficGenerator(address=Endpoint.parse("127.0.0.1:12345")),
            "wifi_conn_info": WifiConnInfo(interface="wlan0", interval=10),
        }
        self.assertEqual(len(ACTION_TYPES), 50)
        for name, implementation in ACTION_TYPES.items():
            with self.subTest(action=name):
                action = factories.get(name)
                if action is None:
                    action = implementation()
                ctx = self.context()
                if not hasattr(action, "open"):
                    if name == "manual":
                        with tempfile.TemporaryFile(mode="w+") as stream:
                            stream.write("\n")
                            stream.seek(0)
                            with patch("sys.stdin", stream), patch("sys.stdout"):
                                await action.run(ctx)
                    else:
                        await action.run(ctx)
                else:
                    async with action.open(ctx) as value:
                        await asyncio.sleep(0)
                    if isinstance(value, Capture):
                        self.assertTrue(value.log.complete)
                        self.assertTrue(value.log.path.exists())
                await ctx.join()
        self.assertEqual(self.conn.files["/setting"], b"old")
        self.assertEqual(
            self.conn.files["/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"], b"old"
        )
        commands = [c.argv for c in self.conn.commands if isinstance(c, Exec)]
        fio = next(c for c in commands if c[0] == "/bin/fio")
        self.assertIn("--name=fixture", fio)
        self.assertIn("--output-format=json", fio)
        self.assertTrue(any("--process" in c for c in commands))

    async def test_process_discovery_and_pid_reuse(self):
        processes = await ScanProcesses(pattern="service").run(self.context())
        self.assertEqual(processes[0].start_ticks, 99)
        self.assertEqual(processes[0].name, "service (worker)")
        from support import stat

        self.conn.files["/proc/123/stat"] = stat(start=100).encode()
        with self.assertRaises(OwnershipError):
            async with PidStat(processes=processes).open(self.context()):
                pass

    async def test_cpu_freq_resolves_only_existing_complete_paths(self):
        self.conn.files["/sys/devices/system/cpu/cpufreq"] = b""
        self.conn.files["/sys/devices/system/cpu/cpuidle"] = b""
        self.conn.files["/sys/devices/system/cpu/cpu12/cpufreq/scaling_cur_freq"] = b"2000000"
        args = await CpuFreq().arguments(self.context())
        self.assertEqual(
            args,
            (
                "--path",
                "/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq",
                "--path",
                "/sys/devices/system/cpu/cpu12/cpufreq/scaling_cur_freq",
            ),
        )
        with self.assertRaises(CapabilityUnavailable):
            await CpuFreq(paths=("/missing/*/frequency",)).arguments(self.context())

    async def test_foreign_processes(self):
        selected = ProcessSet(
            (
                ProcessIdentity(
                    device_id=DeviceId("fixture-device"),
                    pid=123,
                    start_ticks=99,
                    boot_id="fixture",
                    name="x",
                ),
            )
        )
        with self.assertRaises(OwnershipError):
            async with PidStat(processes=selected).open(self.context()):
                pass

    async def test_collect_for_finalizes(self):
        ctx = self.context()
        log = await CollectFor[FileRecord](collector=ProcStat(interval=10), duration=0.01).run(ctx)
        await ctx.join()
        self.assertTrue(log.complete)

    async def test_capture_not_available_early(self):
        c = Capture()
        with self.assertRaises(OwnershipError):
            _ = c.log

    async def test_partial_setting_startup_restores_prior_changes(self):
        self.conn.fail_write = "/proc/sys/net/core/wmem_max"
        with self.assertRaises(PermissionError):
            async with SocketBufferLimit(bytes=999).open(self.context()):
                pass
        self.assertEqual(self.conn.files["/proc/sys/net/core/rmem_max"], b"100")

    async def test_settings_restore_after_cancel(self):
        started = asyncio.Event()

        async def work():
            async with SetFile(path=self.conn.path("/setting"), value=b"new").open(self.context()):
                started.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(work())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.conn.files["/setting"], b"old")

    async def test_invalid_timing(self):
        for interval in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                ProcStat(interval=interval)
        with self.assertRaises(ValueError):
            await Sleep(duration=-1).run(self.context())
        with self.assertRaises(ValueError):
            await CollectFor(collector=ProcStat(), duration=0).run(self.context())
