"""CSV data plus portable descriptions for independent dataset rendering."""

import csv
import json
import math
import re
import tempfile
from collections import Counter
from numbers import Integral, Real
from pathlib import Path

import pandas as pd

from vsm._dataset_metadata import decode, encode, matches
from vsm.chart.bar import Bar
from vsm.chart.box import Box
from vsm.chart.line import Line
from vsm.chart.table import Table
from vsm.processing import Column, Dataset, Provenance

_DTYPES = re.compile(
    r"(?:object|bool|boolean|u?int(?:8|16|32|64)|U?Int(?:8|16|32|64)|float(?:16|32|64)|Float(?:32|64))"
)
_KINDS = {"str", "int", "float", "bool", "json", "none", "na", "nan"}


def _json_cell(value: object) -> None:
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if isinstance(value, list):
        for item in value:
            _json_cell(item)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _json_cell(item)
        return
    raise ValueError(f"unsupported structured CSV value: {type(value).__name__}")


def _cell(value: object) -> tuple[str, str]:
    if value is None:
        return "none", ""
    if value is pd.NA:
        return "na", ""
    if isinstance(value, str):
        return "str", value
    if isinstance(value, bool):
        return "bool", str(value)
    if isinstance(value, Integral):
        return "int", str(int(value))
    if isinstance(value, Real):
        number = float(value)
        return ("nan", "") if math.isnan(number) else ("float", repr(number))
    if isinstance(value, list | dict):
        _json_cell(value)
        return "json", json.dumps(value, ensure_ascii=False, allow_nan=False)
    raise ValueError(f"unsupported CSV value: {type(value).__name__}")


def _uncell(kind: str, value: str) -> object:
    if kind in ("none", "na", "nan"):
        if value:
            raise ValueError("nonempty CSV cell marked as missing")
        return {"none": None, "na": pd.NA, "nan": float("nan")}[kind]
    if kind == "str":
        return value
    if kind == "int":
        return int(value)
    if kind == "float":
        return float(value)
    if kind == "bool" and value in ("True", "False"):
        return value == "True"
    if kind == "json":
        result = json.loads(value)
        if not isinstance(result, list | dict):
            raise ValueError("expected structured CSV cell")
        _json_cell(result)
        return result
    raise ValueError(f"invalid CSV cell kind/value: {kind!r}")


def _dtype(series: pd.Series) -> dict[str, object]:
    dtype = series.dtype
    if isinstance(dtype, pd.StringDtype):
        return {
            "dtype": "string",
            "storage": dtype.storage,
            "na_value": "na" if dtype.na_value is pd.NA else "nan",
        }
    if not _DTYPES.fullmatch(str(dtype)):
        raise ValueError(f"unsupported CSV dtype: {dtype}")
    return {"dtype": str(dtype)}


def _restore_dtype(spec: dict[str, object]) -> object:
    dtype = spec["dtype"]
    if dtype == "string":
        if spec["storage"] not in ("python", "pyarrow") or spec["na_value"] not in ("na", "nan"):
            raise ValueError("invalid string dtype")
        return pd.StringDtype(
            storage=spec["storage"], na_value=pd.NA if spec["na_value"] == "na" else float("nan")
        )
    if not isinstance(dtype, str) or not _DTYPES.fullmatch(dtype):
        raise ValueError(f"unsupported CSV dtype: {dtype!r}")
    return dtype


def save_dataset(data: Dataset, path: Path) -> None:
    path = Path(path)
    names = list(data.frame.columns)
    if not all(isinstance(name, str) for name in names) or len(set(names)) != len(names):
        raise ValueError("CSV datasets require unique string column names")
    # Encode and validate everything before touching either destination.
    metadata = {
        "group_label": encode(data.group_label),
        "data_label": encode(data.data_label),
        "columns": encode(data.columns),
        "charts": encode(data.charts),
        "warnings": encode(data.warnings),
        "provenance": encode(data.provenance),
    }
    encoded_columns = []
    specs = []
    for name in names:
        series = data.frame[name]
        spec = {"name": name, **_dtype(series)}
        cells = [_cell(value) for value in series.tolist()]
        default = Counter(kind for kind, _ in cells).most_common(1)
        spec["kind"] = default[0][0] if default else "str"
        spec["overrides"] = {
            str(i): kind for i, (kind, _) in enumerate(cells) if kind != spec["kind"]
        }
        specs.append(spec)
        encoded_columns.append([value for _, value in cells])
    payload = (
        json.dumps(
            {"dataset": metadata, "frame": {"rows": len(data.frame), "columns": specs}},
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
        )
        + "\n"
    )
    # Stage both files first. Replacing two paths cannot be a single atomic operation.
    path.parent.mkdir(parents=True, exist_ok=True)
    staged: list[Path] = []
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=path.parent, delete=False
        ) as stream:
            staged.append(Path(stream.name))
            writer = csv.writer(stream)
            writer.writerow(names)
            if names:
                writer.writerows(zip(*encoded_columns, strict=True))
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            staged.append(Path(stream.name))
            stream.write(payload)
        staged[0].replace(path)
        staged[1].replace(path.with_name(path.name + ".json"))
    finally:
        for temporary in staged:
            temporary.unlink(missing_ok=True)


def load_dataset(path: Path) -> Dataset:
    path = Path(path)
    sidecar = path.with_name(path.name + ".json")
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise FileNotFoundError(
            f"dataset sidecar required: {sidecar}; keep it beside the CSV"
        ) from error
    except (ValueError, UnicodeError) as error:
        raise ValueError(f"invalid dataset sidecar {sidecar}: {error}") from error
    try:
        return _load(path, payload)
    except (KeyError, TypeError, ValueError, OverflowError, csv.Error) as error:
        raise ValueError(f"invalid CSV dataset {path}: {error}") from error


def _load(path: Path, payload: object) -> Dataset:
    if not isinstance(payload, dict) or set(payload) != {"dataset", "frame"}:
        raise ValueError("expected dataset and frame metadata")
    frame_spec = payload["frame"]
    rows, specs = frame_spec["rows"], frame_spec["columns"]
    if type(rows) is not int or rows < 0 or not isinstance(specs, list):
        raise ValueError("invalid frame dimensions")
    names = [spec["name"] for spec in specs]
    if not all(isinstance(name, str) for name in names) or len(set(names)) != len(names):
        raise ValueError("expected unique string columns")
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.reader(stream, strict=True)
        if next(reader, None) != names:
            raise ValueError("CSV headers differ from sidecar columns")
        records = list(reader)
    if names and len(records) != rows or not names and records:
        raise ValueError("CSV row count differs from sidecar")
    if any(len(record) != len(names) for record in records):
        raise ValueError("CSV row width differs from sidecar columns")
    columns = {}
    for index, spec in enumerate(specs):
        kind, overrides = spec["kind"], spec["overrides"]
        if kind not in _KINDS or not isinstance(overrides, dict):
            raise ValueError("invalid CSV cell descriptions")
        for row, override in overrides.items():
            if (
                not isinstance(row, str)
                or not row.isdecimal()
                or str(int(row)) != row
                or not 0 <= int(row) < rows
                or override not in _KINDS
            ):
                raise ValueError("invalid CSV cell override")
        values = [
            _uncell(overrides.get(str(i), kind), record[index]) for i, record in enumerate(records)
        ]
        columns[spec["name"]] = pd.Series(values, dtype=_restore_dtype(spec))
    frame = pd.DataFrame(columns, index=pd.RangeIndex(rows))
    metadata = payload["dataset"]
    if not isinstance(metadata, dict) or set(metadata) != {
        "group_label",
        "data_label",
        "columns",
        "charts",
        "warnings",
        "provenance",
    }:
        raise ValueError("unexpected or missing dataset metadata")
    decoded = {key: decode(value) for key, value in metadata.items()}
    expected = {
        "group_label": str,
        "data_label": str,
        "columns": tuple[Column, ...],
        "charts": tuple[Line | Bar | Box | Table, ...],
        "warnings": tuple[str, ...],
        "provenance": Provenance | None,
    }
    for key, annotation in expected.items():
        if not matches(decoded[key], annotation):
            raise ValueError(f"invalid dataset {key}")
    result = Dataset(frame=frame, **decoded)
    for chart in result.charts:
        if isinstance(chart, Table):
            chart.select(frame)
        else:
            # Validate axes, filters and grouping even for empty datasets.
            next(chart.series(frame), None)
    return result
