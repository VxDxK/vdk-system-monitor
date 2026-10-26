from __future__ import annotations

import json
from dataclasses import dataclass
from typing import cast

from vsm.chart.bar import Bar
from vsm.chart.line import Line
from vsm.models import LogRef
from vsm.processing import Dataset, Params, dataset
from vsm.util.records import FileRecord


@dataclass(frozen=True)
class CpuTimeInStateProcessor:
    """Compute before/after frequency residency deltas in Hz and ticks.

    Requires complete cpu_time_in_state snapshots. Returns Dataset values with
    units and provenance; Params partial-log opt-ins do not bypass completeness.
    """

    def process(self, source: LogRef[FileRecord], *, params: Params) -> tuple[Dataset, ...]:
        if source.schema.name != "cpu_time_in_state" or not source.complete:
            raise ValueError("invalid snapshot log")
        data = json.loads(source.path.read_text())
        rows: list[dict[str, object]] = []
        before, after = (data["before"], data["after"])
        for path, value in before.items():
            initial = {int(k): int(v) for k, v in (line.split() for line in value.splitlines())}
            final = {int(k): int(v) for k, v in (line.split() for line in after[path].splitlines())}
            for frequency, ticks in final.items():
                delta = ticks - initial[frequency] if frequency in initial else None
                rows.append(
                    {
                        "path": path,
                        "frequency_hz": frequency * 1000,
                        "residency_ticks": delta if delta is not None and delta >= 0 else None,
                        "counter_reset": delta is not None and delta < 0,
                    }
                )
        return (
            dataset(
                "cpu_time_in_state",
                rows,
                units={"frequency_hz": "Hz", "residency_ticks": "ticks"},
                charts=(
                    Line(
                        label="CPU frequency time in state",
                        x="frequency_hz",
                        y=("residency_ticks",),
                        xlabel="Frequency (Hz)",
                        ylabel="ticks",
                        group_by=("path",),
                    ),
                    Bar(
                        additional=True,
                        label="CPU frequency residency",
                        x="frequency_hz",
                        y=("residency_ticks",),
                        xlabel="Frequency (Hz)",
                        ylabel="ticks",
                        group_by=("path",),
                    ),
                ),
                source=cast(LogRef[object], source),
            ),
        )
