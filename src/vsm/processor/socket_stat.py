from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import cast

import pandas as pd

from vsm.chart.line import lines
from vsm.models import LogRef
from vsm.processing import Dataset, Params, dataset
from vsm.processor._datasets import stable_empty
from vsm.processor._parsing import events, rate_rows, time_of
from vsm.processor._snapshots import ObservationEpochs
from vsm.util.labels import label_frame
from vsm.util.records import SocketRecord


@dataclass(frozen=True)
class SocketStatProcessor:
    """Decode inet_diag endpoints, queues and available TCP/memory counters.

    Requires socket_stat logs from SocketStat. Returns Dataset values with units
    and provenance. Missing optional kernel attributes remain unavailable.
    """

    def process(self, source: LogRef[SocketRecord], *, params: Params) -> tuple[Dataset, ...]:
        if source.schema.name != "socket_stat":
            raise ValueError("processor/log schema mismatch")
        batches: list[list[dict[str, object]]] = [[]]
        stream = events(cast(LogRef[object], source), params)
        for e in stream:
            if e["type"] == "sample":
                batches.append([])
            elif e["type"] == "socket":
                batches[-1].append(
                    {
                        "time": time_of(e),
                        **{k: v for k, v in e.items() if k not in ("type", "time_ns", "sequence")},
                    }
                )
        observations = ObservationEpochs()
        rows: list[dict[str, object]] = []
        for index, batch in enumerate(batches):
            for row in batch:
                row["socket_id"] = json.dumps(
                    [
                        row.get(k)
                        for k in (
                            "family",
                            "protocol",
                            "cookie",
                            "inode",
                            "source",
                            "source_port",
                            "destination",
                            "destination_port",
                        )
                    ]
                )
            rows.extend(
                observations.apply(
                    batch, ("socket_id",), complete=source.complete or index < len(batches) - 1
                )
            )
        counters = ("bytes_acked", "bytes_received", "bytes_sent", "bytes_retransmitted")
        measurable: list[dict[str, object]] = []
        for row in rows:
            cookie = row.get("cookie")
            if (
                isinstance(cookie, list)
                and len(cookie) == 2
                and all(type(value) is int and 0 <= value <= 4294967295 for value in cookie)
                and cookie != [4294967295, 4294967295]
                and all(isinstance(row.get(k), (int, float)) for k in counters)
            ):
                measurable.append(row)
        if measurable:
            measured = rate_rows(measurable, ("socket_id", "observation_epoch"), counters)
            for old, new in zip(measurable, measured, strict=True):
                old.update(new)
        frame = label_frame(pd.DataFrame(rows))
        charts = []
        for socket_id in frame.get("socket_id", pd.Series(dtype=str)).drop_duplicates():
            label = str(frame.loc[frame.socket_id == socket_id, "entity_label"].iloc[0])
            for title, fields, unit in (
                ("Congestion window", ("congestion_window_segments",), "segments"),
                ("Socket RTT", ("rtt_us", "receive_rtt_us"), "µs"),
                ("Socket throughput", tuple(key + "_per_second" for key in counters), "B/s"),
                (
                    "Socket buffers",
                    (
                        "receive_queue",
                        "send_queue",
                        "receive_memory",
                        "receive_buffer",
                        "write_memory",
                        "send_buffer",
                        "forward_memory",
                        "write_queued",
                        "option_memory",
                        "backlog_memory",
                    ),
                    "B",
                ),
            ):
                charts.extend(
                    replace(chart, where=(("socket_id", str(socket_id)),))
                    for chart in lines(
                        frame,
                        f"{title} · {label}",
                        fields,
                        ylabel=unit,
                        group_by=("socket_id", "observation_epoch"),
                    )
                )
        if not rows:
            data = dataset("socket_stat", [], source=cast(LogRef[object], source))
            return (
                stable_empty(
                    replace(data, group_label="Socket Statistics"),
                    ("time", "socket_id", "observation_epoch", "receive_queue", "send_queue"),
                    {"time": "s", "receive_queue": "B", "send_queue": "B"},
                ),
            )
        return (
            replace(
                dataset(
                    "socket_stat",
                    rows,
                    units={
                        "time": "s",
                        "rtt_us": "us",
                        "receive_rtt_us": "us",
                        "congestion_window_segments": "segments",
                        **{
                            key: "B"
                            for key in (
                                "receive_memory",
                                "receive_buffer",
                                "write_memory",
                                "send_buffer",
                                "forward_memory",
                                "write_queued",
                                "option_memory",
                                "backlog_memory",
                            )
                        },
                        "receive_queue": "B",
                        "send_queue": "B",
                        **{key: "B" for key in counters},
                        **{key + "_per_second": "B/s" for key in counters},
                    },
                    charts=tuple(charts),
                    source=cast(LogRef[object], source),
                ),
                group_label="Socket Statistics",
            ),
        )
