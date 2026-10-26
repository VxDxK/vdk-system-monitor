"""Explicit tabular chart description for measurements with no meaningful axes."""

from dataclasses import dataclass

import pandas as pd
import plotly.graph_objects as go
from matplotlib.figure import Figure


@dataclass(frozen=True, kw_only=True)
class Table:
    label: str
    columns: tuple[str, ...] = ()
    title: str = ""
    additional: bool = False

    def select(self, frame: pd.DataFrame) -> pd.DataFrame:
        return frame[list(self.columns)] if self.columns else frame

    def as_plotly(self, frame: pd.DataFrame) -> go.Figure:
        selected = self.select(frame)
        return go.Figure(
            go.Table(
                header={"values": list(selected.columns)},
                cells={"values": [selected[column].tolist() for column in selected]},
            )
        )

    def as_matplotlib(self, frame: pd.DataFrame) -> Figure:
        selected = self.select(frame)
        figure = Figure(figsize=(10, max(3, len(selected) * 0.3)), layout="constrained")
        axes = figure.subplots()
        axes.axis("off")
        axes.table(
            cellText=selected.astype(str).values, colLabels=list(selected.columns), loc="center"
        )
        axes.set_title(self.title or self.label)
        return figure
