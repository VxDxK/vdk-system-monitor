# Chart reports

`export_charts` writes self-contained offline HTML by default. Measurement
families have full-width collapsible headings and a two-column desktop grid;
reports switch to one column at 850px. `columns` is a positive integer and
controls the desktop column count. `Layout.TABLE` keeps sections expanded;
`Layout.FILES` emits separate, uniquely named chart files. Explicit Matplotlib
reports embed PNG; separate static charts support PNG, JPEG, SVG and PDF.

Charts fit their containers without nested scrollbars or axis range sliders.
Interactive legends wrap below plots and have keyboard-accessible visibility
buttons. Opening a section or changing container size schedules a Plotly resize;
existing figures retain their zoom and visibility. Only wide **data tables**
scroll horizontally. The report itself scrolls vertically. Source details retain
full paths, raw entity identities, schema, target and completeness information.
Warnings appear outside chart grids. Empty datasets have an explicit empty state.

## Labels and CSV

Entity-bearing processors add `entity_label` to datasets and plain CSV files.
Original identity and measurement columns remain available. CPU-frequency paths
become `cpu0`, `cpu1`, etc.; thermal zones include captured sensor names when
available; batteries use their supply name; process/thread names include PID/TID;
sockets show endpoints rather than serialized identity arrays. Duplicate short
names gain distinguishing suffixes. Grouping uses raw identities, so duplicate
names never combine distinct series. Full grouping identities also appear in
line-chart hover text and source details.

`SeriesChart.label_column` defaults to `entity_label`; set it to `None` to use
raw grouping values. `metric_labels` accepts `(column, display-name)` pairs.
Single-metric legends use entity names; multi-metric legends include the metric.
`additional=True` places a chart after the primary families. Custom charts
implement the `Chart` protocol. `Table` is an explicit tabular chart description;
it also supports direct Plotly and Matplotlib figures.

## Saved datasets

`Dataset.to_csv(Path("cpu.csv"))` writes `cpu.csv` and `cpu.csv.json`.
`Dataset.from_csv(Path("cpu.csv"))` restores the dataframe and metadata, including
all built-in `Line`, `Bar`, `Box`, and `Table` options. Pass the result directly
to `export_charts`; choose Plotly or Matplotlib when rendering, independently of
export. The second script needs no connection, `Run`, or original raw logs.
Provenance paths describe the original source; the loader never opens them.

CSV headers and scalar values remain readable by ordinary CSV tools. The sidecar
records labels, units, warnings, provenance, column types, cell types and missing
values. This preserves literal empty/NA strings, large integer identities,
nullable numbers/booleans, JSON-compatible list/dictionary cells and chart gaps.
Columns must have unique string names. Numeric, boolean, string and object
columns with supported scalar/JSON values are supported; datetime, categorical,
complex and arbitrary Python object values are rejected. The dataframe index is
discarded. The pair also supports empty datasets and datasets without charts.

Keep both files together when moving or renaming them. `from_csv` rejects missing
or malformed sidecars, inconsistent headers/dimensions, invalid cell descriptions
and unsupported charts. It does not guess metadata for arbitrary CSV files or
regenerate chart definitions from numeric columns. Treat the pair as an exported
snapshot: external edits are not a supported dataset editing workflow. Shape and
type checks do not detect every possible external modification.

Custom `Chart` implementations remain supported in memory, but cannot be saved
until an explicit codec is added. Unsupported values/charts fail before replacing
either destination. Both files are staged before replacement; two replacements
are not a single atomic transaction, and write failures propagate. Only the
current release's format is supported; there are no format revision tags or
compatibility readers. No run journal is changed.

For separate-script CSV loading and rendering, see the
[README API snippet](../README.md#charts-and-csv). The two manual examples export
CSV pairs and call `export_charts` directly; neither loads saved datasets.

## Chart families

The following table describes primary chart families. Rendering depends on the
fields captured by each measurement; registration does not establish hardware
availability. Linux kB conversion uses 1024 bytes, CPU and process calculations
use target tick/page metadata, and missing rate baselines and counter resets
remain gaps. Rup uses upstream 0.15.1 response accounting.

| Processor | Primary chart families |
|---|---|
| cpu_freq | CPU frequency, comparing CPUs |
| gpu_freq | GPU frequency |
| ddr_freq | DDR frequency |
| npu_freq | NPU frequency |
| battery_level | Level, full/remaining charge, current, voltage |
| cpu_time_in_state | Frequency versus residency line chart |
| temperature | Named thermal sensors |
| frame_count | Frame rate |
| proc_stat | User, nice, system, idle, I/O wait, IRQ, soft IRQ, steal and total busy |
| pid_stat | Process RSS/thread count; thread user/system/total CPU and CPU binding |
| meminfo | Byte fields, unitless counters, and detailed memory accounting |
| smaps | Memory byte fields and available counters |
| net_dev | Per-interface throughput, packets, drops/errors/FIFO, raw-counter table |
| net_snmp | Rates together per protocol; gauges separately with their own units |
| net_udp | Per-socket queues and available drop rates |
| socket_stat | Per-socket congestion window, RTT, throughput, memory buffers |
| ss | Per-socket congestion window and RTT; extra queue/timer/threshold charts follow |
| iperf | Throughput and available interval packet counts |
| traffic_generator | Throughput and event rates (TCP calls are not packets) |
| ping | RTT timeline and distribution |
| rup | RTT timeline, distribution and summary table in one section |
| top | CPU timeline and distributions for consumers whose mean exceeds 5% |
| wifi_conn_info | Per-station table, signal, duration, TX counters, byte/packet totals, bitrate, bandwidth; typed TX/RX MCS/NSS/GI/DCM |
| fio | Results table and benchmark charts |
| time | Timing table and chart |
| disk_io | Per-disk byte and operation rates |
| swap | Total/used bytes and usage percentage |
| fans | Fan speeds |
| filesystem_usage | Used/free bytes and usage percentage |
| load_average | 1-, 5-, and 15-minute load averages and task counts |
| uptime | Uptime, accumulated CPU idle seconds, and boot timestamp |
| process_io | Process/thread byte and operation rates |

Battery collection discovers existing optional charge/current/voltage files
through the connection. Linux charge is converted from µAh to mAh and current
from µA to mA; voltage remains µV. Vendor `capacity_fcc`/`capacity_rm` fields
are interpreted as mAh. Current polarity is driver-defined. Thermal type files are
captured as optional sensor metadata. Missing optional fields remain
unavailable; battery warnings identify absent chart families. Read errors still
require an explicit processing opt-in.

Wi-Fi datasets include long-format columns, parsed structured parameter rows,
and station-specific wide datasets for related charts. A
parameter type remains part of the categorical value (e.g. `HE-MCS 5`).
Absent optional iw fields do not become zero. Fio supports multiple jobs.
CPU `user_percent` combines user/nice; `user_only_percent` supports the separate
user chart. Absent CPU fields remain unavailable.

Socket diagnostics use schema `socket-stat` with optional, length-checked attributes.
Only the current native log contract is supported; the pinned upstream rup format
remains separate. Optional chart families are emitted only when their fields were
captured; hardware/kernel support is not inferred from a registration.

## Review and verification

Generate synthetic stress reports with:

```bash
uv run python scripts/chart_report.py reports/chart-export
```

The review tool includes many series, long names, duplicate identities across two
devices/captures, gaps, warnings, an incomplete source, wide tables and empty data.
`foldable.html`, `table.html`, and `static.html` are review artifacts, not live
measurements. Generate these files locally; reports are not included in the repository.

Regression tests exercise real plotting libraries, CSV preservation, sources,
script escaping, export modes/formats, chart composition and metric parsing.
Browser interaction and responsive layout require separate acceptance checks.
Inspect 1440px, 1024px and
390px layouts, legends, zoom preservation, collapse/reopen, navigation, keyboard
controls, and offline loading. Content tests and JavaScript syntax checks do not
establish browser behavior. Real Wi-Fi/battery/netlink behavior requires suitable
targets independently of chart acceptance.
