from __future__ import annotations

import re
from typing import ClassVar

import pandas as pd

from vsm.chart.box import Box
from vsm.chart.chart import Chart
from vsm.chart.line import lines
from vsm.processing import Params
from vsm.processor._text import TextProcessor


class PingProcessor(TextProcessor):
    """Decode ping text latency measurements in milliseconds."""

    name = "ping"

    def parse(self, data: str, params: Params) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for line in data.splitlines():
            m = re.search("icmp_seq[= ](\\d+).*time([=<])([\\d.]+)\\s*ms", line)
            if m:
                rows.append(
                    {"request": int(m[1]), "rtt_ms": float(m[3]), "upper_bound": m[2] == "<"}
                )
        return rows

    units: ClassVar[dict[str, str]] = {"rtt_ms": "ms"}

    def charts(self, frame: pd.DataFrame) -> tuple[Chart, ...]:
        return (
            *lines(frame, "Ping RTT", ("rtt_ms",), x="request", ylabel="ms"),
            Box(label="Ping RTT distribution", y=("rtt_ms",), ylabel="ms"),
        )
