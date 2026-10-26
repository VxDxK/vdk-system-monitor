from dataclasses import replace

import pandas as pd

from vsm.chart.chart import Chart
from vsm.processor._scalar import ScalarProcessor


class CpuFreqProcessor(ScalarProcessor):
    """Convert CPU scaling frequency readings from kHz to Hz."""

    name = "cpu_freq"
    chart_title = "CPU frequency"
    group_label = "Hardware Frequency"
    column = "frequency_hz"
    scale = 1000

    unit = "Hz"

    def charts(self, frame: pd.DataFrame) -> tuple[Chart, ...]:
        return tuple(replace(chart, ylabel="GHz", y_scale=1e-9) for chart in super().charts(frame))
