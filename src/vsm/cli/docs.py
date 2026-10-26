"""Capability documentation command; discovery is deferred until dispatch."""

from __future__ import annotations

import argparse

CATEGORIES = ("actions", "processors", "connections", "binaries", "all")


def register(commands: argparse._SubParsersAction) -> None:
    parser = commands.add_parser("docs", help="Browse built-in capability documentation.")
    parser.add_argument("category", nargs="?", choices=CATEGORIES, default="all")
    parser.add_argument("name", nargs="?", help="Registered name for full documentation.")
    parser.add_argument("--brief", action="store_true", help="Print registered names only.")
    parser.add_argument(
        "-s",
        "--search",
        action="append",
        default=[],
        metavar="TEXT",
        help="Case-insensitive literal search; repeat to match any term.",
    )
    parser.set_defaults(handler=run, docs_parser=parser)


def run(options: argparse.Namespace) -> int:
    if options.name and (options.category == "all" or options.brief or options.search):
        options.docs_parser.error(
            "NAME requires one category and cannot be combined with --brief or --search"
        )
    from vsm.cli._catalogue import entries
    from vsm.cli._render import detail, listing

    categories = CATEGORIES[:-1] if options.category == "all" else (options.category,)
    for category in categories:
        items = sorted(entries(category), key=lambda entry: entry.name)
        if options.name:
            item = next((item for item in items if item.name == options.name), None)
            if item is None:
                options.docs_parser.error(
                    f"unknown {category} name: {options.name}; use 'vsm docs {category}' to list names"
                )
            print(detail(item))
        else:
            terms = [term.casefold() for term in options.search]
            selected = [
                item
                for item in items
                if not terms or any(term in detail(item).casefold() for term in terms)
            ]
            print(listing(category, selected, brief=options.brief))
    return 0
