"""Native helper contracts, with real owned children and bounded execution."""

import asyncio
import json
import os
import select
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from native_artifacts import native_artifact

from vsm.action.capture import Capture
from vsm.action.launch import Launch
from vsm.binary import BinaryProvider
from vsm.connection._targetctl_protocol import _decode, _encode, _error
from vsm.connection.host import Host
from vsm.connection.targetctl import TargetctlOptions
from vsm.models import (
    CapabilityUnavailable,
    CleanupUnknown,
    Exec,
    OwnershipError,
    ProcessSet,
    Shell,
    TargetctlProtocolError,
)
from vsm.runtime import Run


class NativeTargetctlTests(unittest.TestCase):
    def test_clock_uses_shared_monotonic_clock_and_boot(self):
        before = time.monotonic_ns()
        event = self.request("clock", {})[-1]
        after = time.monotonic_ns()
        value = event["result"]
        self.assertEqual(event["event"], "result")
        self.assertLessEqual(before, value["monotonic_ns"])
        self.assertLessEqual(value["monotonic_ns"], after)
        self.assertEqual(
            value["boot_id"], Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        )
        self.assertGreater(value["realtime_ns"], 0)
        self.assertGreaterEqual(value["uncertainty_ns"], 0)

    def setUp(self):
        self.tool = native_artifact("targetctl")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def request(self, operation, params, **extra):
        result = subprocess.run(
            [self.tool, "request"],
            input=json.dumps({"id": 1, "operation": operation, "params": params, **extra}).encode()
            + b"\n",
            check=False,
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        events = [json.loads(line) for line in result.stdout.splitlines()]
        for event in events:
            self.assertNotIn("version", event)
        return events

    def start(self, argv, *, lease=0.5, grace=0.05):
        child = subprocess.Popen(
            [self.tool, "supervise"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(self.reap, child)
        self.send(
            child,
            "launch",
            {
                "argv": argv,
                "env": {},
                "cwd": None,
                "directory": str(self.root / f"job-{child.pid}"),
                "lease_seconds": lease,
                "grace_seconds": grace,
            },
        )
        return child

    def reap(self, child):
        if child.poll() is None:
            child.stdin.close()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2)
        for stream in (child.stdin, child.stdout, child.stderr):
            stream.close()

    def send(self, child, operation, params, request_id=1):
        child.stdin.write(
            json.dumps({"id": request_id, "operation": operation, "params": params}).encode()
            + b"\n"
        )
        child.stdin.flush()

    def read_until(self, child, event):
        # Unbuffered descriptor reads avoid readline blocking past the deadline.
        buffer = getattr(child, "_test_buffer", b"")
        deadline = time.monotonic() + 5
        events = []
        while time.monotonic() < deadline:
            if b"\n" not in buffer:
                ready, _, _ = select.select(
                    [child.stdout], [], [], max(0, deadline - time.monotonic())
                )
                self.assertTrue(ready, "helper response timeout")
                data = os.read(child.stdout.fileno(), 65536)
                self.assertTrue(data, "helper ended before expected event")
                buffer += data
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                value = json.loads(line)
                self.assertNotIn("version", value)
                events.append(value)
                if value["event"] == event:
                    child._test_buffer = buffer
                    return events
        self.fail("helper response deadline")

    def test_discovery_validation_and_errors(self):
        child = subprocess.Popen(["/bin/sleep", "10"])
        self.addCleanup(lambda: child.wait(timeout=2))
        self.addCleanup(child.terminate)
        result = self.request("discover", {"pattern": "/bin/sleep\u000010\u0000"})
        identities = [e["result"] for e in result if e["event"] == "item"]
        identity = next(i for i in identities if i["pid"] == child.pid)
        self.assertEqual(result[-1]["event"], "result")
        self.assertEqual(
            self.request("validate", {"processes": [identity, identity]})[-1]["event"], "result"
        )
        reused = {**identity, "start_ticks": identity["start_ticks"] + 1}
        self.assertEqual(
            self.request("validate", {"processes": [reused]})[-1]["error"]["category"], "ownership"
        )
        child.terminate()
        child.wait(timeout=2)
        missing = self.request("validate", {"processes": [identity]})[-1]
        self.assertEqual(missing["error"]["errno"], 2)
        self.assertIn(f"/proc/{child.pid}", missing["error"]["message"])

    def test_clap_help_version_and_argument_rejection(self):
        for args in (("--help",), ("request", "--help"), ("supervise", "--help")):
            with self.subTest(args=args):
                result = subprocess.run(
                    [self.tool, *args],
                    input=b"",
                    capture_output=True,
                    check=False,
                    timeout=5,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(b"Usage:", result.stdout)
                self.assertIn(b"targetctl", result.stdout)
        for args in (("--version",), ("request", "--version"), ("supervise", "--version")):
            with self.subTest(args=args):
                result = subprocess.run(
                    [self.tool, *args],
                    input=b"",
                    capture_output=True,
                    check=False,
                    timeout=5,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertRegex(
                    result.stdout,
                    rb"^targetctl(?:-(?:request|supervise))? \d+\.\d+\.\d+(?:[-+][^\s]+)?\n$",
                )
        for args in (
            (),
            ("unknown",),
            ("request", "extra"),
            ("supervise", "extra"),
            ("request", "--unknown"),
            ("supervise", "--unknown"),
        ):
            with self.subTest(args=args):
                result = subprocess.run(
                    [self.tool, *args],
                    input=b"",
                    capture_output=True,
                    check=False,
                    timeout=5,
                )
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, b"")
                self.assertTrue(result.stderr)

    def test_protocol_and_capabilities(self):
        self.assertIn("launch", self.request("capabilities", {})[-1]["result"]["operations"])
        self.assertEqual(self.request("bogus", {})[-1]["error"]["category"], "unsupported")
        self.assertEqual(
            self.request("capabilities", {}, version=1)[-1]["error"]["category"], "protocol"
        )
        result = subprocess.run(
            [self.tool, "request"],
            input=b"x" * (1024 * 1024 + 1),
            check=False,
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(json.loads(result.stdout)["event"], "error")

    def test_short_command_orders_ack_before_output(self):
        child = self.start(["/bin/echo", "hello"], lease=5)
        events = self.read_until(child, "completed")
        self.assertEqual(events[0]["event"], "launched")
        self.assertEqual(events[-1]["result"]["exit_code"], 0)
        identity = events[0]["result"]["workload"]
        self.assertNotEqual(identity["pid"], events[0]["result"]["supervisor_pid"])
        self.send(child, "identity", {}, 2)
        error = self.read_until(child, "error")[-1]
        self.assertEqual(error["error"]["errno"], 3)
        self.send(child, "stop", {"grace_seconds": 0}, 3)
        self.read_until(child, "cleanup")
        self.assertEqual(child.wait(timeout=3), 0)

    def test_quiet_workload_flushes_final_records_after_idle(self):
        child = self.start(["/bin/sleep", "20"], lease=10, grace=5)
        self.read_until(child, "launched")
        # Longer than the supervisor's output-stall deadline; no workload output.
        time.sleep(2.2)
        self.send(child, "stop", {"grace_seconds": 5}, 2)
        events = self.read_until(child, "cleanup")
        self.assertEqual([event["event"] for event in events], ["completed", "result", "cleanup"])
        self.assertEqual(events[-1]["result"]["cleanup"], "confirmed")
        self.assertEqual(child.wait(timeout=3), 0)

    def test_lease_and_eof_stop_owned_group(self):
        for eof in (False, True):
            with self.subTest(eof=eof):
                child = self.start(["/bin/sleep", "20"])
                identity = self.read_until(child, "launched")[-1]["result"]["workload"]
                if eof:
                    child.stdin.close()
                events = self.read_until(child, "cleanup")
                self.assertEqual(events[-1]["result"]["cleanup"], "confirmed")
                self.assertEqual(child.wait(timeout=3), 0)
                self.assertFalse(Path(f"/proc/{identity['pid']}").exists())
                final = json.loads((self.root / f"job-{child.pid}" / "final.json").read_text())
                self.assertNotIn("version", final)
                self.assertEqual(final["scope"], "process_group")

    def test_failed_exec_reports_errno_and_cleanup(self):
        child = self.start(["/no/such/executable"])
        events = self.read_until(child, "cleanup")
        error = next(e for e in events if e["event"] == "error")
        self.assertEqual(error["error"]["errno"], 2)
        self.assertEqual(child.wait(timeout=3), 0)

    def test_blocked_stdout_does_not_disable_lease(self):
        child = self.start([sys.executable, "-c", "import os;\nwhile True: os.write(1,b'x'*32768)"])
        self.assertEqual(child.wait(timeout=5), 0)
        final = json.loads((self.root / f"job-{child.pid}" / "final.json").read_text())
        self.assertNotIn("version", final)
        self.assertEqual(final["cleanup"], "confirmed")
        self.assertFalse(final["output_complete"])

    def test_closed_output_channel_marks_recovery_output_incomplete(self):
        child = self.start(
            [
                sys.executable,
                "-c",
                "import os,time;time.sleep(.05);os.write(1,b'x'*65536);time.sleep(10)",
            ],
            lease=5,
        )
        self.read_until(child, "launched")
        child.stdout.close()
        self.assertEqual(child.wait(timeout=5), 0)
        final = json.loads((self.root / f"job-{child.pid}" / "final.json").read_text())
        self.assertNotIn("version", final)
        self.assertEqual(final["cleanup"], "confirmed")
        self.assertFalse(final["output_complete"])

    def test_renewal_extends_lease_and_group_survives_leader_exit(self):
        child = self.start(["/bin/sleep", "20"], lease=0.15)
        self.read_until(child, "launched")
        for request_id in range(2, 7):
            time.sleep(0.06)
            self.send(child, "renew", {}, request_id)
            self.assertIsNone(child.poll())
        self.send(child, "stop", {"grace_seconds": 0}, 7)
        self.read_until(child, "cleanup")
        child.wait(timeout=3)
        descendant = self.root / "descendant"
        child = self.start(["/bin/sh", "-c", f"sleep 20 & echo $! > {descendant}; exit 0"], lease=5)
        self.read_until(child, "launched")
        deadline = time.monotonic() + 2
        while not descendant.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        descendant_pid = int(descendant.read_text())
        self.send(child, "stop", {"grace_seconds": 0.05}, 2)
        self.read_until(child, "cleanup")
        child.wait(timeout=3)
        path = Path(f"/proc/{descendant_pid}/stat")
        if path.exists():
            self.assertIn(path.read_text().rsplit(") ", 1)[1].split()[0], ("Z", "X"))

    def test_binary_output_and_invalid_control_are_bounded(self):
        child = self.start(
            [sys.executable, "-c", "import os;os.write(1,bytes(range(256)));os.write(2,b'err')"],
            lease=5,
        )
        events = self.read_until(child, "completed")
        import base64

        streams = {"stdout": bytearray(), "stderr": bytearray()}
        for event in events:
            if event["event"] == "output":
                value = event["result"]
                streams[value["stream"]].extend(base64.b64decode(value["data"]))
        self.assertEqual(streams["stdout"], bytes(range(256)))
        self.assertEqual(streams["stderr"], b"err")
        child.stdin.write(b"not-json\n")
        child.stdin.flush()
        self.read_until(child, "cleanup")
        self.assertEqual(child.wait(timeout=3), 0)


class ProtocolTests(unittest.TestCase):
    def test_unversioned_response_and_obsolete_envelope(self):
        event = {
            "id": 1,
            "operation": "capabilities",
            "event": "result",
            "started_ns": 0,
            "finished_ns": 1,
            "result": {},
        }
        self.assertEqual(_decode(json.dumps(event).encode()), event)
        with self.assertRaises(TargetctlProtocolError):
            _decode(json.dumps(dict(event, version=1)).encode())

    def test_invalid_frames_and_options(self):
        for line in (b"no", b"[]", b'{"version":2,"id":1}', b'{"version":1,"id":true}'):
            with self.assertRaises(TargetctlProtocolError):
                _decode(line)
        with self.assertRaises(ValueError):
            _encode({"large": "x" * (1024 * 1024)})
        for kwargs in (
            {"lease_timeout": 0},
            {"renewal_interval": 30},
            {"startup_timeout": float("nan")},
            {"disconnect_grace": True},
        ):
            with self.assertRaises(ValueError):
                TargetctlOptions(**kwargs)
        self.assertIsInstance(
            _error({"error": {"category": "os", "errno": 13, "message": "read /proc denied"}}),
            PermissionError,
        )


class OwnedSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_owned_identity_capture_and_selection(self):
        async with Host().connect() as connection:  # noqa: SIM117 -- Separate scopes exercise ownership.
            async with connection.launch(Exec(argv=("/bin/sleep", "10"))) as first:
                async with connection.launch(Exec(argv=("/bin/sleep", "10"))) as other:
                    selection = await first.processes()
                    self.assertEqual(selection[0].pid, first.target_pid)
                    self.assertNotEqual(selection[0].pid, other.target_pid)
                    capture = Capture()
                    capture.attach(first)
                    self.assertEqual(await capture.processes(), selection)
                    await connection.validate_processes(selection)
                    reused = ProcessSet((replace(selection[0], start_ticks=0),))
                    with self.assertRaises(OwnershipError):
                        await connection.validate_processes(reused)
                    waiter = asyncio.create_task(first.wait())
                    await asyncio.sleep(0)
                    waiter.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await waiter
                    self.assertEqual((await first.identity()).pid, first.target_pid)
                    await first.stop(grace=0.05)
                    with self.assertRaises(ProcessLookupError):
                        await first.identity()
        with self.assertRaises(CapabilityUnavailable):
            await Capture().processes()

    async def test_run_provider_launch_action_and_short_command(self):
        with tempfile.TemporaryDirectory() as root:
            async with Run(Path(root) / "run") as run:
                device = await run.connect(Host())
                async with device.use(Launch(argv=("/bin/sleep", "10"))) as process:
                    self.assertEqual(len(await process.processes()), 1)
                result = await device.connection.run(Exec(argv=("/bin/echo", "hello")))
                self.assertEqual(result.stdout, b"hello\n")
                with self.assertRaises(FileNotFoundError):
                    await device.connection.run(Exec(argv=("/no/such/executable",)))

    async def test_helper_crash_preserves_unknown_cleanup_evidence(self):
        import signal

        async with Host().connect() as connection:
            manager = connection.launch(Exec(argv=("/bin/sleep", "20")))
            process = await manager.__aenter__()
            identity = await process.identity()
            try:
                process.channel.process.kill()
                with self.assertRaises(CleanupUnknown):
                    await manager.__aexit__(None, None, None)
                self.assertTrue(connection._retain_workspace)
                self.assertTrue(Path(str(process.directory), "launch.json").is_file())
                self.assertFalse(Path(str(process.directory), "final.json").exists())
            finally:
                try:
                    os.killpg(identity.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        import shutil

        shutil.rmtree(str(connection.tmp_dir))

    async def test_shell_and_process_backed_capture_select_workload(self):
        from vsm.action.poll_files import PollFiles

        async with Host().connect() as connection:  # noqa: SIM117 -- Separate launch ownership.
            async with connection.launch(Shell(script="echo $$; sleep .2")) as process:
                self.assertEqual((await process.identity()).pid, process.target_pid)
                result = await process.wait()
                self.assertEqual(int(result.stdout), process.target_pid)
        with tempfile.TemporaryDirectory() as root:
            async with Run(Path(root) / "run") as run:
                device = await run.connect(Host())
                async with device.use(PollFiles(paths=("/proc/stat",), interval=30)) as capture:
                    selection = await capture.processes()
                    self.assertEqual(len(selection), 1)
                    self.assertEqual(selection[0].pid, capture._process.target_pid)
                    self.assertNotEqual(selection[0].pid, capture._process.channel.process.pid)
                self.assertTrue(capture.log.complete)

    async def test_missing_artifact_is_explicit(self):
        with tempfile.TemporaryDirectory() as root:
            async with Host(binaries=BinaryProvider(Path(root))).connect() as connection:
                with self.assertRaises(CapabilityUnavailable):
                    await connection.run(Exec(argv=("true",)))
