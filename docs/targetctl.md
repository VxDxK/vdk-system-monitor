# Target-local operations and owned processes

`targetctl` is a required `binary-utils` executable for Host/SSH process discovery,
startup validation, and public launches. Python runs on the controller; no target
Python is required. Build a fresh bundle with `just build-artifacts`, or supply a
compatible bundle containing targetctl. Missing helper artifacts fail explicitly.
All vsm-owned formats use current contracts without revision tags.

## Python usage and configuration

```python
from pathlib import Path
from vsm.binary import BinaryProvider
from vsm.connection.host import Host
from vsm.connection.targetctl import TargetctlOptions
from vsm.models import Exec

async def example():
    backend = Host(
        binaries=BinaryProvider(Path("artifacts")),
        targetctl=TargetctlOptions(
            startup_timeout=5, renewal_interval=5,
            lease_timeout=30, disconnect_grace=5,
        ),
    )
    async with backend.connect() as connection:
        async with connection.launch(Exec(argv=("sleep", "10"))) as process:
            identity = await process.identity()
            selection = await process.processes()
            assert selection[0].pid == identity.pid == process.target_pid
```

SSH accepts the same `binaries` and `targetctl` options. All configuration times
are finite positive seconds, at most 24 hours; renewal must be shorter than the
lease. `stop(grace=...)` additionally permits zero for immediate escalation.
`Run.connect()` supplies its provider through the built-in backends' optional
`with_binaries()` hook; an explicit backend provider takes precedence. Custom
backends without that hook keep their ordinary `connect()` contract.

`Process.identity()` validates the identity pinned before workload execution.
`Process.processes()` returns its singleton `ProcessSet`. `Capture.processes()`
delegates to the attached process, or raises `CapabilityUnavailable` when none is
attached. Direct launches, Launch actions, custom actions using connections, and
process-backed captures share this contract. Exec selects the executable; Shell
selects its owned shell. Helper/controller PIDs are never workload selections.
Exited workloads raise `ProcessLookupError` on identity selection; short-lived
commands still return their real completion result. Cancelling `wait()` does not
stop the workload. Scope exit owns cleanup, including repeated cancellation.

`ScanProcesses(pattern=...)` discovers external processes using literal substring
matching, including NUL argument boundaries. It returns an immutable
`ProcessSet`. Confirmed disappearance races are skipped; permission and other OS
errors retain errno and source context; malformed procfs data raises `ValueError`.
Discovery is a changing view of visible processes, not proof of launch ownership
or complete visibility under kernel hiding policies. Collectors validate selected
device/boot/start identities before startup. PidStat, ProcessIo, and Smaps also
pin identities during native sampling; their lifecycle/log contracts are described
in [native contracts](native-contracts.md#identity-pinned-process-sampling).

## Clock calibration

The request-mode `clock` operation accepts empty params and returns one terminal
result with `boot_id`, `monotonic_ns`, `realtime_ns`, and `uncertainty_ns`.
The monotonic reading uses system-wide Linux CLOCK_MONOTONIC, unlike the helper's
process-relative envelope timing. Realtime is bracketed by monotonic readings;
the result uses their midpoint and half-width (rounded up). `capabilities` lists
`clock` alongside the other operations.

`Connection.sample_clock()` returns `TargetClock`.
`Connection.calibrate_clock()` returns a `ClockCalibration` snapshot after a
five-probe batch bounded to ten seconds. Host and SSH own initial, periodic
(30-second) and final calibration. Captures also checkpoint at finalization.
Boot identities must match. Timing failure is retained as a warning rather than
making successful measurement data unusable. See
[controller UTC timelines](chart-reports.md#controller-utc-timelines) for accuracy,
uncertainty and offline persistence. Cleanup and process leases are independent
of calibration success.

## Lifecycle and cleanup

Each launch owns a dedicated supervisor, control channel, workload process group,
and private session directory. A gate holds the child until Python-independent
native identity capture succeeds. A close-on-exec pipe confirms executable startup.
Readiness means exec success and pinned identity; collectors still establish their
own sample/marker/service readiness.

The supervisor stays outside the workload group and observes leader completion
without reaping it until cleanup. This reserves the group leader PID. A pidfd,
when available, observes leader exit; child ownership and waitid provide fallback.
TERM targets the owned group; KILL follows after grace. The leader is reaped after
no live members remain in that group. Group-leader exit does not exempt remaining
members from scope cleanup. Descendants which deliberately escape the group are
not contained, and can retain output pipes. After group cleanup, such pipes get a
bounded one-second drain; abandoned output is explicitly incomplete.

EOF, supervisor TERM/INT, or an expired monotonic control lease initiate cleanup.
By default the controller renews every five seconds and the lease expires after
30 seconds, followed by five seconds' termination grace. Controller suspension
longer than the lease can therefore stop its workloads. Workload output and control
handling have separate progress: a blocked consumer cannot disable the lease.
Queues are bounded; output uses backpressure rather than unbounded buffering.
Final protocol records have a fresh two-second no-progress drain deadline starting
at cleanup; previous workload silence does not consume that window.

`wait()` returns exit status and separately captured stdout/stderr bytes or file
paths; each stream preserves its own order. Capture-limit failures initiate cleanup
and preserve the output error. Undelivered/abandoned output is not reported as a
successful complete result. Helper stderr is diagnostic data, separate from both
workload streams, and is drained with bounded retention.

Transport loss, helper SIGKILL/crash, or permanent target loss cannot prove cleanup.
Python raises `CleanupUnknown` and retains the workspace if acknowledgement is
missing. A dead supervisor cannot enforce a future lease. Uninterruptible kernel
work may also prevent timely confirmed cleanup. No durable detached jobs, session
reattachment, automatic reconnection, or strict descendant containment is provided.

## Control protocol

Run `targetctl request` for a one-shot operation or `targetctl supervise` for one
owned workload. Both use UTF-8 JSONL stdin/stdout; stderr contains diagnostics.
Clap handles CLI parsing, root/subcommand help and version reporting. A mode is
required; missing/unknown modes, extra arguments and unknown options exit with
status 2 before reading protocol input. `--version` reports the package version.

Requests have unsigned integer `id`, named `operation`, and object `params`. Unsupported
operations fail explicitly. There is no protocol revision field or negotiation; obsolete
version-bearing requests are rejected. Request fields and launch parameters are strict.
Responses allow additive optional fields and include `id`, `operation`, `event`,
`boot_id`, `started_ns`, `finished_ns`, and object `result`. Timings are monotonic
elapsed nanoseconds within that helper invocation, not aligned measurement timestamps.
Parse errors may use ID zero without an operation/timing envelope because no valid
request exists.

| Operation | Parameters | Response |
|---|---|---|
| `capabilities` | empty object | Supported operation names, procfs presence, usable pidfd probe |
| `discover` | nullable string `pattern` | Zero or more `item` identities followed by `result`; failures terminate with `error` |
| `validate` | `processes` identity array | `result`, or explicit failure |
| `launch` | `argv`, environment overlay object `env`, nullable `cwd`, new private `directory`, `lease_seconds`, `grace_seconds` | `launched` with workload identity and separate supervisor PID |
| `identity` | empty object | Current validated workload identity, or exit/failure |
| `renew` | empty object | No acknowledgement; renews the lease |
| `stop`, `release` | optional `grace_seconds` | Group cleanup result; closes the session after completion |

The first supervisor request must be `launch`. Subsequent requests must use unique
IDs. Python reserves ID 1 for launch/lifecycle events. Identities contain `pid`,
`start_ticks`, nullable `boot_id`, and `name`; Python attaches its backend-derived
DeviceId. Validation preserves ordered selections and duplicates. Reads use opened
procfs directories and consistency checks; no atomic multi-file snapshot is promised.

Supervisor events use the launch ID: `output` has `stream` (`stdout`/`stderr`) and
base64 `data`; `completed` has `exit_code` and boolean `output_complete`; `cleanup`
reports `cleanup: "confirmed"`, `scope: "process_group"`, workload identity,
exit code, and output completeness. Launch acknowledgement precedes workload output;
completion follows stream finalization. Failed exec emits an errno-bearing error
and cleanup, without claiming a workload was successfully launched. Signal exits
use `128 + signal_number`.

An `error` carries category, message with source context, and nullable errno.
Python preserves OS subclasses (including permission and disappearance), maps
identity mismatch to `OwnershipError`, unsupported operations to
`CapabilityUnavailable`, protocol violations to `TargetctlProtocolError`, and
malformed procfs to `ValueError`. Partial discovery results are never returned as a
successful ProcessSet. Unknown/truncated events, invalid IDs/types, timing order,
and lifecycle violations fail explicitly.

Limits: 1 MiB per JSONL frame; 32 KiB decoded output chunks; 65,536 discovered or
selected identities; 16 MiB procfs reads; 8 MiB queued output plus bounded framing
and chunk overhead; at most 64 pending stop requests. Python splits validation
into bounded batches. A result exceeding a limit fails explicitly. Limits do not
turn discovery or batching into atomic operations.

## Deployment, extensions, and recovery

BinaryProvider still validates hashes, ELF identity, architecture/ABI and execution
requirements. Each connection lazily deploys its own helper, independently of
other connections to the same device. Private bootstrap commands perform only
metadata discovery, deployment/loader checks, and acknowledged workspace cleanup;
public launch requires the helper. This avoids recursive helper
deployment. SSH retains host-key verification and safe encoding of the helper's
fixed invocation; workload argv/env/cwd travel in protocol data.

Connection extensions must supply typed `discover_processes()` and
`validate_processes()` alongside the Process selection methods. Internal built-in
transport adapters are not part of the public extension contract. Unsupported
identity evidence must fail explicitly instead of manufacturing a ProcessIdentity.

Session directories are mode 0700 below `connection.tmp_dir`. `launch.json` and
`final.json` are atomically replaced, bounded recovery records; they exclude
workload output, environment values and command arguments. Successful acknowledged
cleanup removes its session directory. Unknown cleanup retains the connection
workspace. On a later verified connection, inspect those records read-only: compare
boot/start identity and session outcome. A matching final record confirms the
recorded process-group cleanup, not containment of escaped descendants. Missing
or malformed final evidence remains unknown; never blindly signal a stored PID.
There is no automated recovery/signaling API.

GNU and static musl entries are configured for the existing target matrix.
Compilation/static inspection is distinct from real target execution; Android,
OpenHarmony, AArch64, network SSH, and restricted-kernel acceptance require their
own suitable environments. CPU affinity, limits, interface discovery and settings
operations are future extensions selected by concrete action requirements.

## Verification

Native helper contract tests cover GNU and x86-64 static musl execution on the
controller. Host and local SSH-adapter tests exercise the Python lifecycle;
transport adapters do not establish live network SSH behavior. Live SSH, AArch64,
Android, OpenHarmony, and physical-device acceptance require suitable targets
and remain separate checks.

Run native and Python tests as described in the [development
guide](../README.md#development-and-tests). Use [native builds](native-builds.md) for
reproducible build commands and exact variant selection. Build provenance records the
builder and flags; successful linking and header inspection alone do not establish
target runtime support.
