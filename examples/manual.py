"""Measure a local TCP rup client/server pair until Enter, then export charts/CSV.

Requires built native artifacts, readable CPU frequency sources and permission to
read both workloads' /proc files. Shared host metrics are collected once. Press
Enter to stop; EOF or cancellation aborts export with cleanup.
"""

import argparse
import asyncio
import time
from dataclasses import replace
from datetime import timedelta, timezone
from pathlib import Path

from vsm.action.cpu_freq import CpuFreq
from vsm.action.fans import Fans
from vsm.action.manual import Manual
from vsm.action.meminfo import Meminfo
from vsm.action.net_dev import NetDev
from vsm.action.pid_stat import PidStat
from vsm.action.proc_stat import ProcStat
from vsm.action.rup import Rup
from vsm.action.smaps import Smaps
from vsm.connection.host import Host
from vsm.output.charts import ExportOptions, export_charts
from vsm.processing import Params, process
from vsm.processor.cpu_freq import CpuFreqProcessor
from vsm.processor.fans import FansProcessor
from vsm.processor.meminfo import MeminfoProcessor
from vsm.processor.net_dev import NetProcessor
from vsm.processor.pid_stat import PidStatProcessor
from vsm.processor.proc_stat import CpuProcessor
from vsm.processor.rup import RupProcessor
from vsm.processor.smaps import SmapsProcessor
from vsm.runtime import Run
from vsm.util.network import Endpoint, ProbeProtocol, ProbeRole

ROOT = Path(__file__).resolve().parents[1]


async def main(port: int = 5000) -> None:
    address = Endpoint.parse(f"127.0.0.1:{port}")
    output = ROOT / "runs" / f"manual-{time.time_ns()}"
    async with Run(output) as run:
        host = await run.connect(Host())
        async with (
            host.use(CpuFreq()) as frequency,
            host.use(ProcStat()) as cpu,
            host.use(Meminfo()) as memory,
            host.use(NetDev()) as network,
            host.use(Fans()) as fans,
            host.use(
                Rup(
                    address=address,
                    role=ProbeRole.SERVER,
                    protocol=ProbeProtocol.TCP,
                )
            ) as server,
        ):
            server_processes = await server.processes()
            async with (
                host.use(PidStat(processes=server_processes)) as server_stats,
                host.use(Smaps(processes=server_processes)) as server_smaps,
                host.use(Rup(address=address, protocol=ProbeProtocol.TCP)) as client,
            ):
                client_processes = await client.processes()
                async with (
                    host.use(PidStat(processes=client_processes)) as client_stats,
                    host.use(Smaps(processes=client_processes)) as client_smaps,
                ):
                    await host.do(
                        Manual(
                            prompt=f"TCP rup client/server at {address} "
                            f"(client PID {client_processes[0].pid}, server PID {server_processes[0].pid}). "
                            "Press Enter to stop and process data."
                        )
                    )
            # Finalize the client while the server can still answer draining probes.
        # Server output is a raw service log, never a RupProcessor input.
        jobs = (
            ("host", run.spawn(process(CpuFreqProcessor(), frequency.log, params=Params()))),
            ("host", run.spawn(process(CpuProcessor(), cpu.log, params=Params()))),
            ("host", run.spawn(process(MeminfoProcessor(), memory.log, params=Params()))),
            ("host", run.spawn(process(NetProcessor(), network.log, params=Params()))),
            ("host", run.spawn(process(FansProcessor(), fans.log, params=Params()))),
            ("client", run.spawn(process(PidStatProcessor(), client_stats.log, params=Params()))),
            ("client", run.spawn(process(SmapsProcessor(), client_smaps.log, params=Params()))),
            ("client", run.spawn(process(RupProcessor(), client.log, params=Params()))),
            ("server", run.spawn(process(PidStatProcessor(), server_stats.log, params=Params()))),
            ("server", run.spawn(process(SmapsProcessor(), server_smaps.log, params=Params()))),
        )
        batches = [(role, await task) for role, task in jobs]
        results = []
        for index, (role, batch) in enumerate(batches):
            for item_index, data in enumerate(batch):
                labeled = replace(data, group_label=f"{role}: {data.group_label}")
                labeled.to_csv(
                    output / role / f"{index:02d}-{item_index:02d}-{data.data_label}.csv"
                )
                results.append(labeled)
        export_charts(
            results,
            output / "charts.html",
            title="Local rup client/server metrics",
            options=ExportOptions(timezone=timezone(timedelta(hours=3))),
        )
    print(output)


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be in 1..65535")
    asyncio.run(main(args.port))


if __name__ == "__main__":
    cli()
