from dataclasses import dataclass
from datetime import UTC
from datetime import timezone as Timezone

import pandas as pd
import plotly.graph_objects as go
from matplotlib import dates as mdates
from matplotlib.figure import Figure
from matplotlib.ticker import MaxNLocator

from ._legend import legend
from ._timezone import timezone_label
from .series import SeriesChart


@dataclass(frozen=True, kw_only=True)
class Line(SeriesChart):
    markers: bool = False
    categorical: bool = False
    y_scale: float = 1.0
    integer_y: bool = False
    step: bool = False
    datetime_x: bool = True

    def _dated(self, frame: pd.DataFrame) -> bool:
        return (
            self.datetime_x
            and self.x == "time"
            and "timestamp_ns" in frame
            and frame["timestamp_ns"].notna().any()
        )

    def _x_values(self, frame: pd.DataFrame, *, static: bool = False, timezone: Timezone = UTC):
        if not self._dated(frame):
            return frame[self.x] if self.x else frame.index
        dates = pd.to_datetime(frame["timestamp_ns"], unit="ns", utc=True)
        if static:
            return mdates.date2num(dates.dt.tz_localize(None).to_numpy())
        if timezone.utcoffset(None):
            # Plotly's date axis has no display-zone setting. Render local wall time
            # without an offset suffix so the browser does not convert it back to UTC.
            dates = dates.dt.tz_convert(timezone).dt.tz_localize(None)
        return [
            value.isoformat().replace("+00:00", "Z") if pd.notna(value) else None for value in dates
        ]

    def as_plotly(self, frame: pd.DataFrame, *, timezone: Timezone = UTC) -> go.Figure:
        zone_label = timezone_label(timezone)
        figure = go.Figure()
        dated = self._dated(frame)
        for label, group, column in self.series(frame):
            hover = self.hover_text(group)
            if dated and "timestamp_uncertainty_ns" in group:
                hover = [
                    f"{text}<br>Estimated UTC uncertainty: ±{error / 1e6:.3f} ms"
                    if pd.notna(error)
                    else text
                    for text, error in zip(hover, group["timestamp_uncertainty_ns"], strict=True)
                ]
            figure.add_trace(
                go.Scatter(
                    x=self._x_values(group, timezone=timezone),
                    y=group[column] if self.categorical else group[column] * self.y_scale,
                    name=label,
                    text=hover,
                    hovertemplate="%{fullData.name}<br>"
                    + ("%{x|%Y-%m-%d %H:%M:%S.%L} " + zone_label if dated else "%{x}")
                    + "<br>%{y} "
                    + self.ylabel
                    + "<br>%{text}<extra></extra>",
                    line_shape="hv" if self.step else "linear",
                    mode="lines+markers" if self.markers else "lines",
                    connectgaps=False,
                )
            )
        figure.update_layout(
            title=self.title or self.label,
            xaxis_title=f"Date/time ({zone_label})" if dated else self.x_label,
            yaxis_title=self.ylabel,
        )
        if dated:
            figure.update_xaxes(type="date", tickformat="%Y-%m-%d<br>%H:%M:%S")
        if self.y_range is not None:
            figure.update_yaxes(range=self.y_range)
        if self.integer_y:
            figure.update_yaxes(tickmode="linear", tick0=0, dtick=1, tickformat="d")
        return figure

    def as_matplotlib(self, frame: pd.DataFrame, *, timezone: Timezone = UTC) -> Figure:
        zone_label = timezone_label(timezone)
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
                * self.y_scale
            )
            axes.plot(
                self._x_values(group, static=True),
                values,
                label=label,
                marker="." if self.markers else None,
                drawstyle="steps-post" if self.step else "default",
            )
        if self.categorical:
            axes.set_yticks(range(len(categories)), categories)
        elif self.integer_y:
            axes.yaxis.set_major_locator(MaxNLocator(integer=True, min_n_ticks=1))
        dated = self._dated(frame)
        axes.set(
            title=self.title or self.label,
            xlabel=f"Date/time ({zone_label})" if dated else self.x_label,
            ylabel=self.ylabel,
        )
        if dated:
            axes.xaxis_date(tz=timezone)
            axes.xaxis.set_major_locator(mdates.AutoDateLocator(tz=timezone))
            axes.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d\n%H:%M:%S", tz=timezone))
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
    y_scale: float = 1.0,
    integer_y: bool = False,
    step: bool = False,
    datetime_x: bool = True,
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
            y_scale=y_scale,
            integer_y=integer_y,
            step=step,
            datetime_x=datetime_x,
        ),
    )
