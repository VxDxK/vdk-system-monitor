"""Allowlisted, typed descriptions for dataset CSV sidecars.

No import paths from disk are evaluated. Custom Chart implementations remain
usable in memory but need an explicitly supported codec before they can be saved.
"""

import types
from dataclasses import fields
from pathlib import Path
from typing import get_args, get_origin, get_type_hints

from vsm.chart.bar import Bar
from vsm.chart.box import Box
from vsm.chart.line import Line
from vsm.chart.table import Table
from vsm.models import ActionId, DeviceId, LogMetadata, TargetInfo
from vsm.processing import Column, Provenance

_CLASSES = {
    cls.__name__: cls
    for cls in (
        Column,
        Provenance,
        ActionId,
        DeviceId,
        LogMetadata,
        TargetInfo,
        Line,
        Bar,
        Box,
        Table,
    )
}


def encode(value: object) -> object:
    if value is None or type(value) in (str, int, float, bool):
        return value
    if isinstance(value, Path):
        return {"kind": "path", "value": str(value)}
    if isinstance(value, tuple):
        return {"kind": "tuple", "items": [encode(item) for item in value]}
    if type(value) in _CLASSES.values():
        return {
            "kind": type(value).__name__,
            "fields": {field.name: encode(getattr(value, field.name)) for field in fields(value)},
        }
    raise ValueError(f"unsupported dataset metadata or custom chart: {type(value).__name__}")


def matches(value: object, annotation: object) -> bool:
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is types.UnionType:
        return any(matches(value, arg) for arg in args)
    if origin is tuple:
        if not isinstance(value, tuple):
            return False
        if len(args) == 2 and args[1] is Ellipsis:
            return all(matches(item, args[0]) for item in value)
        return len(value) == len(args) and all(
            matches(item, arg) for item, arg in zip(value, args, strict=True)
        )
    if annotation is float:
        return type(value) in (int, float)
    if annotation in (str, int, bool, type(None)):
        return type(value) is annotation
    return isinstance(value, annotation)


def decode(value: object) -> object:
    if value is None or type(value) in (str, int, float, bool):
        return value
    if not isinstance(value, dict):
        raise ValueError("expected a metadata value")  # noqa: TRY004 -- Malformed disk data.
    kind = value.get("kind")
    if kind == "path" and set(value) == {"kind", "value"} and isinstance(value["value"], str):
        return Path(value["value"])
    if kind == "tuple" and set(value) == {"kind", "items"} and isinstance(value["items"], list):
        return tuple(decode(item) for item in value["items"])
    if not isinstance(kind, str) or kind not in _CLASSES:
        raise ValueError(f"unsupported metadata/chart kind: {kind!r}")
    cls = _CLASSES[kind]
    payload = value.get("fields")
    if set(value) != {"kind", "fields"} or not isinstance(payload, dict):
        raise ValueError(f"invalid {kind} fields")
    if set(payload) != {field.name for field in fields(cls)}:
        raise ValueError(f"unexpected or missing {kind} fields")
    decoded = {name: decode(item) for name, item in payload.items()}
    annotations = get_type_hints(cls)
    for name, item in decoded.items():
        if not matches(item, annotations[name]):
            raise ValueError(f"invalid {kind}.{name}")
    return cls(**decoded)
