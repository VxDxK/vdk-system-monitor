"""Validate and label fixed-offset chart display time zones."""

from datetime import timedelta, timezone


def timezone_label(zone: timezone) -> str:
    if not isinstance(zone, timezone):
        raise TypeError("chart timezone must be a datetime.timezone fixed offset")
    offset = zone.utcoffset(None)
    if offset % timedelta(minutes=1):
        raise ValueError("chart timezone offset must use whole minutes")
    minutes = offset // timedelta(minutes=1)
    if not minutes:
        return "UTC"
    hours, minutes_part = divmod(abs(minutes), 60)
    return f"UTC{'+' if minutes > 0 else '-'}{hours:02d}:{minutes_part:02d}"
