"""Separate observation periods using complete snapshots, without inventing samples."""

from collections.abc import Hashable


class ObservationEpochs:
    """Epochs separate observed absences or invalidated intervals, not kernel lifetimes."""

    def __init__(self) -> None:
        self._previous: dict[Hashable, set[tuple[object, ...]]] = {}
        self._epochs: dict[tuple[Hashable, tuple[object, ...]], int] = {}

    def apply(
        self,
        rows: list[dict[str, object]],
        keys: tuple[str, ...],
        *,
        partition: Hashable = "",
        complete: bool = True,
        present: set[tuple[object, ...]] | None = None,
    ) -> list[dict[str, object]]:
        current = {tuple(row[key] for key in keys) for row in rows} if present is None else present
        previous = self._previous.get(partition, set())
        for key in current:
            identity = partition, key
            if identity not in self._epochs:
                self._epochs[identity] = 0
            elif key not in previous:
                self._epochs[identity] += 1
        self._previous[partition] = current if complete else previous | current
        return [
            dict(row, observation_epoch=self._epochs[partition, tuple(row[k] for k in keys)])
            for row in rows
        ]

    def invalidate(self, partition: Hashable) -> None:
        self._previous.pop(partition, None)
