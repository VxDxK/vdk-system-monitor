from __future__ import annotations

from typing import ClassVar, cast

import pandas as pd

from vsm.chart.chart import Chart
from vsm.chart.line import lines
from vsm.processing import Params
from vsm.processor._file import NativeFileProcessor
from vsm.processor._parsing import Event, text, time_of
from vsm.util.records import FileRecord


class FilesystemUsageProcessor(NativeFileProcessor[FileRecord]):
    """Compute filesystem capacity and usage in bytes and percent."""

    name = "filesystem_usage"

    def parse_event(self, e: Event, params: Params) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        if e["type"] == "filesystem":
            total = int(cast(int, e["total_bytes"]))
            free = int(cast(int, e["free_bytes"]))
            available = int(cast(int, e["available_bytes"]))
            rows.append(
                {
                    "time": time_of(e),
                    "path": e["path"],
                    "total_bytes": total,
                    "free_bytes": free,
                    "available_bytes": available,
                    "used_bytes": total - free,
                    "used_percent": (total - free) / total * 100 if total else None,
                }
            )
        if e["type"] != "file":
            return rows
        value, path, t = (text(e), str(e["path"]), time_of(e))
        if params.selected and path not in params.selected:
            return rows
        key = "level_percent"
        scale = 1
        rows.append({"time": t, "path": path, key: float(value.strip()) * scale})
        return rows

    units: ClassVar[dict[str, str]] = {
        "time": "s",
        "total_bytes": "B",
        "free_bytes": "B",
        "available_bytes": "B",
        "used_bytes": "B",
        "used_percent": "%",
        "level_percent": "%",
    }

    def charts(self, frame: pd.DataFrame) -> tuple[Chart, ...]:
        return (
            *lines(
                frame,
                "Filesystem usage",
                ("total_bytes", "free_bytes", "available_bytes", "used_bytes"),
                ylabel="B",
                group_by=("path",),
                x="time",
                y_range=None,
            ),
            *lines(
                frame,
                "Filesystem usage percent",
                ("used_percent", "level_percent"),
                ylabel="%",
                group_by=("path",),
                x="time",
                y_range=None,
            ),
        )
