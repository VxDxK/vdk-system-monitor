import asyncio
import io
import os
import tempfile
import unittest
from unittest.mock import patch

from manual_examples import ManualExampleChecks, load_example

from vsm.action.manual import Manual

example = load_example("manual")


class ManualTests(unittest.IsolatedAsyncioTestCase):
    async def test_waits_for_enter_without_blocking_or_consuming_next_line(self):
        reader, writer = os.pipe()
        with os.fdopen(reader) as stream, os.fdopen(writer, "wb", buffering=0) as send:
            output = io.StringIO()
            with patch("sys.stdin", stream), patch("sys.stdout", output):
                task = asyncio.create_task(Manual(prompt="Finish?").run(None))
                try:
                    await asyncio.sleep(0.07)
                    self.assertFalse(task.done())
                    self.assertEqual(output.getvalue(), "Finish? ")
                    send.write(b"x")
                    await asyncio.sleep(0.07)
                    self.assertFalse(task.done())
                    send.write(b"\nnext\n")
                    self.assertIsNone(await asyncio.wait_for(task, 1))
                    self.assertEqual(os.read(reader, 5), b"next\n")
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_eof_and_regular_file(self):
        for contents in ("", "unfinished", "\n"):
            with self.subTest(contents=contents), tempfile.TemporaryFile(mode="w+") as stream:
                stream.write(contents)
                stream.seek(0)
                with patch("sys.stdin", stream), patch("sys.stdout"):
                    if contents == "\n":
                        await Manual().run(None)
                    else:
                        with self.assertRaises(EOFError):
                            await asyncio.wait_for(Manual().run(None), 2)

    async def test_cancel_does_not_leave_input_work(self):
        reader, writer = os.pipe()
        with (
            os.fdopen(reader) as stream,
            os.fdopen(writer, "wb", buffering=0) as send,
            patch("sys.stdin", stream),
            patch("sys.stdout"),
        ):
            before = asyncio.all_tasks()
            task = asyncio.create_task(Manual().run(None))
            await asyncio.sleep(0.07)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 0.2)
            self.assertEqual(asyncio.all_tasks(), before)
            send.write(b"\n")
            self.assertEqual(os.read(reader, 1), b"\n")


class ExampleTests(ManualExampleChecks, unittest.IsolatedAsyncioTestCase):
    example = example


class CLITests(unittest.TestCase):
    def test_port_validation(self):
        for port in ("0", "65536"):
            with (
                patch("sys.argv", ["manual.py", "--port", port]),
                patch("sys.stderr"),
                patch.object(example.asyncio, "run") as run,
                self.assertRaises(SystemExit),
            ):
                example.cli()
            run.assert_not_called()
