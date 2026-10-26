"""Shared orchestration checks; doubles do not establish live target behavior."""

import asyncio
import importlib.util
import tempfile
from contextlib import asynccontextmanager
from datetime import timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from vsm.action.manual import Manual
from vsm.connection.host import Host
from vsm.connection.ssh import SSH
from vsm.models import DeviceId, MonitorError, ProcessIdentity, ProcessSet
from vsm.processing import Dataset
from vsm.util.network import ProbeRole, TrafficProtocol, TrafficRole

ROOT = Path(__file__).resolve().parents[1]


def load_example(name):
    spec = importlib.util.spec_from_file_location(name + "_example", ROOT / f"examples/{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ManualExampleChecks:
    remote = False

    async def exercise(
        self,
        *,
        failure=None,
        protocol=TrafficProtocol.TCP,
        address="192.0.2.10",
        password="secret",
        client_keys=(),
    ):
        example, remote = self.example, self.remote
        events, active, actions = [], [], {}
        selections = {
            role: ProcessSet(
                [
                    ProcessIdentity(
                        device_id=DeviceId(role if remote else "host"),
                        pid=pid,
                        start_ticks=99,
                        boot_id="boot",
                        name="workload",
                    )
                ]
            )
            for role, pid in (("client", 123), ("server", 456))
        }

        class Capture:
            def __init__(self, key):
                self.key = key

            async def processes(self):
                events.append((*self.key, "identity"))
                if failure == "identity" and self.key[0] == "client":
                    raise MonitorError("identity unavailable")
                return selections[self.key[0]]

            async def wait(self):
                raise AssertionError("must wait for controller Enter")

            @property
            def log(self):
                assert not active, "processing before capture finalization"
                return self.key

        class Device:
            def __init__(self, role):
                self.role = role

            @asynccontextmanager
            async def use(self, action):
                name = type(action).__name__
                role = self.role
                if not remote:
                    if name == "Rup":
                        role = "server" if action.role is ProbeRole.SERVER else "client"
                    elif name in ("PidStat", "Smaps"):
                        role = next(
                            r
                            for r, selection in selections.items()
                            if action.processes is selection
                        )
                key = role, name
                if name in ("PidStat", "Smaps"):
                    assert action.processes is selections[role]
                if role == "client" and name in ("Rup", "TrafficGenerator"):
                    assert ("server", name) in active, "server must already be ready"
                if failure == "startup" and key == ("server", "Smaps"):
                    raise MonitorError("startup failed")
                actions[key] = action
                active.append(key)
                events.append((*key, "start"))
                try:
                    yield Capture(key)
                finally:
                    assert active.pop() == key
                    events.append((*key, "stop"))

            async def do(self, action):
                assert isinstance(action, Manual)
                assert self.role == ("client" if remote else "host")
                assert len(active) == (14 if remote else 11)
                assert sum(event[-1] == "identity" for event in events) == 2
                events.append(("manual",))
                if failure in ("eof", "cancel", "collection"):
                    raise {
                        "eof": EOFError,
                        "cancel": asyncio.CancelledError,
                        "collection": MonitorError,
                    }[failure]()

        class Run:
            def __init__(self, output):
                self.tasks = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                assert not active
                await asyncio.gather(*self.tasks, return_exceptions=True)

            async def connect(self, backend):
                if isinstance(backend, Host):
                    return Device("client" if remote else "host")
                assert remote and backend == SSH(
                    destination="server",
                    username="alice",
                    password=password,
                    client_keys=client_keys,
                )
                return Device("server")

            def spawn(self, coroutine):
                task = asyncio.create_task(coroutine)
                self.tasks.append(task)
                return task

        async def process(processor, log, *, params):
            assert not active
            assert not params.allow_partial and not params.allow_read_errors
            expected = {
                "CpuFreq": example.CpuFreqProcessor,
                "ProcStat": example.CpuProcessor,
                "Meminfo": example.MeminfoProcessor,
                "NetDev": example.NetProcessor,
                "PidStat": example.PidStatProcessor,
                "Smaps": example.SmapsProcessor,
            }
            workload = "TrafficGenerator" if remote else "Rup"
            expected[workload] = (
                example.TrafficGeneratorProcessor if remote else example.RupProcessor
            )
            if not remote:
                expected["Fans"] = example.FansProcessor
            assert isinstance(processor, expected[log[1]])
            assert remote or log != ("server", "Rup"), "server service log is not latency"
            events.append((*log, "process"))
            if failure == "processing" and log == ("server", "Smaps"):
                raise ValueError("malformed log")
            names = ("rup", "rup_summary") if log[1] == "Rup" else (log[1],)
            return tuple(
                Dataset(frame=pd.DataFrame({"value": [1]}), group_label=name, data_label=name)
                for name in names
            )

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(example, "ROOT", Path(temporary)),
            patch.object(example, "Run", Run),
            patch.object(example, "process", process),
            patch.object(example, "export_charts") as charts,
            patch("sys.stdout"),
        ):
            args = ("alice", "server", password) if remote else ()
            kwargs = (
                {
                    "protocol": protocol,
                    "client_keys": client_keys,
                    "address": address,
                    "workers": 2,
                    "payload_bytes": 1200,
                    "rate_bps": 1_000_000,
                }
                if remote
                else {}
            )
            if failure:
                error = {
                    "startup": MonitorError,
                    "identity": MonitorError,
                    "collection": MonitorError,
                    "cancel": asyncio.CancelledError,
                    "eof": EOFError,
                    "processing": ValueError,
                }[failure]
                with self.assertRaises(error):
                    await example.main(*args, **kwargs)
                charts.assert_not_called()
                assert not list(Path(temporary).rglob("*.csv"))
                if failure != "processing":
                    assert not any(event[-1] == "process" for event in events)
            else:
                await example.main(*args, **kwargs)
                charts.assert_called_once()
                self.assertEqual(
                    charts.call_args.kwargs["options"].timezone, timezone(timedelta(hours=3))
                )
                datasets, report = charts.call_args.args
                self.assertEqual(report.name, "charts.html")
                self.assertEqual(len(datasets), 14 if remote else 11)
                expected_counts = (
                    {"client": 7, "server": 7} if remote else {"host": 5, "client": 4, "server": 2}
                )
                for role, count in expected_counts.items():
                    paths = list(Path(temporary).glob(f"runs/*/{role}/*.csv"))
                    self.assertEqual(len(paths), count)
                    for path in paths:
                        restored = Dataset.from_csv(path)
                        self.assertTrue(restored.group_label.startswith(role + ": "))
                self.assertEqual(
                    {data.group_label.split(":")[0] for data in datasets}, set(expected_counts)
                )
                workload = "TrafficGenerator" if remote else "Rup"
                client, server = actions["client", workload], actions["server", workload]
                if remote:
                    self.assertEqual(client.role, TrafficRole.PRODUCER)
                    self.assertEqual(server.role, TrafficRole.CONSUMER)
                    self.assertEqual(client.address.address, example.ip_address(address))
                    wildcard = "0.0.0.0" if client.address.address.version == 4 else "::"
                    self.assertEqual(server.address.address, example.ip_address(wildcard))
                    for action in (client, server):
                        self.assertEqual(action.protocol, protocol)
                        self.assertEqual(action.workers, 2)
                        self.assertEqual(action.payload_bytes, 1200)
                        self.assertIsNone(action.duration)
                    self.assertEqual(client.rate_bps, 1_000_000)
                    self.assertIsNone(server.rate_bps)
                else:
                    self.assertEqual(client.address, server.address)
                    self.assertEqual(str(client.address), "127.0.0.1:5000")
                    self.assertEqual(client.role, ProbeRole.CLIENT)
                    self.assertEqual(server.role, ProbeRole.SERVER)
                    self.assertIsNone(client.duration)
                    self.assertIsNone(server.duration)
                    self.assertIsNone(client.count)
                self.assertLess(
                    events.index(("client", workload, "stop")),
                    events.index(("server", workload, "stop")),
                )
                for role in ("client", "server"):
                    for name in ("PidStat", "Smaps"):
                        self.assertLess(
                            events.index((role, name, "stop")),
                            events.index((role, workload, "stop")),
                        )
                for (role, name), action in actions.items():
                    if name not in ("Rup", "TrafficGenerator"):
                        self.assertIsNone(action.duration)
        self.assertFalse(active)

    async def test_normal_pipeline(self):
        await self.exercise()

    async def test_failures_cleanup_and_abort_exports(self):
        for failure in (
            "startup",
            "identity",
            "collection",
            "cancel",
            "eof",
            "processing",
        ):
            with self.subTest(failure=failure):
                await self.exercise(failure=failure)
