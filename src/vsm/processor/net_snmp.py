from __future__ import annotations

from typing import ClassVar

import pandas as pd

from vsm.chart.chart import Chart
from vsm.chart.line import Line, lines
from vsm.processing import Params
from vsm.processor._file import NativeFileProcessor
from vsm.processor._parsing import Event, rate_rows, text, time_of
from vsm.util.records import FileRecord


class NetSnmpProcessor(NativeFileProcessor[FileRecord]):
    """Compute per-protocol SNMP counter rates in events/s."""

    name = "net_snmp"
    group_label = "SNMP Statistics"

    snapshot_keys: ClassVar[tuple[str, ...]] = ("protocol", "counter")

    def parse_event(self, e: Event, params: Params) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        if e["type"] != "file":
            return rows
        value, _path, t = (text(e), str(e["path"]), time_of(e))
        lines = value.splitlines()
        if len(lines) % 2:
            raise ValueError("incomplete SNMP snapshot")
        for i in range(0, len(lines), 2):
            h, v = (lines[i].split(), lines[i + 1].split())
            if not h or not v or h[0] != v[0] or len(h) != len(v):
                raise ValueError("SNMP header/counter mismatch")
            for key, val in zip(h[1:], v[1:], strict=True):
                if (
                    params.selected
                    and h[0].rstrip(":") not in params.selected
                    and (h[0].rstrip(":") + "." + key not in params.selected)
                ):
                    continue
                rows.append(
                    {"time": t, "protocol": h[0].rstrip(":"), "counter": key, "value": int(val)}
                )
        return rows

    def finish(self, rows: list[dict[str, object]]) -> list[dict[str, object]]:
        rows = rate_rows(
            rows,
            ("protocol", "counter")
            + (("observation_epoch",) if rows and "observation_epoch" in rows[0] else ()),
            ("value",),
        )
        return rows

    def charts(self, frame: pd.DataFrame) -> tuple[Chart, ...]:
        gauges = {
            "RtoAlgorithm": "",
            "RtoMin": "ms",
            "RtoMax": "ms",
            "MaxConn": "count",
            "CurrEstab": "connections",
            "Forwarding": "",
            "DefaultTTL": "hops",
        }
        if not {"protocol", "counter"}.issubset(frame):
            return ()
        charts = []
        for protocol in frame.protocol.drop_duplicates():
            selected = frame[frame.protocol == protocol]
            rates = selected[~selected.counter.isin(gauges)]
            if not rates.empty:
                charts.append(
                    Line(
                        label=f"{protocol} statistics",
                        y=("value_per_second",),
                        ylabel="events/s",
                        group_by=("counter", "observation_epoch"),
                        label_column=None,
                        where=(("protocol", str(protocol)),),
                        exclude=tuple(("counter", key) for key in gauges),
                    )
                )
            for counter in selected.counter.drop_duplicates():
                if counter in gauges:
                    charts.extend(
                        lines(
                            frame,
                            f"{protocol} · {counter}",
                            ("value",),
                            ylabel=gauges[counter],
                            group_by=("observation_epoch",),
                            where=(("protocol", str(protocol)), ("counter", str(counter))),
                        )
                    )
        return tuple(charts)
