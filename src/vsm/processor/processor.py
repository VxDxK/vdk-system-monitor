from __future__ import annotations

from typing import Protocol

from vsm.models import LogRef


class Processor[Record, Params, Result](Protocol):
    def process(self, source: LogRef[Record], *, params: Params) -> Result: ...
