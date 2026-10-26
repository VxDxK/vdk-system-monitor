from vsm.processor._scalar import ScalarProcessor


class FrameCountProcessor(ScalarProcessor):
    """Compute display frame rates in frames/s from cumulative counters."""

    name = "frame_count"
    chart_title = "Frame rate"
    group_label = "Graphics"
    column = "frames"
    scale = 1
    cumulative = True

    unit = "frames"
