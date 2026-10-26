"""Validation for public millisecond sampling intervals."""


def validate_interval(interval: int) -> None:
    if type(interval) is not int or interval <= 0:
        raise ValueError("interval must be a positive integer in milliseconds")
