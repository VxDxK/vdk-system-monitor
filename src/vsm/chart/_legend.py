"""Legible static legends that retain every series."""

import textwrap

from matplotlib.axes import Axes
from matplotlib.figure import Figure


def legend(axes: Axes, figure: Figure) -> None:
    handles, labels = axes.get_legend_handles_labels()
    if not handles:
        return
    wrapped = [textwrap.fill(label, 30) for label in labels]
    rows = sum(label.count("\n") + 1 for label in wrapped)
    figure.set_size_inches(9, max(4, rows * 0.21))
    axes.legend(handles, wrapped, fontsize="small", loc="upper left", bbox_to_anchor=(1.02, 1))
