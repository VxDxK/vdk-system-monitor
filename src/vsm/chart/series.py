"""Shared selection and entity grouping for line, box, and bar charts."""

from collections.abc import Iterator
from dataclasses import dataclass
from html import escape

import pandas as pd

from vsm.util.labels import label_frame


@dataclass(frozen=True, kw_only=True)
class SeriesChart:
    sparse: bool = False
    additional: bool = False
    label: str
    y: tuple[str, ...]
    x: str | None = "time"
    title: str = ""
    xlabel: str | None = None
    ylabel: str = ""
    group_by: tuple[str, ...] = ()
    exclude: tuple[tuple[str, str], ...] = ()
    where: tuple[tuple[str, str], ...] = ()
    label_column: str | None = "entity_label"
    metric_labels: tuple[tuple[str, str], ...] = ()
    y_range: tuple[float, float] | None = None

    def series(self, frame: pd.DataFrame) -> Iterator[tuple[str, pd.DataFrame, str]]:
        frame = label_frame(frame)
        required = {
            *self.y,
            *self.group_by,
            *(key for key, _ in self.where),
            *(key for key, _ in self.exclude),
        }
        if self.x is not None:
            required.add(self.x)
        if missing := required - set(frame.columns):
            raise ValueError(f"{self.label}: missing chart columns {sorted(missing)}")
        for column, value in self.where:
            frame = frame[frame[column].astype(str) == value]
        for column, value in self.exclude:
            frame = frame[frame[column].astype(str) != value]
        groups = (
            frame.groupby(list(self.group_by), sort=False, dropna=False)
            if self.group_by
            else [((), frame)]
        )
        for key, group in groups:
            keys = key if isinstance(key, tuple) else (key,)
            entity = " / ".join(str(value) for value in keys)
            if self.group_by and self.label_column and self.label_column in group:
                entity = str(group[self.label_column].iloc[0])
            if self.x is not None:
                group = group.sort_values(self.x, kind="stable")
            for column in self.y:
                values = group.dropna(subset=[column]) if self.sparse else group
                if self.sparse and values[column].isna().all():
                    continue
                metric = dict(self.metric_labels).get(
                    column,
                    column.replace("_per_second", " rate")
                    .replace("_percent", "")
                    .replace("_bytes", "")
                    .replace("_", " "),
                )
                if metric.startswith(("rx ", "tx ")):
                    metric = metric[:2].upper() + metric[2:]
                label = (
                    entity
                    if entity and len(self.y) == 1
                    else f"{entity}: {metric}"
                    if entity
                    else metric
                )
                yield label, values, column

    def hover_text(self, frame: pd.DataFrame) -> list[str]:
        keys = [key for key in self.group_by if key in frame]
        return (
            [
                "<br>".join(
                    f"{escape(key)}: {escape(str(value))}"
                    for key, value in zip(keys, values, strict=True)
                )
                for values in frame[keys].itertuples(index=False, name=None)
            ]
            if keys
            else [""] * len(frame)
        )

    @property
    def x_label(self) -> str:
        return (
            self.xlabel
            if self.xlabel is not None
            else ("Time (s)" if self.x == "time" else self.x or "")
        )
