from __future__ import annotations

import json
from typing import ClassVar

import pandas as pd

from vsm.chart.chart import Chart
from vsm.chart.line import lines
from vsm.clock import TimeOrigin
from vsm.processing import Params
from vsm.processor._text import TextProcessor


class IperfProcessor(TextProcessor):
    """Decode iperf3 JSON throughput intervals and summary metrics."""

    name = "iperf"

    def time_origin(self, raw, source) -> TimeOrigin | None:
        # iperf's connection-start epoch is second-resolution, independently of launch.
        stamp = json.loads(raw).get("start", {}).get("timestamp", {}).get("timesecs")
        if type(stamp) is int and stamp >= 0:
            return TimeOrigin(
                domain="target_realtime",
                value_ns=stamp * 1_000_000_000,
                uncertainty_ns=1_000_000_000,
            )
        return None

    def parse(self, data: str, params: Params) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        value = json.loads(data)
        if "error" in value:
            raise ValueError(value["error"])
        for interval in value.get("intervals", []):
            for item in interval.get("streams", []):
                rows.append(
                    {
                        "time": item["end"],
                        "start": item["start"],
                        "bits_per_second": item["bits_per_second"],
                        "bytes": item["bytes"],
                        "socket": item.get("socket"),
                        **({"packets": item["packets"]} if "packets" in item else {}),
                    }
                )
        return rows

    units: ClassVar[dict[str, str]] = {
        "time": "s",
        "start": "s",
        "bits_per_second": "bit/s",
        "bytes": "B",
        "packets": "packets",
    }

    def charts(self, frame: pd.DataFrame) -> tuple[Chart, ...]:
        return (
            *lines(frame, "Iperf packets", ("packets",), ylabel="packets", group_by=("socket",)),
            *lines(
                frame,
                "Iperf throughput",
                ("bits_per_second",),
                ylabel="bit/s",
                group_by=("socket",),
                x="time",
                y_range=None,
            ),
        )
