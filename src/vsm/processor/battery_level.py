from __future__ import annotations

from dataclasses import replace
from pathlib import PurePosixPath
from typing import ClassVar

from vsm.chart.line import lines
from vsm.processing import Dataset, Params
from vsm.processor._file import NativeFileProcessor
from vsm.processor._parsing import Event, text, time_of
from vsm.util.records import FileRecord


class BatteryLevelProcessor(NativeFileProcessor[FileRecord]):
    """Convert power-supply readings to percent, mAh, mA and microvolts when available."""

    name = "battery_level"
    group_label = "Hardware Battery"
    fields: ClassVar[dict[str, tuple[str, float, str]]] = {
        "capacity": ("level_percent", 1, "%"),
        "capacity_fcc": ("capacity_full_mah", 1, "mAh"),
        "capacity_rm": ("capacity_remaining_mah", 1, "mAh"),
        "charge_full": ("capacity_full_mah", 0.001, "mAh"),
        "charge_now": ("capacity_remaining_mah", 0.001, "mAh"),
        "current_now": ("current_ma", 0.001, "mA"),
        "voltage_now": ("voltage_uv", 1, "µV"),
    }

    def parse_event(self, event: Event, params: Params) -> list[dict[str, object]]:
        if event["type"] != "file":
            return []
        path = str(event["path"])
        if params.selected and path not in params.selected:
            return []
        field = PurePosixPath(path).name
        if field not in self.fields:
            return []
        column, scale, _unit = self.fields[field]
        return [
            {
                "time": time_of(event),
                "path": path,
                "battery": PurePosixPath(path).parent.name,
                "metric": field,
                column: float(text(event).strip()) * scale,
            }
        ]

    def column_units(self, es: list[Event]) -> dict[str, str]:
        return {"time": "s", **{column: unit for column, _scale, unit in self.fields.values()}}

    def describe(self, data: Dataset) -> Dataset:
        specs = (
            ("Battery level", ("level_percent",), "%"),
            ("Battery capacity", ("capacity_full_mah", "capacity_remaining_mah"), "mAh"),
            ("Battery current · driver-defined polarity", ("current_ma",), "mA"),
            ("Battery voltage", ("voltage_uv",), "µV"),
        )
        charts = tuple(
            replace(chart, sparse=True)
            for label, fields, unit in specs
            for chart in lines(data.frame, label, fields, ylabel=unit, group_by=("battery",))
        )
        available = {
            column for column, _scale, _unit in self.fields.values() if column in data.frame
        }
        missing = [label for label, fields, _unit in specs if not available.intersection(fields)]
        return replace(
            data,
            charts=charts,
            warnings=data.warnings
            + tuple(f"Unavailable: {label} (not captured or unsupported)." for label in missing),
        )
