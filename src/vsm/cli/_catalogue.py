"""Discover documentation from the defining implementations."""

from __future__ import annotations

from dataclasses import dataclass

from vsm.cli._source import source_docs


@dataclass(frozen=True)
class Entry:
    name: str
    category: str
    implementation: type | None
    documentation: str

    @property
    def summary(self) -> str:
        return self.documentation.splitlines()[0]


def entries(category: str) -> tuple[Entry, ...]:
    if category == "actions":
        from vsm.action.registry import ACTION_TYPES

        implementations = ACTION_TYPES
    elif category == "processors":
        from vsm.processor.registry import PROCESSORS

        implementations = {name: type(value) for name, value in PROCESSORS.items()}
    elif category == "connections":
        from vsm.connection.registry import CONNECTION_TYPES

        implementations = CONNECTION_TYPES
    elif category == "binaries":
        from vsm.cli._native import descriptions

        return tuple(Entry(name, category, None, doc) for name, doc in descriptions())
    else:
        raise ValueError(f"unknown documentation category: {category}")
    return tuple(
        Entry(name, category, cls, source_docs(cls).description or "No description in source.")
        for name, cls in implementations.items()
    )
