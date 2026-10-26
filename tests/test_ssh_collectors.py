from native_artifacts import native_artifact

"""Run SSH process ownership locally through real shells, without a network transport."""

import asyncio
import glob
import json
import sys
import tempfile
import types
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from asyncssh.misc import async_context_manager

from vsm.action.proc_stat import ProcStat
from vsm.connection._command import shell_command
from vsm.connection.host import Host
from vsm.connection.ssh import SSHConnection
from vsm.models import ActionId, EnvVar, Exec, Shell
from vsm.processing import Params, process
from vsm.processor.proc_stat import CpuProcessor

ROOT = Path(__file__).resolve().parents[1]


class LocalChannel:
    def __init__(self, process):
        self.process = process
        self.stdin, self.stdout, self.stderr = process.stdin, process.stdout, process.stderr

    @property
    def exit_status(self):
        code = self.process.returncode
        return -1 if code is not None and code < 0 else code

    @property
    def exit_signal(self):
        return "TERM" if self.exit_status == -1 else None

    async def wait_closed(self):
        await self.process.wait()

    def close(self):
        if self.process.returncode is None:
            self.process.terminate()


class LocalClient:
    async def run(self, command, *, check=False, timeout=None, encoding="utf-8"):
        async with asyncio.timeout(timeout):
            process = await asyncio.create_subprocess_exec(
                "/bin/sh",
                "-c",
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
        if check and process.returncode:
            raise RuntimeError(stderr.decode())
        return types.SimpleNamespace(
            stdout=stdout.decode() if encoding else stdout,
            stderr=stderr.decode() if encoding else stderr,
            exit_status=process.returncode,
        )

    @async_context_manager
    async def create_process(self, command, **kwargs):
        native = await asyncio.create_subprocess_exec(
            "/bin/sh",
            "-c",
            command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return LocalChannel(native)


class LocalSFTP:
    async def glob(self, pattern):
        return glob.glob(pattern)


class LocalSSHConnection(SSHConnection):
    async def read_bytes(self, path):
        self.check_path(path)
        return await asyncio.to_thread(Path(str(path)).read_bytes)

    async def push(self, source, destination):
        await asyncio.to_thread(Path(str(destination)).write_bytes, source.read_bytes())

    async def _read_text(self, path):
        return await asyncio.to_thread(Path(path).read_text)


class SSHCollectorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="vsm ssh '")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.host_manager = Host().connect()
        host = await self.host_manager.__aenter__()
        self.addAsyncCleanup(self.host_manager.__aexit__, None, None, None)
        self.connection = LocalSSHConnection(
            host.device_id, host.target, LocalClient(), LocalSFTP(), str(self.root)
        )
        self.addAsyncCleanup(self.connection._close_processes)
        # Bash can optimize away a final command's shell; an EXIT trap forces
        # it to wait like shells which do not perform that optimization.
        patcher = patch(
            "vsm.connection.ssh.shell_command",
            side_effect=lambda command: "trap : EXIT; " + shell_command(command),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_exec_preserves_pid_arguments_environment_and_cwd(self):
        code = (
            "import json,os,sys; "
            "print(json.dumps([os.getpid(),os.getcwd(),os.environ['VSM_TEST'],sys.argv[1:]]))"
        )
        async with self.connection.launch(
            Exec(
                argv=(sys.executable, "-c", code, "", "a b", "'$(false);"),
                env=(EnvVar("VSM_TEST", "a 'quoted' value; $(false)"),),
                cwd=self.connection.path(str(self.root)),
            )
        ) as proc:
            result = await proc.wait()
            pid, cwd, env, argv = json.loads(result.stdout)
            self.assertEqual(pid, proc.target_pid)
            self.assertEqual(cwd, str(self.root))
            self.assertEqual(env, "a 'quoted' value; $(false)")
            self.assertEqual(argv, ["", "a b", "'$(false);"])

    async def test_shell_retains_script_semantics(self):
        result = await self.connection.run(
            Shell(
                script='printf "%s" "$VSM_TEST"; printf " second"',
                env=(EnvVar("VSM_TEST", "first"),),
            )
        )
        self.assertEqual(result.stdout, b"first second")

    async def test_quiet_process_shutdown_keeps_connection_usable(self):
        async with self.connection.launch(Exec(argv=("/bin/sleep", "20"))) as process:
            await asyncio.sleep(2.2)
            result = await process.stop()
            self.assertEqual(result.exit_code, 143)
        self.assertFalse(self.connection._retain_workspace)
        self.assertFalse(self.connection._processes)
        result = await self.connection.run(Exec(argv=("/bin/echo", "still connected")))
        self.assertEqual(result.stdout, b"still connected\n")

    async def test_graceful_collector_shutdown_then_processing(self):
        manifest = ROOT / "artifacts/manifest.json"
        if not manifest.exists():
            self.skipTest("built native artifacts unavailable")
        artifact = native_artifact("rfpoll")
        if not artifact.is_file():
            self.skipTest("built rfpoll unavailable")

        async def binary(name):
            self.assertEqual(name, "rfpoll")
            return self.connection.path(str(artifact))

        async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
            ctx = types.SimpleNamespace(
                connection=self.connection,
                action_id=ActionId("cpu"),
                binary=binary,
                output_path=lambda name: self.root / name,
                supervise=tasks.create_task,
            )
            async with ProcStat(interval=20).open(ctx) as capture:
                await asyncio.sleep(0.08)
            log = capture.log
            self.assertTrue(log.complete)
            self.assertEqual(json.loads(log.path.read_text().splitlines()[-1])["type"], "end")
            self.assertEqual((await capture.wait()).exit_code, 0)
            results = await process(CpuProcessor(), log, params=Params())
            self.assertGreater(len(results[0].frame), 0)
            with self.assertRaisesRegex(ValueError, "log is incomplete") as error:
                await process(CpuProcessor(), replace(log, complete=False), params=Params())
            self.assertIn(str(log.path), str(error.exception))
        self.assertFalse(self.connection._processes)
