from __future__ import annotations

from typing import ClassVar

import pandas as pd

from vsm.chart.chart import Chart
from vsm.chart.line import lines
from vsm.processing import Params
from vsm.processor._file import NativeFileProcessor
from vsm.processor._parsing import Event, text, time_of
from vsm.util.records import FileRecord


class LoadAverageProcessor(NativeFileProcessor[FileRecord]):
    """Decode dimensionless Linux load averages and task counts."""

    name = "load_average"

    def parse_event(self, e: Event, params: Params) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        if e["type"] != "file":
            return rows
        value, path, t = (text(e), str(e["path"]), time_of(e))
        if params.selected and path not in params.selected:
            return rows
        v = value.split()
        running, total = v[3].split("/")
        rows.append(
            {
                "time": t,
                "load1": float(v[0]),
                "load5": float(v[1]),
                "load15": float(v[2]),
                "running": int(running),
                "processes": int(total),
            }
        )
        return rows

    units: ClassVar[dict[str, str]] = {
        "time": "s",
        "load1": "tasks",
        "load5": "tasks",
        "load15": "tasks",
        "running": "tasks",
        "processes": "tasks",
    }

    def charts(self, frame: pd.DataFrame) -> tuple[Chart, ...]:
        return (
            *lines(
                frame,
                "Load averages",
                ("load1", "load5", "load15"),
                ylabel="tasks",
                group_by=(),
                x="time",
                y_range=None,
            ),
            *lines(
                frame,
                "Task counts",
                ("running", "processes"),
                ylabel="tasks",
                group_by=(),
                x="time",
                y_range=None,
            ),
        )
