from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import ClassVar

import pandas as pd

from vsm.chart.chart import Chart
from vsm.chart.line import Line
from vsm.chart.table import Table
from vsm.models import LogRef
from vsm.processing import Dataset, Params, dataset
from vsm.processor._snapshots import ObservationEpochs
from vsm.processor._text import TextProcessor
from vsm.util.records import TextRecord


class WifiConnInfoProcessor(TextProcessor):
    """Decode available Wi-Fi signal, bitrate and channel metrics from iw output."""

    name = "wifi_conn_info"
    group_label = "Wi-Fi Statistics"

    empty_columns: ClassVar[tuple[str, ...]] = (
        "time",
        "station",
        "observation_epoch",
        "metric",
        "value",
        "unit",
    )

    def parse(self, data: str, params: Params) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        if not data.strip():
            raise ValueError("no Wi-Fi snapshots")
        observations = ObservationEpochs()
        for event in data.splitlines():
            snapshot = []
            record = json.loads(event)
            t = float(record["time_ns"]) / 1e9
            station = ""
            present = set()
            for line in str(record["stdout"]).splitlines():
                if match := re.search(r"Station ([0-9a-fA-F:]+)", line):
                    station = match[1].lower()
                    present.add((station,))
                    continue
                match = re.match(r"\s*([^:]+):\s*(-?[\d.]+)(.*)", line)
                if not match or not station:
                    continue
                metric, raw, suffix = match.groups()
                metric = metric.strip()
                snapshot.append(
                    {
                        "time": t,
                        "station": station,
                        "metric": metric,
                        "value": float(raw),
                        "unit": suffix.strip(),
                    }
                )
                direction = metric.split()[0]
                if metric in ("tx bitrate", "rx bitrate"):
                    if bandwidth := re.search(r"(\d+(?:\.\d+)?)MHz", suffix):
                        snapshot.append(
                            {
                                "time": t,
                                "station": station,
                                "metric": direction + " bandwidth",
                                "value": float(bandwidth[1]),
                                "unit": "MHz",
                            }
                        )
                    for parameter in re.finditer(
                        r"\b((?:(?:HT|VHT|HE|EHT)-)?(?:MCS|NSS|GI|DCM))\s+([\d.]+)", suffix
                    ):
                        kind, value = parameter.groups()
                        snapshot.append(
                            {
                                "time": t,
                                "station": station,
                                "metric": direction + " " + kind.split("-")[-1].lower(),
                                "value": float(value),
                                "unit": "",
                                "parameter_type": kind,
                            }
                        )
                    if re.search(r"\bshort GI\b", suffix):
                        snapshot.append(
                            {
                                "time": t,
                                "station": station,
                                "metric": direction + " gi",
                                "value": 1.0,
                                "unit": "",
                                "parameter_type": "HT-GI",
                            }
                        )
            if str(record["stdout"]).strip() and not station:
                raise ValueError("unsupported Wi-Fi station snapshot")
            rows.extend(observations.apply(snapshot, ("station",), present=present))
        return rows

    def process(self, source: LogRef[TextRecord], *, params: Params) -> tuple[Dataset, ...]:
        # Preserve the original long-format measurement columns and add parameter rows.
        original = super().process(source, params=params)[0]
        results = [original]
        for (station, epoch), selected in original.frame.groupby(
            ["station", "observation_epoch"], sort=False
        ):
            wide_rows = []
            for t, samples in selected.groupby("time", sort=False):
                row: dict[str, object] = {"time": t, "station": station, "observation_epoch": epoch}
                for sample in samples.itertuples():
                    metric = sample.metric.replace(" ", "_")
                    row[metric] = sample.value
                    if hasattr(sample, "parameter_type") and pd.notna(sample.parameter_type):
                        row[metric + "_typed"] = f"{sample.parameter_type} {sample.value:g}"
                wide_rows.append(row)
            value = dataset(
                "wifi_parameters_" + str(station) + (f"_observation_{epoch}" if epoch else ""),
                wide_rows,
                units={"time": "s"},
                source=source,
            )
            frame = value.frame
            charts: list[Chart] = [Table(label=f"All parameters over time · {station}")]
            specs = (
                (
                    "Signal quality",
                    ("signal", "signal_avg", "last_ack_signal", "avg_ack_signal"),
                    "dBm",
                ),
                ("Connection duration", ("connected_time",), "s"),
                ("Transmission counters", ("tx_retries", "tx_failed"), "count"),
                ("Traffic volume", ("tx_bytes", "rx_bytes"), "B"),
                ("Traffic packets", ("tx_packets", "rx_packets"), "packets"),
                ("Bitrate", ("tx_bitrate", "rx_bitrate"), "Mbit/s"),
                ("Channel bandwidth", ("tx_bandwidth", "rx_bandwidth"), "MHz"),
            )
            for title, fields, unit in specs:
                fields = tuple(field for field in fields if field in frame)
                if fields:
                    charts.append(Line(label=f"{title} · {station}", y=fields, ylabel=unit))
            for direction in ("tx", "rx"):
                for parameter in ("mcs", "nss", "gi", "dcm"):
                    column = f"{direction}_{parameter}_typed"
                    if column in frame:
                        charts.append(
                            Line(
                                label=f"{direction.upper()} {parameter.upper()} · {station}",
                                y=(column,),
                                ylabel=parameter.upper(),
                                categorical=True,
                                markers=True,
                            )
                        )
            results.append(
                replace(
                    value,
                    group_label=self.group_label,
                    provenance=original.provenance,
                    charts=tuple(charts),
                )
            )
        return tuple(results)

    def charts(self, frame: pd.DataFrame) -> tuple[Chart, ...]:
        # Unknown numeric iw fields remain inspectable as additional charts.
        return tuple(
            Line(
                label=str(metric),
                y=("value",),
                ylabel=str(unit) or "count",
                group_by=("station", "observation_epoch"),
                where=(("metric", str(metric)), ("unit", str(unit))),
                additional=True,
            )
            for metric, unit in frame[["metric", "unit"]]
            .drop_duplicates()
            .itertuples(index=False, name=None)
        )
