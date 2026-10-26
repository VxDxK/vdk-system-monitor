from __future__ import annotations

import json
import re
from typing import ClassVar

import pandas as pd

from vsm.chart.chart import Chart
from vsm.chart.line import lines
from vsm.processing import Params
from vsm.processor._snapshots import ObservationEpochs
from vsm.processor._text import TextProcessor


class SsProcessor(TextProcessor):
    """Decode periodically captured ss socket queues and TCP statistics."""

    name = "ss"
    group_label = "Socket Statistics"

    empty_columns: ClassVar[tuple[str, ...]] = (
        "time",
        "local",
        "remote",
        "observation_epoch",
        "state",
        "receive_queue",
        "send_queue",
    )

    def parse(self, data: str, params: Params) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        if not data.strip():
            raise ValueError("no ss snapshots")
        observations = ObservationEpochs()
        for event in data.splitlines():
            snapshot = []
            record = json.loads(event)
            value = str(record["stdout"])
            t = float(record["time_ns"]) / 1000000000.0
            identity: dict[str, object] = {}
            for line in value.splitlines():
                fields = line.split()
                if len(fields) >= 5 and fields[1].isdigit() and fields[2].isdigit():
                    identity = {
                        "state": fields[0],
                        "receive_queue": int(fields[1]),
                        "send_queue": int(fields[2]),
                        "local": fields[3],
                        "remote": fields[4],
                    }
                    snapshot.append({"time": t, **identity})
                elif identity:
                    details = {
                        m[1]: float(m[2]) for m in re.finditer("([a-zA-Z_]+):([\\d.]+)", line)
                    }
                    snapshot[-1].update(details)
                elif fields and fields[0] not in ("State", "Netid"):
                    raise ValueError("unsupported ss snapshot row")
            rows.extend(observations.apply(snapshot, ("local", "remote")))
        return rows

    units: ClassVar[dict[str, str]] = {
        "time": "s",
        "receive_queue": "B",
        "send_queue": "B",
        "rtt": "ms",
        "rto": "ms",
        "rcv_rtt": "ms",
        "cwnd": "segments",
        "ssthresh": "segments",
        "bytes_sent": "B",
        "bytes_received": "B",
        "bytes_acked": "B",
        "segs_out": "segments",
        "segs_in": "segments",
    }

    def _charts(self, frame: pd.DataFrame) -> tuple[Chart, ...]:
        return (
            *lines(
                frame,
                "Socket timers",
                ("rto",),
                ylabel="ms",
                group_by=("local", "remote")
                + (("observation_epoch",) if "observation_epoch" in frame else ()),
            ),
            *lines(
                frame,
                "Slow start thresholds",
                ("ssthresh",),
                ylabel="segments",
                group_by=("local", "remote")
                + (("observation_epoch",) if "observation_epoch" in frame else ()),
            ),
            *lines(
                frame,
                "Socket queues",
                ("receive_queue", "send_queue"),
                ylabel="B",
                group_by=("local", "remote")
                + (("observation_epoch",) if "observation_epoch" in frame else ()),
                x="time",
                y_range=None,
            ),
            *lines(
                frame,
                "Socket RTT",
                ("rtt", "rcv_rtt"),
                ylabel="ms",
                group_by=("local", "remote")
                + (("observation_epoch",) if "observation_epoch" in frame else ()),
                x="time",
                y_range=None,
            ),
            *lines(
                frame,
                "Congestion windows",
                ("cwnd",),
                ylabel="segments",
                group_by=("local", "remote")
                + (("observation_epoch",) if "observation_epoch" in frame else ()),
                x="time",
                y_range=None,
            ),
        )

    def charts(self, frame: pd.DataFrame) -> tuple[Chart, ...]:
        from dataclasses import replace

        keys = ("local", "remote") + (
            ("observation_epoch",) if "observation_epoch" in frame else ()
        )
        if not all(key in frame for key in keys):
            return ()
        return tuple(
            replace(
                chart,
                additional=chart.label
                in {"Socket queues", "Socket timers", "Slow start thresholds"},
                label=f"{chart.label} · {entity}",
                where=tuple((key, str(value)) for key, value in zip(keys, values, strict=True)),
            )
            for values in frame[list(keys)].drop_duplicates().itertuples(index=False, name=None)
            for entity in [" → ".join(str(value) for value in values[:2])]
            for chart in self._charts(frame)
        )
