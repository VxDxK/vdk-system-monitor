from vsm.processor._scalar import ScalarProcessor


class NpuFreqProcessor(ScalarProcessor):
    """Convert NPU devfreq frequency readings to Hz."""

    name = "npu_freq"
    chart_title = "NPU frequency"
    group_label = "Hardware Frequency"
    column = "frequency_hz"
    scale = 1

    unit = "Hz"
