"""SSH adapter tests use protocol doubles; no authentication/network parity claim."""

import asyncio
import shlex
import tempfile
import types
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

from asyncssh.misc import async_context_manager

from vsm.connection.ssh import SSH, SSHConnection
from vsm.connection.targetctl import TargetctlClient
from vsm.models import CleanupUnknown, Exec, FileOutput, OwnershipError


class SFTPGlobTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_matches_are_empty_but_permission_and_transport_errors_propagate(self):
        import asyncssh
        from support import FakeConnection

        target = FakeConnection()
        sftp = types.SimpleNamespace(glob=AsyncMock())
        connection = SSHConnection(target.device_id, target.target, None, sftp, "/tmp/fixture")
        for error in (asyncssh.SFTPNoSuchFile("missing"), asyncssh.SFTPNoSuchPath("missing")):
            with self.subTest(error=type(error).__name__):
                sftp.glob.side_effect = error
                self.assertEqual(await connection.glob("/missing/*"), ())
        for error in (asyncssh.SFTPPermissionDenied("denied"), OSError("transport lost")):
            with self.subTest(error=type(error).__name__):
                sftp.glob.side_effect = error
                with self.assertRaises(type(error)):
                    await connection.glob("/restricted/*")


class Result:
    def __init__(self, stdout="", status=0, stderr=""):
        self.stdout = stdout
        self.stderr = stderr
        self.exit_status = status


class Stream:
    def __init__(self, channel, data):
        self.channel = channel
        self.data = data

    async def read(self, n):
        if self.data:
            value = self.data
            self.data = b""
            return value
        await self.channel.done.wait()
        return b""


class Channel:
    """Duplex helper protocol double; it does not execute a target workload."""

    def __init__(self, client):
        self.client = client
        self.done = asyncio.Event()
        self.exit_status = None
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdin = self
        self.directory = None
        self.operations = []

    def emit(self, kind, value, request_id=1, operation="launch"):
        import json

        event = {
            "id": request_id,
            "operation": operation,
            "event": kind,
            "result": value,
            "started_ns": 0,
            "finished_ns": 1,
            "boot_id": "boot-id",
        }
        self.stdout.feed_data(json.dumps(event).encode() + b"\n")

    def write(self, data):
        import json

        request = json.loads(data)
        operation = request["operation"]
        self.operations.append(operation)
        if operation == "launch":
            self.directory = request["params"]["directory"]
            self.emit(
                "launched",
                {
                    "workload": {
                        "pid": 123,
                        "start_ticks": 99,
                        "boot_id": "boot-id",
                        "name": "fixture",
                    },
                    "supervisor_pid": 122,
                },
            )
            self.emit("output", {"stream": "stdout", "data": "aGVsbG8="})
            self.emit("output", {"stream": "stderr", "data": "d2FybmluZw=="})
        elif operation == "stop":
            if not self.client.deny_stop:
                self.emit("completed", {"exit_code": 0, "output_complete": True})
                self.emit("result", {"cleanup": "confirmed"}, request["id"], "stop")
                self.emit("cleanup", {"cleanup": "confirmed", "scope": "process_group"})
            self.close()

    async def drain(self):
        pass

    def write_eof(self):
        self.close()

    async def wait_closed(self):
        await self.done.wait()

    def close(self):
        if not self.done.is_set():
            self.exit_status = 0
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self.done.set()


class SFTP:
    def __init__(self):
        self.files = {}
        self.channels = []

    @asynccontextmanager
    async def open(self, path, mode):
        if mode == "rb":
            if path.endswith("/pid"):
                data = b"123"
            elif path.endswith("/start"):
                data = b"99"
            else:
                if path not in self.files:
                    raise FileNotFoundError(path)
                data = self.files[path]
        else:
            data = b""
        owner = self

        class File:
            async def read(self, n):
                return data[:n]

            async def write(self, value):
                owner.files[path] = value

        yield File()

    async def exists(self, path):
        return path in self.files or path.endswith(("/pid", "/start"))

    async def put(self, source, destination):
        self.files[destination] = Path(source).read_bytes()

    async def get(self, source, destination):
        Path(destination).write_bytes(self.files[source])

    async def glob(self, pattern):
        return sorted(self.files)


class Client:
    def __init__(self):
        self.sftp = SFTP()
        self.channel = None
        self.commands = []
        self.deny_stop = False
        self._temporary_count = 0

    def get_extra_info(self, name, default=None):
        return {"username": "user", "host": "server"}.get(name, default)

    async def run(self, command, check=False, timeout=None, encoding="utf-8"):
        self.commands.append(command)
        if command.startswith("uname -s;"):
            return Result(
                "Linux\nx86_64\nfixture\n100\n4096\nboot-id\n/bin/setsid\n/bin/kill\n/bin/awk\n/bin/sed\n"
            )
        if command.startswith("mktemp"):
            self._temporary_count += 1
            return Result(
                shlex.split(command)[-1].replace("XXXXXXXX", f"test{self._temporary_count}") + "\n"
            )
        if command.startswith("command -v"):
            return Result("/bin/true\n")
        if "/bin/kill" in command:
            if self.deny_stop:
                raise OSError("lost transport")
            if self.channel:
                self.channel.close()
        return Result()

    @async_context_manager
    async def create_process(self, command, encoding=None):
        self.commands.append(command)
        self.channel = Channel(self)
        return self.channel

    @asynccontextmanager
    async def start_sftp_client(self):
        yield self.sftp


class SSHTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = Client()
        module = types.ModuleType("asyncssh")

        @asynccontextmanager
        async def connect(destination, **kwargs):
            self.destination = destination
            self.options = kwargs
            yield self.client

        module.connect = connect
        self.patcher = patch("vsm.connection.ssh.asyncssh", module)
        self.patcher.start()

        async def helper_path(client):
            return client.connection.path("/targetctl")

        self.helper_patcher = patch.object(TargetctlClient, "path", helper_path)
        self.helper_patcher.start()

    async def asyncTearDown(self):
        self.patcher.stop()
        self.helper_patcher.stop()

    async def test_connect_probe_run_stop_and_auth_options(self):
        async with SSH(
            destination="server", username="user", known_hosts=Path("/known")
        ).connect() as conn:
            self.assertEqual(conn.target.clock_ticks_per_second, 100)
            async with conn.launch(Exec(argv=("printf", "%s", "a b"))) as proc:
                self.assertEqual(proc.target_pid, 123)
                result = await proc.stop()
                self.assertEqual(result.stdout, b"hello")
                self.assertEqual(result.stderr, b"warning")
            self.assertEqual(self.options["known_hosts"], "/known")
            self.assertEqual(self.options["username"], "user")
            self.assertNotIn("password", self.options)
            self.assertTrue(any("/targetctl supervise" in c for c in self.client.commands))
            self.assertFalse(any("setsid" in c for c in self.client.commands))
            self.assertTrue(any("rm -rf" in c for c in self.client.commands))
        with self.assertRaises(OwnershipError):
            async with conn.launch(Exec(argv=("true",))):
                pass

    async def test_password_forwarding_and_private_representation(self):
        for password in ("example-secret", ""):
            with self.subTest(empty=password == ""):
                backend = SSH(destination="server", username="user", password=password)
                self.assertNotIn("password=", repr(backend))
                async with backend.connect():
                    self.assertEqual(self.options["password"], password)
                    self.assertNotIn("known_hosts", self.options)

    async def test_sftp_contract(self):
        async with SSH(destination="server").connect() as conn:
            path = conn.path("/file")
            await conn.write_bytes(path, b"bytes")
            self.assertEqual(await conn.read_bytes(path), b"bytes")
            with tempfile.TemporaryDirectory() as d:
                local = Path(d) / "a"
                local.write_bytes(b"source")
                await conn.push(local, conn.path("/upload"))
                with self.assertRaises(FileExistsError):
                    await conn.push(local, conn.path("/upload"))
                await conn.pull(conn.path("/upload"), Path(d) / "b")
                self.assertEqual((Path(d) / "b").read_bytes(), b"source")
                with self.assertRaises(FileExistsError):
                    await conn.pull(conn.path("/upload"), Path(d) / "b")
            self.assertEqual(str(await conn.executable("true")), "/bin/true")
            self.assertEqual(len(await conn.glob("/*")), 2)

    async def test_unconfirmed_cleanup_is_error(self):
        async with SSH(destination="server").connect() as conn:
            with self.assertRaises(CleanupUnknown):
                async with conn.launch(Exec(argv=("sleep", "20"))) as proc:
                    self.client.deny_stop = True
            self.assertFalse(any(c.startswith("rm -rf") for c in self.client.commands))
            self.client.channel.close()
            try:
                await proc.wait(check=False)
            except Exception:  # noqa: BLE001, S110 -- Preserve the original failure while joining cleanup.
                pass

    async def test_output_files(self):
        with tempfile.TemporaryDirectory() as d:
            async with SSH(destination="server").connect() as conn:
                async with conn.launch(
                    Exec(argv=("true",)),
                    stdout=FileOutput(Path(d) / "out"),
                    stderr=FileOutput(Path(d) / "err"),
                ) as proc:
                    await proc.stop()
                self.assertEqual((Path(d) / "out").read_bytes(), b"hello")
                self.assertEqual((Path(d) / "err").read_bytes(), b"warning")

    async def test_repeated_ssh_connections_share_identity_but_not_workspace(self):
        backend = SSH(destination="server", username="user")
        async with backend.connect() as first:
            async with backend.connect() as second:
                self.assertEqual(first.device_id.value, "user@server:22")
                self.assertEqual(first.device_id, second.device_id)
                self.assertNotEqual(first.tmp_dir, second.tmp_dir)
                await first.write_bytes(first.path("/shared"), b"shared")
                self.assertEqual(await second.read_bytes(first.path("/shared")), b"shared")
            self.assertFalse(first.closed)
            self.assertTrue(second.closed)
            self.assertIn("rm -rf -- " + str(second.tmp_dir), self.client.commands)
            self.assertNotIn("rm -rf -- " + str(first.tmp_dir), self.client.commands)

    async def test_workspace_creation_is_reconciled_after_repeated_cancellation(self):
        started, release = asyncio.Event(), asyncio.Event()
        original = self.client.run

        async def delayed(command, **kwargs):
            result = await original(command, **kwargs)
            if command.startswith("mktemp"):
                started.set()
                await release.wait()
            return result

        with patch.object(self.client, "run", delayed):
            manager = SSH(destination="server").connect()
            task = asyncio.create_task(manager.__aenter__())
            await started.wait()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(
            any(
                command.startswith("rm -rf -- /tmp/vsm-tmp-data-")
                for command in self.client.commands
            )
        )

    async def test_process_startup_cancellation_joins_awaitable_and_stops_channel(self):
        started, release = asyncio.Event(), asyncio.Event()
        original = self.client.create_process

        @async_context_manager
        async def delayed(*args, **kwargs):
            channel = await original(*args, **kwargs)
            started.set()
            await release.wait()
            return channel

        async with SSH(destination="server").connect() as conn:
            with patch.object(self.client, "create_process", delayed):
                manager = conn.launch(Exec(argv=("sleep", "20")))
                task = asyncio.create_task(manager.__aenter__())
                await started.wait()
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertTrue(self.client.channel.done.is_set())
            self.assertIn("stop", self.client.channel.operations)
            self.assertTrue(any("rm -rf -- " in c for c in self.client.commands))
            self.assertFalse(conn._retain_workspace)

    async def test_malformed_target_metadata_does_not_allocate_workspace(self):
        original = self.client.run

        async def invalid(command, **kwargs):
            result = await original(command, **kwargs)
            if command.startswith("uname -s;"):
                result.stdout = result.stdout.replace("100\n", "invalid\n")
            return result

        with patch.object(self.client, "run", invalid), self.assertRaises(ValueError):
            async with SSH(destination="server").connect():
                pass
        self.assertFalse(any(command.startswith("mktemp") for command in self.client.commands))
