# Development guide for vsm

## Working scope

Treat this directory as the complete development workspace. Continue from the
local implementation, tests, and documentation. Keep implementation, examples,
tests, and project documentation here. Do not
modify sibling projects as part of vsm development.

The distribution and Python import name are both `vsm`. Python orchestrates
experiments on a controller; small Rust executables collect data on targets.
Targets do not require Python. Current backends are Host and SSH, with Linux
x86-64 as the initial supported target. ADB, HDC, Windows, and ARM64 support
remain future work. No YAML frontend is provided. rup follows pinned upstream 0.15.1 CLI/wire/JSONL
behavior; the other native tools use vsm-specific contracts.

## Read first

- [README.md](README.md): setup and public Python usage.
- [docs/architecture.md](docs/architecture.md): protocols and ownership rules.
- [docs/capabilities.md](docs/capabilities.md): action inventory, parameters,
  processor associations, units, and limitations.
- [docs/native-contracts.md](docs/native-contracts.md): native CLI, JSONL, wire
  formats, and artifact compatibility.

These documents are the local reference. Inspect implementation and tests before
assuming a documented capability has been exercised against real hardware.
Keep observations, test-double results, and unverified behavior distinguishable.

## Development conventions

vsm uses a rolling release model. Only the current release's APIs, interfaces,
protocols, and data formats are supported. Do not preserve backward compatibility,
add compatibility shims or migration readers, or introduce format revision tags
or version negotiation. Update producers and consumers together. Package versions
remain provenance; platform ABI and runtime requirement checks remain mandatory.
The pinned upstream rup contract is an explicit external interoperability contract.

- Use Python 3.14+ with annotations and modern generics: `class Capture[R]`,
  `class LogRef[Record]`, and `def spawn[T](...)`. Avoid `TypeVar`/`Generic` syntax.
- Use real objects and typed results for devices, commands, paths, processes,
  actions, and logs. Device labels are descriptive; do not introduce string
  routing such as `on="phone"` or dictionaries as the public configuration API.
- Use native `async`/`await`, structured concurrency, and scoped resources.
  Support simultaneous actions on one connection and pipelines across devices.
- Do not use psutil, including for the Host implementation. Actions must work
  through the connection abstraction rather than controller-local probes.
- Pandas, AsyncSSH, Matplotlib, and Plotly are approved required runtime dependencies. Use ordinary
  imports; do not add `if TYPE_CHECKING` imports, authored third-party stubs,
  or typing test fixtures. Ask before introducing additional Python dependencies.
- Rust dependencies are allowed. Keep `binary-utils` minimal and compile its
  tools into deployable artifacts.
- Keep separate action, connection, and processor packages with focused modules.
  Avoid restoring giant implementation files or measurement-name dispatchers.
- Run runtime unit tests and collect Python coverage. There is no coverage
  threshold. Static typing tests and Rust coverage are not required.
- Keep `src/vsm/py.typed`: the empty marker advertises annotations to downstream
  IDEs/type checkers. It has no runtime behavior and requires no typing test suite.

## Source map and extension points

| Location | Responsibility |
|---|---|
| `src/vsm/__init__.py` | Docstring only; import from defining modules |
| `src/vsm/models.py` | Generic string identities, commands, paths, log metadata, errors |
| `src/vsm/runtime.py` | Run, Device, TaskScope/TaskHandle, Context, services, registries |
| `src/vsm/_cleanup.py` | Cancellation-resistant asynchronous cleanup, independent of transports |
| `src/vsm/action/action.py` | Action, ResourceAction, ActionContext, ClaimedResource protocols |
| `src/vsm/action/` | One concrete action per module; explicit collector lifecycles |
| `src/vsm/connection/connection.py` | Connection/ConnectionBackend protocols and shared base behavior |
| `src/vsm/connection/` | Host and SSH implementations, Process protocol, stream/command helpers |
| `src/vsm/processor/processor.py` | Generic Processor protocol |
| `src/vsm/processor/` | Concrete measurement processors, shared parsing/validation/rate helpers |
| `src/vsm/processing.py` | Dataset/Params, units/provenance/chart descriptions, CSV save/load API, async processing |
| `src/vsm/binary.py` | Manifest validation and generic executable deployment |
| `src/vsm/util/` | Measurement record markers, network configuration, Rup format |
| `src/vsm/chart/`, `src/vsm/output/` | Plotly/Matplotlib charts and output generators |
| `binary-utils/` | Rust crate with rfpoll, pspider, socket-stat, rgen, standalone rup, and targetctl binaries |
| `tests/` | Runtime, action/processor contracts, native fixtures, and SSH tests |
| `examples/` | Manual local rup pair and Host/SSH rgen pair, metrics, CSV and charts |

An `Action[T]` implements `async run(ctx) -> T` and is invoked through `await
device.do(action)`. A `ResourceAction[T]` implements `open(ctx)` as an async context
manager and is scoped with `async with device.use(action)`. Use connection methods for
target access and Context for output paths, binary deployment, services, and supervised
workers. Inheritance is optional: protocols allow ordinary user-defined classes. Add
action discovery entries to `action/registry.py`; import implementations directly from
their defining modules. Package initializers have no reexports.

A connection backend supplies `connect()` as an async context
manager. Its connection owns commands, launches, transfers, target paths, and
target metadata. Preserve ownership checks for device-bound paths and process
identities. Extend the protocol and backend tests together when adding behavior.

`ProcessSet` is an immutable sequence with private storage. Construct it from a
positional iterable; use `len(selection)`, `selection[index]`, iteration, and
slicing directly. Slices retain `ProcessSet`. There is no public `.processes`
field or `processes=` constructor keyword. `PidStat`, `ProcessIo`, and `Smaps`
accept `processes=selection` and share startup device/boot/start validation.
Their native collection pins identities
through procfs directory descriptors and emits entity_end for confirmed exit.
Series end without synthetic rows; unexpected read errors remain strict.

`DeviceId` and `ActionId` format directly as their stored values. `TargetPath`
supports string formatting, `.name`, `.parent`, and `/` with strings or
`PurePosixPath`; derived paths retain device identity. Joining another
`TargetPath` is rejected. Keep target I/O through connections; these paths do
not implement the controller-local `__fspath__` protocol.

A processor implements synchronous `process(LogRef[Record], params=...)`.
Add each measurement in a dedicated module and register it in
`processor/registry.py`. Share validation and numerical helpers. The processor
package initializer intentionally stays small: implementations import dataset
helpers from `processing.py`, so eager barrel imports can create a cycle.
There are no root-level convenience exports or eager measurement imports.
`FileProcessor(name)` and `ExternalProcessor(name)` are supported factories
selecting concrete classes. New direct Python usage should prefer those classes.

Currently there are 50 action registrations and 32 processor registrations;
recheck registries after changes. Settings/discovery may return typed values,
profilers may retain raw artifacts, and one processor may emit several datasets.
Do not impose a one-to-one action/processor relationship.

## Runtime and measurement invariants

- Run owns fresh output directories, connections, workers, and cleanup. Existing
  output directories are rejected. `run.spawn()` returns an awaitable handle;
  TaskGroup joins children on normal exit and cancels siblings on failure.
  Owned children may still start supervised descendants while normal exit drains.
- `connection.launch()` owns a target process; cancelling one `wait()` does not
  itself terminate it. Drain stdout and stderr concurrently with bounded capture
  or file sinks. Scope exit stops/reaps owned work and joins cleanup even through
  repeated cancellation. Do not silently detach workers or swallow failures.
- Native collectors, rgen and framework rup default to no duration limit; scope
  exit owns their lifetime. Explicit finite durations remain available.
- Collectors establish their documented readiness before resource entry returns.
  `capture.log` is available after finalization; `capture.wait()` can await finite
  tool completion. Preserve raw logs, explicit incomplete status, and read errors.
- Settings claim affected target paths and restore previous values, including
  after partial startup/cancellation. Claims cover a run/device, not arbitrary
  external writers, aliases, or independent runs.
- SSH retains host-key verification and safe argv encoding. PID/start identity
  checks precede process-group signals. Transport loss does not prove remote
  cleanup succeeded; preserve `CleanupUnknown` and recovery evidence.
- Rfpoll, pspider, socket-stat, and rgen use metadata, record sequence, monotonic timestamps,
  and a clean end marker. Coordinate collector and processor contract changes.
  Tick frequency/page size come from target metadata, not host assumptions.
- Preserve corrected units: Linux kB means 1024 bytes; frequency Hz; latency ms;
  disk accounting sectors 512 bytes. Missing rate baselines and counter resets
  remain unavailable values rather than zero. Avoid double-counting CPU guest
  ticks or overlapping memory categories; handle absent swap explicitly.
- CSV exports write ordinary CSV plus a JSON sidecar; Dataset.from_csv restores
  chart descriptions, units and provenance for offline rendering. Processing threads
  are joined on cancellation: Python cannot forcibly stop their synchronous work.

## Environment and commands

Run commands from this directory. The selected toolchains are Python 3.14.8 and
Rust 1.94.0, with Rust edition 2024. Rust is pinned by `rust-toolchain.toml` and
`binary-utils/Cargo.lock`. Install dependencies and use the ordinary development
commands:

```bash
uv sync --dev --locked
just lint
just test
uv build
```

`just test-python` builds native artifacts first and collects Python branch
coverage under `reports/`. Use focused pytest or unittest selections during
implementation; run relevant Rust checks when native code changes. There is no
separate verification ledger or mandatory all-project verification ceremony.
Report relevant results and limitations briefly in the task or handoff.

`uv.lock` includes all four required runtime dependencies, including plotting.
Use the project `.venv`; do not modify shared Python environments to run tests.
Refresh the lock through actual dependency resolution when dependencies change.

`scripts/build_artifacts.py` builds selected Cargo/cross targets and generates a
unversioned multi-variant `artifacts/manifest.json`. Configuration lives in
`scripts/build-targets.toml` and `Cross.toml`; see `docs/native-builds.md`.
Run uses repository artifacts lazily by default; explicit BinaryProvider paths
override this. Installed packages outside a checkout need an explicit bundle.
Rebuild artifacts after native changes. BinaryProvider validates compatibility;
do not relabel x86-64/glibc executables as ARM64/musl. Tool names and directory
names alone do not prove ABI compatibility. Update format producers and consumers together.

`runs/`, `reports/`, `dist/`, native `target/`, `.venv/`, and artifact executables
are generated/ignored; do not rely on their presence in a fresh checkout.

## Tests and handoff

Run checks appropriate to the change. Preserve runtime tests and Python coverage;
no coverage threshold, static typing suite, or Rust coverage is required.
Documentation-only changes need content/link checks, not a full runtime suite.
Update capability/native contracts when behavior changes. Do not maintain a
handwritten test-count, coverage, or artifact-hash ledger.

Integration tests depend on the current environment. Some managed runners deny
socket creation or asyncio wakeup writes; use bounded execution and report skips
or hangs accurately. Recheck capabilities rather than treating historical sandbox
failures as permanent. Test doubles, local SSH adapters, live SSH, and real-device
checks provide different evidence; do not conflate them. Use isolated loopback or
explicit test targets and avoid changing real machine settings for inspection.

Browser acceptance requires interaction and responsive-layout checks.
HTML-content tests alone do not establish either.

## rup compatibility

Maintain our implementation in `binary-utils/src/bin/rup.rs`. Do not replace it
with upstream source, a vendored crate, or a dependency on the rup implementation.
The compatibility target is upstream 0.15.1's CLI, wire protocol, and JSONL v3.
Use an external executable selected by `VSM_RUP_REFERENCE` for optional
interoperability checks. Keep the ordinary Rust dependencies minimal. The tool
reports its own version; exact help prose/diagnostic wording is not a contract.

Rup stdout follows upstream `rup.ping` JSONL version 3, including terminal
responses and summary. It has no native `type`, record sequence, readiness, or
end records. Framework `LogRef` uses schema `rup`, wrapping `rup.ping` JSONL v3. Preserve
upstream terminal-response accounting rather than silently treating these as
lost echo replies. `VSM_RUP_READY_FILE` opts into marker-based readiness and
SIGTERM draining; without it the upstream CLI remains unchanged. Fractional
Python durations use an owned framework timer. `BindEndpoint` permits local
port zero while remote `Endpoint` keeps its existing positive-port invariant.

## Identity, workspaces, and rendering

DeviceId is an opaque backend-derived string, never a generated UUID. Multiple
connections to the same device are supported. Paths and discovered processes can
cross equal-device connections; boot/start identity checks remain. Conflict claims
span equal-device connections within one run. Connection objects independently
own workspaces and processes; temporary paths and deployment cache keys must
include the workspace. Connection output names and action IDs use run counters.

Connection.tmp_dir is initialized by the backend before entry and cleaned on
backend exit, even outside Run. Host uses private /tmp/vsm-tmp-data-<date>-<suffix>
directories. Do not hardcode target temp roots in actions or runtime. Preserve
recovery workspaces after unconfirmed cleanup.

Public context-manager methods explicitly return AbstractAsyncContextManager;
private decorated generators implement them. Use class-qualified private calls
in public wrappers so super().open() does not recurse through an override.

Processors own measurement-specific units and chart definitions; never infer
charts from every numeric dataframe column. Plotly supplies interactive HTML;
Matplotlib supplies static images and lightweight HTML reports. Keep output
composition separate from processing. CSV exports retain dataset metadata and built-in
chart descriptions in companion `<filename>.json` sidecars; keep pairs together.
Changes to run journaling require an explicit task.

Use real runtime dependencies for tests. Production examples retain the standard
asyncio loop. IDE diagnostics and browser behavior require their respective tools;
do not infer either from unit-test success.

## Targetctl lifecycle

Host and SSH public launches, discovery, and startup identity validation use
required targetctl artifacts. See docs/targetctl.md for the unversioned protocol and
recovery semantics. Private bootstrap transports deploy and validate the helper
without recursively using public launch. Preserve Process.identity(),
Process.processes(), and Capture.processes(): all select the pinned workload,
never the helper. Scoped process-group cleanup uses a reserved unreaped leader
identity and a renewable lease (5-second renewal, 30-second expiry by default).
Unknown cleanup retains recovery records; escaped descendants remain outside the
guarantee. PidStat, ProcessIo, and Smaps additionally pin procfs identities during
sampling; see docs/native-contracts.md for process identity logs and entity_end.
