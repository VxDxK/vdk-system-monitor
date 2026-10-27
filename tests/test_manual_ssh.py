"""Manual SSH orchestration doubles do not establish live network behavior."""

import asyncio
import socket
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from manual_examples import ManualExampleChecks, load_example

from vsm.util.network import Endpoint, TrafficProtocol

example = load_example("manual_ssh")


class PipelineTests(ManualExampleChecks, unittest.IsolatedAsyncioTestCase):
    example = example
    remote = True

    async def test_udp_pipeline(self):
        await self.exercise(protocol=TrafficProtocol.UDP)

    async def test_ipv6_pipeline(self):
        await self.exercise(address="2001:db8::1")

    async def test_key_auth_pipeline(self):
        await self.exercise(password=None, client_keys=(Path("/home/alice/.ssh/id_rsa"),))

    async def test_address_resolution_and_override(self):
        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", new_callable=AsyncMock) as resolve:
            resolve.return_value = [
                (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", 6000, 0, 0))
            ]
            self.assertEqual(
                str(await example.resolve_address("server", 6000, None)), "[2001:db8::1]:6000"
            )
            resolve.assert_awaited_once_with(
                "server", 6000, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM
            )
            resolve.reset_mock()
            self.assertEqual(
                await example.resolve_address("server", 5000, "192.0.2.1"),
                Endpoint.parse("192.0.2.1:5000"),
            )
            resolve.assert_not_called()
            resolve.side_effect = socket.gaierror("unknown host")
            with self.assertRaises(socket.gaierror):
                await example.resolve_address("server", 5000, None)


class CLITests(unittest.TestCase):
    def test_defaults_and_overrides(self):
        for extra in (
            [],
            ["--auth", "password"],
            [
                "--protocol",
                "udp",
                "--port",
                "6000",
                "--address",
                "::1",
                "--workers",
                "2",
                "--payload-bytes",
                "1200",
                "--rate-bps",
                "1000000",
            ],
        ):
            with (
                self.subTest(extra=extra),
                patch("sys.argv", ["manual_ssh.py", "alice", "server", *extra]),
                patch.object(example.getpass, "getpass", return_value="secret") as prompt,
                patch.object(
                    example, "main", new_callable=MagicMock, return_value=object()
                ) as main,
                patch.object(example.asyncio, "run") as run,
            ):
                example.cli()
                prompt.assert_called_once_with("Password for alice@server: ")
                args = main.call_args.kwargs
                self.assertEqual(args["password"], "secret")
                self.assertEqual(args["client_keys"], ())
                overrides = "--protocol" in extra
                self.assertEqual(
                    args["protocol"], TrafficProtocol.UDP if overrides else TrafficProtocol.TCP
                )
                self.assertEqual(args["port"], 6000 if overrides else 5000)
                run.assert_called_once_with(main.return_value)

    def test_key_auth_skips_password_and_expands_key_path(self):
        with (
            patch("sys.argv", ["manual_ssh.py", "alice", "server", "--auth", "key"]),
            patch.object(example.getpass, "getpass") as prompt,
            patch.object(example, "main", new_callable=MagicMock, return_value=object()) as main,
            patch.object(example.asyncio, "run") as run,
        ):
            example.cli()
        prompt.assert_not_called()
        self.assertIsNone(main.call_args.kwargs["password"])
        self.assertEqual(main.call_args.kwargs["client_keys"], (Path.home() / ".ssh" / "id_rsa",))
        self.assertNotIn("auth", main.call_args.kwargs)
        run.assert_called_once_with(main.return_value)

    def test_invalid_arguments_fail_before_password(self):
        for option, value in (
            ("--port", "0"),
            ("--port", "65536"),
            ("--workers", "257"),
            ("--workers", "0"),
            ("--payload-bytes", "0"),
            ("--payload-bytes", "65508"),
            ("--rate-bps", "0"),
            ("--address", "hostname"),
            ("--protocol", "icmp"),
            ("--auth", "agent"),
        ):
            with (
                self.subTest(option=option, value=value),
                patch("sys.argv", ["manual_ssh.py", "alice", "server", option, value]),
                patch("sys.stderr"),
                patch.object(example.getpass, "getpass") as prompt,
                self.assertRaises(SystemExit) as error,
            ):
                example.cli()
            self.assertEqual(error.exception.code, 2)
            prompt.assert_not_called()

    def test_missing_arguments_do_not_prompt(self):
        with (
            patch("sys.argv", ["manual_ssh.py", "alice"]),
            patch("sys.stderr"),
            patch.object(example.getpass, "getpass") as prompt,
            self.assertRaises(SystemExit),
        ):
            example.cli()
        prompt.assert_not_called()

    def test_password_eof_does_not_start_run(self):
        with (
            patch("sys.argv", ["manual_ssh.py", "alice", "server"]),
            patch.object(example.getpass, "getpass", side_effect=EOFError),
            patch.object(example, "main") as main,
            self.assertRaises(EOFError),
        ):
            example.cli()
        main.assert_not_called()

    def test_password_cancellation_does_not_start_run(self):
        with (
            patch("sys.argv", ["manual_ssh.py", "alice", "server"]),
            patch.object(example.getpass, "getpass", side_effect=KeyboardInterrupt),
            patch.object(example, "main") as main,
            self.assertRaises(KeyboardInterrupt),
        ):
            example.cli()
        main.assert_not_called()
