"""Bounded targetctl wire framing and Python result/error conversion."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from vsm.models import (
    CapabilityUnavailable,
    OwnershipError,
    ProcessIdentity,
    TargetctlProtocolError,
)

MAX_LINE = 1024 * 1024
MAX_ITEMS = 65_536


def _encode(request: dict) -> bytes:
    data = json.dumps(request, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
    if len(data) > MAX_LINE:
        raise ValueError("targetctl request exceeds 1 MiB")
    return data


def _decode(line: bytes) -> dict:
    try:
        event = json.loads(line)
    except (ValueError, UnicodeError) as error:
        raise TargetctlProtocolError("invalid targetctl JSON") from error
    if (
        not isinstance(event, dict)
        or "version" in event
        or type(event.get("id")) is not int
        or event["id"] < 0
    ):
        raise TargetctlProtocolError("invalid targetctl response envelope")
    if event.get("event") not in {
        "item",
        "result",
        "error",
        "launched",
        "output",
        "completed",
        "cleanup",
    }:
        raise TargetctlProtocolError("unknown targetctl event")
    if event["event"] != "error" and (
        not isinstance(event.get("result"), dict)
        or type(event.get("started_ns")) is not int
        or type(event.get("finished_ns")) is not int
        or not 0 <= event["started_ns"] <= event["finished_ns"]
    ):
        raise TargetctlProtocolError("invalid targetctl result or timing")
    return event


def _error(event: dict) -> Exception:
    error = event.get("error")
    if not isinstance(error, dict) or not isinstance(error.get("message"), str):
        return TargetctlProtocolError("malformed targetctl error")
    category, message, number = error.get("category"), error["message"], error.get("errno")
    if category == "ownership":
        return OwnershipError(message)
    if category == "unsupported":
        return CapabilityUnavailable(message)
    if category == "protocol":
        return TargetctlProtocolError(message)
    if category == "malformed":
        return ValueError(message)
    if type(number) is int:
        return OSError(number, message)
    return OSError(message)


def _identity(connection, value: dict) -> ProcessIdentity:
    if (
        not isinstance(value, dict)
        or type(value.get("pid")) is not int
        or value["pid"] <= 0
        or type(value.get("start_ticks")) is not int
        or value["start_ticks"] < 0
        or "boot_id" not in value
        or not isinstance(value.get("name"), str)
        or not (value.get("boot_id") is None or isinstance(value["boot_id"], str))
    ):
        raise TargetctlProtocolError("invalid workload identity")
    if (
        connection.target.boot_id
        and value.get("boot_id")
        and connection.target.boot_id != value["boot_id"]
    ):
        raise OwnershipError("target boot changed")
    return ProcessIdentity(
        device_id=connection.device_id,
        **{k: value[k] for k in ("pid", "start_ticks", "boot_id", "name")},
    )


async def _events(channel) -> AsyncIterator[dict]:
    buffer = bytearray()
    while data := await channel.stdout.read(65536):
        buffer.extend(data)
        while (end := buffer.find(b"\n")) >= 0:
            if end + 1 > MAX_LINE:
                raise TargetctlProtocolError("targetctl response exceeds 1 MiB")
            line = bytes(buffer[: end + 1])
            del buffer[: end + 1]
            yield _decode(line)
        if len(buffer) > MAX_LINE:
            raise TargetctlProtocolError("targetctl response exceeds 1 MiB")
    if buffer:
        raise TargetctlProtocolError("truncated targetctl response")


async def _diagnostics(channel) -> bytes:
    result = bytearray()
    while data := await channel.stderr.read(65536):
        result.extend(data[: max(0, 65536 - len(result))])
    return bytes(result)
