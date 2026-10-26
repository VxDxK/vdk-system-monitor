"""Execution and normal-finalization policies for owned target workloads."""

from enum import Enum


class LaunchMode(Enum):
    """Foreground waits in do(); background modes return a scoped handle.

    BACKGROUND_WAIT waits on normal scope exit. BACKGROUND_STOP stops and reaps
    on scope exit. Errors/cancellation stop owned work
    in every mode; no mode relinquishes ownership.
    """

    FOREGROUND = "foreground"
    BACKGROUND_WAIT = "background_wait"
    BACKGROUND_STOP = "background_stop"
