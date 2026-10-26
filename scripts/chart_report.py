"""Generate an offline report to review layout, long legends and table overflow.

Run: python scripts/chart_report.py [output-directory]
All measurements are synthetic; no target collection is performed.
"""

import argparse
from pathlib import Path

import pandas as pd

from vsm.chart.line import Line
from vsm.models import ActionId, DeviceId, LogMetadata, LogRef, Schema, TargetInfo
from vsm.output.charts import Backend, ExportOptions, Layout, export_charts
from vsm.processing import Dataset, dataset


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", type=Path, default=Path("reports/chart-export"))
    args = parser.parse_args()
    values = [
        dataset(
            "Hardware Frequency",
            [
                {
                    "time": t,
                    "path": f"/sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_cur_freq",
                    "frequency_hz": (1.2 + (cpu + t) % 8 / 10) * 1e9 if t != 10 else None,
                }
                for t in range(20)
                for cpu in range(32)
            ],
            charts=(
                Line(label="CPU frequency", y=("frequency_hz",), ylabel="Hz", group_by=("path",)),
            ),
        ),
        dataset(
            "Thread Statistics",
            [
                {
                    "time": t,
                    "pid": 123,
                    "tid": 123 + thread,
                    "start_ticks": 99,
                    "name": "worker with a deliberately long descriptive name " + str(thread),
                    "user_percent": (thread + t) % 80,
                    "system_percent": (thread + t) % 20,
                }
                for t in range(20)
                for thread in range(24)
            ],
            charts=tuple(
                Line(
                    label=title,
                    y=(column,),
                    ylabel="%",
                    group_by=("pid", "tid", "start_ticks", "name"),
                )
                for title, column in (
                    ("Thread load · user", "user_percent"),
                    ("Thread load · system", "system_percent"),
                )
            ),
        ),
        Dataset(
            frame=pd.DataFrame({f"Column {i}": ["Wide table content"] for i in range(15)}),
            group_label="Summary",
            data_label="Wide table",
            warnings=("Synthetic fixture: table-only dataset.",),
        ),
        Dataset(
            frame=pd.DataFrame(),
            group_label="Unavailable",
            data_label="Empty",
            warnings=("Synthetic fixture: no measurement samples were captured.",),
        ),
    ]

    def source(device: str, action: str, *, complete: bool = True) -> LogRef[object]:
        return LogRef(
            path=Path("synthetic") / device / action / "data.log",
            schema=Schema(name="synthetic"),
            device_id=DeviceId(device),
            action_id=ActionId(action),
            complete=complete,
            metadata=LogMetadata(
                target=TargetInfo(os="linux", architecture="x86_64", kernel="synthetic"),
                provider="synthetic",
            ),
        )

    values = [
        value.with_source(source("Device A", f"capture-{index}"))
        for index, value in enumerate(values)
    ]
    values.extend(
        (
            values[0].with_source(source("Device B", "capture-0", complete=False)),
            values[1].with_source(source("Device B", "capture-1")),
        )
    )
    args.output.mkdir(parents=True, exist_ok=True)
    for index, value in enumerate(values):
        value.to_csv(args.output / f"{index:02d}.csv")
    for layout in (Layout.FOLDABLE, Layout.TABLE):
        export_charts(
            values,
            args.output / f"{layout}.html",
            options=ExportOptions(layout=layout),
            title="vsm report review · synthetic data",
        )
    export_charts(
        values,
        args.output / "static.html",
        options=ExportOptions(backend=Backend.MATPLOTLIB),
        title="vsm static report · synthetic data",
    )
    print(args.output / "foldable.html")


if __name__ == "__main__":
    main()
