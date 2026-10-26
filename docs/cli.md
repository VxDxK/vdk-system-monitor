# Command-line reference

Install the package to obtain `vsm`, or use `python -m vsm` in the installed
environment. The CLI provides built-in capability documentation.

```text
vsm [--help] [--version]
vsm docs [CATEGORY] [NAME] [--brief] [-s TEXT ...]
```

Categories are `actions`, `processors`, `connections`, `binaries`, and `all`.
Omitting the category selects `all`. Listings show names and one-line descriptions,
sorted alphabetically within each category. `--brief` shows names only, retaining
category headings. Named detail requires one category and cannot be combined with
`all`, `--brief`, or search.

```bash
vsm docs actions cpu_freq
vsm docs actions --brief
vsm docs processors -s latency -s throughput
vsm docs connections ssh
vsm docs binaries rfpoll
```

`-s`/`--search` is a case-insensitive literal substring search over names,
qualified Python names, signatures, and documentation. Repeating the flag matches
any term. No matches is a successful empty listing with an explanatory message.
There is no regex interpretation, paging, color, or terminal-specific output.

Documentation describes implementations, not detected target availability or
proof of real-hardware verification. No connections, collection, deployment,
native execution, or artifacts manifest are required. Binary documentation is an
integration overview; each native executable's `--help` supplies its full CLI.
See [capabilities](capabilities.md) and [native contracts](native-contracts.md)
for measurement and deployment contracts.

## Source of documentation

The action and processor registries supply their inventories. The connection
registry lists Host and SSH. Python class docstrings supply descriptions and
limitations; an adjacent block of `#` or `#:` comments immediately before a class
(or its decorators) is also accepted. Dataclasses' automatic signature docstrings
are not treated as authored descriptions.

Parameter types, required status, and defaults come from constructor signatures.
Comments immediately before annotated class fields and trailing field comments
supply parameter explanations, including those inherited from base classes.
Default factories are described without being called. Private parameters and
ClassVar fields are excluded. For example, documentation lives with its code:

```python
@dataclass(frozen=True, kw_only=True)
class Example:
    """Describe the capability, required permissions, and limitations here."""

    #: Sampling interval in milliseconds.
    interval: int = 250
```

Internal comments inside method bodies are not copied into documentation.
Details show shared base-class documentation and derive invocation patterns
from action methods or processor/connection interfaces. Variables in these
patterns must already be bound in the surrounding Python experiment.

Native names and descriptions come from `binary-utils/src/bin/*.rs`: module-level
`//!` comments and the clap `about` declaration. The wheel packages the same Rust
sources as resources so this works outside the checkout with no executable
artifacts. Keep usage examples and integration notes beside the Rust code.
There is no separately authored CLI capability catalogue.

## Adding commands

The console entry point is `vsm.cli.main:main`; `vsm.__main__` is a thin wrapper.
Each command module defines `register(subparsers)`, creates its parser, and sets
its handler with `set_defaults(handler=...)`. The handler accepts the parsed
Namespace and returns an integer exit status. Register new command modules
explicitly in the dispatcher. Keep command-specific imports deferred until the
handler runs, so root help and version stay lightweight.

Experiment execution and processing use the Python API. The CLI does not expose
`run`, `process`, conversion commands, or external plugin discovery.

## Exit status and streams

| Code | Meaning |
|---|---|
| 0 | Successful docs, help, version, or bare `vsm` showing help |
| 1 | Operational failure, such as unreadable documentation sources |
| 2 | Invalid command, category, name, option, or option combination |
| 130 | Interrupted command |

Normal output goes to stdout; usage errors and operational errors go to stderr.
Expected operational errors have concise messages without a traceback.
