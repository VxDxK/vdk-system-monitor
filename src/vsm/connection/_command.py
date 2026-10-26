from __future__ import annotations

import shlex

from vsm.models import Command, Exec


def shell_command(command: Command) -> str:
    """POSIX encoding preserves empty/quoted arguments; SSH exec sends command text."""
    body = shlex.join(command.argv) if isinstance(command, Exec) else command.script
    prefix = " ".join(f"{item.name}={shlex.quote(item.value)}" for item in command.env)
    # Environment names are validated further here to prevent shell syntax injection.
    if any(
        not item.name.replace("_", "a").isalnum() or item.name[0].isdigit() for item in command.env
    ):
        raise ValueError("POSIX environment names must be identifiers")
    if prefix:
        body = f"env {prefix} " + (
            body if isinstance(command, Exec) else f"/bin/sh -c {shlex.quote(body)}"
        )
    if isinstance(command, Exec):
        # Keep the owned PID and exit status attached to the executable, not a
        # shell waiting for it when the entire process group receives a signal.
        body = "exec " + body
    if command.cwd:
        body = f"cd {shlex.quote(str(command.cwd))} && {body}"
    return body
