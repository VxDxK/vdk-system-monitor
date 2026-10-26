from dataclasses import dataclass

import pandas as pd
import plotly.graph_objects as go
from matplotlib.figure import Figure

from .series import SeriesChart


@dataclass(frozen=True, kw_only=True)
class Box(SeriesChart):
    x: str | None = None
    points: bool = True

    def as_plotly(self, frame: pd.DataFrame) -> go.Figure:
        figure = go.Figure()
        for label, group, column in self.series(frame):
            figure.add_trace(
                go.Box(y=group[column], name=label, boxpoints="all" if self.points else False)
            )
        figure.update_layout(
            title=self.title or self.label, xaxis_title=self.x_label, yaxis_title=self.ylabel
        )
        if self.y_range is not None:
            figure.update_yaxes(range=self.y_range)
        return figure

    def as_matplotlib(self, frame: pd.DataFrame) -> Figure:
        figure = Figure(figsize=(8, 4), layout="constrained")
        axes = figure.subplots()
        series = [
            (label, pd.to_numeric(group[column]).dropna().to_numpy())
            for label, group, column in self.series(frame)
        ]
        if series:
            axes.boxplot(
                [values for _, values in series],
                tick_labels=[label for label, _ in series],
                showfliers=self.points,
            )
        if series:
            axes.tick_params(axis="x", labelrotation=30, labelsize=8)
        axes.set(title=self.title or self.label, xlabel=self.x_label, ylabel=self.ylabel)
        if self.y_range is not None:
            axes.set_ylim(*self.y_range)
        return figure
