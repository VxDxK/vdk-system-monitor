"""Static/interactive HTML reports and individual chart exports."""

import base64
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC
from datetime import timezone as Timezone
from enum import StrEnum
from html import escape
from io import BytesIO
from pathlib import Path

import pandas as pd
from matplotlib.backends.backend_agg import FigureCanvasAgg
from plotly.offline import get_plotlyjs

from vsm.chart._timezone import timezone_label
from vsm.chart.chart import Chart
from vsm.chart.line import Line
from vsm.chart.table import Table
from vsm.processing import Dataset


class Backend(StrEnum):
    MATPLOTLIB = "matplotlib"
    PLOTLY = "plotly"


class Layout(StrEnum):
    FOLDABLE = "foldable"
    TABLE = "table"
    FILES = "files"


class ImageFormat(StrEnum):
    PNG = "png"
    JPEG = "jpeg"
    SVG = "svg"
    PDF = "pdf"


@dataclass(frozen=True, kw_only=True)
class ExportOptions:
    backend: Backend = Backend.PLOTLY
    layout: Layout = Layout.FOLDABLE
    image_format: ImageFormat = ImageFormat.PNG
    columns: int = 2
    timezone: Timezone = UTC

    def __post_init__(self) -> None:
        timezone_label(self.timezone)
        if (
            not isinstance(self.backend, Backend)
            or not isinstance(self.layout, Layout)
            or not isinstance(self.image_format, ImageFormat)
        ):
            raise TypeError("typed backend, layout, and image format required")
        if type(self.columns) is not int:
            raise TypeError("columns must be an integer")
        if self.columns < 1:
            raise ValueError("columns must be positive")


_DEFAULT_OPTIONS = ExportOptions()


def _safe(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", label).strip(".-")[:100] or "chart"


def _image(
    chart: Chart, frame: pd.DataFrame, format: ImageFormat, timezone: Timezone = UTC
) -> bytes:
    figure = (
        chart.as_matplotlib(frame, timezone=timezone)
        if isinstance(chart, Line)
        else chart.as_matplotlib(frame)
    )
    try:
        FigureCanvasAgg(figure)
        buffer = BytesIO()
        figure.savefig(buffer, format=format.value, dpi=115, bbox_inches="tight")
        return buffer.getvalue()
    finally:
        figure.clear()


def _group_key(data: Dataset) -> tuple[str, str, Path | None, str]:
    origin = data.provenance
    return (
        (str(origin.device_id), str(origin.action_id), origin.path, data.group_label)
        if origin
        else ("", "", None, data.group_label)
    )


def _source(data: Dataset) -> str:
    origin = data.provenance
    if not origin:
        return ""
    fields = {
        "Source": str(origin.path),
        "Device": str(origin.device_id),
        "Action": str(origin.action_id),
        "Schema": origin.schema,
        "Complete": str(origin.complete),
        "Target": str(origin.metadata.target),
        "Numerical policy": origin.numerical_policy,
    }
    if clock := origin.metadata.clock:
        fields["Controller clock"] = (
            f"UTC anchor {clock.controller_utc_ns} ns since Unix epoch; "
            f"{len(clock.samples)} measured target calibration samples"
        )
    if time_origin := origin.metadata.time_origin:
        fields["Measurement clock"] = (
            f"{time_origin.domain}; origin {time_origin.value_ns} ns; "
            f"origin uncertainty {time_origin.uncertainty_ns} ns"
        )
    columns = [
        column
        for column in (
            "entity_label",
            "path",
            "pid",
            "tid",
            "start_ticks",
            "socket_id",
            "local",
            "remote",
            "station",
            "cpu",
            "interface",
        )
        if column in data.frame
    ]
    entities = _table(data.frame[columns].drop_duplicates()) if columns else ""
    return (
        f"<details class='source'><summary>Source details · {escape(data.data_label)}</summary><dl>"
        + "".join(
            f"<dt>{escape(key)}</dt><dd>{escape(value)}</dd>" for key, value in fields.items()
        )
        + "</dl>"
        + entities
        + "</details>"
    )


def _table(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "<p class='empty'>No measurement data available.</p>"
    return (
        "<div class='table-wrap' tabindex='0' aria-label='Data table'>"
        + frame.to_html(index=False, escape=True)
        + "</div>"
    )


def _card(
    chart: Chart, data: Dataset, number: int, backend: Backend, timezone: Timezone = UTC
) -> str:
    heading = getattr(chart, "title", "") or chart.label
    parts = [f"<article class='chart'><h3>{escape(heading)}</h3>"]
    if data.frame.empty:
        parts.append("<p class='empty'>No measurement data available.</p>")
    elif isinstance(chart, Table):
        parts.append(_table(chart.select(data.frame)))
    elif backend is Backend.PLOTLY:
        figure = (
            chart.as_plotly(data.frame, timezone=timezone)
            if isinstance(chart, Line)
            else chart.as_plotly(data.frame)
        )
        if not figure.data:
            parts.append(
                "<p class='empty'>No measurement data available for this chart.</p></article>"
            )
            return "\n".join(parts)
        # JSON in a script element must not be able to terminate that element.
        payload = (
            figure.to_json().replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
        )
        parts.append(
            f"<div id='chart-{number:06d}' class='plot'></div><div class='legend' aria-label='Chart legend'></div>"
        )
        parts.append(
            f"<script type='application/json' data-figure='chart-{number:06d}'>{payload}</script>"
        )
    else:
        image = base64.b64encode(_image(chart, data.frame, ImageFormat.PNG, timezone)).decode(
            "ascii"
        )
        parts.append(
            f"<img alt='{escape(chart.label, quote=True)}' src='data:image/png;base64,{image}'>"
        )
    parts.append("</article>")
    return "\n".join(parts)


def export_charts(
    datasets: Iterable[Dataset],
    destination: Path,
    *,
    options: ExportOptions = _DEFAULT_OPTIONS,
    title: str = "vsm measurements",
) -> tuple[Path, ...]:
    """Export an offline report, or independently named chart files.

    Chart and legend content expands in the page; only data tables scroll sideways.
    Source identities are separate from their readable presentation labels.
    """
    groups: dict[tuple[str, str, Path | None, str], list[Dataset]] = {}
    for data in datasets:
        groups.setdefault(_group_key(data), []).append(data)
    if options.layout is Layout.FILES:
        destination.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        for group_index, (key, values) in enumerate(groups.items(), 1):
            directory = destination / f"{group_index:04d}-{_safe(key[3])}"
            directory.mkdir(exist_ok=True)
            for data_index, data in enumerate(values, 1):
                charts = data.charts or (Table(label=data.data_label),)
                for chart_index, chart in enumerate(charts, 1):
                    stem = f"{data_index:04d}-{chart_index:04d}-{_safe(chart.label)}"
                    if options.backend is Backend.MATPLOTLIB and not isinstance(chart, Table):
                        path = directory / (stem + "." + options.image_format.value)
                        path.write_bytes(
                            _image(chart, data.frame, options.image_format, options.timezone)
                        )
                    else:
                        path = directory / (stem + ".html")
                        export_charts(
                            (replace(data, charts=(chart,)),),
                            path,
                            options=replace(options, layout=Layout.TABLE, columns=1),
                            title=title,
                        )
                    paths.append(path)
        return tuple(paths)
    destination.parent.mkdir(parents=True, exist_ok=True)
    assets = Path(__file__).parent
    html = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>",
        f"<title>{escape(title)}</title><style>{assets.joinpath('report.css').read_text()}</style>",
    ]
    if options.backend is Backend.PLOTLY:
        html.append("<script>" + get_plotlyjs() + "</script>")
    html.append(
        f"</head><body style='--columns:{options.columns}'><main><header><h1>{escape(title)}</h1><p class='muted'>Measurements · {len(groups)} sections</p>"
    )
    if options.layout is Layout.FOLDABLE:
        html.append(
            "<div class='toolbar'><button type='button' data-expand='true'>Expand all</button><button type='button' data-expand='false'>Collapse all</button></div>"
        )
    html.append("<nav aria-label='Measurements'>")
    for index, key in enumerate(groups, 1):
        html.append(
            f"<a href='#section-{index}'>{escape(key[3])}{' · ' + escape(key[0]) if key[0] else ''} · {index}</a>"
        )
    html.append("</nav></header>")
    chart_number = 0
    if not groups:
        html.append("<p class='empty'>No measurement data available.</p>")
    for index, (key, values) in enumerate(groups.items(), 1):
        device, action, source, group = key
        context = " · ".join(
            part for part in (device, action, source.parent.parent.name if source else "") if part
        )
        heading = escape(group) + f"<span class='source-label'>{escape(context)}</span>"
        if options.layout is Layout.FOLDABLE:
            html.append(
                f"<details open class='section' id='section-{index}'><summary>{heading}</summary>"
            )
        else:
            html.append(f"<section class='section' id='section-{index}'><h2>{heading}</h2>")
        html.append("<div class='section-body'>")
        for data in values:
            notices = list(data.warnings)
            if data.provenance and not data.provenance.complete:
                notices.insert(0, "Incomplete source log")
            for warning in notices:
                html.append(
                    f"<p class='notice'><strong>{escape(data.data_label)}</strong>: {escape(warning)}</p>"
                )
        for additional in (False, True):
            cards = []
            for data in values:
                charts = data.charts or (Table(label=data.data_label),)
                for chart in charts:
                    if bool(getattr(chart, "additional", False)) != additional:
                        continue
                    chart_number += 1
                    cards.append(
                        _card(chart, data, chart_number, options.backend, options.timezone)
                    )
            if cards:
                if additional:
                    html.append("<h3 class='additional'>Additional vsm charts</h3>")
                html.extend(["<div class='charts'>", *cards, "</div>"])
        html.extend(_source(data) for data in values)
        html.append(
            "</div>" + ("</details>" if options.layout is Layout.FOLDABLE else "</section>")
        )
    html.append(f"<script>{assets.joinpath('report.js').read_text()}</script></main></body></html>")
    destination.write_text("\n".join(html), encoding="utf-8")
    return (destination,)
