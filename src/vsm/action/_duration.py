"""Optional native collection deadlines; resource scopes own open-ended work."""

import math


def validate_duration(duration: float | None) -> None:
    if duration is not None and (
        isinstance(duration, bool)
        or not math.isfinite(duration)
        or not 0 < duration <= 86400
        or round(duration * 1000) == 0
    ):
        raise ValueError("duration must round to at least 1ms and be at most 24h, or None")


def duration_arguments(duration: float | None) -> tuple[str, ...]:
    return () if duration is None else ("--duration-ms", str(round(duration * 1000)))
