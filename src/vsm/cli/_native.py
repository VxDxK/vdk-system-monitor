"""Read native descriptions from Rust module comments and clap metadata."""

from __future__ import annotations

import re
from importlib.resources import files
from pathlib import Path


def descriptions() -> tuple[tuple[str, str], ...]:
    directory = files("vsm").joinpath("_native_sources")
    if not directory.is_dir():
        # Source checkout: wheel packaging copies the same directory as resources.
        directory = Path(__file__).resolve().parents[3] / "binary-utils/src/bin"
    if not directory.is_dir():
        raise RuntimeError("native documentation sources are missing; reinstall the vsm wheel")
    result = []
    for path in sorted(directory.iterdir(), key=lambda path: path.name):
        if not path.name.endswith(".rs"):
            continue
        source = path.read_text(encoding="utf-8")
        about = re.search(r'\babout\s*=\s*"((?:\\.|[^"\\])*)"', source)
        module_comments = []
        for line in source.splitlines():
            if line.startswith("//!"):
                module_comments.append(line[3:].removeprefix(" "))
            elif line.strip():
                break
        pieces = [about[1] if about else "", "\n".join(module_comments)]
        description = "\n\n".join(piece for piece in pieces if piece)
        result.append((path.name.removesuffix(".rs"), description or "No description in source."))
    return tuple(result)
