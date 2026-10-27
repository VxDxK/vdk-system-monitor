# vdk-system-monitor

Typed asynchronous experiments across Linux devices, with six small Rust tools.
Python runs on the controller only; targets receive compatible native artifacts
and do not need Python or psutil. Initial target: Linux x86-64.

The package implements 50 action classes and 32 processing entry points. Their
availability depends on target files, permissions, hardware, and installed tools.
See [capabilities](docs/capabilities.md), [architecture](docs/architecture.md),
and [native contracts](docs/native-contracts.md).

## Release model

vsm uses a rolling release model. Only the current release's APIs, interfaces,
protocols, and data formats are supported. Update the controller and native tools
together; there are no compatibility shims, migration readers, or format
negotiation. Package versions record provenance, while platform ABI and runtime
requirements are checked independently. The pinned upstream rup contract is an
explicit external interoperability contract.

`run.json` records `vsm_version` from installed distribution metadata, generated
from `[project].version` in `pyproject.toml`. This identifies the controller
package used for successful, failed, and cancelled runs.

## Setup

From the checkout root, with [uv](https://docs.astral.sh/uv/) and the pinned
Rust toolchain installed:

```bash
uv sync --dev --locked
uv run python scripts/build_artifacts.py
```

Python 3.14.8 and Rust 1.94.0 are the selected development versions. Cargo.lock pins
native dependencies. `uv sync --dev --locked` creates the project `.venv` from
`uv.lock`. Native artifacts default to the imported checkout’s `artifacts/` directory,
independently of the current working directory. See [native
builds](docs/native-builds.md) for target configuration, Cargo/cross prerequisites,
selection, and custom bundles.

Pandas, AsyncSSH, Matplotlib, and Plotly are required runtime dependencies.
The lock includes all four; use `uv sync --dev --locked` for reproducible
installation. Targets need compatible native artifacts; see the build guide for
cross-compilation and installed-package bundle configuration.

## Command line

The installed package provides `vsm` (also available as `python -m vsm`):

```bash
vsm --help
vsm --version
vsm docs
vsm docs actions --brief
vsm docs actions cpu_freq
vsm docs processors --search latency
vsm docs connections ssh
vsm docs binaries rup
```

`docs` lists built-in implementations without connecting to targets or requiring
built artifacts. Named entries show Python signatures, inherited parameters,
source comments, limitations and usage patterns. Search is case-insensitive
literal text; repeat `--search` to match any term. See [CLI reference](docs/cli.md)
for categories, exit codes and extension instructions. Experiment execution and
processing currently use the Python API and examples below.

## API

```python
import asyncio
from pathlib import Path
from vsm.action.meminfo import Meminfo
from vsm.action.proc_stat import ProcStat
from vsm.connection.host import Host
from vsm.output.charts import export_charts
from vsm.processing import Params, process
from vsm.processor.proc_stat import CpuProcessor
from vsm.runtime import Run

async def main() -> None:
    async with Run(Path("runs/example")) as run:
        host = await run.connect(Host())
        async with host.use(ProcStat()) as cpu, host.use(Meminfo()) as memory:
            await asyncio.sleep(2)
        cpu_task = run.spawn(process(CpuProcessor(), cpu.log, params=Params()))
        datasets = await cpu_task
        datasets[0].to_csv(run.output / "cpu.csv")
        export_charts(datasets, run.output / "charts.html")

asyncio.run(main())
```

Use fresh output directories: Run rejects an existing directory to prevent
accidental overwrite. Device objects select targets; labels never route actions.
`run.spawn` schedules a Python coroutine; `connection.launch` owns a target
process. A collector's `capture.log` is available only after scope exit. For
finite benchmarks, `await capture.wait()` waits for the tool before leaving its
scope. Processors return dataframes and chart descriptions; output generators
render interactive Plotly charts by default, with Matplotlib available explicitly.

With an open connection, set command deadlines at the call site using
`asyncio.timeout()`; `connection.run()` has no timeout argument:

```python
from vsm.models import Exec

async with asyncio.timeout(5):
    result = await connection.run(Exec(argv=("uname", "-a")))
```

The deadline covers command startup and execution. On expiry, `run()` exits its
owned launch scope and joins cleanup before `TimeoutError` reaches the caller;
cleanup can take longer than the deadline. One timeout block can also cover
several operations or an experiment workflow. Timing out only `process.wait()`
or `capture.wait()` cancels that wait and leaves termination to its owning scope.
Tool durations request normal finite completion, and probe reply timeouts and
internal startup/cleanup/lease safeguards retain their distinct meanings.

Launch-like actions support both invocation styles. `await device.do(action)`
waits for natural completion and cleanup, returning a finalized `LogRef`
(`Completed` for `Launch`). `async with device.use(action)` establishes readiness
and yields an owned capture/process; scope exit stops and reaps remaining work
and finalizes its log. To let a scoped workload finish naturally, explicitly
`await capture.wait()` inside the scope. Its log becomes available after scope exit.
Successful tool completion is allowed while the scope remains open; readiness and
nonzero-exit checks still apply.

Natural waits are cancellable. Errors and scope cancellation stop owned work,
with cancellation-resistant cleanup joined before returning. Cancelling an
individual `capture.wait()` does not itself stop the workload. Waiting requires
a tool-side completion condition such as rgen's duration, rup's count/duration,
or ping's `-c` option; an open-ended tool keeps `do()` waiting until cancelled.
Use `run.spawn(device.do(action))` to run a finite action concurrently under the
run's supervision; the run joins its tasks on normal exit and cancels siblings
on failure.

```python
from vsm.action.fio import Fio

# fio must be installed on the target; choose a path on the filesystem to measure.
benchmark = Fio(args=(
    "--name=sequential-write", "--filename=/path/to/benchmark-file",
    "--rw=write", "--bs=1m", "--size=64m",
))
log = await host.do(benchmark)

async with host.use(benchmark) as capture:
    await capture.wait()
log = capture.log

# The run owns and joins the task even if its handle is not explicitly awaited.
task = run.spawn(host.do(benchmark))
log = await task
```

Examples:

```bash
uv run python examples/manual.py --port 5000
uv run python examples/manual_ssh.py alice benchmark-server --port 5000
uv run python examples/manual_ssh.py alice benchmark-server --auth key --port 5000
```

`examples/manual.py` starts a local TCP rup server and client on loopback. It
collects CPU frequency/utilization, memory and interface counters once for the
shared host, and process/thread CPU, memory and smaps separately for each owned
workload. The client log supplies RTT charts and the rup summary; the server's
service log is retained raw. Both workloads are selected through their capture's
`processes()` method, never by a process-name search. No external traffic generator
is needed. Raw logs, CSV/sidecar pairs under `host/`, `client/`, and `server/`, and
one combined `charts.html` stay under a fresh `runs/manual-*` directory.

`examples/manual_ssh.py NAME HOST` starts a local rgen producer through Host and a
remote consumer through SSH on port 22. By default (`--auth password`), it prompts
for the password without echo. Use `--auth key` to load the controller's
`~/.ssh/id_rsa` private key without a password prompt. Both modes retain normal
known-host verification. Both machines require Linux x86-64,
compatible built native artifacts, readable CPU-frequency sources, and permission
to read the workloads' `/proc` files; the SSH target also needs SFTP. The consumer
binds a wildcard address and must accept direct traffic from the controller on
`--port` (default 5000); the example creates no SSH tunnel or firewall changes.
The controller resolves HOST and uses its first IPv4/IPv6 result; `--address IP`
overrides the traffic destination when it differs from the SSH hostname.

The SSH example collects CPU frequency/utilization, memory, interface counters,
process/thread CPU and memory, smaps, and rgen throughput on both machines. TCP
is the default; `--protocol udp` selects UDP. `--workers` defaults to 1 and
`--payload-bytes` to 1024. Traffic is unlimited unless `--rate-bps` supplies an
aggregate producer limit in bit/s, for example `--protocol udp --rate-bps 10000000`.
All raw logs and exports stay locally under `runs/manual-ssh-*`, with `client/`
and `server/` CSV/sidecar directories and one combined `charts.html`.

Both manual examples display chart dates in UTC+03:00. Dataset and CSV timestamps
remain UTC. Other exports default to UTC; select a fixed offset with
`ExportOptions(timezone=timezone(timedelta(hours=3)))` from `vsm.output.charts`,
using `timezone` and `timedelta` from `datetime`. See [chart reports](docs/chart-reports.md).

Both examples wait for Enter on the controller after the server, client, and
collectors are ready. There is no collection deadline; Enter ends the resource
scopes and stops the owned workloads and collectors. The client stops
before the server, allowing rup to drain pending replies. All captures finalize
and all processing succeeds before CSV/chart export. EOF raises `EOFError`;
Ctrl-C, cancellation, or collection failure unwinds owned resources and
aborts export. `Manual` needs exclusive controller stdin access with a readable
file descriptor on Linux; cancellation leaves no blocking input thread behind.

System counters include other machine activity and cover setup/shutdown too.
Each chart retains its capture's time basis; the combined report does not imply
synchronized clocks across machines. Rgen TCP operations count I/O calls, not
network packets. Both examples use strict `Params()`; unavailable CPU-frequency
sources and unexpected read failures remain errors. CSV and HTML sections retain
host/client/server labels. The examples call `export_charts` directly and do not
load datasets from disk or provide a render-only mode.

Examples and tests use the standard asyncio event loop. Environments that deny
asyncio wakeup-socket writes may prevent asynchronous operations from completing.

`PollFiles` and its subclasses, `PidStat`/`ProcessIo`, `SocketStat`, rgen, and
framework rup default to `duration=None`: their resource scopes own collection
lifetime. Supply an explicit `duration` in seconds for a timed run, or use
`CollectFor` for a scoped collection window. Waiting for natural completion needs
a completion condition such as an explicit duration or rup count.

File, process, and socket collectors with an explicit duration may finish while
their resource scope remains open. `await capture.wait()` waits for that completion;
`capture.log` remains unavailable until scope exit finalizes it. Recorded readiness
is still required, nonzero exits fail the run, and missing end markers leave incomplete
logs. An open-ended collector exiting while its scope is active remains an error.

Action `interval` values are positive integer milliseconds, for example
`ProcStat(interval=100)`. File/process/socket collectors default to 250 ms,
periodic commands and Ss to 1000 ms, Wi-Fi to 500 ms, and Rup to 100 ms.
Durations, timeouts, and log metadata's `interval_seconds` use seconds.
See [capabilities](docs/capabilities.md) for parameters, defaults, and limitations.

## Development and tests

Install [Just](https://just.systems/) alongside uv, Python 3.14+, and the pinned Rust
toolchain, then run `uv sync --dev --locked`. Artifact packaging inspects ELF images
without executing them. Run `just` to list the commands:

| Target | Purpose |
|---|---|
| `fmt-rust`, `fmt-python`, `fmt` | Format Rust, Python, or both |
| `lint-rust` | Cargo check and Clippy with warnings denied |
| `lint-python` | Ruff lint and formatting checks |
| `lint` | Run both language lint targets |
| `test-rust` | Run Rust unit tests |
| `test-python` | Build native artifacts, then run pytest with branch coverage |
| `test` | Run Rust and Python tests |
| `build-artifacts` | Build and package all six native release binaries and their manifest |
| `clean` | Remove generated output, preserving `.venv` |

Python tests use native binaries, so `test-python` builds and packages them first.
Coverage includes terminal output and HTML, JSON, and XML reports under `reports/`.
Aggregate targets run sequentially and stop on failure.

GitHub Actions runs formatting and lint checks for both languages, Rust unit
tests, and the full Python suite on pull requests and pushes to `master`.
The workflow also supports manual runs from the Actions tab. CI uses Ubuntu
x86-64, the Python version in `.python-version`, and the pinned Rust toolchain.
It builds the default Linux GNU native artifacts with `just test-python`.
Download the `python-coverage` artifact from a workflow
run for HTML, JSON, and XML coverage reports, including reports produced by
failed tests. No coverage threshold is enforced.

To characterize processing memory and runtime with synthetic CPU/process logs:

```bash
.venv/bin/python scripts/benchmark_processing.py
```

The Linux benchmark generates inputs before measurement and processes 1,000,
10,000, and 100,000 ticks in fresh subprocesses, three times each. Ordinary runs
measure wall time and peak RSS; separate tracemalloc runs measure Python allocation
peaks. Results, dataframe sizes, row counts, parameters and runtime provenance go
to `reports/processing-benchmark.json`. Each worker has a 120-second timeout;
timeouts/errors are recorded and make the command fail. Smaller smoke runs can use
`--sizes 1000 --repetitions 1`. RSS includes interpreter/import overhead, and
tracemalloc adds substantial time and memory overhead. These are characterization
results, without a memory threshold. Processing currently retains whole-log events
and complete output dataframes; it is not a bounded-memory streaming API.

`just clean` removes `binary-utils/target/`, `artifacts/` (including its generated
manifest), `dist/`, `reports/`, `runs/`, and coverage data using `rm -rf`.
It preserves `.venv`; cache cleanup is left to the developer.

Use the existing development targets directly:

```bash
just lint
just test
uv build
```

For focused checks, use `uv run pytest tests/test_models.py` or the relevant test
module. The unittest-compatible suite can also run with
`.venv/bin/python -m unittest discover -s tests -v`. Build native artifacts first
when running collector tests directly. Static typing checks and Rust coverage
are not required; Python tests collect coverage through `just test-python`.

Loopback SSH/network tests require sockets; ICMP and hardware tests may require
additional permissions or suitable targets. Optional upstream rup comparisons
use `VSM_RUP_REFERENCE`. Report skips as skips, and distinguish transport doubles
from live integration. Some managed runners restrict sockets or asyncio wakeups;
use bounded execution there rather than assuming an old restriction still applies.
Test results come from the commands themselves; no separate verification ledger
is maintained.

## Package layout and annotations

Import implementations from defining modules, such as `vsm.connection.host`,
`vsm.runtime`, and `vsm.action.proc_stat`. Package initializers do not reexport
classes or eagerly load built-in measurements. Extension
protocols live in `vsm.action.action`, `vsm.connection.connection`, and
`vsm.processor.processor`. Each action and measurement processor has its own
module; Host and SSH have separate connection implementations. Shared parsing,
readiness and cancellation utilities remain independent of concrete measurements.
Collectors explicitly own their launch, readiness, stop, and finalization lifecycle.

Generics use Python 3.14 syntax, such as `class Capture[R]`. The empty `py.typed`
file tells IDEs and type checkers that the installed package supplies annotations.
It has no runtime effect and requires no typing test suite; it preserves useful
autocomplete and downstream checking for users who want them.

## Rup compatibility

Our native implementation is `binary-utils/src/bin/rup.rs`. It implements the
rup 0.15.1 CLI, 12-byte little-endian echo protocol, persistent TCP measurements,
Linux ping sockets, and `rup.ping` JSONL v3. No upstream source is embedded or
built into the project. Tokio and socket2 are ordinary Rust dependencies.

The vsm action uses the compatible CLI and opt-in readiness/SIGTERM handling. Framework
logs use unversioned schema `rup` wrapping `rup.ping` JSONL v3.
`VSM_RUP_REFERENCE=/path/to/original/rup` enables optional external CLI and
bidirectional TCP/UDP interoperability comparisons. Network checks need permitted
loopback sockets. Our executable reports its own package version; help descriptions and
diagnostic wording are not byte-for-byte copies.

## Device identity and temporary data

`DeviceId` is an opaque backend-supplied string: `host:<hostname>` for Host and
`user@hostname:port` for SSH. Future ADB/HDC backends can use serial numbers.
Several connections may share one device ID. Paths and process selections can
cross connections to the same device; process boot/start identities are still
checked. Settings claims span those connections within one run.

`DeviceId` and `ActionId` format directly as their values: `str(device.id)`
returns the backend identity. They retain distinct types and equality rules.
Target paths support `str(path)`, `path.name`, `path.parent`, and `path / "child"`;
derived paths retain device ownership. Joining accepts strings and
`PurePosixPath` values and follows POSIX path semantics, including replacement
by an absolute operand. Joining another `TargetPath` raises `TypeError`.
Target paths do not implement the local filesystem path protocol; use the
connection for target I/O.

Process discovery returns an immutable sequence. Indexing returns a process
identity, and slicing returns another `ProcessSet` that collectors can use:

```python
from vsm.action.pid_stat import PidStat
from vsm.action.scan_processes import ScanProcesses
from vsm.action.smaps import Smaps

processes = await device.do(ScanProcesses(pattern="my-service"))
if processes:
    print(len(processes), processes[0].pid)
    async with (
        device.use(PidStat(processes=processes[:1])),
        device.use(Smaps(processes=processes[:1])),
    ):
        await asyncio.sleep(2)
```

`ProcessSet(iterable)` snapshots its input and preserves ordering and duplicates;
`ProcessSet()` is empty. Pass discovered identities as `Smaps(processes=selection)`.
`PidStat`, `ProcessIo`, and `Smaps` reject empty selections, foreign devices, known boot
mismatches, and reused PIDs before collection. An exited or unreadable process raises
the target read error. The native collectors also pin procfs identities during sampling
and do not follow reused PIDs. Confirmed process/thread exit ends its series without a
synthetic row or caller opt-in; unexpected read failures remain strict. Rebuild
rfpoll/pspider artifacts together with the controller when native contracts change.

Snapshot measurements also handle changing membership. NetDev, DiskIo,
ProcStat, NetSnmp, NetUdp, SocketStat, Ss, Top, and WifiConnInfo add an
`observation_epoch`
column to distinguish observed periods of presence. If an entity disappears from
a complete snapshot and later returns, its charts form separate series and its
counter rates start with an unavailable baseline. No disappearance rows or zeros
are inserted. Valid empty disk/interface/socket/station snapshots return empty datasets.
Unexpected read failures and malformed snapshots remain errors; diagnostic
read-error opt-ins invalidate affected counter baselines instead of bridging them.

These epochs describe observed membership, not a guaranteed kernel birth identity.
Top and the default ss output cannot detect PID/endpoint reuse that occurs entirely
between snapshots. Use identity-pinned PidStat/Smaps/ProcessIo for processes and
SocketStat's kernel cookies for socket identity. Missing sysfs sensor files remain
strict errors because absence alone cannot confirm device termination.

Periodic query failures (including Ss/WifiConnInfo startup failures) propagate and
leave incomplete captures. CpuTimeInState retains its before/partial-after data
and read-error evidence when final reads fail. Frequency residency without a
matching initial frequency counter is unavailable rather than assuming zero.

Each connection owns a private `tmp_dir`, available even outside a `Run`.
Host uses `/tmp/vsm-tmp-data-<UTC-date>-<unique-suffix>`. Binary deployments and
collector control files live there. Connections stop their owned processes and
remove their own workspace on exit; unconfirmed cleanup retains recovery files.
Connection output directories use `connection-0001` names, and action IDs use
run-wide `action-000001` counters. No UUIDs are generated by vsm.

## Charts and CSV

`Dataset.to_csv(path)` writes an ordinary CSV and a companion `<filename>.json`
sidecar (for example, `cpu.csv.json`). The sidecar preserves chart descriptions,
labels, units, warnings, provenance, column types, and missing-value information.
Keep both files together to load the dataset in another script:

```python
from pathlib import Path
from vsm.processing import Dataset
from vsm.output.charts import export_charts

cpu = Dataset.from_csv(Path("results/cpu.csv"))
export_charts((cpu,), Path("reports/cpu.html"))
```

Loading needs neither a `Run` nor the original connection or raw logs. Use the
snippet above in a separate script when needed; the two manual examples collect,
export CSV pairs, and render charts directly. Move CSV/sidecar pairs together.
`from_csv` requires a vsm sidecar; use Pandas for arbitrary external CSV files.
The dataframe index is not saved. Built-in line, bar, box, and table charts are
supported; unsupported custom charts or dataframe types raise an error on export.
See [chart reports](docs/chart-reports.md#saved-datasets) for the disk contract.
Raw logs and `LogRef` values remain available for rerunning measurement processors;
loading a CSV restores already processed results.

```python
from vsm.output.charts import Backend, ExportOptions, ImageFormat, Layout, export_charts

# Default: two-column, collapsible offline Plotly HTML with wrapping legends.
export_charts(datasets, output / "charts.html")
# Interactive, self-contained HTML; Plotly.js is included once, without a CDN.
export_charts(datasets, output / "interactive.html",
              options=ExportOptions(backend=Backend.PLOTLY))
# Other layouts: Layout.TABLE and Layout.FILES.
export_charts(datasets, output / "images",
              options=ExportOptions(backend=Backend.MATPLOTLIB, layout=Layout.FILES,
                                    image_format=ImageFormat.SVG))
# Direct figures for notebooks/DataSpell:
figure = datasets[0].charts[0].as_plotly(datasets[0].frame)
static_figure = datasets[0].charts[0].as_matplotlib(datasets[0].frame)
```

Matplotlib supports PNG, JPEG, SVG, and PDF files and headless report generation.
Plotly produces interactive HTML. Reports preserve entity grouping, separate
units, and missing-value gaps; summary-only datasets appear as tables. Exporters
never open browsers automatically. Chart and output APIs are independent of
measurement names; custom charts implement `vsm.chart.chart.Chart`.

Reports group related datasets by device/capture and measurement family. Full
paths live in expandable source details; legends use readable entity names such
as `cpu0`. Processor CSVs add `entity_label` while retaining original identifiers.
Plots and legends expand in the page; only wide data tables scroll sideways.
See [chart reports](docs/chart-reports.md) for chart families, labels, and the
synthetic `scripts/chart_report.py` review tool.

Public context-manager entry methods have explicit `AbstractAsyncContextManager`
return types. Their private generators use `@asynccontextmanager`, helping IDEs
check structural protocols while keeping inheritance optional.

Host and SSH launches require the native `targetctl` artifact. It supplies pinned
workload identities, discovery, validation, and leased process-group supervision.
See [targetctl](docs/targetctl.md) for configuration, protocol, and recovery limits.
