"""Shared construction of empty, labeled measurement datasets."""

from dataclasses import replace

import pandas as pd

from vsm.processing import Column, Dataset


def stable_empty(data: Dataset, columns: tuple[str, ...], units: dict[str, str]) -> Dataset:
    if not data.frame.empty:
        return data
    return replace(
        data,
        frame=pd.DataFrame(columns=columns),
        columns=tuple(Column(name=c, unit=units.get(c)) for c in columns),
        charts=(),
    )
