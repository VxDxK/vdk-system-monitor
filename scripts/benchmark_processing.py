"""Measure CPU/process parsing in fresh Linux subprocesses; no target access required."""

from __future__ import annotations

import argparse
import json
import platform
import resource
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
from importlib.metadata import version
from pathlib import Path


def write_log(kind: str, path: Path, ticks: int) -> None:
    """Write a complete native stream with two CPU rows or process/thread rows per tick."""
    with path.open("w") as stream:
        sequence = 0

        def emit(event: dict[str, object], tick: int = 0) -> None:
            nonlocal sequence
            stream.write(json.dumps(dict(event, sequence=sequence, time_ns=tick * 10**9)) + "\n")
            sequence += 1

        emit(
            {
                "type": "metadata",
                "schema": "rfpoll" if kind == "cpu" else "pspider",
                "version": version("vsm"),
                "os": "linux",
                "architecture": "x86_64",
                "kernel": "synthetic",
                "boot_id": "benchmark",
                "clock_ticks_per_second": 100,
                "page_size_bytes": 4096,
            }
        )
        emit({"type": "ready"})
        tags = {
            "pid": 123,
            "tid": 123,
            "scope": "process",
            "start_ticks": 99,
            "process_start_ticks": 99,
        }
        thread = dict(tags, tid=124, scope="thread", start_ticks=100)

        def file(name: str, value: str, tick: int, identity: dict | None = None) -> None:
            emit(
                {
                    "type": "file",
                    "path": name,
                    "encoding": "hex",
                    "data": value.encode().hex(),
                    **({"tags": identity} if identity is not None else {}),
                },
                tick,
            )

        for tick in range(ticks):
            emit({"type": "sample"}, tick)
            if kind == "cpu":
                counters = f"{100 + tick * 10} 0 {50 + tick * 5} {850 + tick * 85} 0 0 0 0"
                file("/proc/stat", f"cpu {counters}\ncpu0 {counters}\n", tick)
            else:
                for identity, prefix in ((tags, "/proc/123"), (thread, "/proc/123/task/124")):
                    fields = ["0"] * 38
                    fields[0] = "S"
                    fields[11], fields[12] = str(100 + tick * 10), str(50 + tick * 5)
                    fields[17], fields[19], fields[21] = "2", str(identity["start_ticks"]), "2"
                    value = f"{identity['tid']} (benchmark) " + " ".join(fields) + "\n"
                    file(prefix + "/stat", value, tick, identity)
                file(
                    "/proc/123/io",
                    f"read_bytes: {tick * 4096}\nwrite_bytes: {tick * 1024}\n",
                    tick,
                    tags,
                )
        if kind == "process":
            for identity in (thread, tags):
                emit({"type": "entity_end", "tags": identity, "reason": "exited"}, ticks)
        emit({"type": "end", "interrupted": False}, ticks)


def measure(kind: str, path: Path, mode: str) -> dict[str, object]:
    import pandas as pd

    from vsm.models import Schema
    from vsm.processing import Params, load_native
    from vsm.processor.pid_stat import PidStatProcessor
    from vsm.processor.proc_stat import CpuProcessor

    processor = CpuProcessor() if kind == "cpu" else PidStatProcessor()
    source = load_native(path, schema=Schema(name=processor.name))
    baseline = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    if mode == "allocations":
        tracemalloc.start()
    started = time.perf_counter()
    datasets = processor.process(source, params=Params())
    elapsed = time.perf_counter() - started
    # Read peaks before computing deep dataframe sizes, which can allocate temporary objects.
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    allocations = tracemalloc.get_traced_memory()[1] if mode == "allocations" else None
    if mode == "allocations":
        tracemalloc.stop()
    return {
        "status": "ok",
        "kind": kind,
        "mode": mode,
        "input_bytes": path.stat().st_size,
        "wall_seconds": elapsed,
        "baseline_peak_rss_bytes": baseline,
        "peak_rss_bytes": peak,
        "python_allocation_peak_bytes": allocations,
        "dataframe_bytes": sum(int(data.frame.memory_usage(deep=True).sum()) for data in datasets),
        "rows": [len(data.frame) for data in datasets],
        "python": platform.python_version(),
        "vsm": version("vsm"),
        "pandas": pd.__version__,
        "platform": platform.platform(),
    }


def run_case(kind: str, path: Path, mode: str, *, timeout: float = 120) -> dict[str, object]:
    command = [sys.executable, str(Path(__file__).resolve()), "--worker", kind, str(path), mode]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, check=True, timeout=timeout
        )
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "kind": kind, "mode": mode, "timeout_seconds": timeout}
    except subprocess.CalledProcessError as error:
        return {
            "status": "error",
            "kind": kind,
            "mode": mode,
            "exit_code": error.returncode,
            "stderr": error.stderr,
        }
    except ValueError as error:
        return {"status": "error", "kind": kind, "mode": mode, "error": str(error)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=[1000, 10000, 100000])
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument(
        "--kinds", nargs="+", choices=("cpu", "process"), default=["cpu", "process"]
    )
    parser.add_argument("--output", type=Path, default=Path("reports/processing-benchmark.json"))
    parser.add_argument(
        "--worker", nargs=3, metavar=("KIND", "PATH", "MODE"), help=argparse.SUPPRESS
    )
    args = parser.parse_args()
    if args.worker:
        kind, path, mode = args.worker
        print(json.dumps(measure(kind, Path(path), mode)))
        return 0
    if min(args.sizes) < 2 or args.repetitions < 1 or not 0 < args.timeout <= 120:
        parser.error("sizes must be >=2, repetitions >=1, and timeout in (0, 120]")
    results = []
    report = {
        "parameters": {
            "sizes": args.sizes,
            "repetitions": args.repetitions,
            "timeout_seconds": args.timeout,
            "kinds": args.kinds,
        },
        "results": results,
        "medians": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="vsm-processing-benchmark-") as directory:
        path = Path(directory) / "input.jsonl"
        for kind in args.kinds:
            for ticks in args.sizes:
                write_log(kind, path, ticks)
                for mode in ("rss", "allocations"):
                    cases = []
                    for repetition in range(args.repetitions):
                        result = run_case(kind, path, mode, timeout=args.timeout)
                        result.update(
                            ticks=ticks, repetition=repetition + 1, input_bytes=path.stat().st_size
                        )
                        cases.append(result)
                        results.append(result)
                        args.output.write_text(json.dumps(report, indent=2) + "\n")
                        print(
                            f"{kind} ticks={ticks} {mode} run={repetition + 1}: {result['status']}",
                            flush=True,
                        )
                    successful = [r for r in cases if r["status"] == "ok"]
                    if successful:
                        keys = ("wall_seconds", "peak_rss_bytes", "dataframe_bytes") + (
                            ("python_allocation_peak_bytes",) if mode == "allocations" else ()
                        )
                        report["medians"].append(
                            {
                                "kind": kind,
                                "ticks": ticks,
                                "mode": mode,
                                "successful_runs": len(successful),
                                **{
                                    key: statistics.median(r[key] for r in successful)
                                    for key in keys
                                },
                            }
                        )
                    args.output.write_text(json.dumps(report, indent=2) + "\n")
    return int(any(result["status"] != "ok" for result in results))


if __name__ == "__main__":
    raise SystemExit(main())
