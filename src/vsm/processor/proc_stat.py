from __future__ import annotations

import re
from dataclasses import replace
from typing import ClassVar, cast

import pandas as pd

from vsm.chart.chart import Chart
from vsm.chart.line import lines
from vsm.processing import Params
from vsm.processor._file import NativeFileProcessor
from vsm.processor._parsing import Event, rate_rows, text, time_of
from vsm.util.records import CpuRecord


class CpuProcessor(NativeFileProcessor[CpuRecord]):
    """Compute CPU utilization percent from tick deltas without double-counting guest ticks."""

    name = "proc_stat"
    group_label = "CPU Load"

    snapshot_keys: ClassVar[tuple[str, ...]] = ("cpu",)

    def parse_event(self, e: Event, params: Params) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        if e["type"] != "file":
            return rows
        value, _path, t = (text(e), str(e["path"]), time_of(e))
        for line in value.splitlines():
            parts = line.split()
            if not parts or not re.fullmatch("cpu\\d*", parts[0]):
                continue
            if params.selected and parts[0] not in params.selected:
                continue
            vals = [int(x) for x in parts[1:9]]
            if len(vals) < 4:
                raise ValueError("short CPU counters")
            rows.append(
                {
                    "time": t,
                    "cpu": parts[0],
                    "total": sum(vals),
                    "idle": vals[3] + (vals[4] if len(vals) > 4 else 0),
                    "user": vals[0] + vals[1],
                    "system": vals[2],
                    **{
                        key: vals[index]
                        for index, key in enumerate(
                            (
                                "user_only",
                                "nice",
                                "sys",
                                "idle_only",
                                "iowait",
                                "irq",
                                "softirq",
                                "steal",
                            )
                        )
                        if index < len(vals)
                    },
                }
            )
        return rows

    def finish(self, rows: list[dict[str, object]]) -> list[dict[str, object]]:
        rows = rate_rows(
            rows,
            ("cpu",) + (("observation_epoch",) if rows and "observation_epoch" in rows[0] else ()),
            tuple(
                key
                for key in (
                    "total",
                    "idle",
                    "user",
                    "system",
                    "user_only",
                    "nice",
                    "sys",
                    "idle_only",
                    "iowait",
                    "irq",
                    "softirq",
                    "steal",
                )
                if all(key in row for row in rows)
            ),
        )
        for row in rows:
            total = row["total_per_second"]
            row["busy_percent"] = (
                (1 - float(cast(float, row["idle_per_second"])) / float(cast(float, total))) * 100
                if total
                else None
            )
            for key in (
                "user",
                "system",
                "user_only",
                "nice",
                "sys",
                "idle_only",
                "iowait",
                "irq",
                "softirq",
                "steal",
            ):
                if key + "_per_second" not in row:
                    continue
                row[key + "_percent"] = (
                    float(cast(float, row[key + "_per_second"])) / float(cast(float, total)) * 100
                    if total
                    else None
                )
        return rows

    units: ClassVar[dict[str, str]] = {
        **{
            key + "_percent": "%"
            for key in (
                "user_only",
                "nice",
                "sys",
                "idle_only",
                "iowait",
                "irq",
                "softirq",
                "steal",
            )
        },
        "time": "s",
        "total": "ticks",
        "idle": "ticks",
        "user": "ticks",
        "system": "ticks",
        "total_per_second": "ticks/s",
        "idle_per_second": "ticks/s",
        "user_per_second": "ticks/s",
        "system_per_second": "ticks/s",
        "busy_percent": "%",
        "user_percent": "%",
        "system_percent": "%",
    }

    def charts(self, frame: pd.DataFrame) -> tuple[Chart, ...]:
        return tuple(
            chart
            for field, label in (
                ("user_only_percent", "CPU load · user"),
                ("nice_percent", "CPU load · nice"),
                ("sys_percent", "CPU load · system"),
                ("idle_only_percent", "CPU load · idle"),
                ("iowait_percent", "CPU load · I/O wait"),
                ("irq_percent", "CPU load · IRQ"),
                ("softirq_percent", "CPU load · soft IRQ"),
                ("steal_percent", "CPU load · steal"),
                ("busy_percent", "CPU busy"),
                ("user_percent", "CPU user"),
                ("system_percent", "CPU system"),
            )
            for described in lines(
                frame,
                label,
                (field,),
                ylabel="%",
                group_by=("cpu",)
                + (("observation_epoch",) if "observation_epoch" in frame else ()),
                y_range=(0, 100),
            )
            for chart in (
                replace(described, additional=field in {"user_percent", "system_percent"}),
            )
        )
