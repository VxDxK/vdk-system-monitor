from __future__ import annotations

from typing import ClassVar

import pandas as pd

from vsm.chart.chart import Chart
from vsm.chart.line import lines
from vsm.processing import Params
from vsm.processor._file import NativeFileProcessor
from vsm.processor._parsing import Event, memory_fields, text, time_of
from vsm.util.records import FileRecord


class SwapProcessor(NativeFileProcessor[FileRecord]):
    """Compute swap bytes and utilization percent with explicit absent-swap handling."""

    name = "swap"

    def parse_event(self, e: Event, params: Params) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        if e["type"] != "file":
            return rows
        value, path, t = (text(e), str(e["path"]), time_of(e))
        if params.selected and path not in params.selected:
            return rows
        fields = memory_fields(value)
        if not fields:
            raise ValueError("required memory counters missing")
        row: dict[str, object] = {"time": t, "path": path, **fields}
        if "SwapTotal" not in fields or "SwapFree" not in fields:
            raise ValueError("swap counters missing")
        total = fields["SwapTotal"]
        used = total - fields["SwapFree"]
        row = {
            "time": t,
            "total_bytes": total,
            "used_bytes": used,
            "used_percent": used / total * 100 if total else None,
            "swap_present": bool(total),
        }
        rows.append(row)
        return rows

    units: ClassVar[dict[str, str]] = {
        "time": "s",
        "total_bytes": "B",
        "used_bytes": "B",
        "used_percent": "%",
    }

    def charts(self, frame: pd.DataFrame) -> tuple[Chart, ...]:
        return (
            *lines(
                frame,
                "Swap usage",
                ("total_bytes", "used_bytes"),
                ylabel="B",
                group_by=(),
                x="time",
                y_range=None,
            ),
            *lines(
                frame,
                "Swap usage percent",
                ("used_percent",),
                ylabel="%",
                group_by=(),
                x="time",
                y_range=None,
            ),
        )
