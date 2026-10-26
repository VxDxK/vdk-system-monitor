"""Readable entity identities without discarding the captured source columns."""

import re
from pathlib import PurePosixPath

import pandas as pd


def path_label(value: str) -> str:
    path = PurePosixPath(value)
    if match := re.search(r"/(cpu\d+|policy\d+)(?:/|$)", value):
        return match[1]
    if match := re.search(r"/(?:power_supply|thermal)/([^/]+)", value):
        return match[1]
    if match := re.search(r"/proc/(\d+)/", value):
        return f"PID {match[1]}"
    return path.parent.name if path.name in {"cur_freq", "temp", "scaling_cur_freq"} else path.name


def label_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Add a display column; raw identity columns still determine chart grouping."""
    if frame.empty or "entity_label" in frame:
        return frame
    identities = next(
        (
            keys
            for keys in (
                ("socket_id",),
                ("pid", "tid", "start_ticks", "name"),
                ("pid", "command"),
                ("pid", "start_ticks"),
                ("local", "remote", "inode"),
                ("local", "remote"),
                ("battery",),
                ("path",),
                ("cpu",),
                ("interface",),
                ("station",),
                ("socket",),
            )
            if all(key in frame for key in keys)
        ),
        (),
    )
    if not identities:
        return frame
    unique = frame[list(identities)].drop_duplicates()
    labels: dict[tuple[str, ...], str] = {}
    for values in unique.itertuples(index=False, name=None):
        key = tuple(str(value) for value in values)
        row = dict(zip(identities, values, strict=True))
        if "socket_id" in row and "source" in frame:
            sample = frame[frame.socket_id == row["socket_id"]].iloc[0]
            label = f"{sample.get('protocol', '')} [{sample.source}]:{sample.get('source_port', '?')} → [{sample.destination}]:{sample.get('destination_port', '?')}"
        elif "socket_id" in row:
            label = "Socket"
        elif "pid" in row:
            name = str(row.get("name", row.get("command", "Process")))
            ids = str(row["pid"]) + (
                f"/{row['tid']}" if "tid" in row and row["tid"] != row["pid"] else ""
            )
            label = f"{PurePosixPath(name).name} [{ids}]"
        elif "local" in row:
            label = f"{row['local']} → {row['remote']}"
        elif "path" in row:
            label = path_label(str(row["path"]))
        else:
            label = " / ".join(key)
        labels[key] = label
    # Collision suffixes are stable across input order and preserve distinct series.
    for label in set(labels.values()):
        keys = sorted(key for key, value in labels.items() if value == label)
        if len(keys) > 1:
            for index, key in enumerate(keys, 1):
                if identities == ("path",):
                    parts = PurePosixPath(key[0]).parts
                    suffix = key[0]
                    for length in range(1, len(parts) + 1):
                        candidate = "/".join(parts[-length:])
                        if (
                            sum(
                                "/".join(PurePosixPath(other[0]).parts[-length:]) == candidate
                                for other in keys
                            )
                            == 1
                        ):
                            suffix = candidate
                            break
                    labels[key] = suffix
                else:
                    labels[key] = f"{label} [{index}]"
    result = frame.copy()
    result["entity_label"] = [
        labels[tuple(str(value) for value in values)]
        for values in frame[list(identities)].itertuples(index=False, name=None)
    ]
    return result
