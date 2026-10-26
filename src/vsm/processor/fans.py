from vsm.processor._scalar import ScalarProcessor


class FansProcessor(ScalarProcessor):
    """Convert fan sensor readings to rotational speeds in rpm."""

    name = "fans"
    column = "rpm"
    scale = 1

    unit = "rpm"
