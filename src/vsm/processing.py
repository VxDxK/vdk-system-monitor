"""Datasets, provenance, units, chart descriptions, and asynchronous processing."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

import pandas as pd

from vsm.chart.chart import Chart
from vsm.models import ActionId, DeviceId, LogMetadata, LogRef, Schema
from vsm.processor.processor import Processor


@dataclass(frozen=True, kw_only=True)
class Column:
    name: str
    unit: str | None = None


@dataclass(frozen=True, kw_only=True)
class Provenance:
    path: Path
    schema: str
    device_id: DeviceId
    action_id: ActionId
    complete: bool
    metadata: LogMetadata
    numerical_policy: str = "corrected"


@dataclass(frozen=True, kw_only=True)
class Dataset:
    frame: pd.DataFrame
    group_label: str
    data_label: str
    columns: tuple[Column, ...] = ()
    charts: tuple[Chart, ...] = ()
    warnings: tuple[str, ...] = ()
    provenance: Provenance | None = None

    def with_source[Record](self, source: LogRef[Record]) -> Dataset:
        from vsm._timestamps import timestamp_dataset

        result = replace(
            self,
            provenance=Provenance(
                path=source.path,
                schema=source.schema.name,
                device_id=source.device_id,
                action_id=source.action_id,
                complete=source.complete,
                metadata=source.metadata,
            ),
        )
        return timestamp_dataset(result, source.metadata)

    def __post_init__(self) -> None:
        missing = {c.name for c in self.columns} - set(self.frame.columns)
        if missing:
            raise ValueError(f"dataset missing columns: {missing}")

    def to_csv(self, path: Path) -> None:
        """Write CSV and a ``<filename>.json`` sidecar for independent rendering.

        Keep both files together. The dataframe index is intentionally not saved.
        """
        from ._dataset_csv import save_dataset

        save_dataset(self, path)

    @classmethod
    def from_csv(cls, path: Path) -> Dataset:
        """Restore a dataset exported by vsm, including its chart descriptions."""
        from ._dataset_csv import load_dataset

        return load_dataset(path)


@dataclass(frozen=True, kw_only=True)
class Params:
    """Explicit processor validation and selection policies.

    Not all processors support every option. Defaults retain strict validation;
    opt-ins do not repair malformed records or incompatible schemas.
    """

    #: Permit supported incomplete logs; malformed records still fail.
    allow_partial: bool = False
    #: Permit supported recorded read errors while preserving warnings.
    allow_read_errors: bool = False
    #: CPU/interface/disk names, SNMP protocol/counter names, or source paths; empty selects all.
    selected: tuple[str, ...] = ()


async def process[Record, P, Result](
    processor: Processor[Record, P, Result], source: LogRef[Record], *, params: P
) -> Result:
    from ._cleanup import cleanup

    worker = asyncio.create_task(asyncio.to_thread(processor.process, source, params=params))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        # Python threads cannot be killed. Join before input owners can release files.
        try:
            await cleanup(worker)
        except Exception:  # noqa: BLE001, S110 -- Preserve the original failure while joining cleanup.
            pass
        raise


def dataframe(rows: list[dict[str, object]]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def dataset(
    name: str,
    rows: list[dict[str, object]],
    *,
    units: dict[str, str] | None = None,
    warnings: tuple[str, ...] = (),
    source: LogRef[object] | None = None,
    charts: tuple[Chart, ...] = (),
) -> Dataset:
    from vsm.util.labels import label_frame

    frame = label_frame(dataframe(rows))
    cols = tuple(Column(name=str(c), unit=(units or {}).get(str(c))) for c in frame.columns)
    result = Dataset(
        frame=frame,
        group_label=name,
        data_label=name,
        columns=cols,
        charts=charts,
        warnings=warnings,
    )

    return result.with_source(source) if source else result


def load_native[Record](
    path: Path,
    *,
    schema: Schema[Record],
    device_id: DeviceId | None = None,
    action_id: ActionId | None = None,
) -> LogRef[Record]:
    """Import a standalone binary-utils log with an explicit expected measurement schema."""
    from .models import ActionId, DeviceId, LogMetadata, TargetInfo

    first: dict[str, object]
    with path.open("rb") as stream:
        value: object = json.loads(stream.readline())
        if not isinstance(value, dict):
            raise ValueError("native metadata required")  # noqa: TRY004 -- Validation uses ValueError consistently.
        first = cast(dict[str, object], value)
        upstream_rup = (
            first.get("record") == "metadata"
            and first.get("schema") == "rup.ping"
            and first.get("version") == 3
        )
        if upstream_rup and schema.name != "rup":
            raise ValueError('upstream rup requires Schema(name="rup")')
        if not upstream_rup:
            from vsm.processor._parsing import native_schema

            if first.get("schema") != native_schema(schema.name):
                raise ValueError("native/framework schema mismatch")
        if first.get("type") != "metadata" and not upstream_rup:
            raise ValueError("native metadata required")
    target = TargetInfo(
        os=str(first.get("os", "unknown" if upstream_rup else "linux")),
        architecture=str(first.get("architecture", "unknown")),
        kernel=str(first.get("kernel", "unknown")),
        boot_id=str(first["boot_id"]) if first.get("boot_id") else None,
        clock_ticks_per_second=int(str(first["clock_ticks_per_second"]))
        if first.get("clock_ticks_per_second")
        else None,
        page_size_bytes=int(str(first["page_size_bytes"]))
        if first.get("page_size_bytes")
        else None,
    )
    with path.open("rb") as stream:
        last = b""
        for line in stream:
            if line.strip():
                last = line
    try:
        last_record = json.loads(last)
        complete = isinstance(last_record, dict) and (
            last_record.get("record") == "summary"
            if upstream_rup
            else last_record.get("type") == "end"
        )
    except ValueError, UnicodeDecodeError:
        complete = False
    metadata = LogMetadata(
        target=target, provider="rup.ping/3" if upstream_rup else str(first.get("schema"))
    )
    companion = path.with_name(path.name + ".metadata.json")
    if companion.exists():
        from vsm._dataset_metadata import decode

        saved = decode(json.loads(companion.read_text()))
        if not isinstance(saved, LogMetadata):
            raise ValueError("invalid framework log metadata")
        if not upstream_rup and saved.target.boot_id != target.boot_id:
            raise ValueError("framework/native log boot mismatch")
        metadata = saved
    return LogRef(
        path=path,
        schema=schema,
        device_id=device_id or DeviceId("unknown"),
        action_id=action_id or ActionId("imported"),
        complete=complete,
        metadata=metadata,
    )
