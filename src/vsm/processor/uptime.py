from __future__ import annotations

from typing import ClassVar

import pandas as pd

from vsm.chart.chart import Chart
from vsm.chart.line import lines
from vsm.processing import Params
from vsm.processor._file import NativeFileProcessor
from vsm.processor._parsing import Event, text, time_of
from vsm.util.records import FileRecord


class UptimeProcessor(NativeFileProcessor[FileRecord]):
    """Decode uptime, accumulated CPU idle time and boot epoch in seconds."""

    name = "uptime"

    def parse_event(self, e: Event, params: Params) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        if e["type"] != "file":
            return rows
        value, path, t = (text(e), str(e["path"]), time_of(e))
        if params.selected and path not in params.selected:
            return rows
        if path.endswith("/uptime"):
            v = value.split()
            rows.append({"time": t, "uptime_seconds": float(v[0]), "idle_cpu_seconds": float(v[1])})
        else:
            for line in value.splitlines():
                if line.startswith("btime "):
                    rows.append({"time": t, "boot_epoch_seconds": int(line.split()[1])})
        return rows

    units: ClassVar[dict[str, str]] = {
        "time": "s",
        "uptime_seconds": "s",
        "idle_cpu_seconds": "s",
        "boot_epoch_seconds": "s",
    }

    def charts(self, frame: pd.DataFrame) -> tuple[Chart, ...]:
        return (
            *lines(
                frame,
                "Uptime",
                ("uptime_seconds",),
                ylabel="s",
                group_by=("path",),
                x="time",
                y_range=None,
            ),
            *lines(
                frame,
                "Accumulated CPU idle time",
                ("idle_cpu_seconds",),
                ylabel="CPU s",
                group_by=("path",),
                x="time",
                y_range=None,
            ),
            *lines(
                frame,
                "Boot timestamp",
                ("boot_epoch_seconds",),
                ylabel="Unix s",
                group_by=("path",),
                x="time",
                y_range=None,
            ),
        )
