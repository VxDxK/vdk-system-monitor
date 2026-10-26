from __future__ import annotations

import re
from typing import ClassVar, cast

import pandas as pd

from vsm.chart.bar import Bar
from vsm.chart.chart import Chart
from vsm.chart.table import Table
from vsm.processing import Params
from vsm.processor._text import TextProcessor


class TimeProcessor(TextProcessor):
    """Decode POSIX real, user and system durations in seconds from CopyFile logs."""

    name = "time"

    def parse(self, data: str, params: Params) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        values = {
            m[1]: float(m[2]) for m in re.finditer("(?m)^(real|user|sys)\\s+([\\d.]+)$", data)
        }
        if not values:
            raise ValueError("no timing records")
        rows.append(cast(dict[str, object], values))
        return rows

    units: ClassVar[dict[str, str]] = {"real": "s", "user": "s", "sys": "s"}

    def charts(self, frame: pd.DataFrame) -> tuple[Chart, ...]:
        return (
            Table(label="Timing results"),
            Bar(
                additional=True,
                label="Execution time",
                y=tuple(column for column in ("real", "user", "sys") if column in frame.columns),
                x=None,
                xlabel="Measurement",
                ylabel="s",
            ),
        )
