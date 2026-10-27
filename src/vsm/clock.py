"""Measured target/controller clock relationships; UTC comes only from the controller."""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, replace
from itertools import pairwise


def _nonnegative(**values: int) -> None:
    for name, value in values.items():
        if type(value) is not int:
            raise TypeError(f"{name} must be an integer")
        if value < 0:
            raise ValueError(f"{name} must be nonnegative")


@dataclass(frozen=True, kw_only=True)
class TargetClock:
    boot_id: str
    monotonic_ns: int
    realtime_ns: int
    uncertainty_ns: int

    def __post_init__(self) -> None:
        if not isinstance(self.boot_id, str) or not self.boot_id:
            raise ValueError("clock boot identity is required")
        _nonnegative(
            monotonic_ns=self.monotonic_ns,
            realtime_ns=self.realtime_ns,
            uncertainty_ns=self.uncertainty_ns,
        )


@dataclass(frozen=True, kw_only=True)
class ClockSample:
    target: TargetClock
    controller_before_ns: int
    controller_after_ns: int

    def __post_init__(self) -> None:
        _nonnegative(
            controller_before_ns=self.controller_before_ns,
            controller_after_ns=self.controller_after_ns,
        )
        if self.controller_after_ns < self.controller_before_ns:
            raise ValueError("reversed controller clock bracket")

    @property
    def midpoint_ns(self) -> int:
        return self.controller_before_ns + self.roundtrip_ns // 2

    @property
    def roundtrip_ns(self) -> int:
        return self.controller_after_ns - self.controller_before_ns

    @property
    def uncertainty_ns(self) -> int:
        return (self.roundtrip_ns + 1) // 2 + self.target.uncertainty_ns


@dataclass(frozen=True, kw_only=True)
class TimeOrigin:
    domain: str
    value_ns: int
    uncertainty_ns: int = 0

    def __post_init__(self) -> None:
        if self.domain not in ("target_monotonic", "target_realtime", "controller_monotonic"):
            raise ValueError("unknown timestamp domain")
        _nonnegative(value_ns=self.value_ns, uncertainty_ns=self.uncertainty_ns)


@dataclass(frozen=True, kw_only=True)
class ClockCalibration:
    controller_utc_ns: int
    controller_monotonic_ns: int
    anchor_uncertainty_ns: int = 0
    samples: tuple[ClockSample, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _nonnegative(
            controller_utc_ns=self.controller_utc_ns,
            controller_monotonic_ns=self.controller_monotonic_ns,
            anchor_uncertainty_ns=self.anchor_uncertainty_ns,
        )

    def timestamp(self, origin: TimeOrigin, elapsed_ns: int) -> tuple[int, int]:
        value = origin.value_ns + elapsed_ns
        base_error = self.anchor_uncertainty_ns + origin.uncertainty_ns
        if origin.domain == "controller_monotonic":
            return self.controller_utc_ns + value - self.controller_monotonic_ns, base_error
        if not self.samples:
            raise ValueError("no target/controller clock calibration")
        samples = sorted(self.samples, key=lambda s: s.target.monotonic_ns)
        if len({s.target.boot_id for s in samples}) != 1:
            raise ValueError("clock samples span different target boots")
        if origin.domain == "target_realtime":
            for left, right in pairwise(samples):
                delta = right.target.monotonic_ns - left.target.monotonic_ns
                jump = abs(right.target.realtime_ns - left.target.realtime_ns - delta)
                tolerance = left.uncertainty_ns + right.uncertainty_ns + 1_000_000 + delta // 10_000
                if jump > tolerance:
                    raise ValueError("target realtime clock discontinuity")
            nearest = min(samples, key=lambda s: abs(s.target.realtime_ns - value))
            value += nearest.target.monotonic_ns - nearest.target.realtime_ns
        left = max(
            (s for s in samples if s.target.monotonic_ns <= value),
            key=lambda s: s.target.monotonic_ns,
            default=samples[0],
        )
        right = min(
            (s for s in samples if s.target.monotonic_ns >= value),
            key=lambda s: s.target.monotonic_ns,
            default=samples[-1],
        )
        delta = right.target.monotonic_ns - left.target.monotonic_ns
        if delta:
            # Integer arithmetic retains epoch nanoseconds without float rounding.
            controller = (
                left.midpoint_ns
                + (value - left.target.monotonic_ns)
                * (right.midpoint_ns - left.midpoint_ns)
                // delta
            )
            error = max(left.uncertainty_ns, right.uncertainty_ns)
        else:
            controller = left.midpoint_ns + value - left.target.monotonic_ns
            # Outside calibration, budget 100 ppm drift. This is an estimate, not a bound.
            error = left.uncertainty_ns + abs(value - left.target.monotonic_ns) // 10_000
        return (
            self.controller_utc_ns + controller - self.controller_monotonic_ns,
            base_error + error,
        )


class ClockTracker:
    """One connection's bounded, serialized calibration worker."""

    def __init__(self, connection):
        self.connection = connection
        before = time.monotonic_ns()
        utc = time.time_ns()
        after = time.monotonic_ns()
        self.calibration = ClockCalibration(
            controller_utc_ns=utc,
            controller_monotonic_ns=before + (after - before) // 2,
            anchor_uncertainty_ns=(after - before + 1) // 2,
        )
        self._lock = asyncio.Lock()

    async def refresh(self) -> ClockCalibration:
        async with self._lock:
            try:
                async with asyncio.timeout(10):
                    samples = []
                    for _ in range(5):
                        before = time.monotonic_ns()
                        target = await self.connection.sample_clock()
                        after = time.monotonic_ns()
                        if not isinstance(target, TargetClock):
                            raise TypeError("invalid clock sample")
                        if target.boot_id != self.connection.target.boot_id:
                            raise ValueError("clock sample belongs to another boot")
                        samples.append(
                            ClockSample(
                                target=target,
                                controller_before_ns=before,
                                controller_after_ns=after,
                            )
                        )
                    best = min(samples, key=lambda s: s.roundtrip_ns)
                    self.calibration = replace(
                        self.calibration, samples=(*self.calibration.samples, best)
                    )
            except Exception as error:  # noqa: BLE001 -- Clock failure must not discard measurements.
                message = (
                    f"Controller clock calibration unavailable: {type(error).__name__}: {error}"
                )
                self.calibration = replace(
                    self.calibration,
                    warnings=tuple(dict.fromkeys((*self.calibration.warnings, message))),
                )
            return self.calibration

    def session(self) -> AbstractAsyncContextManager[None]:
        return ClockTracker._session(self)

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[None]:
        from vsm._cleanup import cleanup

        await self.refresh()

        async def renew():
            while True:
                await asyncio.sleep(30)
                await self.refresh()

        worker = asyncio.create_task(renew())
        try:
            yield
        finally:

            async def finish():
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
                await self.refresh()

            await cleanup(finish())
