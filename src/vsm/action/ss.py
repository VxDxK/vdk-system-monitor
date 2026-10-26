from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from vsm.action.periodic_launcher import PeriodicLauncher


@dataclass(frozen=True, kw_only=True)
class Ss(PeriodicLauncher):
    """Periodically capture target ss TCP socket statistics."""

    argv: tuple[str, ...] = ("ss", "-tinm")
    schema_name: ClassVar[str] = "ss"
