"""Measure a Host rgen producer and SSH consumer until Enter, then export CSV/charts.

Usage: python examples/manual_ssh.py NAME HOST --protocol tcp
Requires built native artifacts, Linux x86-64 on both machines, known SSH host
key, SFTP, readable CPU-frequency sources and access to each workload's /proc.
HOST must accept direct traffic on --port; no SSH tunnel is created. Use
--auth password (default) to prompt without echo, or --auth key for ~/.ssh/id_rsa.
Press Enter to stop; EOF or
cancellation aborts export with cleanup. All output stays on the controller.
"""

import argparse
import asyncio
import getpass
import socket
import time
from dataclasses import replace
from datetime import timedelta, timezone
from ipaddress import IPv4Address, IPv6Address, ip_address
from pathlib import Path

from vsm.action.cpu_freq import CpuFreq
from vsm.action.manual import Manual
from vsm.action.meminfo import Meminfo
from vsm.action.net_dev import NetDev
from vsm.action.pid_stat import PidStat
from vsm.action.proc_stat import ProcStat
from vsm.action.smaps import Smaps
from vsm.action.traffic_generator import TrafficGenerator
from vsm.connection.host import Host
from vsm.connection.ssh import SSH
from vsm.output.charts import ExportOptions, export_charts
from vsm.processing import Params, process
from vsm.processor.cpu_freq import CpuFreqProcessor
from vsm.processor.meminfo import MeminfoProcessor
from vsm.processor.net_dev import NetProcessor
from vsm.processor.pid_stat import PidStatProcessor
from vsm.processor.proc_stat import CpuProcessor
from vsm.processor.smaps import SmapsProcessor
from vsm.processor.traffic_generator import TrafficGeneratorProcessor
from vsm.runtime import Run
from vsm.util.network import Endpoint, TrafficProtocol, TrafficRole

ROOT = Path(__file__).resolve().parents[1]


async def resolve_address(host: str, port: int, address: str | None) -> Endpoint:
    if address is not None:
        return Endpoint(ip_address(address), port)
    addresses = await asyncio.get_running_loop().getaddrinfo(
        host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM
    )
    return Endpoint(ip_address(addresses[0][4][0]), port)


async def main(
    name: str,
    host: str,
    password: str | None = None,
    *,
    client_keys: tuple[Path, ...] = (),
    port: int = 5000,
    protocol: TrafficProtocol = TrafficProtocol.TCP,
    address: str | None = None,
    workers: int = 1,
    payload_bytes: int = 1024,
    rate_bps: int | None = None,
) -> None:
    endpoint = await resolve_address(host, port, address)
    wildcard = IPv4Address("0.0.0.0") if endpoint.address.version == 4 else IPv6Address("::")
    output = ROOT / "runs" / f"manual-ssh-{time.time_ns()}"
    async with Run(output) as run:
        client = await run.connect(Host())
        server = await run.connect(
            SSH(destination=host, username=name, password=password, client_keys=client_keys)
        )
        async with (
            client.use(CpuFreq()) as client_frequency,
            client.use(ProcStat()) as client_cpu,
            client.use(Meminfo()) as client_memory,
            client.use(NetDev()) as client_network,
            server.use(CpuFreq()) as server_frequency,
            server.use(ProcStat()) as server_cpu,
            server.use(Meminfo()) as server_memory,
            server.use(NetDev()) as server_network,
            server.use(
                TrafficGenerator(
                    address=Endpoint(wildcard, port),
                    role=TrafficRole.CONSUMER,
                    protocol=protocol,
                    workers=workers,
                    payload_bytes=payload_bytes,
                )
            ) as consumer,
        ):
            server_processes = await consumer.processes()
            async with (
                server.use(PidStat(processes=server_processes)) as server_stats,
                server.use(Smaps(processes=server_processes)) as server_smaps,
                client.use(
                    TrafficGenerator(
                        address=endpoint,
                        role=TrafficRole.PRODUCER,
                        protocol=protocol,
                        workers=workers,
                        payload_bytes=payload_bytes,
                        rate_bps=rate_bps,
                    )
                ) as producer,
            ):
                client_processes = await producer.processes()
                async with (
                    client.use(PidStat(processes=client_processes)) as client_stats,
                    client.use(Smaps(processes=client_processes)) as client_smaps,
                ):
                    await client.do(
                        Manual(
                            prompt=f"{protocol.upper()} rgen client to {endpoint} "
                            f"(client PID {client_processes[0].pid}, server PID {server_processes[0].pid}). "
                            "Press Enter to stop and process data."
                        )
                    )
            # Stop the producer before the consumer; finalize collectors before processing.
        jobs = (
            (
                "client",
                run.spawn(process(CpuFreqProcessor(), client_frequency.log, params=Params())),
            ),
            ("client", run.spawn(process(CpuProcessor(), client_cpu.log, params=Params()))),
            ("client", run.spawn(process(MeminfoProcessor(), client_memory.log, params=Params()))),
            ("client", run.spawn(process(NetProcessor(), client_network.log, params=Params()))),
            (
                "client",
                run.spawn(process(PidStatProcessor(), client_stats.log, params=Params())),
            ),
            (
                "client",
                run.spawn(process(SmapsProcessor(), client_smaps.log, params=Params())),
            ),
            (
                "client",
                run.spawn(process(TrafficGeneratorProcessor(), producer.log, params=Params())),
            ),
            (
                "server",
                run.spawn(process(CpuFreqProcessor(), server_frequency.log, params=Params())),
            ),
            ("server", run.spawn(process(CpuProcessor(), server_cpu.log, params=Params()))),
            ("server", run.spawn(process(MeminfoProcessor(), server_memory.log, params=Params()))),
            ("server", run.spawn(process(NetProcessor(), server_network.log, params=Params()))),
            (
                "server",
                run.spawn(process(PidStatProcessor(), server_stats.log, params=Params())),
            ),
            (
                "server",
                run.spawn(process(SmapsProcessor(), server_smaps.log, params=Params())),
            ),
            (
                "server",
                run.spawn(process(TrafficGeneratorProcessor(), consumer.log, params=Params())),
            ),
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
            title="Host client and SSH server rgen metrics",
            options=ExportOptions(timezone=timezone(timedelta(hours=3))),
        )
    print(output)


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", help="SSH login username")
    parser.add_argument(
        "host", help="SSH hostname or IP address; also the default traffic destination"
    )
    parser.add_argument(
        "--auth",
        choices=("password", "key"),
        default="password",
        help="SSH authentication: password prompt (default) or ~/.ssh/id_rsa",
    )
    parser.add_argument("--port", type=int, default=5000, help="direct traffic port (not SSH)")
    parser.add_argument(
        "--protocol",
        type=TrafficProtocol,
        choices=tuple(TrafficProtocol),
        default=TrafficProtocol.TCP,
    )
    parser.add_argument(
        "--address", help="override traffic destination with a numeric IPv4/IPv6 address"
    )
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--payload-bytes", type=int, default=1024)
    parser.add_argument(
        "--rate-bps", type=int, help="aggregate producer bit/s limit; default unlimited"
    )
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be in 1..65535")
    if not 1 <= args.workers <= 256:
        parser.error("--workers must be in 1..256")
    if not 1 <= args.payload_bytes <= 65507:
        parser.error("--payload-bytes must be in 1..65507")
    if args.rate_bps is not None and args.rate_bps <= 0:
        parser.error("--rate-bps must be positive")
    if args.address is not None:
        try:
            ip_address(args.address)
        except ValueError:
            parser.error("--address must be a numeric IPv4/IPv6 address")
    auth = vars(args).pop("auth")
    password = (
        getpass.getpass(f"Password for {args.name}@{args.host}: ") if auth == "password" else None
    )
    client_keys = (Path("~/.ssh/id_rsa").expanduser(),) if auth == "key" else ()
    asyncio.run(main(password=password, client_keys=client_keys, **vars(args)))


if __name__ == "__main__":
    cli()
