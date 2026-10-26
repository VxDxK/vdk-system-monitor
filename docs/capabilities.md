# Capability catalogue

All public action `interval` values are positive integer **milliseconds**. Booleans,
floats, zero, and negatives are rejected during construction. Defaults are:

| Actions | Default interval |
|---|---:|
| PollFiles and its subclasses (including Smaps and other file collectors) | 250 ms |
| PidStat, ProcessIo, SocketStat | 250 ms |
| PeriodicLauncher, Ss | 1000 ms |
| WifiConnInfo | 500 ms |
| Rup | 100 ms |

For example, `interval=100` selects a 100 ms sampling interval. Durations,
timeouts, and `LogMetadata.interval_seconds` use seconds.

All entries have implementation and fixture/contract tests. This is not a claim of
execution on every device. Native file/process sampling has executed on Host; network,
privileged, and hardware behavior needs its corresponding integration environment.

| Registered name | Python type | API | Processor | Parameters |
|---|---|---|---|---|
| `launch` | `Launch` | `do` / `use` | `—` | `argv` (required), `executable`, `mode` |
| `periodic_launcher` | `PeriodicLauncher` | `use` | `—` | `argv` (required), `interval` |
| `poll_files` | `PollFiles` | `use` | `—` | `paths` (required), `interval`, `duration` |
| `pull` | `Pull` | `do` | `—` | `source` (required), `destination` (required) |
| `push` | `Push` | `do` | `—` | `source` (required), `destination` (required) |
| `shell` | `ShellAction` | `do` | `—` | `script` (required) |
| `sleep` | `Sleep` | `do` | `—` | `duration` (required) |
| `manual` | `Manual` | `do` | `—` | `prompt` |
| `battery_level` | `BatteryLevel` | `use` | `battery_level` | `paths`, `interval`, `duration` |
| `copy_file` | `CopyFile` | `do` | `time` | `source` (required), `destination` (required) |
| `cpu_freq` | `CpuFreq` | `use` | `cpu_freq` | `paths`, `interval`, `duration` |
| `cpu_freq_governor` | `CpuFreqGovernor` | `use` | `—` | `governor` (required), `paths` |
| `cpu_time_in_state` | `CpuTimeInState` | `use` | `cpu_time_in_state` | `paths` |
| `ddr_freq` | `DdrFreq` | `use` | `ddr_freq` | `paths`, `interval`, `duration` |
| `dmesg` | `Dmesg` | `do` / `use` | `—` | `args`, `mode` |
| `fio` | `Fio` | `do` / `use` | `fio` | `args`, `mode` |
| `frame_count` | `FrameCount` | `use` | `frame_count` | `paths`, `interval`, `duration` |
| `ftrace` | `Ftrace` | `do` / `use` | `—` | `events`, `directory`, `mode` |
| `gpu_freq` | `GpuFreq` | `use` | `gpu_freq` | `paths`, `interval`, `duration` |
| `iface_info` | `IfaceInfo` | `do` | `—` |  |
| `iperf` | `Iperf` | `do` / `use` | `iperf` | `host`, `port`, `duration`, `server`, `udp`, `mode` |
| `meminfo` | `Meminfo` | `use` | `meminfo` | `paths`, `interval`, `duration` |
| `net_dev` | `NetDev` | `use` | `net_dev` | `paths`, `interval`, `duration` |
| `net_snmp` | `NetSnmp` | `use` | `net_snmp` | `paths`, `interval`, `duration` |
| `net_udp` | `NetUdp` | `use` | `net_udp` | `paths`, `interval`, `duration` |
| `npu_freq` | `NpuFreq` | `use` | `npu_freq` | `paths`, `interval`, `duration` |
| `pid_stat` | `PidStat` | `use` | `pid_stat` | `processes` (required), `interval`, `duration`, `self_only` |
| `ping` | `Ping` | `do` / `use` | `ping` | `args`, `destination` (required), `mode` |
| `proc_stat` | `ProcStat` | `use` | `proc_stat` | `paths`, `interval`, `duration` |
| `rup` | `Rup` | `do` / `use` | `rup` | `address` (required), `role`, `protocol`, `duration`, `interval`, `timeout`, `count`, `adaptive`, `local`, `request_size`, `response_size`, `traffic_class`, `mode` |
| `scan_processes` | `ScanProcesses` | `do` | `—` | `pattern` |
| `set_file` | `SetFile` | `use` | `—` | `path` (required), `value` (required) |
| `simpleperf` | `Simpleperf` | `do` / `use` | `—` | `args`, `mode` |
| `smaps` | `Smaps` | `use` | `smaps` | `paths`, `interval`, `duration`, `processes` (required) |
| `socket_buffer_limit` | `SocketBufferLimit` | `use` | `—` | `bytes` (required) |
| `socket_stat` | `SocketStat` | `use` | `socket_stat` | `interval`, `duration` |
| `ss` | `Ss` | `use` | `ss` | `argv`, `interval` |
| `strace` | `Strace` | `do` / `use` | `—` | `args`, `mode` |
| `tcpdump` | `Tcpdump` | `do` / `use` | `—` | `args`, `mode` |
| `temperature` | `Temperature` | `use` | `temperature` | `paths`, `interval`, `duration` |
| `top` | `Top` | `do` / `use` | `top` | `args`, `mode` |
| `traffic_generator` | `TrafficGenerator` | `do` / `use` | `traffic_generator` | `address` (required), `role`, `protocol`, `duration`, `workers`, `payload_bytes`, `rate_bps`, `mode` |
| `wifi_conn_info` | `WifiConnInfo` | `use` | `wifi_conn_info` | `argv`, `interval`, `interface` (required) |
| `disk_io` | `DiskIo` | `use` | `disk_io` | `paths`, `interval`, `duration` |
| `swap` | `Swap` | `use` | `swap` | `paths`, `interval`, `duration` |
| `fans` | `Fans` | `use` | `fans` | `paths`, `interval`, `duration` |
| `filesystem_usage` | `FilesystemUsage` | `use` | `filesystem_usage` | `paths`, `interval`, `duration` |
| `load_average` | `LoadAverage` | `use` | `load_average` | `paths`, `interval`, `duration` |
| `uptime` | `Uptime` | `use` | `uptime` | `paths`, `interval`, `duration` |
| `process_io` | `ProcessIo` | `use` | `process_io` | `processes` (required), `interval`, `duration`, `self_only` |

Launch-like entries marked `do` / `use` accept `LaunchMode` from
`vsm.action.launch_mode`. Select `FOREGROUND` with `device.do()` to await a finalized
`LogRef` (`Completed` for `Launch`), or use `device.use()` with `BACKGROUND_WAIT` to
wait on normal scope exit and `BACKGROUND_STOP` to stop and reap on exit. The default is
`BACKGROUND_STOP`. Wrong entrypoint/mode combinations fail before launch. All modes
retain ownership and stop on errors/cancellation. Natural waits are cancellable;
stop/reap/finalization is cancellation resistant. Waiting does not invent a completion
condition for open-ended services. Finite ping invocations use foreground or
background-wait; background-stop retains early-exit supervision. Repeated-command loops
and file collectors retain their scoped API. There is no unowned detach mode.

`PollFiles` (including its measurement subclasses), `PidStat`/`ProcessIo`,
`SocketStat`, `TrafficGenerator` and framework `Rup` default to `duration=None`.
Their scopes control lifetime; explicit positive durations still request timed
runs. The manual examples wait for Enter without an imposed deadline.

Timed file/process/socket collectors allow natural completion inside an open
scope, including completion before readiness is observed. Recorded readiness and
successful exit are still required. `capture.wait()` does not finalize the log;
scope exit does, retaining incomplete status when the clean end marker is missing.
Unexpected completion without a configured duration remains an error.

There are 50 action and 32 processor registrations. The controller-side `manual`
action provides an interactive Enter-to-continue pause.

Native actions use the repository artifact bundle automatically through Run; an explicit
BinaryProvider selects a custom bundle. See [native builds](native-builds.md) for
platform compatibility and defaults. External tools are detected through the connection
and missing tools raise CapabilityUnavailable. Hardware polling requires matching target
sources; read failures remain explicit records and processors reject them unless
allow_read_errors is requested. Android-specific simpleperf/top variants remain
dependent on a suitable executable/profile; ADB/HDC transports are not implemented.

No one-to-one action/processor relationship is required: settings/discovery have typed
values, traces/profilers retain raw outputs, and processors can emit several datasets.
This release supports binary-utils JSONL, Linux text tool outputs, iperf JSON, fio JSON,
and before/after residency snapshots. Rup uses pinned upstream 0.15.1 CLI/wire behavior
and JSONL v3; other native tools expose vsm-specific contracts.

`scan_processes` returns an immutable `ProcessSet` sequence: use `len(selection)`,
`selection[0].pid`, and `for process in selection`. Slicing retains the selection
type. Pass the result directly to `PidStat`, `ProcessIo`, or `Smaps` using
`processes=selection`; all three validate device, known boot identity, and process
start ticks before launch. Missing/unreadable stat files propagate their read
errors. Native sampling pins process/thread
identities through procfs directory descriptors and start ticks. Confirmed exit
ends the corresponding series at its last sample without a synthetic row or an
`allow_read_errors` opt-in. Unexpected read failures stay strict. These actions
use current identity-aware `pspider` or process-aware `rfpoll` records;
only the current native log contract is supported.

Snapshot membership is tracked by NetDev, DiskIo, ProcStat, NetUdp, SocketStat,
Ss, Top, WifiConnInfo, and NetSnmp. Their datasets add `observation_epoch` to
separate observed disappearances/reappearances in chart groups and rate baselines.
Empty interface/disk/UDP/socket/station snapshots and recognized header-only top
snapshots are valid; they produce empty datasets without synthetic rows. CPU and
SNMP formats still require valid source content. IPv4/IPv6 UDP tables are tracked
independently. Read failures remain strict; diagnostic opt-ins invalidate relevant
baselines, including cumulative scalar measurements such as FrameCount.

Observation epochs cannot identify replacement entities that never disappear from
an observed snapshot. Top/default ss lack birth identifiers; interface names,
disk numbers, UDP inodes and station MAC addresses may be reused. Native process
collectors use pinned start identities; SocketStat additionally uses validated
kernel cookies for rates. Sensors and filesystem paths retain strict source-read
semantics rather than treating every missing path as a confirmed device exit.

Ss/WifiConnInfo periodic query failures surface during startup or collection and
mark logs incomplete. CpuTimeInState preserves before/partial-after snapshots and
`read_errors` on final-read failure; its processor rejects incomplete captures.
New frequencies without initial counters have unavailable residency deltas.

Units: CPU and process utilization are percentages; frequency is Hz; memory is bytes
with Linux kB converted using 1024; latency is ms; byte/packet rates retain their
operation units. Missing baselines and resets are not zero observations. Disk sector
counters use Linux’s 512-byte accounting unit. RAM pressure is based on MemAvailable,
without stacking overlapping shared/cache categories.

Params.selected selects CPU names, network interface names, disk names, SNMP
protocol/counter names, or source paths for other file processors. Datasets retain
labels, column units, warnings, and source/device/action/target provenance. CSV export
writes ordinary CSV plus a `<filename>.json` sidecar retaining this metadata,
column types, missing values and built-in chart descriptions. `Dataset.from_csv`
loads the pair for offline rendering in another script without raw logs or a
connection. Raw logs permit reprocessing with the matching vsm release;
cross-release format compatibility is not guaranteed.

Known limits: POSIX process groups do not contain deliberately daemonized descendants;
restoration cannot be guaranteed after permanent target loss; synchronous worker threads
must finish before cancellation can release their inputs; cross-run coordination and
symlink-alias resource claims need caller coordination.

Rup's Python defaults are UDP, no collection deadline, 100ms fixed interval,
and 1-second response timeout. These are explicit argv/lifecycle choices; stock rup
client defaults remain ICMP and a 1-second interval. `adaptive=True` omits the
fixed-interval option. `local=BindEndpoint.parse("127.0.0.1:0")` requests an ephemeral
local port. Server configuration accepts an explicit endpoint and duration; client-only
options are rejected. `duration=None` removes the framework timer, so use count or an
enclosing resource scope for bounded client work. Rup server logs cannot be passed to
the client measurement processor. Terminal responses are preserved separately from echo
replies and included in upstream received/loss statistics. Framework schema `rup` wraps
the pinned upstream `rup.ping` JSONL v3 format.

All processors provide explicit chart descriptions where their measurements are
plottable. Plotly and Matplotlib render the same selected metrics with appropriate
entity grouping and axis units. CPU percentages, memory bytes/counts/percentages,
network/disk byte and event rates, sensor values, frequency residency, process
CPU/memory/thread counts, latency distributions, and benchmark summaries have
separate charts. Numeric identities and reset flags are not graphed. Time-only
summaries and Rup summary records can remain tables. Missing baselines and counter
resets remain gaps. Matplotlib percentage axes are bounded only where meaningful;
process CPU load may exceed 100 percent.

Use `export_charts` from `vsm.output.charts`; layouts are FOLDABLE, TABLE, or FILES.
The default is an interactive offline Plotly report. Select MATPLOTLIB
for static reports with embedded PNG images; standalone Matplotlib images support PNG, JPEG,
SVG, and PDF. Each chart also exposes direct figures for Python callers.
Readable `entity_label` columns are additive to CSVs; raw paths and process/socket
identities remain. Primary chart families precede additional charts.
See [chart reports](chart-reports.md) for chart families,
optional battery/thermal collection, CPU/network/thread metrics, Wi-Fi
parameter parsing, and verification limits.

`Manual` (`from vsm.action.manual import Manual`) is awaited through `device.do`.
It displays `prompt` (default `Press Enter to continue...`) on the controller and
returns `None` after a newline. It needs exclusive access to controller stdin
with a readable file descriptor on Linux. Waiting yields to concurrent collectors;
EOF raises `EOFError` and cancellation propagates without a lingering input
thread. It does not stop collectors itself: leave their resource scopes before
processing finalized logs, as demonstrated in `examples/manual.py`.

`CpuFreq` resolves complete source paths through the connection before launching
the sampler. This excludes nonexistent suffixes below `/sys/devices/system/cpu/cpufreq`
and `cpuidle`; genuine read failures remain explicit and processing stays strict.

Process discovery, startup validation, and Host/SSH public launches require
`targetctl`. Discovery skips confirmed disappearance and propagates permission,
read, and malformed-data failures. Prefer `await process.processes()` or
`await capture.processes()` for owned workloads; `ScanProcesses.pattern` remains
a literal substring. See [targetctl](targetctl.md) for identities and cleanup.
