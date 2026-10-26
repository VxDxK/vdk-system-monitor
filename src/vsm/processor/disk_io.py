from __future__ import annotations

from typing import ClassVar

import pandas as pd

from vsm.chart.chart import Chart
from vsm.chart.line import lines
from vsm.processing import Params
from vsm.processor._file import NativeFileProcessor
from vsm.processor._parsing import Event, rate_rows, text, time_of
from vsm.util.records import FileRecord


class DiskIoProcessor(NativeFileProcessor[FileRecord]):
    """Compute disk throughput and operation rates; accounting sectors are 512 bytes."""

    name = "disk_io"

    snapshot_keys: ClassVar[tuple[str, ...]] = ("major", "minor", "device")
    empty_columns: ClassVar[tuple[str, ...]] = (
        "time",
        "major",
        "minor",
        "device",
        "observation_epoch",
        "read_bytes",
        "write_bytes",
        "reads",
        "writes",
        "read_bytes_per_second",
        "write_bytes_per_second",
    )

    def parse_event(self, e: Event, params: Params) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        if e["type"] != "file":
            return rows
        value, _path, t = (text(e), str(e["path"]), time_of(e))
        for line in value.splitlines():
            v = line.split()
            if len(v) < 14:
                raise ValueError("short diskstats row")
            if params.selected and v[2] not in params.selected:
                continue
            rows.append(
                {
                    "time": t,
                    "major": int(v[0]),
                    "minor": int(v[1]),
                    "device": v[2],
                    "read_bytes": int(v[5]) * 512,
                    "write_bytes": int(v[9]) * 512,
                    "reads": int(v[3]),
                    "writes": int(v[7]),
                }
            )
        return rows

    def finish(self, rows: list[dict[str, object]]) -> list[dict[str, object]]:
        rows = rate_rows(
            rows,
            ("major", "minor", "device")
            + (("observation_epoch",) if rows and "observation_epoch" in rows[0] else ()),
            ("read_bytes", "write_bytes", "reads", "writes"),
        )
        return rows

    units: ClassVar[dict[str, str]] = {
        "time": "s",
        "read_bytes": "B",
        "write_bytes": "B",
        "reads": "operations",
        "writes": "operations",
        "read_bytes_per_second": "B/s",
        "write_bytes_per_second": "B/s",
        "reads_per_second": "operations/s",
        "writes_per_second": "operations/s",
    }

    def charts(self, frame: pd.DataFrame) -> tuple[Chart, ...]:
        return (
            *lines(
                frame,
                "Disk throughput",
                ("read_bytes_per_second", "write_bytes_per_second"),
                ylabel="B/s",
                group_by=("major", "minor", "device")
                + (("observation_epoch",) if "observation_epoch" in frame else ()),
                x="time",
                y_range=None,
            ),
            *lines(
                frame,
                "Disk operations",
                ("reads_per_second", "writes_per_second"),
                ylabel="operations/s",
                group_by=("major", "minor", "device")
                + (("observation_epoch",) if "observation_epoch" in frame else ()),
                x="time",
                y_range=None,
            ),
        )
