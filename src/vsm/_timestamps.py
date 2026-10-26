"""Attach controller UTC timestamps without changing measurement elapsed values."""

from __future__ import annotations

import math
from dataclasses import replace

import pandas as pd

from vsm.models import LogMetadata
from vsm.processing import Column, Dataset


def timestamp_dataset(data: Dataset, metadata: LogMetadata) -> Dataset:
    if "time" not in data.frame or data.frame.empty:
        return data
    if not any(c.name == "time" and c.unit == "s" for c in data.columns):
        return data
    clock, origin = metadata.clock, metadata.time_origin
    warnings = [*data.warnings, *metadata.warnings]
    if clock is not None:
        warnings.extend(clock.warnings)
    try:
        if clock is None or origin is None:
            raise ValueError("controller clock mapping or measurement time origin was not recorded")
        if origin.domain != "controller_monotonic" and any(
            s.target.boot_id != metadata.target.boot_id for s in clock.samples
        ):
            raise ValueError("clock samples belong to another target boot")
        stamps, errors = [], []
        for elapsed in data.frame["time"]:
            if pd.isna(elapsed):
                stamps.append(None)
                errors.append(None)
                continue
            if not math.isfinite(float(elapsed)):
                raise ValueError("non-finite measurement time")
            stamp, error = clock.timestamp(origin, round(float(elapsed) * 1e9))
            stamps.append(stamp)
            errors.append(error)
        frame = data.frame.copy()
        frame["timestamp_ns"] = pd.array(stamps, dtype="Int64")
        frame["timestamp_uncertainty_ns"] = pd.array(errors, dtype="Int64")
        columns = tuple(
            c for c in data.columns if c.name not in ("timestamp_ns", "timestamp_uncertainty_ns")
        )
        return replace(
            data,
            frame=frame,
            columns=(
                *columns,
                Column(name="timestamp_ns", unit="ns since Unix epoch (UTC)"),
                Column(name="timestamp_uncertainty_ns", unit="ns"),
            ),
            warnings=tuple(dict.fromkeys(warnings)),
        )
    except ValueError as error:
        warnings.append(f"UTC timeline unavailable; using elapsed seconds: {error}.")
        return replace(data, warnings=tuple(dict.fromkeys(warnings)))
