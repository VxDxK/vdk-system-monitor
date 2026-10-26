"""Shared real Linux acceptance for Host and isolated loopback SSH."""

import asyncio
import json
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

from vsm.action.meminfo import Meminfo
from vsm.action.pid_stat import PidStat
from vsm.action.poll_files import PollFiles
from vsm.action.proc_stat import ProcStat
from vsm.action.process_io import ProcessIo
from vsm.action.smaps import Smaps
from vsm.action.socket_stat import SocketStat
from vsm.models import CapabilityUnavailable, Exec, OwnershipError
from vsm.processing import Params, process
from vsm.processor.meminfo import MeminfoProcessor
from vsm.processor.pid_stat import PidStatProcessor
from vsm.processor.proc_stat import CpuProcessor
from vsm.processor.process_io import ProcessIoProcessor
from vsm.processor.smaps import SmapsProcessor
from vsm.processor.socket_stat import SocketStatProcessor
from vsm.runtime import Run


async def command_deadline(test, backend):
    """Expire a caller deadline after an owned workload has actually started."""
    async with asyncio.timeout(15), backend.connect() as connection:
        original = connection.launch
        selection = None

        @asynccontextmanager
        async def launch(command, **kwargs):
            nonlocal selection
            async with original(command, **kwargs) as process:
                selection = await process.processes()
                deadline.reschedule(asyncio.get_running_loop().time())
                yield process

        with patch.object(connection, "launch", launch), test.assertRaises(TimeoutError):
            async with asyncio.timeout(None) as deadline:
                await connection.run(Exec(argv=("/bin/sleep", "30")))
        test.assertIsNotNone(selection)
        with test.assertRaises((ProcessLookupError, FileNotFoundError)):
            await connection.validate_processes(selection)
        result = await connection.run(Exec(argv=("printf", "reused")))
        test.assertEqual(result.stdout, b"reused")


async def finite_collection(test, backend, root):
    output = root / "run"
    async with asyncio.timeout(30), Run(output) as run:
        device = await run.connect(backend)
        workspace = Path(str(device.connection.tmp_dir))
        unrelated = run.spawn(asyncio.sleep(0, result="healthy"))
        with test.assertRaises(CapabilityUnavailable):
            async with device.use(PollFiles(paths=(str(device.connection.tmp_dir / "missing"),))):
                test.fail("missing source accepted")
        # Both backends execute on this machine; keep a socket present in snapshots.
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            async with device.connection.launch(Exec(argv=("/bin/sleep", "30"))) as workload:
                selection = await workload.processes()
                for collector, processor in (
                    (ProcStat, CpuProcessor()),
                    (Meminfo, MeminfoProcessor()),
                    (PidStat, PidStatProcessor()),
                    (ProcessIo, ProcessIoProcessor()),
                    (Smaps, SmapsProcessor()),
                    (SocketStat, SocketStatProcessor()),
                ):
                    with test.subTest(collector=collector.__name__):
                        kwargs = (
                            {"processes": selection}
                            if collector in (PidStat, ProcessIo, Smaps)
                            else {}
                        )
                        async with device.use(
                            collector(interval=10, duration=0.15, **kwargs)
                        ) as cap:
                            await cap.wait()
                            # Give real transport supervision time to observe natural completion.
                            await asyncio.sleep(0.02)
                            with test.assertRaises(OwnershipError):
                                _ = cap.log
                        test.assertTrue(cap.log.complete)
                        datasets = await process(processor, cap.log, params=Params())
                        test.assertTrue(any(not data.frame.empty for data in datasets))
                        test.assertTrue(all(not data.warnings for data in datasets))
                        test.assertTrue(all(data.provenance.complete for data in datasets))
            with test.assertRaises((ProcessLookupError, FileNotFoundError)):
                await device.connection.validate_processes(selection)
        test.assertEqual(await unrelated, "healthy")
        result = await device.connection.run(Exec(argv=("printf", "reused")))
        test.assertEqual(result.stdout, b"reused")
    test.assertFalse(workspace.exists())
    manifest = json.loads((output / "run.json").read_text())
    test.assertEqual(manifest["status"], "finished")
    test.assertEqual(manifest["cleanup_errors"], [])
