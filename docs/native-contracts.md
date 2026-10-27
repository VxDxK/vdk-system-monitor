# Native tool contracts

The six native tools are rfpoll, pspider, socket-stat, rgen, rup, and targetctl.
Rfpoll, pspider, socket-stat, and rgen use vsm-specific contracts and emit compact
UTF-8 JSONL to stdout and diagnostics to stderr. Rup follows the pinned upstream
contract described below; targetctl has a separate control protocol.
Each record from rfpoll, pspider, socket-stat, or rgen has `type`, increasing
`sequence`, and elapsed monotonic `time_ns`.
The first record is metadata with a tool schema, package version, wall-clock
origin, target tick frequency/page size, and boot identity. Native metadata also
records `monotonic_origin_ns`, the system-wide Linux CLOCK_MONOTONIC origin of
elapsed `time_ns`, and `origin_uncertainty_ns`, its bracketing precision. Controller
UTC is derived through framework clock calibration, never from native `wall_ns`.
This shared origin contract applies to rfpoll, pspider, socket-stat and rgen;
upstream rup JSONL remains unchanged. A clean `end` record
identifies a complete stream. SIGTERM/SIGINT request graceful exit; forced stop
or missing/truncated end means incomplete data. Read errors are distinct records.

Rfpoll, pspider, socket-stat, and rgen have no deadline when `--duration-ms` is
omitted. Explicit durations must be positive and at most 24h. Signal-driven
shutdown still emits a clean end marker. Rup
has its own upstream count/duration options, and vsm bounds its optional duration
to 24h. File reads are bounded to 16 MiB.
Backpressure may delay sampling and generate overrun records; no real-time
scheduling guarantee is claimed. The host continuously drains both streams.

Python file/process/socket collection accepts successful natural completion when
an explicit duration was requested, even if completion precedes observation of
the recorded readiness. The capture remains scoped and its log is finalized at
scope exit. Missing readiness or a nonzero exit remains an error; a missing clean
end marker leaves the log incomplete. Open-ended collection still treats an
unexpected tool exit as a failure.

Launch-like Python actions (Launch, command wrappers, TrafficGenerator, and Rup)
accept successful natural completion through either invocation style. `do()`
awaits completion and cleanup; `use()` stops/finalizes on exit, with explicit
`wait()` inside the scope to await natural completion. Readiness, nonzero-exit,
and log-completeness checks still apply. This Python lifecycle policy does not
change native CLI or log formats.

| Tool | Main interface | Data |
|---|---|---|
| rfpoll | `--path PATH` repeated, `--filesystem PATH`, `--interval-ms`, optional `--duration-ms` and `--samples` | sample ticks, hex-encoded arbitrary file bytes, read times/errors, filesystem byte counters |
| pspider | repeated `--process PID:START_TICKS` or `--pid`, optional substring `--pattern`, `--self-only`, interval, optional duration/sample limits | raw process stat/io/status/cmdline and thread stat, with PID/TID/scope tags |
| socket-stat | interval, optional duration/sample limits | inet_diag IPv4/IPv6 TCP/UDP snapshots, cookies, endpoints, state, queues, available TCP counters/info |
| rgen | `producer|consumer`, address, TCP/UDP, optional duration, workers, payload bytes, optional rate in bit/s and burst bytes | bytes, datagrams or I/O calls, interval throughput, final totals |
| rup | `rup [OPTIONS] <remote-address>`; `rup -p tcp server <local-address>` | upstream human output or `--output jsonl` (`rup.ping`, version 3) |
| targetctl | target-local discovery, identity validation, and process supervision; see [protocol](targetctl.md) | operation/supervision JSONL control messages and recovery records |

rfpoll, pspider, and socket-stat default to `--interval-ms 250`. Python collector
defaults match these native defaults. Explicit intervals are passed as integer
milliseconds; elapsed timestamps use nanoseconds. Rup retains its upstream
1000 ms native CLI default while the Python action explicitly selects 100 ms
unless adaptive mode is requested.

rfpoll discovery supports `*` and `?` per path component. `--root` substitutes a
filesystem root for collector fixture tests; it does not sandbox execution.
pspider without selection samples discoverable processes. Its process/thread
stat records retain start identity for processors; visibility can change during
collection.

## Identity-pinned process sampling

Pspider emits schema `pspider`; Rfpoll emits schema `rfpoll` for both ordinary
polling and process-aware collection. Python PidStat, ProcessIo, and Smaps require
the current identity-aware records.
Rebuild artifacts together with the controller after native contract changes.

Both tools accept repeatable `--process PID:START_TICKS` and optional `--boot-id ID`.
Pspider's pinned selection conflicts with `--pid`/`--pattern`; those
options retain dynamic discovery with identities separated by start ticks.
Process-aware rfpoll requires literal immediate `/proc/PID/FILE` paths for selected
processes, at least one path per process, and cannot combine with `--filesystem`.
Globs, traversal, foreign process paths and nested paths are rejected in this mode.
Smaps uses this mode for `smaps_rollup`; generic PollFiles does not infer process
ownership from a pathname.

Startup validates the selected start ticks and known boot ID. Sampling holds
procfs directory descriptors and validates identity before and after buffering
an entity's reads. Thread directories are pinned independently. Confirmed
termination discards that attempt's buffered measurements, retires the identity,
and closes its descriptor. An explicitly selected process never follows a reused
PID. Thread discovery may add a new generation of a reused TID as a separate series.

Process-aware `file`, `read_error`, and `entity_end` records carry `tags` with
`pid`, `tid`, `scope` (`process` or `thread`), `start_ticks`, and
`process_start_ticks`. For process records, PID equals TID and both start values
agree. Errors include `path`, `operation` (`read`, `identity`, or `enumerate`),
`errno` (integer or null), and diagnostic `error` text. An `entity_end` has reason
`exited` (zombie/dead state), `disappeared` (ENOENT/ESRCH for the pinned identity),
or `identity_changed`; it is emitted once per retired identity. All records retain
sequence and monotonic timestamps. Collectors remain scoped even after all
selected identities have ended and still emit a clean end marker when finalized.

A missing metric on a live process remains a read error. Permission, I/O,
malformed-identity, and enumeration failures are never inferred to be termination
from diagnostic text. Unexpected errors observed during a concurrent exit remain
in the log; the default processing policy rejects them. The existing explicit
read-error opt-in retains them as warnings for diagnostic use.

Processors consume lifecycle events without adding measurement rows or warnings. A
series simply ends at its last valid sample, with no synthetic gap or terminal row. Rate
baselines and chart groups include the relevant process/thread generation. Confirmed
termination before any valid sample produces stable empty datasets and no charts.
Malformed lifecycle records, records after retirement, incompatible native/framework
schemas, and incomplete streams remain errors. Target-native metadata, not host
assumptions, defines units.

## Snapshot processing

Snapshot processors use successful complete table/dump boundaries to track membership.
The processed-data `observation_epoch` column separates observed periods of presence, so
counters and charts do not bridge an observed absence. Epochs are derived during
processing rather than recorded by the native tools. There are no synthetic terminal
samples. Missing source reads still fail by default; an explicitly allowed read error
invalidates affected file baselines. Cookie-based SocketStat rates require two unsigned
32-bit cookie values other than the unavailable sentinel; malformed/unavailable cookies
never establish counter identity. Socket sample boundaries establish observed
membership. Unknown membership in an incomplete final snapshot is not used as evidence
of disappearance.

The CpuTimeInState JSON snapshot log retains `before`, partial `after`, and a
`read_errors` array when final reads fail, with `LogRef.complete=False`.
Successful captures retain both snapshots. The processor remains strict about
completeness and does not invent a zero baseline for newly appearing frequencies.

## Traffic generation

rgen uses one shared token bucket across workers, including an aggregate burst.
TCP I/O calls are not network packets. Consumers limit active TCP worker sessions;
completed workers are reaped. Traffic-generator throughput requires benchmark
validation on the intended machines and network.

rup is implemented locally in `binary-utils/src/bin/rup.rs`, targeting the
upstream 0.15.1 CLI, wire protocol, and JSONL v3. It is not built from upstream
source. Its own version string and help/diagnostic prose may differ.
The client defaults to ICMP and the server to UDP. Protocol options precede the
server subcommand. Requests use the upstream 12-byte little-endian header
`{id: u64, len: u16, resp_size: u16}` with zero padding; servers can return a
separately requested response size.

TCP uses persistent application echo after a successful first connection, or
per-request connect probes after initial failure. Terminal TCP outcomes and
matching Linux UDP ICMP errors count as received responses, with payload size
zero. They are distinguishable from echo replies. ICMP supports IPv4/IPv6 Linux
datagram ping sockets, subject to the system's ping permissions. No ICMP server
exists. Fixed/adaptive schedules, count, local binding, packet sizes, and
TOS/traffic class follow upstream behavior.

Client `--output jsonl` emits metadata (`schema="rup.ping", version=3`), then
`reply`, `terminal_reply`, `timeout`, or `reorder_or_loss` records, and a final
summary. Records use `record`, absolute UTC `timestamp_ms`, and request `seq`;
they do not use the other tools' `type`, record sequence, or `end` marker. RTTs
are in ms. Servers retain human-readable service logs, not measurement JSONL.

vsm opts into an out-of-band readiness file and graceful SIGTERM handling through
`VSM_RUP_READY_FILE`. Readiness means transport initialization for clients and
successful bind for servers; it does not promise a reachable remote echo server.
The action owns and removes its control directory, retaining it on unconfirmed
cleanup. Client completion requires exit zero and a valid final summary. Forced
stops remain incomplete. Framework duration timers support fractional seconds;
upstream CLI `--run-time` still uses whole seconds and its default is unchanged.

Captures use unversioned `LogRef` schema `rup`, wrapping upstream JSONL version
3. `RupProcessor` retains outcomes and full summary statistics, with upstream
loss accounting. Standalone JSONL can be imported using
`load_native(path, schema=Schema(name="rup"))`.
Pre-JSONL upstream human logs and server service logs are not measurement inputs.

socket-stat uses Linux inet_diag with length-checked TCP_INFO attributes. Fields
absent in a kernel response remain absent. TCP counters use the Linux UAPI
layout; raw TCP_INFO is retained for audit. Socket
cookie/endpoints/state are preserved. Fields include
`receive_rtt_us`, `congestion_window_segments`, and available memory diagnostics
(`receive_memory`, `receive_buffer`, `write_memory`, `send_buffer`,
`forward_memory`, `write_queued`, `option_memory`, `backlog_memory`). The request
includes INET_DIAG_MEMINFO, INFO and SKMEMINFO. Every field is bounds-checked;
short/older kernel attributes leave fields absent. Only the current native
log contract is supported. Live netlink acceptance requires a suitable Linux
target; static inspection and fixture tests do not establish kernel behavior.

Artifacts use an unversioned multi-target index with per-tool variants. The default
build is `x86_64-unknown-linux-gnu`; configurable cross entries add static x86-64
and AArch64 musl candidates. The provider inspects ELF identity and linkage,
checks hashes and runtime requirements, and prefers compatible static musl.
Build bundles together with the matching controller release.

`Run(...)` automatically uses repository artifacts, with lazy lookup independent
of the working directory. Explicit `BinaryProvider(directory)` overrides this;
installed packages outside the checkout require an explicit directory. Deployment
remains private to each connection workspace, including Host. See
[native builds](native-builds.md) for the manifest, matching rules, build commands,
platform verification limits, and static musl candidates on Android/OpenHarmony.

`targetctl` uses a separate unversioned operation/supervision JSONL control protocol,
not a measurement log. See [targetctl protocol](targetctl.md). Its launch identity
and cleanup semantics do not change the collector schemas above.
