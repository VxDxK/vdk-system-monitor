from __future__ import annotations

from typing import ClassVar

import pandas as pd

from vsm.chart.chart import Chart
from vsm.chart.line import lines
from vsm.processing import Params
from vsm.processor._file import NativeFileProcessor
from vsm.processor._parsing import Event, endpoint, rate_rows, text, time_of
from vsm.util.records import FileRecord


class NetUdpProcessor(NativeFileProcessor[FileRecord]):
    """Decode IPv4/IPv6 UDP socket endpoints and queue sizes in bytes."""

    name = "net_udp"
    group_label = "UDP Socket Statistics"

    snapshot_keys: ClassVar[tuple[str, ...]] = ("local", "remote", "inode")
    empty_columns: ClassVar[tuple[str, ...]] = (
        "time",
        "local",
        "remote",
        "inode",
        "observation_epoch",
        "tx_queue",
        "rx_queue",
        "drops",
        "drops_per_second",
    )

    def parse_event(self, e: Event, params: Params) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        if e["type"] != "file":
            return rows
        value, path, t = (text(e), str(e["path"]), time_of(e))
        if params.selected and path not in params.selected:
            return rows
        content = value.splitlines()
        if not content or (
            len(content) == 1
            and not {"sl", "local_address", "rem_address", "st", "inode"}.issubset(
                content[0].split()
            )
        ):
            raise ValueError("invalid UDP table snapshot")
        for line in content[1:]:
            v = line.split()
            if len(v) < 10:
                raise ValueError("short UDP table row")
            tx, rx = (int(x, 16) for x in v[4].split(":"))
            rows.append(
                {
                    "time": t,
                    "local": endpoint(v[1]),
                    "remote": endpoint(v[2]),
                    "inode": int(v[9]),
                    "tx_queue": tx,
                    "rx_queue": rx,
                    "state": int(v[3], 16),
                    **({"drops": int(v[12])} if len(v) > 12 else {}),
                }
            )
        return rows

    def finish(self, rows: list[dict[str, object]]) -> list[dict[str, object]]:
        if rows and all("drops" in row for row in rows):
            return rate_rows(
                rows,
                ("local", "remote", "inode")
                + (("observation_epoch",) if rows and "observation_epoch" in rows[0] else ()),
                ("drops",),
            )
        return rows

    units: ClassVar[dict[str, str]] = {
        "time": "s",
        "tx_queue": "B",
        "rx_queue": "B",
        "drops": "drops",
        "drops_per_second": "drops/s",
    }

    def _charts(self, frame: pd.DataFrame) -> tuple[Chart, ...]:
        return (
            *lines(
                frame,
                "UDP drops",
                ("drops_per_second",),
                ylabel="drops/s",
                group_by=("local", "remote", "inode")
                + (("observation_epoch",) if "observation_epoch" in frame else ()),
            ),
            *lines(
                frame,
                "UDP queues",
                ("tx_queue", "rx_queue"),
                ylabel="B",
                group_by=("local", "remote", "inode")
                + (("observation_epoch",) if "observation_epoch" in frame else ()),
                x="time",
                y_range=None,
            ),
        )

    def charts(self, frame: pd.DataFrame) -> tuple[Chart, ...]:
        from dataclasses import replace

        keys = ("local", "remote", "inode") + (
            ("observation_epoch",) if "observation_epoch" in frame else ()
        )
        if not all(key in frame for key in keys):
            return ()
        return tuple(
            replace(
                chart,
                label=f"{chart.label} · {entity}",
                where=tuple((key, str(value)) for key, value in zip(keys, values, strict=True)),
            )
            for values in frame[list(keys)].drop_duplicates().itertuples(index=False, name=None)
            for entity in [" → ".join(str(value) for value in values[:2])]
            for chart in self._charts(frame)
        )
