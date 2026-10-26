from __future__ import annotations

from typing import ClassVar, cast

import pandas as pd

from vsm.chart.chart import Chart
from vsm.models import LogRef
from vsm.processing import Dataset, Params, dataset
from vsm.processor._datasets import stable_empty
from vsm.processor._parsing import Event, events
from vsm.processor._snapshots import ObservationEpochs
from vsm.util.records import FileRecord


class NativeFileProcessor[T: FileRecord = FileRecord]:
    """Process a complete native file log into Dataset values with units and provenance.

    The input schema must match the processor. Missing rate baselines and counter
    resets remain unavailable values. Read errors are rejected unless
    Params.allow_read_errors is enabled; warnings are retained.
    """

    name: str
    group_label: str | None = None

    snapshot_keys: ClassVar[tuple[str, ...]] = ()
    empty_columns: ClassVar[tuple[str, ...]] = ()

    units: ClassVar[dict[str, str]] = {"time": "s"}

    def column_units(self, es: list[Event]) -> dict[str, str]:
        return self.units

    def describe(self, data: Dataset) -> Dataset:
        from dataclasses import replace

        return replace(data, charts=self.charts(data.frame))

    def charts(self, frame: pd.DataFrame) -> tuple[Chart, ...]:
        return ()

    def process(self, source: LogRef[T], *, params: Params) -> tuple[Dataset, ...]:
        if source.schema.name != self.name:
            raise ValueError("processor/log schema mismatch")
        return self.transform(source, params, events(cast(LogRef[object], source), params))

    def parse_event(self, event: Event, params: Params) -> list[dict[str, object]]:
        raise NotImplementedError

    def finish(self, rows: list[dict[str, object]]) -> list[dict[str, object]]:
        return rows

    def transform(self, source: LogRef[T], params: Params, es: list[Event]) -> tuple[Dataset, ...]:
        observations = ObservationEpochs()
        parsed = []
        for event in es:
            batch = self.parse_event(event, params)
            if self.snapshot_keys and event["type"] == "file":
                batch = observations.apply(batch, self.snapshot_keys, partition=str(event["path"]))
            elif event["type"] == "read_error":
                observations.invalidate(str(event.get("path")))
            parsed.extend(batch)
        rows = self.finish(parsed)
        warnings = tuple(
            str(e.get("path")) + ": " + str(e.get("error")) for e in es if e["type"] == "read_error"
        )
        empty_snapshot = bool(self.empty_columns) and any(e["type"] == "file" for e in es)
        if not rows and not warnings and not empty_snapshot:
            raise ValueError("no measurement records")
        result = dataset(
            self.name,
            rows,
            units=self.column_units(es),
            warnings=warnings,
            source=cast(LogRef[object], source),
        )
        from dataclasses import replace

        result = replace(result, group_label=self.group_label or self.name)
        if not rows and self.empty_columns:
            return (stable_empty(result, self.empty_columns, self.column_units(es)),)
        return (self.describe(result),)
