from vsm.processor._scalar import ScalarProcessor


class CpuFreqProcessor(ScalarProcessor):
    """Convert CPU scaling frequency readings from kHz to Hz."""

    name = "cpu_freq"
    chart_title = "CPU frequency"
    group_label = "Hardware Frequency"
    column = "frequency_hz"
    scale = 1000

    unit = "Hz"
