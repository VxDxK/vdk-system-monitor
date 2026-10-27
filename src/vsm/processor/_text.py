from __future__ import annotations

from dataclasses import replace
from typing import ClassVar, cast

import pandas as pd

from vsm.chart.chart import Chart
from vsm.clock import TimeOrigin
from vsm.models import LogRef
from vsm.processing import Dataset, Params, dataset
from vsm.processor._datasets import stable_empty
from vsm.util.records import TextRecord


class TextProcessor:
    """Process supported target-tool text or JSON output into Dataset values.

    Input schemas must match. Incomplete logs require Params.allow_partial;
    that opt-in does not repair invalid or unsupported formats. Target executable
    versions may differ in their output contracts.
    """

    name: str
    group_label: str | None = None

    empty_columns: ClassVar[tuple[str, ...]] = ()

    units: ClassVar[dict[str, str]] = {"time": "s"}

    def charts(self, frame: pd.DataFrame) -> tuple[Chart, ...]:
        return ()

    def time_origin(self, raw: str, source: LogRef[TextRecord]) -> TimeOrigin | None:
        return source.metadata.time_origin

    def process(self, source: LogRef[TextRecord], *, params: Params) -> tuple[Dataset, ...]:
        if source.schema.name != self.name:
            raise ValueError("processor/log schema mismatch")
        if not source.complete and (not params.allow_partial):
            raise ValueError("incomplete external log")
        raw = source.path.read_text()
        rows = self.parse(raw, params)
        source = replace(
            source, metadata=replace(source.metadata, time_origin=self.time_origin(raw, source))
        )
        if not rows and not self.empty_columns:
            raise ValueError(f"no valid {self.name} records")
        result = dataset(self.name, rows, units=self.units, source=cast(LogRef[object], source))
        if not rows:
            result = replace(result, group_label=self.group_label or self.name)
            return (stable_empty(result, self.empty_columns, self.units),)
        return (
            replace(
                result, group_label=self.group_label or self.name, charts=self.charts(result.frame)
            ),
        )

    def parse(self, data: str, params: Params) -> list[dict[str, object]]:
        raise NotImplementedError
