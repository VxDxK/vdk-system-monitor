"""Chart extension protocol, independent of measurement names."""

from typing import Protocol

import pandas as pd
from matplotlib.figure import Figure
from plotly.graph_objects import Figure as PlotlyFigure


class Chart(Protocol):
    @property
    def label(self) -> str: ...
    def as_plotly(self, frame: pd.DataFrame) -> PlotlyFigure: ...
    def as_matplotlib(self, frame: pd.DataFrame) -> Figure: ...
