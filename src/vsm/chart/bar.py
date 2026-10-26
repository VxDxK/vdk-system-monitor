from dataclasses import dataclass

import pandas as pd
import plotly.graph_objects as go
from matplotlib.figure import Figure

from ._legend import legend
from .series import SeriesChart


@dataclass(frozen=True, kw_only=True)
class Bar(SeriesChart):
    def as_plotly(self, frame: pd.DataFrame) -> go.Figure:
        figure = go.Figure()
        for label, group, column in self.series(frame):
            figure.add_trace(
                go.Bar(x=group[self.x] if self.x else group.index, y=group[column], name=label)
            )
        figure.update_layout(
            title=self.title or self.label,
            xaxis_title=self.x_label,
            yaxis_title=self.ylabel,
            barmode="group",
        )
        return figure

    def as_matplotlib(self, frame: pd.DataFrame) -> Figure:
        figure = Figure(figsize=(8, 4), layout="constrained")
        axes = figure.subplots()
        series = list(self.series(frame))
        categories = list(
            dict.fromkeys(
                str(value)
                for _, group, _ in series
                for value in (group[self.x] if self.x else group.index)
            )
        )
        width = 0.8 / max(len(series), 1)
        for index, (label, group, column) in enumerate(series):
            xs = [
                categories.index(str(value)) for value in (group[self.x] if self.x else group.index)
            ]
            axes.bar(
                [value + (index - (len(series) - 1) / 2) * width for value in xs],
                pd.to_numeric(group[column]).to_numpy(dtype=float, na_value=float("nan")),
                width,
                label=label,
            )
        axes.set_xticks(range(len(categories)), categories)
        axes.set(title=self.title or self.label, xlabel=self.x_label, ylabel=self.ylabel)
        legend(axes, figure)
        return figure
