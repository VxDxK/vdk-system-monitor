"""Deterministic plain-text documentation, without target access."""

from __future__ import annotations

import inspect
from annotationlib import Format
from dataclasses import MISSING, fields, is_dataclass

from vsm.cli._catalogue import Entry
from vsm.cli._source import parameter_docs, source_docs


def signature(value: object) -> str:
    return str(inspect.signature(value, annotation_format=Format.STRING))


def parameters(cls: type) -> list[str]:
    result = []
    docs = parameter_docs(cls)
    for name, parameter in inspect.signature(cls).parameters.items():
        if name.startswith("_"):
            continue
        annotation = parameter.annotation
        kind = annotation if isinstance(annotation, str) else inspect.formatannotation(annotation)
        default = (
            "required"
            if parameter.default is inspect.Parameter.empty
            else f"default: {parameter.default!r}"
        )
        if is_dataclass(cls):
            field = next((field for field in fields(cls) if field.name == name), None)
            if field is not None and field.default_factory is not MISSING:
                default = "default factory (not evaluated)"
        result.append(f"  {name}: {kind} ({default})")
        if name in docs:
            result.append("    " + docs[name].replace("\n", "\n    "))
    return result


def detail(entry: Entry) -> str:
    lines = [f"{entry.category}: {entry.name}", entry.documentation]
    cls = entry.implementation
    if cls is None:
        return "\n\n".join(lines)
    lines += [
        f"Import: from {cls.__module__} import {cls.__name__}",
        f"Constructor: {cls.__name__}{signature(cls)}",
    ]
    params = parameters(cls)
    if params:
        lines.append("Parameters:\n" + "\n".join(params))
    # Include shared semantic docs, but not dataclasses' generated signature docstrings.
    for base in cls.__mro__[1:-1]:
        doc = source_docs(base).description
        if doc:
            lines.append(f"Shared behavior ({base.__name__}):\n{inspect.cleandoc(doc)}")
    required = ", ".join(
        f"{name}={name}"
        for name, p in inspect.signature(cls).parameters.items()
        if p.default is inspect.Parameter.empty
        and p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    )
    constructor = f"{cls.__name__}({required})"
    prefix = f"from {cls.__module__} import {cls.__name__}"
    if entry.category == "actions":
        method = "open" if hasattr(cls, "open") else "run"
        lines.append(f"Lifecycle: {method}{signature(getattr(cls, method))}")
        if method == "open":
            example = f"async with device.use({constructor}) as resource:\n    ...  # use the resource inside this scope"
        else:
            example = f"result = await device.do({constructor})"
        lines.append(
            "Example (inside a Run; device and required argument variables already bound):\n"
            + prefix
            + "\n"
            + example
        )
    elif entry.category == "processors":
        from vsm.processing import Params

        lines.append(f"Processing: process{signature(cls.process)}")
        lines.append("Processing options:\n" + "\n".join(parameters(Params)))
        lines.append(source_docs(Params).description)
        lines.append(
            "Example (source is a finalized LogRef with the expected schema):\n"
            + prefix
            + "\nfrom vsm.processing import Params\n"
            + f"datasets = {constructor}.process(source, params=Params())"
        )
    elif entry.category == "connections":
        lines.append(
            "Example (inside a Run; required argument variables already bound):\n"
            + prefix
            + f"\ndevice = await run.connect({constructor})"
        )
    return "\n\n".join(lines)


def listing(category: str, items: list[Entry], *, brief: bool) -> str:
    lines = [category + ":"]
    lines.extend(item.name if brief else f"  {item.name}: {item.summary}" for item in items)
    if not items:
        lines.append("  No matching capabilities.")
    return "\n".join(lines) + "\n"
