from __future__ import annotations

import json
from typing import ClassVar

import pandas as pd

from vsm.chart.bar import Bar
from vsm.chart.chart import Chart
from vsm.chart.table import Table
from vsm.processing import Params
from vsm.processor._text import TextProcessor


class FioProcessor(TextProcessor):
    """Decode fio JSON bandwidth, operations and latency benchmark results."""

    name = "fio"

    def parse(self, data: str, params: Params) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        value = json.loads(data)
        for job in value["jobs"]:
            for operation in ("read", "write", "trim"):
                counters = job.get(operation, {})
                if counters:
                    rows.append(
                        {
                            "job": job["jobname"],
                            "operation": operation,
                            "io_bytes": counters.get("io_bytes"),
                            "bandwidth_bytes_per_second": counters.get(
                                "bw_bytes", float(counters.get("bw", 0)) * 1024
                            ),
                            "iops": counters.get("iops"),
                        }
                    )
        return rows

    units: ClassVar[dict[str, str]] = {
        "io_bytes": "B",
        "bandwidth_bytes_per_second": "B/s",
        "iops": "operations/s",
    }

    def charts(self, frame: pd.DataFrame) -> tuple[Chart, ...]:
        return (Table(label="Fio results"),) + tuple(
            Bar(
                additional=True,
                label=label,
                x="job",
                y=(column,),
                ylabel=unit,
                group_by=("operation",),
            )
            for label, column, unit in (
                ("Fio throughput", "bandwidth_bytes_per_second", "B/s"),
                ("Fio operations", "iops", "operations/s"),
                ("Fio transferred bytes", "io_bytes", "B"),
            )
        )
