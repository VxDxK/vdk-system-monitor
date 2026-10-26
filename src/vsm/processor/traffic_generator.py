from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import pandas as pd

from vsm.chart.line import lines
from vsm.models import LogRef
from vsm.processing import Dataset, Params, dataset
from vsm.processor._parsing import events, time_of
from vsm.util.records import TrafficRecord


@dataclass(frozen=True)
class TrafficGeneratorProcessor:
    """Decode native rgen throughput and final totals; TCP I/O calls are not packets.

    Requires traffic_generator logs from TrafficGenerator. Returns Dataset values
    with byte/event totals and throughput; performance needs target validation.
    """

    def process(self, source: LogRef[TrafficRecord], *, params: Params) -> tuple[Dataset, ...]:
        if source.schema.name != "traffic_generator":
            raise ValueError("processor/log schema mismatch")
        rows = [
            {
                "time": time_of(e),
                **{k: v for k, v in e.items() if k not in ("type", "time_ns", "sequence")},
            }
            for e in events(cast(LogRef[object], source), params)
            if e["type"] == "traffic"
        ]
        return (
            dataset(
                "traffic_generator",
                rows,
                units={
                    "time": "s",
                    "bits_per_second": "bit/s",
                    "operations_per_second": "operations/s",
                    "bytes": "B",
                    "operations": "operations",
                },
                charts=(
                    *lines(
                        pd.DataFrame(rows),
                        "Traffic throughput",
                        ("bits_per_second",),
                        ylabel="bit/s",
                    ),
                    *lines(
                        pd.DataFrame(rows),
                        "Traffic operations",
                        ("operations_per_second",),
                        ylabel="operations/s",
                    ),
                ),
                source=cast(LogRef[object], source),
            ),
        )
