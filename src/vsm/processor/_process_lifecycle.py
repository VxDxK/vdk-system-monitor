"""Validate structured process identities; lifecycle events never create samples."""

from vsm.processor._parsing import Event, proc_stat_line, text


def validate_lifecycle(events: list[Event]) -> None:
    ended = set()
    ended_processes = set()
    for event in events:
        kind = event["type"]
        if kind not in ("file", "read_error", "entity_end"):
            if kind not in ("metadata", "sample", "ready", "overrun", "end"):
                raise ValueError("unknown process record type")
            continue
        tags = event.get("tags")
        if not isinstance(tags, dict):
            raise ValueError("process identity tags required")  # noqa: TRY004 -- Malformed log records use ValueError.
        for key in ("pid", "tid", "start_ticks", "process_start_ticks"):
            value = tags.get(key)
            if type(value) is not int or value < (1 if key in ("pid", "tid") else 0):
                raise ValueError("invalid process identity")
        scope = tags.get("scope")
        if scope not in ("process", "thread"):
            raise ValueError("invalid process scope")
        if scope == "process" and (
            tags["pid"] != tags["tid"] or tags["start_ticks"] != tags["process_start_ticks"]
        ):
            raise ValueError("inconsistent process identity")
        identity = (
            tags["pid"],
            tags["process_start_ticks"],
            scope,
            tags["tid"],
            tags["start_ticks"],
        )
        if identity in ended:
            raise ValueError("records after entity termination")
        if kind == "entity_end":
            if event.get("reason") not in ("exited", "disappeared", "identity_changed"):
                raise ValueError("invalid entity termination reason")
            ended.add(identity)
            if scope == "process":
                ended_processes.add((tags["pid"], tags["process_start_ticks"]))
            continue
        if (tags["pid"], tags["process_start_ticks"]) in ended_processes:
            raise ValueError("samples after process termination")
        prefix = f"/proc/{tags['pid']}/"
        if scope == "thread":
            prefix += f"task/{tags['tid']}/"
        path = event.get("path")
        if (
            not isinstance(path, str)
            or not path.startswith(prefix)
            or not path[len(prefix) :]
            or "/" in path[len(prefix) :]
            or path[len(prefix) :] in (".", "..")
        ):
            raise ValueError("process path/identity mismatch")
        if kind == "read_error":
            errno = event.get("errno")
            if (
                "errno" not in event
                or (errno is not None and type(errno) is not int)
                or event.get("operation") not in ("read", "identity", "enumerate")
                or not isinstance(event.get("error"), str)
            ):
                raise ValueError("invalid structured read error")
        elif path.endswith("/stat"):
            tid, _, fields = proc_stat_line(text(event))
            if tid != tags["tid"] or int(fields[19]) != tags["start_ticks"]:
                raise ValueError("stat/identity mismatch")
