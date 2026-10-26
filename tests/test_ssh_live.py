"""Real isolated AsyncSSH/SFTP integration, skipped when dependency or sockets are unavailable."""

import asyncio
import importlib.util
import socket
import tempfile
import unittest
from pathlib import Path

from vsm.connection.ssh import SSH
from vsm.models import Exec


class SSHLiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if importlib.util.find_spec("asyncssh") is None:
            self.skipTest("AsyncSSH unavailable; package-index access denied")
        try:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
        except PermissionError:
            self.skipTest("loopback socket creation denied")
        import asyncssh

        class Server(asyncssh.SSHServer):
            def begin_auth(self, username):
                return False  # Isolated loopback fixture only.

        async def execute(process):
            native = await asyncio.create_subprocess_exec(
                "/bin/sh",
                "-c",
                process.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            async def forward_input():
                try:
                    while data := await process.stdin.read(65536):
                        native.stdin.write(data)
                        await native.stdin.drain()
                except BrokenPipeError, ConnectionResetError:
                    pass
                finally:
                    native.stdin.close()

            async def forward_output(source, destination):
                while data := await source.read(65536):
                    destination.write(data)
                    await destination.drain()

            try:
                async with asyncio.TaskGroup() as tasks:
                    input_task = tasks.create_task(forward_input())
                    tasks.create_task(forward_output(native.stdout, process.stdout))
                    tasks.create_task(forward_output(native.stderr, process.stderr))
                    await native.wait()
                    input_task.cancel()
                process.exit(
                    native.returncode if native.returncode >= 0 else 128 - native.returncode
                )
            finally:
                if native.returncode is None:
                    native.kill()
                    await native.wait()

        key = asyncssh.generate_private_key("ssh-ed25519")
        server = await asyncssh.create_server(
            Server,
            "127.0.0.1",
            0,
            server_host_keys=[key],
            process_factory=execute,
            sftp_factory=asyncssh.SFTPServer,
            encoding=None,
        )

        async def close_server():
            server.close()
            await server.wait_closed()

        self.addAsyncCleanup(close_server)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        known = self.root / "known_hosts"
        known.write_text(f"[127.0.0.1]:{server.get_port()} " + key.export_public_key().decode())
        self.backend = SSH(destination="127.0.0.1", port=server.get_port(), known_hosts=known)

    async def test_real_command_process_and_sftp(self):
        async with self.backend.connect() as connection:
            result = await connection.run(Exec(argv=("printf", "%s", "a b;$(false)")))
            self.assertEqual(result.stdout, b"a b;$(false)")
            local = self.root / "local"
            local.write_bytes(b"transfer")
            await connection.push(local, connection.path(str(self.root / "remote")))
            await connection.pull(
                connection.path(str(self.root / "remote")), self.root / "retrieved"
            )
            self.assertEqual((self.root / "retrieved").read_bytes(), b"transfer")
            async with connection.launch(Exec(argv=("sleep", "20"))) as process:
                self.assertIsNotNone(process.target_pid)
                await process.stop()

    async def test_finite_collection_and_processing(self):
        from collector_acceptance import finite_collection

        await finite_collection(self, self.backend, self.root)

    async def test_caller_deadline_reaps_command_and_connection_is_reusable(self):
        from collector_acceptance import command_deadline

        await command_deadline(self, self.backend)
