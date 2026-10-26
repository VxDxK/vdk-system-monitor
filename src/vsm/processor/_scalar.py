from __future__ import annotations

from typing import ClassVar

import pandas as pd

from vsm.chart.chart import Chart
from vsm.chart.line import lines
from vsm.processing import Params
from vsm.processor._file import NativeFileProcessor
from vsm.processor._parsing import Event, rate_rows, text, time_of
from vsm.util.records import FileRecord


class ScalarProcessor(NativeFileProcessor[FileRecord]):
    chart_title: str | None = None
    column: str
    unit: str
    scale: float = 1
    cumulative: bool = False
    snapshot_keys: ClassVar[tuple[str, ...]] = ("path",)

    def parse_event(self, event: Event, params: Params) -> list[dict[str, object]]:
        if event["type"] != "file":
            return []
        path = str(event["path"])
        if params.selected and path not in params.selected:
            return []
        return [
            {
                "time": time_of(event),
                "path": path,
                self.column: float(text(event).strip()) * self.scale,
            }
        ]

    def finish(self, rows: list[dict[str, object]]) -> list[dict[str, object]]:
        return (
            rate_rows(
                rows,
                ("path",)
                + (("observation_epoch",) if rows and "observation_epoch" in rows[0] else ()),
                (self.column,),
            )
            if self.cumulative
            else rows
        )

    def column_units(self, es: list[Event]) -> dict[str, str]:
        return {"time": "s", self.column: self.unit, self.column + "_per_second": self.unit + "/s"}

    def charts(self, frame: pd.DataFrame) -> tuple[Chart, ...]:
        column = self.column + "_per_second" if self.cumulative else self.column
        return lines(
            frame,
            self.chart_title or self.name.replace("_", " ").capitalize(),
            (column,),
            ylabel=self.unit + "/s" if self.cumulative else self.unit,
            group_by=("path",) + (("observation_epoch",) if "observation_epoch" in frame else ()),
        )
