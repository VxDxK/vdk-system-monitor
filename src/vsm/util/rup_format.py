"""Validation of upstream rup.ping JSONL v3, independent of pandas and actions."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import cast

type RupRecord = dict[str, object]
RTT_FIELDS = ("rtt_min_ms", "rtt_mean_ms", "rtt_median_ms", "rtt_max_ms", "rtt_std_dev_ms")


def integer(value: object, name: str, maximum: int | None = None) -> int:
    if type(value) is not int or value < 0 or (maximum is not None and value > maximum):
        raise ValueError(f"invalid rup {name}")
    return value


def number(value: object, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError(f"invalid rup {name}")
    return float(value)


def validate_summary(record: RupRecord) -> None:
    if record.get("record") != "summary":
        raise ValueError("missing rup summary")
    integer(record.get("timestamp_ms"), "timestamp_ms")
    sent = integer(record.get("sent"), "sent")
    received = integer(record.get("received"), "received")
    loss = number(record.get("loss_percent"), "loss_percent")
    if received > sent or not math.isclose(
        loss, (sent - received) / sent * 100 if sent else 0, abs_tol=1e-7
    ):
        raise ValueError("inconsistent rup summary counts/loss")
    for key in RTT_FIELDS:
        if key not in record:
            raise ValueError("missing rup RTT summary field")
        value = record[key]
        if received:
            number(value, key)
        elif value is not None:
            raise ValueError("loss-only summary must have null RTT statistics")


def read_records(path: Path, *, complete: bool, allow_partial: bool) -> list[RupRecord]:
    if not complete and not allow_partial:
        raise ValueError("rup log is incomplete")
    lines = path.read_bytes().splitlines()
    records: list[RupRecord] = []
    for index, line in enumerate(lines):
        try:
            value: object = json.loads(line)
        except ValueError, UnicodeDecodeError:
            if allow_partial and index == len(lines) - 1 and records:
                break
            raise ValueError(
                f"invalid rup client JSONL at line {index + 1}; server logs are not measurements"
            ) from None
        if not isinstance(value, dict):
            raise ValueError("rup JSONL records must be objects")  # noqa: TRY004 -- Validation uses ValueError consistently.
        records.append(cast(RupRecord, value))
    if not records or records[0].get("record") != "metadata":
        raise ValueError("expected rup client metadata; server logs are not measurements")
    header = records[0]
    if header.get("schema") != "rup.ping" or header.get("version") != 3:
        raise ValueError("unsupported rup JSONL schema/version")
    start = integer(header.get("started_at_ms"), "started_at_ms")
    if header.get("protocol") not in ("tcp", "udp", "icmp"):
        raise ValueError("invalid rup metadata protocol")
    previous_time = start
    seen: set[int] = set()
    replies = 0
    summary: RupRecord | None = None
    for index, record in enumerate(records[1:], 1):
        timestamp = integer(record.get("timestamp_ms"), "timestamp_ms")
        if timestamp < previous_time:
            raise ValueError("rup timestamps must be nondecreasing")
        previous_time = timestamp
        kind = record.get("record")
        if kind == "summary":
            if index != len(records) - 1:
                raise ValueError("rup summary must be the final record")
            validate_summary(record)
            summary = record
            continue
        if kind not in ("reply", "terminal_reply", "timeout", "reorder_or_loss"):
            raise ValueError("unsupported rup event")
        seq = integer(record.get("seq"), "seq")
        if seq in seen:
            raise ValueError("duplicate rup sequence outcome")
        seen.add(seq)
        if kind in ("reply", "terminal_reply"):
            number(record.get("rtt_ms"), "rtt_ms")
            size = integer(record.get("size_bytes"), "size_bytes")
            ttl = record.get("ttl")
            if ttl is not None:
                integer(ttl, "ttl", 255)
            if (kind == "terminal_reply") != (size == 0):
                raise ValueError("rup terminal response must have zero payload size")
            replies += 1
    if summary is None:
        if not allow_partial:
            raise ValueError("missing rup summary")
    elif not allow_partial:
        sent = cast(int, summary["sent"])
        if cast(int, summary["received"]) != replies or any(seq >= sent for seq in seen):
            raise ValueError("rup events disagree with summary")
    return records
