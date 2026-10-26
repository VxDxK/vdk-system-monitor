from __future__ import annotations

from dataclasses import replace
from typing import cast

from vsm.chart.line import lines
from vsm.models import LogRef
from vsm.processing import Dataset, Params, dataset
from vsm.processor._datasets import stable_empty
from vsm.processor._file import NativeFileProcessor
from vsm.processor._parsing import Event, memory_fields, memory_units, text, time_of
from vsm.util.records import FileRecord


class SmapsProcessor(NativeFileProcessor[FileRecord]):
    """Aggregate process memory mappings in bytes; Linux kB means 1024 bytes."""

    name = "smaps"
    group_label = "Process Smaps"

    def parse_event(self, e: Event, params: Params) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        if e["type"] != "file":
            return rows
        value, path, t = (text(e), str(e["path"]), time_of(e))
        if params.selected and path not in params.selected:
            return rows
        fields = memory_fields(value)
        if not fields or "Rss" not in fields:
            raise ValueError("required memory counters missing")
        tags = cast(dict[str, object], e.get("tags", {}))
        identity = {"pid": tags["pid"], "start_ticks": tags["process_start_ticks"]}
        row: dict[str, object] = {"time": t, "path": path, **identity, **fields}
        rows.append(row)
        return rows

    def transform(
        self, source: LogRef[FileRecord], params: Params, es: list[Event]
    ) -> tuple[Dataset, ...]:
        if any(e["type"] == "entity_end" for e in es) and not any(
            self.parse_event(e, params) for e in es
        ):
            data = dataset(self.name, [], source=cast(LogRef[object], source))
            data = replace(
                data,
                group_label=self.group_label,
                warnings=tuple(
                    f"{e.get('path')}: {e.get('error')}" for e in es if e["type"] == "read_error"
                ),
            )
            return (
                stable_empty(
                    data,
                    ("time", "path", "pid", "start_ticks", "Rss", "Pss"),
                    {"time": "s", "Rss": "B", "Pss": "B"},
                ),
            )
        return super().transform(source, params, es)

    def column_units(self, es: list[Event]) -> dict[str, str]:
        units = {"time": "s", "used_percent": "%"}
        for event in es:
            if event["type"] == "file":
                units.update(memory_units(text(event)))
        return units

    def describe(self, data: Dataset) -> Dataset:
        groups = (
            ("B", "Memory bytes"),
            ("count", "Memory counters"),
            ("%", "Memory usage percent"),
        )
        charts = tuple(
            chart
            for unit, label in groups
            for chart in lines(
                data.frame,
                label,
                tuple(column.name for column in data.columns if column.unit == unit),
                ylabel=unit,
                group_by=("path", "pid", "start_ticks"),
            )
        )
        return replace(data, charts=charts)
