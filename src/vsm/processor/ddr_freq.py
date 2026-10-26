from vsm.processor._scalar import ScalarProcessor


class DdrFreqProcessor(ScalarProcessor):
    """Convert DDR devfreq frequency readings to Hz."""

    name = "ddr_freq"
    chart_title = "DDR memory frequency"
    group_label = "Hardware Frequency"
    column = "frequency_hz"
    scale = 1

    unit = "Hz"
