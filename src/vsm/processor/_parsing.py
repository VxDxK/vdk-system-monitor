from __future__ import annotations

import ipaddress
import json
import re
from typing import cast

from vsm.models import LogRef
from vsm.processing import Params

type Event = dict[str, object]


def native_schema(measurement: str) -> str:
    """Expected native producer for a framework measurement."""
    if measurement in ("pid_stat", "process_io"):
        return "pspider"
    if measurement == "socket_stat":
        return "socket-stat"
    if measurement == "traffic_generator":
        return "rgen"
    return "rfpoll"


def events(source: LogRef[object], params: Params) -> list[Event]:
    if not source.complete and (not params.allow_partial):
        raise ValueError(f"log is incomplete: {source.path}")
    process_log = source.schema.name in ("pid_stat", "process_io", "smaps")
    result: list[Event] = []
    lines = source.path.read_bytes().splitlines()
    for number, line in enumerate(lines, 1):
        try:
            event = json.loads(line)
        except ValueError, UnicodeDecodeError:
            if params.allow_partial and number == len(lines):
                break
            raise ValueError(f"invalid JSONL record {number}") from None
        if not isinstance(event, dict) or not isinstance(cast(Event, event).get("type"), str):
            raise ValueError(f"invalid event {number}")  # noqa: TRY004 -- Validation uses ValueError consistently.
        result.append(cast(Event, event))
    if not result or result[0].get("type") != "metadata":
        raise ValueError("missing native metadata")
    if result[0].get("schema") != native_schema(source.schema.name):
        raise ValueError("native/framework schema mismatch")
    if any("sequence" in e for e in result) and any(
        (type(e.get("sequence")) is not int or e["sequence"] != i for i, e in enumerate(result))
    ):
        raise ValueError("native record sequence is incomplete")
    if result[-1].get("type") != "end" and (not params.allow_partial):
        raise ValueError("missing native end record")
    if process_log:
        from vsm.processor._process_lifecycle import validate_lifecycle

        validate_lifecycle(result)
    elif any(e["type"] == "entity_end" for e in result):
        raise ValueError("lifecycle records require a process measurement")
    if any(e.get("type") == "read_error" for e in result) and (not params.allow_read_errors):
        raise ValueError("source read errors present; explicitly allow unavailable data")
    return result


def text(event: Event) -> str:
    if event.get("encoding") != "hex":
        raise ValueError("unknown file encoding")
    return bytes.fromhex(str(event["data"])).decode("utf-8")


def time_of(event: Event) -> float:
    return float(cast(float, event["time_ns"])) / 1000000000.0


def rate_rows(
    rows: list[dict[str, object]], keys: tuple[str, ...], counters: tuple[str, ...]
) -> list[dict[str, object]]:
    previous: dict[tuple[object, ...], dict[str, object]] = {}
    out: list[dict[str, object]] = []
    for row in rows:
        key = tuple(row[k] for k in keys)
        old = previous.get(key)
        elapsed = float(cast(float, row["time"])) - float(cast(float, old["time"])) if old else 0
        item = row.copy()
        reset = old is not None and any(
            float(cast(float, row[c])) < float(cast(float, old[c])) for c in counters
        )
        item["counter_reset"] = reset
        for c in counters:
            item[c + "_per_second"] = (
                (float(cast(float, row[c])) - float(cast(float, old[c]))) / elapsed
                if old and elapsed > 0 and (not reset)
                else None
            )
        previous[key] = row
        out.append(item)
    return out


def memory_fields(value: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for line in value.splitlines():
        m = re.fullmatch("([A-Za-z_()0-9]+):\\s+(\\d+)(?:\\s+(kB))?", line.strip())
        if m:
            result[m[1]] = int(m[2]) * (1024 if m[3] else 1)
    return result


def proc_stat_line(line: str) -> tuple[int, str, list[str]]:
    begin, end = (line.find("("), line.rfind(")"))
    if begin < 0 or end < begin:
        raise ValueError("invalid proc stat comm field")
    fields = line[end + 2 :].split()
    if len(fields) < 22:
        raise ValueError("truncated proc stat")
    return (int(line[:begin].strip()), line[begin + 1 : end], fields)


def endpoint(hex_address: str) -> str:
    address, port = hex_address.split(":")
    raw = bytes.fromhex(address)
    raw = b"".join(raw[i : i + 4][::-1] for i in range(0, len(raw), 4))
    address = ipaddress.ip_address(raw)
    host = f"[{address}]" if isinstance(address, ipaddress.IPv6Address) else str(address)
    return f"{host}:{int(port, 16)}"


def memory_units(value: str) -> dict[str, str]:
    """Retain the distinction between kB fields and unitless page/count fields."""
    result: dict[str, str] = {}
    for line in value.splitlines():
        match = re.fullmatch(r"([A-Za-z_()0-9]+):\s+(\d+)(?:\s+(kB))?", line.strip())
        if match:
            result[match[1]] = "B" if match[3] else "count"
    return result
