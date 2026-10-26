"""Real native optional deadlines, bounded only by the test harness."""

import json
import os
import socket
import subprocess
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path

from native_artifacts import native_artifact


def records(path):
    contents = path.read_text()
    # A writer may currently be emitting the last line.
    return [json.loads(line) for line in contents.split("\n")[:-1] if line]


class NativeDurationTests(unittest.TestCase):
    @contextmanager
    def running(self, tool, *args):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stdout"
            with path.open("w") as stdout, (Path(directory) / "stderr").open("w+") as stderr:
                process = subprocess.Popen(
                    [str(native_artifact(tool)), *args], stdout=stdout, stderr=stderr
                )
                try:
                    yield process, path
                finally:
                    if process.poll() is None:
                        process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)
                        self.fail(f"{tool} failed to stop gracefully")
                    stderr.seek(0)
                    self.assertEqual(process.returncode, 0, stderr.read())

    def ready(self, process, path):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if any(record["type"] == "ready" for record in records(path)):
                return
            self.assertIsNone(process.poll(), "native tool exited before readiness")
            time.sleep(0.01)
        self.fail("native readiness deadline exceeded")

    def stop_and_check(self, process, path):
        process.terminate()
        process.wait(timeout=3)
        events = records(path)
        self.assertEqual(events[-1]["type"], "end")
        self.assertTrue(events[-1]["interrupted"])
        self.assertEqual([event["sequence"] for event in events], list(range(len(events))))
        return events

    def test_collectors_without_deadline_stop_with_clean_end(self):
        for tool, args in (
            ("rfpoll", ("--path", "/proc/stat")),
            ("pspider", ("--pid", str(os.getpid()), "--self-only")),
            ("socket-stat", ()),
        ):
            with (
                self.subTest(tool=tool),
                self.running(tool, *args, "--interval-ms", "10") as (process, path),
            ):
                self.ready(process, path)
                time.sleep(0.1)
                self.assertIsNone(process.poll())
                events = self.stop_and_check(process, path)
                self.assertGreaterEqual(sum(event["type"] == "sample" for event in events), 2)

    def test_explicit_collector_duration_completes_without_signal(self):
        for tool, args in (
            ("rfpoll", ("--path", "/proc/stat")),
            ("pspider", ("--pid", str(os.getpid()), "--self-only")),
            ("socket-stat", ()),
        ):
            with (
                self.subTest(tool=tool),
                self.running(tool, *args, "--interval-ms", "5", "--duration-ms", "50") as (
                    process,
                    path,
                ),
            ):
                process.wait(timeout=3)
                events = records(path)
                self.assertEqual(events[-1]["type"], "end")
                self.assertFalse(events[-1]["interrupted"])

    def test_rate_limited_rgen_pair_without_deadline(self):
        for protocol in ("tcp", "udp"):
            with self.subTest(protocol=protocol):
                with socket.socket() as reservation:
                    reservation.bind(("127.0.0.1", 0))
                    address = f"127.0.0.1:{reservation.getsockname()[1]}"
                common = ("--address", address, "--protocol", protocol)
                with self.running("rgen", "consumer", *common) as (server, server_path):
                    self.ready(server, server_path)
                    with self.running(
                        "rgen", "producer", *common, "--rate-bps", "8", "--workers", "2"
                    ) as (client, client_path):
                        self.ready(client, client_path)
                        time.sleep(0.2)
                        self.assertIsNone(client.poll())
                        client_events = self.stop_and_check(client, client_path)
                        summary = next(
                            event for event in client_events if event["type"] == "summary"
                        )
                        self.assertGreater(summary["bytes"], 0)
                    self.assertIsNone(server.poll())
                    server_events = self.stop_and_check(server, server_path)
                    summary = next(event for event in server_events if event["type"] == "summary")
                    self.assertGreater(summary["bytes"], 0)

    def test_invalid_explicit_duration(self):
        for tool, args in (
            ("rfpoll", ("--path", "/proc/stat")),
            ("pspider", ()),
            ("socket-stat", ()),
            ("rgen", ("producer", "--address", "127.0.0.1:1")),
        ):
            for duration in ("0", "86400001"):
                with self.subTest(tool=tool, duration=duration):
                    result = subprocess.run(
                        [str(native_artifact(tool)), *args, "--duration-ms", duration],
                        capture_output=True,
                        check=False,
                        timeout=3,
                    )
                    self.assertNotEqual(result.returncode, 0)
