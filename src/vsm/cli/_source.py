"""Read authored docstrings and comments beside Python declarations."""

from __future__ import annotations

import ast
import inspect
import io
import tokenize
from dataclasses import dataclass
from functools import cache
from pathlib import Path


@dataclass(frozen=True)
class SourceDocs:
    description: str
    parameters: dict[str, str]


def preceding_comments(lines: list[str], line: int) -> str:
    comments = []
    for text in reversed(lines[: line - 1]):
        stripped = text.strip()
        if not stripped.startswith("#"):
            break
        comments.append(stripped.lstrip("#").removeprefix(":").strip())
    return "\n".join(reversed(comments))


@cache
def source_docs(cls: type) -> SourceDocs:
    filename = inspect.getsourcefile(cls)
    if filename is None:
        return SourceDocs("", {})
    source = Path(filename).read_text(encoding="utf-8")
    tree = ast.parse(source)
    lines = source.splitlines()
    comments = {
        token.start[0]: token.string.lstrip("#").removeprefix(":").strip()
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
    }
    node = next(
        (
            item
            for item in ast.walk(tree)
            if isinstance(item, ast.ClassDef) and item.name == cls.__name__
        ),
        None,
    )
    if node is None:
        return SourceDocs("", {})
    first_line = min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)])
    description = ast.get_docstring(node) or preceding_comments(lines, first_line)
    parameters = {}
    for item in node.body:
        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            pieces = [preceding_comments(lines, item.lineno), comments.get(item.end_lineno, "")]
            text = "\n".join(piece for piece in pieces if piece)
            if text:
                parameters[item.target.id] = text
    return SourceDocs(description, parameters)


def parameter_docs(cls: type) -> dict[str, str]:
    result = {}
    for base in reversed(cls.__mro__[:-1]):
        result.update(source_docs(base).parameters)
    return result
