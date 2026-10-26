from __future__ import annotations

from typing import ClassVar

import pandas as pd

from vsm.chart.chart import Chart
from vsm.chart.line import lines
from vsm.processing import Params
from vsm.processor._file import NativeFileProcessor
from vsm.processor._parsing import Event, rate_rows, text, time_of
from vsm.util.records import NetRecord


class NetProcessor(NativeFileProcessor[NetRecord]):
    """Compute per-interface network byte, packet and error rates."""

    name = "net_dev"
    group_label = "Interface Statistics"

    snapshot_keys: ClassVar[tuple[str, ...]] = ("interface",)
    empty_columns: ClassVar[tuple[str, ...]] = (
        "time",
        "interface",
        "observation_epoch",
        "rx_bytes",
        "tx_bytes",
        "rx_packets",
        "tx_packets",
        "rx_bytes_per_second",
        "tx_bytes_per_second",
    )

    def parse_event(self, e: Event, params: Params) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        if e["type"] != "file":
            return rows
        value, _path, t = (text(e), str(e["path"]), time_of(e))
        content = value.splitlines()
        if not any(":" in line for line in content) and not (
            any(line.strip().startswith("Inter-|") for line in content)
            and any(
                line.strip().startswith("face") and "bytes" in line and "packets" in line
                for line in content
            )
        ):
            raise ValueError("invalid net_dev snapshot")
        for line in content:
            if ":" not in line:
                continue
            interface, data = line.split(":", 1)
            v = data.split()
            if len(v) < 16:
                raise ValueError("short net_dev counters")
            if params.selected and interface.strip() not in params.selected:
                continue
            rows.append(
                {
                    "time": t,
                    "interface": interface.strip(),
                    "rx_bytes": int(v[0]),
                    "rx_packets": int(v[1]),
                    "tx_bytes": int(v[8]),
                    "tx_packets": int(v[9]),
                    **{
                        f"{direction}_{key}": int(v[offset + index])
                        for direction, offset in (("rx", 0), ("tx", 8))
                        for key, index in (("errors", 2), ("drops", 3), ("fifo", 4))
                    },
                }
            )
        return rows

    def finish(self, rows: list[dict[str, object]]) -> list[dict[str, object]]:
        rows = rate_rows(
            rows,
            ("interface",)
            + (("observation_epoch",) if rows and "observation_epoch" in rows[0] else ()),
            (
                "rx_bytes",
                "tx_bytes",
                "rx_packets",
                "tx_packets",
                "rx_errors",
                "tx_errors",
                "rx_drops",
                "tx_drops",
                "rx_fifo",
                "tx_fifo",
            ),
        )
        return rows

    units: ClassVar[dict[str, str]] = {
        **{
            f"{direction}_{key}{suffix}": unit
            for direction in ("rx", "tx")
            for key in ("errors", "drops", "fifo")
            for suffix, unit in (("", "events"), ("_per_second", "events/s"))
        },
        "time": "s",
        "rx_bytes": "B",
        "tx_bytes": "B",
        "rx_packets": "packets",
        "tx_packets": "packets",
        "rx_bytes_per_second": "B/s",
        "tx_bytes_per_second": "B/s",
        "rx_packets_per_second": "packets/s",
        "tx_packets_per_second": "packets/s",
    }

    def charts(self, frame: pd.DataFrame) -> tuple[Chart, ...]:
        from dataclasses import replace

        from vsm.chart.table import Table

        return tuple(
            replace(chart, where=(("interface", str(interface)),))
            for interface in frame.get("interface", pd.Series(dtype=str)).drop_duplicates()
            for chart in (
                *lines(
                    frame,
                    f"Interface {interface} · throughput",
                    ("rx_bytes_per_second", "tx_bytes_per_second"),
                    ylabel="B/s",
                    group_by=("interface",)
                    + (("observation_epoch",) if "observation_epoch" in frame else ()),
                ),
                *lines(
                    frame,
                    f"Interface {interface} · packets",
                    ("rx_packets_per_second", "tx_packets_per_second"),
                    ylabel="packets/s",
                    group_by=("interface",)
                    + (("observation_epoch",) if "observation_epoch" in frame else ()),
                ),
                *lines(
                    frame,
                    f"Interface {interface} · drops and errors",
                    tuple(
                        f"{direction}_{key}_per_second"
                        for direction in ("rx", "tx")
                        for key in ("drops", "errors", "fifo")
                    ),
                    ylabel="events/s",
                    group_by=("interface",)
                    + (("observation_epoch",) if "observation_epoch" in frame else ()),
                ),
            )
        ) + (
            Table(
                label="Raw interface counters",
                columns=tuple(
                    c
                    for c in (
                        "time",
                        "interface",
                        "rx_bytes",
                        "tx_bytes",
                        "rx_packets",
                        "tx_packets",
                    )
                    if c in frame
                ),
            ),
        )
