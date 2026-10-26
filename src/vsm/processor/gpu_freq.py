from vsm.processor._scalar import ScalarProcessor


class GpuFreqProcessor(ScalarProcessor):
    """Convert GPU devfreq frequency readings to Hz."""

    name = "gpu_freq"
    chart_title = "GPU frequency"
    group_label = "Hardware Frequency"
    column = "frequency_hz"
    scale = 1

    unit = "Hz"
