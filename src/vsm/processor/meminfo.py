from __future__ import annotations

from dataclasses import replace

from vsm.chart.line import lines
from vsm.processing import Dataset, Params
from vsm.processor._file import NativeFileProcessor
from vsm.processor._parsing import Event, memory_fields, memory_units, text, time_of
from vsm.util.records import FileRecord


class MeminfoProcessor(NativeFileProcessor[FileRecord]):
    """Process memory bytes/counts/percent without stacking overlapping categories."""

    name = "meminfo"
    group_label = "Memory Info"

    def parse_event(self, e: Event, params: Params) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        if e["type"] != "file":
            return rows
        value, path, t = (text(e), str(e["path"]), time_of(e))
        if params.selected and path not in params.selected:
            return rows
        fields = memory_fields(value)
        if not fields or "MemTotal" not in fields:
            raise ValueError("required memory counters missing")
        row: dict[str, object] = {"time": t, "path": path, **fields}
        total = fields.get("MemTotal", 0)
        available = fields.get("MemAvailable")
        row["used_percent"] = (
            (total - available) / total * 100 if total and available is not None else None
        )
        rows.append(row)
        return rows

    def column_units(self, es: list[Event]) -> dict[str, str]:
        units = {"time": "s", "used_percent": "%"}
        for event in es:
            if event["type"] == "file":
                units.update(memory_units(text(event)))
        return units

    def describe(self, data: Dataset) -> Dataset:
        physical = (
            "MemTotal",
            "MemFree",
            "MemAvailable",
            "Buffers",
            "Cached",
            "SReclaimable",
            "Shmem",
        )
        capacity = ("VmallocTotal", "VmallocChunk")
        byte_fields = tuple(column.name for column in data.columns if column.unit == "B")
        charts = (
            *lines(data.frame, "Physical memory", physical, ylabel="B", group_by=()),
            *lines(
                data.frame,
                "Memory accounting",
                tuple(field for field in byte_fields if field not in physical + capacity),
                ylabel="B",
                group_by=(),
            ),
            *lines(
                data.frame,
                "Kernel virtual address space capacity",
                capacity,
                ylabel="B",
                group_by=(),
            ),
            *lines(
                data.frame,
                "Memory counters",
                tuple(column.name for column in data.columns if column.unit == "count"),
                ylabel="count",
                group_by=(),
            ),
            *lines(
                data.frame,
                "Memory usage percent",
                ("used_percent",),
                ylabel="%",
                group_by=(),
                y_range=(0, 100),
            ),
        )
        overview = lines(data.frame, "Memory usage", byte_fields, ylabel="B", group_by=())
        return replace(
            data,
            charts=(
                *overview,
                *(
                    chart if chart.label == "Memory counters" else replace(chart, additional=True)
                    for chart in charts
                ),
            ),
        )
