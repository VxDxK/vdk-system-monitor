from dataclasses import dataclass

import pandas as pd
import plotly.graph_objects as go
from matplotlib.figure import Figure

from ._legend import legend
from .series import SeriesChart


@dataclass(frozen=True, kw_only=True)
class Line(SeriesChart):
    markers: bool = False
    categorical: bool = False

    def as_plotly(self, frame: pd.DataFrame) -> go.Figure:
        figure = go.Figure()
        for label, group, column in self.series(frame):
            figure.add_trace(
                go.Scatter(
                    x=group[self.x] if self.x else group.index,
                    y=group[column],
                    name=label,
                    text=self.hover_text(group),
                    hovertemplate="%{fullData.name}<br>%{x}<br>%{y}<br>%{text}<extra></extra>",
                    mode="lines+markers" if self.markers else "lines",
                    connectgaps=False,
                )
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
        categories = (
            list(dict.fromkeys(str(value) for column in self.y for value in frame[column].dropna()))
            if self.categorical
            else []
        )
        for label, group, column in self.series(frame):
            values = (
                group[column]
                .map({label: index for index, label in enumerate(categories)})
                .to_numpy(dtype=float, na_value=float("nan"))
                if self.categorical
                else pd.to_numeric(group[column]).to_numpy(dtype=float, na_value=float("nan"))
            )
            axes.plot(
                group[self.x] if self.x else group.index,
                values,
                label=label,
                marker="." if self.markers else None,
            )
        if self.categorical:
            axes.set_yticks(range(len(categories)), categories)
        axes.set(title=self.title or self.label, xlabel=self.x_label, ylabel=self.ylabel)
        if self.y_range is not None:
            axes.set_ylim(*self.y_range)
        legend(axes, figure)
        return figure


def lines(
    frame: pd.DataFrame,
    label: str,
    y: tuple[str, ...],
    *,
    ylabel: str,
    group_by: tuple[str, ...] = (),
    x: str = "time",
    y_range: tuple[float, float] | None = None,
    where: tuple[tuple[str, str], ...] = (),
) -> tuple[Line, ...]:
    """Describe available fields of one measurement, omitting absent optional fields."""
    selected = tuple(column for column in y if column in frame.columns)
    if not selected or x not in frame.columns:
        return ()
    return (
        Line(
            label=label,
            y=selected,
            x=x,
            group_by=tuple(column for column in group_by if column in frame.columns),
            ylabel=ylabel,
            y_range=y_range,
            where=where,
        ),
    )
