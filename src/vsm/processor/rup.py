from __future__ import annotations

from dataclasses import dataclass, replace
from typing import cast

from vsm.chart.box import Box
from vsm.chart.line import lines
from vsm.clock import TimeOrigin
from vsm.models import LogRef
from vsm.processing import Column, Dataset, Params, dataset
from vsm.util.records import LatencyRecord
from vsm.util.rup_format import RTT_FIELDS, read_records


@dataclass(frozen=True)
class RupProcessor:
    """Process rup.ping/3 client measurements wrapped in framework rup logs.

    Returns Dataset values with latency in ms and upstream summary accounting.
    Echo and terminal responses remain distinct; both count as received. Server
    logs and historical framework formats are rejected.
    """

    def process(self, source: LogRef[LatencyRecord], *, params: Params) -> tuple[Dataset, ...]:
        if source.schema.name != "rup":
            raise ValueError("processor/log schema mismatch")
        records = read_records(
            source.path, complete=source.complete, allow_partial=params.allow_partial
        )
        start = cast(int, records[0]["started_at_ms"])
        source = replace(
            source,
            metadata=replace(
                source.metadata,
                time_origin=TimeOrigin(
                    domain="target_realtime", value_ns=start * 1_000_000, uncertainty_ns=1_000_000
                ),
            ),
        )
        rows = [
            {
                "time": (cast(int, record["timestamp_ms"]) - start) / 1000,
                "timestamp_ms": record["timestamp_ms"],
                "request": record["seq"],
                "seq": record["seq"],
                "record": record["record"],
                "rtt_ms": record.get("rtt_ms"),
                "size_bytes": record.get("size_bytes"),
                "ttl": record.get("ttl"),
            }
            for record in records[1:]
            if record["record"] != "summary"
        ]
        units = {"time": "s", "timestamp_ms": "ms", "rtt_ms": "ms", "size_bytes": "bytes"}
        result = dataset("rup", rows, units=units, source=cast(LogRef[object], source))
        if rows:
            result = replace(
                result,
                charts=(
                    *lines(result.frame, "Rup RTT", ("rtt_ms",), ylabel="ms", group_by=("record",)),
                    Box(
                        label="Rup RTT distribution",
                        y=("rtt_ms",),
                        ylabel="ms",
                        group_by=("record",),
                    ),
                ),
            )
        if not rows:
            names = (
                "time",
                "timestamp_ms",
                "request",
                "seq",
                "record",
                "rtt_ms",
                "size_bytes",
                "ttl",
            )
            result = replace(
                result,
                frame=result.frame.reindex(columns=names),
                columns=tuple(Column(name=n, unit=units.get(n)) for n in names),
            )
        results = [result]
        if records[-1]["record"] == "summary":
            summary = {key: value for key, value in records[-1].items() if key != "record"}
            summary["lost"] = cast(int, summary["sent"]) - cast(int, summary["received"])
            results.append(
                dataset(
                    "rup_summary",
                    [summary],
                    units={
                        **{key: "ms" for key in RTT_FIELDS},
                        "loss_percent": "%",
                        "timestamp_ms": "ms",
                    },
                    source=cast(LogRef[object], source),
                )
            )
        return tuple(replace(value, group_label="Rup latency") for value in results)
