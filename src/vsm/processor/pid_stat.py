from __future__ import annotations

from dataclasses import dataclass, replace
from typing import cast

from vsm.chart.line import lines
from vsm.models import LogRef
from vsm.processing import Dataset, Params, dataset
from vsm.processor._datasets import stable_empty
from vsm.processor._parsing import events, proc_stat_line, rate_rows, text, time_of
from vsm.util.records import ProcessRecord


@dataclass(frozen=True)
class PidStatProcessor:
    """Compute process/thread CPU percent, memory bytes and I/O metrics.

    Target tick frequency and page size come from log metadata. CPU utilization
    may exceed 100 percent. Returns Dataset values with units and provenance;
    missing baselines and counter resets remain unavailable.
    """

    name: str = "pid_stat"

    def process(self, source: LogRef[ProcessRecord], *, params: Params) -> tuple[Dataset, ...]:
        if source.schema.name != self.name:
            raise ValueError("processor/log schema mismatch")
        es = events(cast(LogRef[object], source), params)
        native_hz = es[0].get("clock_ticks_per_second")
        native_page = es[0].get("page_size_bytes")
        hz = (
            native_hz
            if isinstance(native_hz, int) and native_hz > 0
            else source.metadata.target.clock_ticks_per_second
        )
        page = (
            native_page
            if isinstance(native_page, int) and native_page > 0
            else source.metadata.target.page_size_bytes
        )
        if not hz or not page:
            raise ValueError("target tick frequency and page size required")
        procs: list[dict[str, object]] = []
        threads: list[dict[str, object]] = []
        ios: list[dict[str, object]] = []
        for e in es:
            if e["type"] != "file":
                continue
            path, value, t = (str(e["path"]), text(e), time_of(e))
            tags = cast(dict[str, object], e.get("tags", {}))
            pid = int(cast(int, tags.get("pid", 0)))
            if path.endswith("/stat"):
                tid, name, f = proc_stat_line(value)
                start = int(f[19])
                row: dict[str, object] = {
                    "time": t,
                    "pid": pid,
                    "tid": tid,
                    "name": name,
                    "start_ticks": start,
                    "process_start_ticks": tags["process_start_ticks"],
                    "user_ticks": int(f[11]),
                    "system_ticks": int(f[12]),
                    "rss_bytes": int(f[21]) * page,
                    "threads": int(f[17]),
                    **({"cpu_binding": int(f[36])} if len(f) > 36 else {}),
                }
                (threads if tags.get("scope") == "thread" else procs).append(row)
            elif path.endswith("/io"):
                counters = {
                    k: int(v.strip())
                    for line in value.splitlines()
                    if ":" in line
                    for k, v in [line.split(":", 1)]
                }
                ios.append(
                    {
                        "time": t,
                        "pid": pid,
                        "start_ticks": tags["process_start_ticks"],
                        **counters,
                    }
                )
        for rows in (procs, threads):
            transformed = rate_rows(
                rows,
                ("pid", "tid", "start_ticks", "process_start_ticks"),
                ("user_ticks", "system_ticks"),
            )
            rows[:] = transformed
            for row in rows:
                for key in ("user", "system"):
                    value = row[key + "_ticks_per_second"]
                    row[key + "_percent"] = (
                        float(cast(float, value)) / hz * 100 if value is not None else None
                    )
                user, system = row["user_percent"], row["system_percent"]
                row["busy_percent"] = (
                    float(user) + float(system) if user is not None and system is not None else None
                )
        warnings = tuple(
            f"{e.get('path', e.get('pid', 'process'))}: {e.get('error')}"
            for e in es
            if e["type"] == "read_error"
        )
        if self.name == "process_io":
            if ios:
                counters = tuple(k for k in ios[0] if k not in ("time", "pid", "start_ticks"))
                ios = rate_rows(ios, ("pid", "start_ticks"), counters)
            return (replace(io_dataset(ios, source), warnings=warnings),)
        results = (
            task_dataset("processes_info", procs, source),
            task_dataset("threads_info", threads, source),
        )
        return tuple(replace(data, warnings=data.warnings + warnings) for data in results)


def task_dataset(
    name: str, rows: list[dict[str, object]], source: LogRef[ProcessRecord]
) -> Dataset:
    result = dataset(
        name,
        rows,
        units={
            "time": "s",
            "rss_bytes": "B",
            "threads": "threads",
            "user_ticks": "ticks",
            "system_ticks": "ticks",
            "user_ticks_per_second": "ticks/s",
            "system_ticks_per_second": "ticks/s",
            "user_percent": "%",
            "system_percent": "%",
            "busy_percent": "%",
            "cpu_binding": "CPU",
        },
        source=cast(LogRef[object], source),
    )
    if result.frame.empty:
        units = {
            "time": "s",
            "rss_bytes": "B",
            "threads": "threads",
            "user_ticks": "ticks",
            "system_ticks": "ticks",
            "user_ticks_per_second": "ticks/s",
            "system_ticks_per_second": "ticks/s",
            "user_percent": "%",
            "system_percent": "%",
            "busy_percent": "%",
            "cpu_binding": "CPU",
        }
        return stable_empty(
            replace(result, group_label="Thread Statistics"),
            (
                "time",
                "pid",
                "tid",
                "name",
                "start_ticks",
                "process_start_ticks",
                "rss_bytes",
                "threads",
                "user_ticks",
                "system_ticks",
                "user_ticks_per_second",
                "system_ticks_per_second",
                "counter_reset",
                "user_percent",
                "system_percent",
                "busy_percent",
                "cpu_binding",
            ),
            units,
        )
    group = ("pid", "tid", "start_ticks", "name", "process_start_ticks")
    frame = result.frame
    if name == "processes_info":
        primary = (
            *lines(frame, "Process RSS", ("rss_bytes",), ylabel="B", group_by=group),
            *lines(frame, "Threads per process", ("threads",), ylabel="threads", group_by=group),
        )
        extra = lines(
            frame, "Process CPU", ("user_percent", "system_percent"), ylabel="%", group_by=group
        )
    else:
        primary = tuple(
            chart
            for column, label, unit in (
                ("user_percent", "Thread load · user", "%"),
                ("system_percent", "Thread load · system", "%"),
                ("busy_percent", "Thread load · total", "%"),
                ("cpu_binding", "Thread CPU binding", "CPU"),
            )
            for chart in lines(frame, label, (column,), ylabel=unit, group_by=group)
        )
        extra = (
            *lines(frame, "Thread RSS", ("rss_bytes",), ylabel="B", group_by=group),
            *lines(frame, "Thread count", ("threads",), ylabel="threads", group_by=group),
        )
    return replace(
        result,
        warnings=result.warnings
        + (
            ("Unavailable: thread CPU binding (not captured).",)
            if name == "threads_info" and "cpu_binding" not in frame and not frame.empty
            else ()
        ),
        group_label="Thread Statistics",
        charts=(*primary, *(replace(chart, additional=True) for chart in extra)),
    )


def io_dataset(rows: list[dict[str, object]], source: LogRef[ProcessRecord]) -> Dataset:
    byte_fields = ("read_bytes", "write_bytes", "cancelled_write_bytes", "rchar", "wchar")
    operation_fields = ("syscr", "syscw")
    units = {
        "time": "s",
        **{key: "B" for key in byte_fields},
        **{key + "_per_second": "B/s" for key in byte_fields},
        **{key: "operations" for key in operation_fields},
        **{key + "_per_second": "operations/s" for key in operation_fields},
    }
    result = dataset("process_io", rows, units=units, source=cast(LogRef[object], source))
    if result.frame.empty:
        return stable_empty(
            result,
            (
                "time",
                "pid",
                "start_ticks",
                *byte_fields,
                *operation_fields,
                *(key + "_per_second" for key in (*byte_fields, *operation_fields)),
                "counter_reset",
            ),
            units,
        )
    return replace(
        result,
        charts=(
            *lines(
                result.frame,
                "Process I/O bytes",
                tuple(key + "_per_second" for key in byte_fields),
                ylabel="B/s",
                group_by=("pid", "start_ticks"),
            ),
            *lines(
                result.frame,
                "Process I/O operations",
                tuple(key + "_per_second" for key in operation_fields),
                ylabel="operations/s",
                group_by=("pid", "start_ticks"),
            ),
        ),
    )
