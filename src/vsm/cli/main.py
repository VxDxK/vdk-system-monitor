"""Small, explicit dispatcher for independently implemented commands."""

from __future__ import annotations

import argparse
import sys
from importlib.metadata import version

from vsm.cli import docs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vsm", description="Multidevice experiments and diagnostics."
    )
    parser.add_argument("--version", action="version", version=f"vsm {version('vsm')}")
    commands = parser.add_subparsers(title="commands", dest="command")
    docs.register(commands)
    options = parser.parse_args(argv)
    if options.command is None:
        parser.print_help()
        return 0
    try:
        return options.handler(options)
    except KeyboardInterrupt:
        print("vsm: interrupted", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError) as error:
        print(f"vsm: {error}", file=sys.stderr)
        return 1
