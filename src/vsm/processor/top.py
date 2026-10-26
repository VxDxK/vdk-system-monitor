from __future__ import annotations

from typing import ClassVar

import pandas as pd

from vsm.chart.box import Box
from vsm.chart.chart import Chart
from vsm.chart.line import lines
from vsm.processing import Params
from vsm.processor._snapshots import ObservationEpochs
from vsm.processor._text import TextProcessor


class TopProcessor(TextProcessor):
    """Decode supported batch top CPU percent and process memory metrics."""

    name = "top"
    group_label = "Process Statistics"

    empty_columns: ClassVar[tuple[str, ...]] = (
        "snapshot",
        "pid",
        "command",
        "observation_epoch",
        "cpu_percent",
    )

    def parse(self, data: str, params: Params) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        header: list[str] = []
        snapshot = 0
        current = []
        observations = ObservationEpochs()
        have_snapshot = False
        for line in data.splitlines():
            parts = line.split()
            if "PID" in parts and any(k in parts for k in ("%CPU", "CPU%")):
                if have_snapshot:
                    rows.extend(observations.apply(current, ("pid", "command")))
                current = []
                snapshot += 1
                have_snapshot = True
                header = parts
                continue
            if not header or not parts or (not parts[0].isdigit()):
                continue
            if len(parts) < len(header):
                raise ValueError("short top row")
            row = dict(zip(header, parts[: len(header)], strict=True))
            current.append(
                {
                    "snapshot": snapshot,
                    "pid": int(row["PID"]),
                    "cpu_percent": float(row.get("%CPU", row.get("CPU%", "0"))),
                    "command": row.get("COMMAND", row.get("NAME", "")),
                }
            )
        if not have_snapshot:
            raise ValueError("unsupported top snapshot format")
        rows.extend(observations.apply(current, ("pid", "command")))
        return rows

    units: ClassVar[dict[str, str]] = {"cpu_percent": "%"}

    def charts(self, frame: pd.DataFrame) -> tuple[Chart, ...]:
        frame_means = frame.groupby(["pid", "command"], dropna=False).cpu_percent.mean()
        selected = set(frame_means[frame_means > 5].index)
        omitted = tuple(
            ("entity_label", str(row.entity_label))
            for row in frame.itertuples()
            if (row.pid, row.command) not in selected
        )
        charts = (
            *lines(
                frame,
                "Process CPU by snapshot",
                ("cpu_percent",),
                x="snapshot",
                ylabel="%",
                group_by=("pid", "command", "observation_epoch"),
            ),
            Box(
                label="Top CPU consumers · mean > 5%",
                exclude=omitted,
                points=False,
                y=("cpu_percent",),
                ylabel="%",
                group_by=("pid", "command", "observation_epoch"),
            ),
        )

        return tuple(chart for chart in charts if not isinstance(chart, Box) or selected) + (
            Box(
                label="All process CPU distributions",
                y=("cpu_percent",),
                ylabel="%",
                group_by=("pid", "command", "observation_epoch"),
                additional=True,
            ),
        )
